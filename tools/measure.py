"""Exact address and size of every source line, measured by the assembler itself.

Macros, charmaps, conditional assembly and REPT blocks make it impossible to size a
line from its text, so we let rgbasm tell us. A copy of the source gets an anchor
label after every SECTION line and, before every line, a `PRINTLN` of the line's
offset from the current anchor. Lines in IF branches that are not assembled print
nothing; MACRO, REPT/FOR and LOAD blocks are measured as a whole. Linking the copy
puts the anchors in a .sym file, which turns offsets into addresses.

walk() is the shared reading order: every source line in the order the assembler
sees it (INCLUDEs expanded). asmparse reads the source through it too.
"""
import os
import re
import subprocess

import build

INCLUDE_RE = re.compile(r'^\s*INCLUDE\s+"([^"]+)"', re.I)
SECTION_RE = re.compile(r'^\s*SECTION\b', re.I)
ENDSECTION_RE = re.compile(r'^\s*ENDSECTION\b', re.I)
OPEN_BLOCK = re.compile(r'^\s*(MACRO|REPT|FOR|LOAD)\b', re.I)
CLOSE_BLOCK = re.compile(r'^\s*(ENDM|ENDR|ENDL)\b', re.I)

PRINT = 'PRINTLN STRFMT("#L {} {} %d %d %d", __gb_o, __gb_k, @ - {{__gb_cur}})'    # file, line
ANCHOR = ('REDEF __gb_k = __gb_k + 1\n'
          '__gb_s{d:__gb_o}_{d:__gb_k}::\n'
          'REDEF __gb_cur EQUS "__gb_s{d:__gb_o}_{d:__gb_k}"')


def code_part(raw):
    """The line without its comment (strings respected)."""
    q = False
    for i, ch in enumerate(raw):
        if ch == '"':
            q = not q
        elif ch == ';' and not q:
            return raw[:i]
    return raw


def norm(path):
    return os.path.normpath(path).replace('\\', '/')


def prelude_files():
    """Files pulled in with `-P` (macros, constants): never instrumented."""
    out = set()
    flags = build.GAME['asm_flags']
    stack = [flags[i + 1] for i, f in enumerate(flags) if f == '-P' and i + 1 < len(flags)]
    while stack:
        fn = norm(stack.pop())
        if fn in out or not os.path.exists(os.path.join(build.SRC, fn)):
            continue
        out.add(fn)
        for raw in open(os.path.join(build.SRC, fn), encoding='utf-8', errors='replace'):
            m = INCLUDE_RE.match(code_part(raw))
            if m:
                stack.append(m.group(1))
    return out


class Src:
    """One source line as the assembler meets it."""
    __slots__ = ('file', 'lineno', 'raw', 'ctx', 'in_section', 'is_section', 'obj')

    def __init__(self, file, lineno, raw, ctx, in_section, is_section, obj=0):
        self.file, self.lineno, self.raw, self.ctx = file, lineno, raw, ctx
        self.in_section, self.is_section, self.obj = in_section, is_section, obj


def walk():
    """[Src] for every object file, in assembly order. ctx: 'top' (an ordinary line),
    'open' / 'close' (first / last line of a MACRO, REPT, FOR or LOAD block), 'block'
    (inside one, or a continuation line), 'eof' (the end of an object's main file)."""
    out = []
    skip = prelude_files()
    st = {'section': False, 'depth': 0}

    def visit(fname, top):
        text = open(os.path.join(build.SRC, fname), encoding='utf-8', errors='replace').read().split('\n')
        if text and text[-1] == '':
            text.pop()
        cont = False
        for n, raw in enumerate(text, 1):
            code = code_part(raw)
            if st['depth']:
                ctx = 'block'
                if OPEN_BLOCK.match(code):
                    st['depth'] += 1
                elif CLOSE_BLOCK.match(code):
                    st['depth'] -= 1
                    ctx = 'block' if st['depth'] else 'close'
            elif cont:
                ctx = 'block'
            elif OPEN_BLOCK.match(code):
                st['depth'] = 1
                ctx = 'open'
            else:
                ctx = 'top'
            cont = code.rstrip().endswith('\\')
            is_sec = ctx == 'top' and bool(SECTION_RE.match(code))
            out.append(Src(fname, n, raw, ctx, st['section'], is_sec, st['obj']))
            if is_sec:
                st['section'] = True
            if ctx == 'top':
                if ENDSECTION_RE.match(code):
                    st['section'] = False
                m = INCLUDE_RE.match(code)
                if m and m.group(1).endswith('.asm') and norm(m.group(1)) not in skip:
                    visit(norm(m.group(1)), False)
        if top:
            out.append(Src(fname, len(text) + 1, '', 'eof', st['section'], False, st['obj']))

    for i, obj in enumerate(build.GAME['objects']):
        st.update(section=False, depth=0, obj=i)
        visit(norm(obj), True)
    return out


def instrument(lines, only=None):
    """Write the instrumented copy of the walked files to out/measure (only the files
    of the objects in `only`, if given; a file is rewritten only when it changes)."""
    root = os.path.join(build.OUT, 'measure')
    files, before, after = [], {}, {}
    for s in lines:
        if only is not None and s.obj not in only:
            continue
        if s.file not in files:
            files.append(s.file)
        key = (s.file, s.lineno)
        if s.ctx in ('top', 'open', 'eof') and s.in_section:
            before[key] = PRINT.format(s.file, s.lineno)     # also ends the section before a SECTION line
        if s.is_section:
            after[key] = ANCHOR
    objects = {norm(o): i for i, o in enumerate(build.GAME['objects'])}
    for fname in files:
        text = open(os.path.join(build.SRC, fname), encoding='utf-8', errors='replace').read().split('\n')
        if text and text[-1] == '':
            text.pop()
        out = []
        if fname in objects:
            out += ['DEF __gb_o EQU {}'.format(objects[fname]), 'DEF __gb_k = 0', 'DEF __gb_cur EQUS "__gb_none"']
        for n, raw in enumerate(text + [''], 1):
            if (fname, n) in before:
                out.append(before[(fname, n)])
            if n <= len(text):
                out.append(raw)
            if (fname, n) in after:
                out.append(after[(fname, n)])
        dst = os.path.join(root, fname)
        new = '\n'.join(out) + '\n'
        if os.path.exists(dst) and open(dst, encoding='utf-8').read() == new:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, 'w', encoding='utf-8', newline='\n').write(new)
    return root


def measure():
    """(walk(), {(file, lineno): [(cpu_addr, bank, size, is_ram), ...]}): one entry per
    time the line is assembled, in assembly order. Lines not assembled are missing.
    Cached per object file in out/measure.pickle: after an edit only the objects whose
    files changed are assembled again."""
    import hashlib
    import pickle
    lines = walk()
    flags = repr((build.GAME['objects'], build.GAME['asm_flags'], build.GAME['link_flags'])).encode()
    prelude = b''.join(f.encode() + open(os.path.join(build.SRC, f), 'rb').read() for f in sorted(prelude_files()))
    keys = []
    for i in range(len(build.GAME['objects'])):
        h = hashlib.sha1(flags + prelude)
        for f in sorted({s.file for s in lines if s.obj == i}):
            h.update(f.encode() + open(os.path.join(build.SRC, f), 'rb').read())
        keys.append(h.hexdigest())
    cache = os.path.join(build.OUT, 'measure.pickle')
    old = {}
    if os.path.exists(cache):
        try:
            old = pickle.load(open(cache, 'rb'))
            if not isinstance(old, dict) or old.get('version') != 2:
                old = {}
        except Exception:
            old = {}
    if old.get('keys') == keys:
        return lines, old['data']
    was = old.get('keys') or [None] * len(keys)
    stale = {i for i, k in enumerate(keys) if was[i] != k or not os.path.exists(object_path(i))}
    prints = old.get('prints') or [[] for _ in keys]
    root = instrument(lines, stale)
    for i in sorted(stale):
        obj = norm(build.GAME['objects'][i])
        r = subprocess.run([build.tool('rgbasm')] + build.GAME['asm_flags'] + ['-I', build.SRC, '-o', object_path(i), obj],
                           cwd=root, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit('measure: rgbasm failed on the instrumented {}:\n{}'.format(obj, r.stderr[-3000:]))
        prints[i] = []
        for row in r.stdout.split('\n'):
            if row.startswith('#L '):
                _, f, n, o_, k, off = row.split()
                prints[i].append((f, int(n), (int(o_), int(k)), int(off)))
    anchors = link_anchors(root)
    data = combine([p for ps in prints for p in ps], anchors)
    pickle.dump({'version': 2, 'keys': keys, 'prints': prints, 'anchors': anchors, 'data': data}, open(cache, 'wb'))
    return lines, data


def object_path(i):
    return os.path.join(build.OUT, 'measure', '__obj{}.o'.format(i))


def link_anchors(root):
    """Anchor (object, number) -> (bank, CPU address), from linking the instrumented objects."""
    sym = os.path.join(root, 'measure.sym')
    objs = [object_path(i) for i in range(len(build.GAME['objects']))]
    r = subprocess.run([build.tool('rgblink')] + build.GAME['link_flags'] + ['-n', sym, '-o', os.path.join(root, 'measure.gb')] + objs,
                       cwd=build.SRC, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit('measure: rgblink failed:\n' + r.stderr[-3000:])
    anchors = {}
    for row in open(sym):
        m = re.match(r'^([0-9a-fA-F]+):([0-9a-fA-F]+) __gb_s(\d+)_(\d+)\s*$', row)
        if m:
            anchors[(int(m.group(3)), int(m.group(4)))] = (int(m.group(1), 16), int(m.group(2), 16))
    return anchors


def combine(prints, anchors):
    """Line addresses and sizes: a line's size runs to the next print in its section."""
    out = {}
    for i, (f, n, sec, off) in enumerate(prints):
        if sec not in anchors:
            continue                               # printed before the first anchor of its object
        nxt = prints[i + 1] if i + 1 < len(prints) else None
        size = max(0, nxt[3] - off) if nxt and nxt[2] == sec else 0
        bank, base = anchors[sec]
        a = base + off
        out.setdefault((f, n), []).append((a, bank, size, a >= 0x8000))
    return out
