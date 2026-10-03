"""Turn `db` bytes that are really code into instructions.

    python tools/codify.py LO HI [seeds.json]

Seeds are addresses known to execute (default: out/soundtrace.json from
soundtrace.py). From them the code is followed statically (fall-through,
jumps, calls) inside [LO, HI). Every db line in that range is rewritten as
instructions where code was found and as db elsewhere; branch targets get
`jr_000_xxxx` labels. The ROM is rebuilt; if it differs, nothing changes.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import asmparse  # noqa: E402
import build  # noqa: E402
from analyze import load_io_names  # noqa: E402
from sm83 import decode  # noqa: E402


def follow(rom, seeds, lo, hi):
    code = {}
    work = [s for s in seeds if lo <= s < hi]
    while work:
        pc = work.pop()
        while lo <= pc < hi and pc not in code:
            ins = decode(rom, pc)
            if ins.kind in ('invalid', 'stop') or pc + ins.length > hi:
                break
            code[pc] = ins
            if ins.kind in ('jump', 'cjump', 'call', 'ccall') and ins.target is not None:
                work.append(ins.target)
            if ins.kind in ('jump', 'ret', 'jphl'):
                break
            pc += ins.length
    # drop instructions overlapping an earlier one (conflicting decodes)
    taken, clean = set(), {}
    for a in sorted(code):
        span = set(range(a, a + code[a].length))
        if span & taken:
            continue
        taken |= span
        clean[a] = code[a]
    return clean


def rgbds(ins, name_for, label_for):
    t = ins.text
    t = t.replace('($ff00+c)', '[c]')
    if t.startswith('ld [c]') or t.endswith('[c]'):
        t = t.replace('ld ', 'ldh ', 1)
    t = t.replace('(hl+)', '[hli]').replace('(hl-)', '[hld]').replace('(', '[').replace(')', ']')
    if ins.target is not None and ins.kind in ('jump', 'cjump', 'call', 'ccall'):
        t = re.sub(r'\$[0-9a-f]{4}$', label_for(ins.target), t)
    else:
        def sub(m):
            a = int(m.group(1), 16)
            return name_for(a) or m.group(0)
        t = re.sub(r'\$([0-9a-f]{4})(?![0-9a-f])', sub, t)
    t = re.sub(r'sp([+-]\d+)', r'sp\1', t)
    return t


def main():
    lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
    seeds_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(build.OUT, 'soundtrace.json')
    ok, msg = build.build()
    if not ok:
        sys.exit(msg)
    rom = open(build.BUILT, 'rb').read()
    syms = build.read_sym()
    parsed = asmparse.parse(build.SRC, rom, syms)
    seeds = json.load(open(seeds_path))
    code = follow(rom, seeds, lo, hi)

    io_by_addr, _ = load_io_names(build.SRC)
    var_by_addr = {v['addr']: n for n, v in parsed.vars.items() if v['type'] != 'io'}
    label_by_addr = {}
    for n, a in syms.items():
        if '.' not in n:
            label_by_addr.setdefault(a, n)

    data_lines = [ln for ln in parsed.lines if ln.kind == 'data' and ln.addr is not None
                  and lo <= ln.addr < hi and ln.text.startswith('db ') and '"' not in ln.text]
    covered = set()
    for ln in data_lines:
        covered |= set(range(ln.addr, ln.addr + ln.size))
    code = {a: i for a, i in code.items() if set(range(a, a + i.length)) <= covered}
    if not code:
        sys.exit('no code found in db lines of that range')

    new_labels = {}

    def label_for(a):
        if a in label_by_addr:
            return label_by_addr[a]
        if lo <= a < hi and a in code:
            new_labels[a] = 'jr_000_{:04x}'.format(a)
            return new_labels[a]
        return '${:04x}'.format(a)

    def name_for(a):
        if a in io_by_addr:
            return io_by_addr[a]
        return var_by_addr.get(a)

    # first pass to collect labels
    for a in sorted(code):
        rgbds(code[a], name_for, label_for)

    files = {ln.file for ln in data_lines}
    if len(files) != 1:
        sys.exit('the range spans several source files: ' + ', '.join(sorted(files)))
    path = os.path.join(build.SRC, files.pop())
    lines = open(path, encoding='utf-8').read().split('\n')
    backup = '\n'.join(lines)
    by_raw_index = {}
    # map parsed data lines to their line in that file
    for ln in data_lines:
        by_raw_index[ln.lineno - 1] = ln
    changed = 0
    for idx in sorted(by_raw_index, reverse=True):
        ln = by_raw_index[idx]
        raw = rom[ln.addr:ln.addr + ln.size]
        if not any(ln.addr <= a < ln.addr + ln.size for a in code) and \
                not any(a < ln.addr < a + i.length for a, i in code.items()):
            continue
        out, a, pending = [], ln.addr, []

        def flush():
            if pending:
                out.append('\tdb ' + ', '.join('${:02x}'.format(b) for b in pending))
                pending.clear()
        end = ln.addr + ln.size
        while a < end:
            if a in code:
                flush()
                if a in new_labels and a not in label_by_addr:
                    out.append('')
                    out.append('{}:'.format(new_labels[a]))
                ins = code[a]
                out.append('\t' + rgbds(ins, name_for, label_for))
                a += ins.length
            elif any(c < a < c + code[c].length for c in code if c < a):
                a += 1          # inside an instruction started on an earlier line
            else:
                pending.append(rom[a])
                a += 1
        flush()
        lines[idx:idx + 1] = out
        changed += 1
    # instructions crossing a db line boundary: refuse
    open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
    ok, msg = build.build()
    if not ok:
        open(path, 'w', encoding='utf-8', newline='\n').write(backup)
        sys.exit('reverted - ' + msg)
    print('codified {} db line(s), {} instructions; {}'.format(changed, len(code), msg))


if __name__ == '__main__':
    main()
