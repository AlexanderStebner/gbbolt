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
    point inside one; `.name` is local."""
    return not name.startswith('.') and not name.startswith('jr_') and colons == '::'


def parse(src_dir, rom, syms):
    """rom: bytes of the built ROM, syms: label -> address."""
    p = Parsed()
    parse_ram_inc(os.path.join(src_dir, 'ram.inc'), p)
    constdoc = p.vars.pop('__constdoc', {})
    p.constdoc = constdoc

    files = []

    def walk(fname):
        for n, raw in enumerate(open(os.path.join(src_dir, fname), encoding='utf-8').read().split('\n'), 1):
            m = re.match(r'^\s*INCLUDE\s+"([^"]+)"', raw, re.I)
            if m and m.group(1).endswith('.asm'):
                walk(m.group(1))
            elif fname != main:
                files.append((fname, raw, n))     # n: line number within its own file
    main = build.GAME['main']
    walk(main)

    pc = 0
    scope = None
    unit = None
    pending_header = []
    for idx, (fname, raw, fline) in enumerate(files):
        ln = Line(fname, fline, raw)
        p.lines.append(ln)
        code, comment = strip_comment(raw)
        stripped = code.strip()
        cstrip = comment.strip()
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
            if unit is not None:
                unit.lines.append(idx)
                ln.group = unit.cur
            continue

        ln.comment = cstrip
        m = LABEL_RE.match(stripped)
        if m and not stripped.upper().startswith('DEF '):
            name = m.group(1) or m.group(4)
            full = name if not name.startswith('.') else '{}{}'.format(scope, name)
            if not name.startswith('.'):
                scope = name
            ln.kind = 'label'
            ln.label = full
            if full in syms:
                if syms[full] != pc and unit is not None:
                    p.warnings.append('address drift at {}: walked ${:04X}, sym ${:04X}'.format(
                        full, pc, syms[full]))
                pc = syms[full]
            ln.addr = pc
            if is_unit_label(name, m.group(2) if m.group(1) else m.group(5)):
                if unit is not None:
                    unit.end = pc
                unit = Unit(full, idx)
                unit.start = pc
                unit.header = pending_header
                pending_header = []
                if unit.header:
                    hdr = parse_header(unit.header)
                    if hdr['asset']:
                        unit.asset = dict(hdr['asset'], doc=hdr['doc'])
                    unit.path = hdr['path']
                    path_only = all(t.startswith(';@ path:') or not re.match(r';@ \w+:', t) for t in unit.header)                         and not any(t.startswith(';@ def ') for t in unit.header)
                    if hdr['def'] or not (hdr['asset'] or path_only):
                        unit.func = hdr
                p.units.append(unit)
            if unit is not None:
                unit.lines.append(idx)
                ln.group = unit.cur
            rest =(m.group(3) if m.group(1) else m.group(6)).strip()
            if not rest:
                continue
            stripped = rest  # instruction on the same line as the label
        if pending_header and unit is not None:
            # header comments not followed by a label belong to nobody
            p.warnings.append('{}:{}: ;@ header not directly above a label'.format(fname, fline))
            pending_header = []

        word = stripped.split(None, 1)
        mnem = word[0].lower()
        ops = word[1] if len(word) > 1 else ''
        if mnem == 'section':
            sm = re.search(r'\[\$([0-9a-fA-F]+)\]', stripped)
            pc = int(sm.group(1), 16) if sm else pc
            ln.kind = 'directive'
            continue
        if mnem in ('include', 'def', 'charmap', 'assert', 'export', 'macro', 'endm'):
            ln.kind = 'directive'
            continue
        if ln.kind != 'label':
            ln.kind = 'data' if mnem in ('db', 'dw', 'ds') else 'insn'
        ln.addr = pc
        ln.text = stripped
        if mnem in ('db', 'dw', 'ds'):
            ln.size = data_size(mnem, ops)
            ln.datakind = True
        else:
            ln.size = decode(rom, pc).length
            ln.datakind = False
        if ln.kind == 'label':
            # label + instruction on one line: treat as instruction line
            ln.kind = 'data' if ln.datakind else 'insn'
        pc += ln.size
        if unit is not None:
            unit.lines.append(idx)
            ln.group = unit.cur
            unit.joinable = False
    if unit is not None:
        unit.end = pc

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
