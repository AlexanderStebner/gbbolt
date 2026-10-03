"""Rewrite a `db` table of little-endian addresses as `dw Label` lines.

    python tools/dwtable.py TableLabel COUNT

The words must point at existing labels. The ROM is rebuilt; if it differs,
nothing changes.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asmparse  # noqa: E402
import build  # noqa: E402


def main():
    name, count = sys.argv[1], int(sys.argv[2], 0)
    ok, msg = build.build()
    rom = open(build.BUILT, 'rb').read()
    syms = build.read_sym()
    by_addr = {}
    for n, a in syms.items():
        if '.' not in n and not n.startswith('jr_'):
            by_addr.setdefault(a, n)
    start = syms[name]
    words = [rom[start + 2 * i] | rom[start + 2 * i + 1] << 8 for i in range(count)]
    missing = [w for w in words if w not in by_addr]
    if missing:
        sys.exit('no label at ' + ', '.join('${:04X}'.format(w) for w in missing))
    path = build.file_of_label(name)
    text = open(path, encoding='utf-8').read()
    lines = text.split('\n')
    i = lines.index(name + '::') + 1
    j = i
    total = 0
    while total < 2 * count:
        m = re.match(r'^\tdb (.*)$', lines[j])
        if not m:
            sys.exit('table is not a run of db lines')
        total += len(asmparse.split_operands(m.group(1)))
        j += 1
    if total != 2 * count:
        sys.exit('db lines do not end at the table end ({} bytes)'.format(total))
    lines[i:j] = ['\tdw ' + by_addr[w] for w in words]
    open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
    ok, msg = build.build()
    if not ok:
        open(path, 'w', encoding='utf-8', newline='\n').write(text)
        sys.exit('reverted - ' + msg)
    print('{}: {} entries; {}'.format(name, count, msg))


if __name__ == '__main__':
    main()
