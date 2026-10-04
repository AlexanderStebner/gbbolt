"""Parse the disassembly plus its gbbolt annotations.

Annotation format (all inside ordinary asm comments, so RGBDS ignores them):

    ;@ def IsItemInBag(item: a) -> carry        function header (Python def)
    ;@ Free text lines become the description.
    ;@ reads: wBagItems                          declared memory reads
    ;@ writes: wFoo, hBar                        declared memory writes
    ;@ clobbers: b, hl                           registers destroyed
    ;@ test: item = rng.randrange(256)           setup statement for diff tests
    ;@ test: skip touches the LCD                skip diff testing (reason)
    ;@ sig: 1a2b3c4d                             checksum of the bytes it describes
    Label::
    ;> for id, qty in ...:                       pseudo-code for the asm below it
        ld hl, wBagItems

Every source line gets a ROM address and its bytes; the source is split into
units (functions or data blocks) at non-local labels.
"""
import os
import re

import build
import zlib

from sm83 import decode

LABEL_RE = re.compile(r'^([A-Za-z_][\w.]*)(::?)\s*(.*)$|^(\.[A-Za-z_][\w]*)(:{0,2})\s*(.*)$')
DEF_RE = re.compile(r'^\s*DEF\s+(\w+)\s+EQU\s+([^;]+?)\s*(?:;@\s*(\S+)\s*(.*))?$', re.I)
HEADER_KEYS = ('reads', 'writes', 'clobbers', 'test', 'sig', 'asset', 'path')


def parse_number(s):
    s = s.strip()
    if s.startswith('$'):
        return int(s[1:], 16)
    if s.startswith('%'):
        return int(s[1:], 2)
    return int(s, 0)


def split_operands(s):
    out, cur, q = [], '', False
    for ch in s:
        if ch == '"':
            q = not q
        if ch == ',' and not q:
            out.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def data_size(mnemonic, operands):
    if mnemonic == 'ds':
        return parse_number(split_operands(operands)[0])
    n = 0
    for item in split_operands(operands):
        if item.startswith('"'):
            n += len(item[1:-1].encode().decode('unicode_escape'))
        else:
            n += 2 if mnemonic == 'dw' else 1
    return n


def strip_comment(line):
    q = False
    for i, ch in enumerate(line):
        if ch == '"':
            q = not q
        elif ch == ';' and not q:
            return line[:i], line[i:]
    return line, ''


class Line:
    def __init__(self, file, lineno, raw):
        self.file, self.lineno, self.raw = file, lineno, raw
        self.kind = 'blank'    # blank|comment|label|insn|data|directive|header|pseudo
        self.addr = None
        self.size = 0
        self.label = None
        self.text = ''
        self.comment = ''
        self.group = 0
        self.xref = None       # (unit, group) for lines linked with `;=@Unit.tag`
        self.bank = None       # the bank the assembler put it in
        self.macro = None      # name of the macro this line invokes, if any
        self.datakind = False


class Unit:
    def __init__(self, name, line_index):
        self.name = name
        self.first_line = line_index
        self.lines = []          # indices into Parsed.lines
        self.header = []         # raw ';@' texts
        self.start = None
        self.end = None
        self.kind = 'code'
        self.pseudo_groups = []  # [{'pseudo': [str], 'lines': [idx]}]
        self.tags = {}           # `;>@name` -> group number (1-based)
        self.cur = 0             # group the next lines belong to (or '@name' until resolved)
        self.joinable = False    # the next `;>` line joins the current group
        self.foreign = []        # lines of other units linked to this one's groups (`;=@Unit.tag`)
        self.func = None         # parsed header dict when annotated
        self.asset = None        # {'type', params..., 'doc'} for `;@ asset:` data
        self.path = None         # virtual folder (`;@ path: game/piece`)

    @property
    def annotated(self):
        return self.func is not None


class Parsed:
    def __init__(self):
        self.lines = []
        self.units = []
        self.vars = {}       # name -> {'addr','type','size','desc'}
        self.consts = {}     # name -> value
        self.warnings = []


def parse_header(texts):
    func = {'def': None, 'doc': [], 'reads': [], 'writes': [], 'clobbers': [],
            'test': [], 'sig': None, 'asset': None, 'path': None}
    for t in texts:
        body = t[2:].strip() if t.startswith(';@') else t
        if body.startswith('def '):
            func['def'] = body.rstrip(':')
            continue
        m = re.match(r'^(\w+):\s*(.*)$', body)
        if m and m.group(1) in HEADER_KEYS:
            key, val = m.group(1), m.group(2).strip()
            if key == 'test':
                func['test'].append(val)
            elif key in ('sig', 'path'):
                func[key] = val
            elif key == 'asset':
                func['asset'] = parse_asset(val)
            else:
                func[key] += [v.strip() for v in val.split(',') if v.strip()]
            continue
        func['doc'].append(body)
    return func


def parse_asset(val):
    """'tiles bpp=1 length=$138' -> {'type': 'tiles', 'bpp': '1', 'length': '$138'}"""
    parts = val.split()
    asset = {'type': parts[0] if parts else 'raw'}
    for p in parts[1:]:
        k, _, v = p.partition('=')
        asset[k] = v
    return asset


def parse_ram_inc(path, parsed):
    if not os.path.exists(path):
        return
    for line in open(path, encoding='utf-8'):
        m = DEF_RE.match(line)
        if not m:
            continue
        name, value, vtype, desc = m.group(1), m.group(2), m.group(3), (m.group(4) or '').strip()
        try:
            value = parse_number(value)
        except ValueError:
            continue
        if vtype is None or vtype == 'const':
            parsed.consts[name] = value
            if vtype == 'const' and desc:
                parsed.vars.setdefault('__constdoc', {})[name] = desc
            continue
        size = 1
        am = re.match(r'^(\w+)\[([\w$]+)\]$', vtype)
        base = vtype
        if am:
            base, size = am.group(1), parse_number(am.group(2))
        elif vtype == 'u16':
            size = 2
        parsed.vars[name] = {'name': name, 'addr': value, 'type': vtype, 'base': base,
                             'size': size, 'desc': desc}


def is_unit_label(name, colons='::'):
    """`Name::` starts a unit; `Name:` (and `jr_000_xxxx:`) is a shared entry
    point inside one; `.name` is local. pret projects (game.json "unit_labels":
    "global"): every global label starts a unit."""
    if name.startswith('.') or name.startswith('__gb_'):
        return False
    if build.GAME.get('unit_labels') == 'global':
        return colons in (':', '::')
    return not name.startswith('jr_') and colons == '::'


SM83 = {'ld', 'ldh', 'ldi', 'ldd', 'add', 'adc', 'sub', 'sbc', 'and', 'or', 'xor', 'cp', 'inc', 'dec', 'daa',
        'cpl', 'ccf', 'scf', 'nop', 'halt', 'stop', 'di', 'ei', 'rlca', 'rla', 'rrca', 'rra', 'rlc', 'rl', 'rrc',
        'rr', 'sla', 'sra', 'srl', 'swap', 'bit', 'set', 'res', 'jp', 'jr', 'call', 'ret', 'reti', 'rst', 'push', 'pop'}
DATA = {'db', 'dw', 'dl', 'ds', 'incbin'}
DIRECTIVES = {'section', 'endsection', 'include', 'def', 'redef', 'charmap', 'newcharmap', 'setcharmap', 'pushc',
              'popc', 'assert', 'static_assert', 'export', 'macro', 'endm', 'if', 'elif', 'else', 'endc', 'rept',
              'for', 'endr', 'break', 'load', 'endl', 'union', 'nextu', 'endu', 'pushs', 'pops', 'opt', 'pusho',
              'popo', 'align', 'println', 'print', 'warn', 'fail', 'purge', 'shift', 'rsreset', 'rsset'}


def macro_kinds(lines):
    """Macro name -> 'code' if its body assembles instructions (directly or through
    other code macros), else 'data'. Macros come from the walked source and the
    `-P` prelude files."""
    import measure
    bodies = {}
    texts = [s.raw for s in lines]
    for f in sorted(measure.prelude_files()):
        texts += open(os.path.join(build.SRC, f), encoding='utf-8', errors='replace').read().split('\n')
    cur = None
    for raw in texts:
        code = measure.code_part(raw).strip()
        m = re.match(r'^MACRO\??\s+(\w+)', code, re.I) or re.match(r'^(\w+):?\s+MACRO\b', code, re.I)
        if m:
            cur = bodies.setdefault(m.group(1), set())
            continue
        if re.match(r'^ENDM\b', code, re.I):
            cur = None
            continue
        if cur is not None and code:
            w = re.sub(r'^[.\w]+:+\s*', '', code).split(None, 1)
            if w:
                cur.add(w[0].lower())
    kinds = {n: 'code' if b & SM83 else 'data' for n, b in bodies.items()}
    changed = True
    while changed:
        changed = False
        for n, b in bodies.items():
            if kinds[n] == 'data' and any(kinds.get(x) == 'code' for x in b):
                kinds[n] = 'code'
                changed = True
    return {n.lower(): k for n, k in kinds.items()}


def ram_from_labels(p):
    """RAM variables from the labels in RAM sections (pret style: `wFoo:: ds 2`, comments
    above or beside them). Size: up to the next label; arrays unless one byte."""
    labs = p.ram_labels
    for i, (name, addr, bank, cmt, li, own) in enumerate(labs):
        if name in p.vars:
            continue
        nxt = [lab[1] for lab in labs[i + 1:i + 40]
               if lab[1] > addr and lab[2] == bank and lab[1] >> 13 == addr >> 13]    # same bank and region
        # an alias label right above another at the same address (`wCoordIndex::` over
        # `wLoadedMonLevel:: db` in a UNION) has that one's size
        alias = 0
        if not own:
            # a label alone on its line: the first data line below it (past other labels at the
            # same address, comments and blanks) is what it names
            for j in range(li + 1, min(li + 60, len(p.lines))):
                ln = p.lines[j]
                if ln.kind in ('blank', 'comment', 'header'):
                    continue
                if ln.kind == 'label' and ln.addr == addr:
                    continue
                if ln.kind == 'directive' and ln.addr == addr and ln.size and re.match(r'(?:db|dw|ds|dl)(?=\s|$)', ln.text, re.I):
                    alias = ln.size
                break
        size = own or alias or max(1, min(min(nxt) - addr if nxt else 1, 0x1000))    # `wFoo:: ds 30` says it itself
        desc = []
        if cmt:
            desc.append(cmt.lstrip(';').strip())
        else:
            j = li - 1
            while j >= 0 and p.lines[j].kind == 'comment':
                desc.insert(0, p.lines[j].comment.lstrip(';').strip())
                j -= 1
        p.vars[name] = {'name': name, 'addr': addr, 'type': 'u8' if size == 1 else 'u8[{}]'.format(size),
                        'base': 'u8', 'size': size, 'desc': ' '.join(desc), 'bank': bank}


def parse(src_dir, rom, syms):
    """rom: bytes of the built ROM, syms: label -> CPU address (banks in build.SYM_BANK).
    Every line's address and size come from the assembler (measure.py); ROM addresses
    are linear (offsets in the ROM file)."""
    import measure
    p = Parsed()
    parse_ram_inc(os.path.join(src_dir, 'ram.inc'), p)
    constdoc = p.vars.pop('__constdoc', {})
    p.constdoc = constdoc
    walked, measured = measure.measure()
    mkinds = macro_kinds(walked)
    p.macro_kinds = mkinds
    any_colon = build.GAME.get('unit_labels') == 'global'     # pret style: every global label starts a unit
    seen = {}

    scope = None
    unit = None
    pending_header = []
    ram_labels = []                                # (name, addr, bank, comment, line index) in RAM sections

    def attach(idx, ln):
        if unit is not None:
            unit.lines.append(idx)
            ln.group = unit.cur

    for idx, s in enumerate(walked):
        fname, raw, fline = s.file, s.raw, s.lineno
        occ = measured.get((fname, fline))
        k = seen.get((fname, fline), 0)
        seen[(fname, fline)] = k + 1
        here = occ[k] if occ and k < len(occ) else None      # (cpu addr, bank, size, ram) or not assembled
        ln = Line(fname, fline, raw)
        p.lines.append(ln)
        if s.ctx == 'eof' or s.is_section:
            unit = None                            # a unit never runs on into another section
        if s.ctx == 'eof':
            continue
        code, comment = strip_comment(raw)
        stripped = code.strip()
        cstrip = comment.strip()
        is_ram = here is not None and here[3]
        if here is not None:
            ln.addr = here[0] if is_ram else build.linear(here[1], here[0])
            ln.bank = here[1]
        if stripped and (s.ctx == 'block' or (here is None and s.in_section and s.ctx in ('top', 'open'))):
            # inside a MACRO / REPT / LOAD block, or a line the assembler skipped (IF branch)
            ln.kind = 'directive' if s.ctx == 'block' else 'inactive'
            ln.text, ln.comment, ln.addr = stripped, cstrip, None
            if pending_header and LABEL_RE.match(stripped):
                pending_header = []                # the header of a label that is not assembled
            if not (s.in_section is False and s.ctx == 'block'):
                attach(idx, ln)
            continue
        if not stripped:
            if cstrip.startswith(';@'):
                ln.kind = 'header'
                pending_header.append(cstrip)
            elif cstrip.startswith(';>'):
                ln.kind = 'pseudo'
                text = comment.split(';>', 1)[1]
                tm = re.match(r'^@(\w+) ?', text)     # `;>@name code`: a line other runs can link to
                if tm:
                    text = text[tm.end():]
                ln.text = text[1:] if text.startswith(' ') and not tm else text
                if unit is not None:
                    gs = unit.pseudo_groups
                    if gs and unit.joinable and not tm:
                        gs[-1]['pseudo'].append(ln.text)
                    else:
                        gs.append({'pseudo': [ln.text], 'lines': []})
                    if tm:
                        if tm.group(1) in unit.tags:
                            p.warnings.append('{}:{}: tag @{} defined twice'.format(fname, fline, tm.group(1)))
                        unit.tags[tm.group(1)] = len(gs)
                    unit.cur = len(gs)
                    unit.joinable = not tm       # a tagged line is a group of its own
            elif cstrip.startswith(';='):
                # `;=@name`: the instructions below also belong to the `;>@name` line
                ln.kind = 'pseudo'
                # `;=@Unit.name`: they belong to a line of another unit (shown there too)
                tm = re.match(r'^;=\s*@((?:\w+\.)?\w+)\s*$', cstrip)
                if not tm:
                    p.warnings.append('{}:{}: expected `;=@name` or `;=@Unit.name`'.format(fname, fline))
                elif unit is not None:
                    unit.cur = '@' + tm.group(1)
                    unit.joinable = False
            elif cstrip:
                ln.kind = 'comment'
                ln.comment = cstrip
            attach(idx, ln)
            continue

        ln.comment = cstrip
        m = LABEL_RE.match(stripped)
        if m and not re.match(r'^(DEF|REDEF)\s', stripped, re.I) and not re.match(r'^\w+:?\s+MACRO\b', stripped, re.I):
            name = m.group(1) or m.group(4)
            full = name if not name.startswith('.') else '{}{}'.format(scope, name)
            if not name.startswith('.'):
                scope = name
            ln.kind = 'label'
            ln.label = full
            colons = m.group(2) if m.group(1) else m.group(5)
            if is_ram:
                if not name.startswith('.'):
                    ram_labels.append((full, ln.addr, here[1], cstrip, idx, here[2]))
                continue
            if here is None:
                ln.addr = None
            elif (is_unit_label(name, colons) or (any_colon and not name.startswith('.') and colons)) \
                    and s.in_section and not full.startswith('__gb_'):
                # comments directly above the label (no blank line between) describe this unit,
                # not the end of the one before
                moved = []
                if unit is not None:
                    while unit.lines and p.lines[unit.lines[-1]].kind in ('comment', 'header'):
                        moved.insert(0, unit.lines.pop())
                    if moved and all(p.lines[i].kind == 'header' for i in moved):
                        unit.lines += moved                  # only header lines: as before
                        moved = []
                unit = Unit(full, idx)
                for i in moved:
                    p.lines[i].group = 0
                unit.lines += moved
                unit.start = unit.end = ln.addr
                unit.header = pending_header
                pending_header = []
                if unit.header:
                    hdr = parse_header(unit.header)
                    if hdr['asset']:
                        unit.asset = dict(hdr['asset'], doc=hdr['doc'])
                    unit.path = hdr['path']
                    keys = [re.match(r';@ (\w+):', t) for t in unit.header]      # doc lines may start with "Word:"
                    path_only = all(not k or k.group(1) not in HEADER_KEYS or k.group(1) == 'path' for k in keys) \
                        and not any(t.startswith(';@ def ') for t in unit.header)
                    if hdr['def'] or not (hdr['asset'] or path_only):
                        unit.func = hdr
                p.units.append(unit)
            attach(idx, ln)
            rest = (m.group(3) if m.group(1) else m.group(6)).strip()
            if not rest:
                continue
            stripped = rest  # instruction on the same line as the label
        if pending_header and unit is not None:
            # header comments not followed by a label belong to nobody
            p.warnings.append('{}:{}: ;@ header not directly above a label'.format(fname, fline))
            pending_header = []

        mnem = stripped.split(None, 1)[0].lower()
        if mnem in DIRECTIVES or here is None or is_ram:
            if ln.kind != 'label':
                ln.kind = 'directive'
                ln.text = stripped
            if is_ram:
                ln.size = here[2]                  # RAM data lines: the space they reserve
            if not is_ram and ln.kind != 'label':
                attach(idx, ln)
            continue
        if mnem in SM83:
            kind = 'insn'
        elif mnem in DATA:
            kind = 'data'
        else:
            kind = 'insn' if mkinds.get(mnem) == 'code' else 'data'
            ln.macro = mnem
        if ln.kind != 'label':
            attach(idx, ln)
        ln.kind = kind
        ln.datakind = kind == 'data'
        ln.text = stripped
        ln.size = here[2]
        if unit is not None:
            unit.joinable = False
            unit.end = max(unit.end, ln.addr + ln.size)
    p.ram_labels = ram_labels
    if not os.path.exists(os.path.join(src_dir, 'ram.inc')):
        ram_from_labels(p)
        for name, v in build.read_consts().items():
            if name not in p.vars and not name.startswith('__gb_'):
                p.consts.setdefault(name, v)

    # resolve `;=@name` links (they may point forward) and fill each group's lines
    by_name = {u.name: u for u in p.units}
    for u in p.units:
        for i in u.lines:
            ln = p.lines[i]
            if isinstance(ln.group, str) and '.' in ln.group:
                owner, tag = ln.group[1:].split('.')
                o = by_name.get(owner)
                g = o.tags.get(tag) if o else None
                ln.group = 0
                if g is None:
                    p.warnings.append('{}:{}: unknown tag @{}.{}'.format(ln.file, ln.lineno, owner, tag))
                    continue
                ln.xref = (owner, g)
                o.foreign.append(i)
                if ln.kind in ('insn', 'data'):
                    o.pseudo_groups[g - 1]['lines'].append(i)
                continue
            if isinstance(ln.group, str):
                g = u.tags.get(ln.group[1:])
                if g is None:
                    p.warnings.append('{}:{}: unknown tag {} in {}'.format(ln.file, ln.lineno, ln.group, u.name))
                ln.group = g or 0
            if ln.kind in ('insn', 'data') and ln.group:
                u.pseudo_groups[ln.group - 1]['lines'].append(i)

    for u in p.units:
        kinds = {p.lines[i].kind for i in u.lines}
        u.kind = 'code' if 'insn' in kinds else 'data'
        u.bytes = rom[u.start:u.end]
    return p


def unit_sig(unit):
    return '{:08x}'.format(zlib.crc32(bytes(unit.bytes)) & 0xFFFFFFFF)
