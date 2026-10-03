"""Put a label at an address inside a data block, splitting the `db` line.

    python tools/addlabel.py 415F Font1bpp ["optional comment"]

The ROM is rebuilt afterwards and the change reverted if it no longer matches.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import SRC, build, read_sym, BUILT  # noqa: E402
import asmparse  # noqa: E402


def main():
    addr, name = int(sys.argv[1].lstrip('$'), 16), sys.argv[2]
    comment = sys.argv[3] if len(sys.argv) > 3 else None
    ok, msg = build()
    if not ok:
        sys.exit(msg)
    parsed = asmparse.parse(SRC, open(BUILT, 'rb').read(), read_sym())
    target = None
    for ln in parsed.lines:
        if ln.kind == 'data' and ln.addr is not None and ln.addr <= addr < ln.addr + ln.size:
            target = ln
            break
    if target is None:
        sys.exit('no db line covers ${:04X}'.format(addr))
    if not target.text.startswith('db ') or '"' in target.text:
        sys.exit('can only split plain db lines: ' + target.text)
    items = asmparse.split_operands(target.text[3:])
    k = addr - target.addr
    out = []
    if k:
        out.append('\tdb ' + ', '.join(items[:k]))
        out.append('')
    if comment:
        out.append('; ' + comment)
    out.append('{}::'.format(name))
    out.append('\tdb ' + ', '.join(items[k:]))

    path = os.path.join(SRC, target.file)
    lines = open(path, encoding='utf-8').read().split('\n')
    i = next(n for n, raw in enumerate(lines) if raw == target.raw and
             sum(1 for x in parsed.lines[:parsed.lines.index(target)] if x.file == target.file and x.raw == raw) ==
             sum(1 for x in lines[:n] if x == raw))
    backup = '\n'.join(lines)
    lines[i:i + 1] = out
    open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
    ok, msg = build()
    if not ok:
        open(path, 'w', encoding='utf-8', newline='\n').write(backup)
        sys.exit('reverted - ' + msg)
    print('{} at ${:04X}; {}'.format(name, addr, msg))


if __name__ == '__main__':
    main()
