"""Rename a label, or give a RAM/IO address a name, across src/.

    python tools/rename.py Call_0166 AddScoreBCD
    python tools/rename.py '$ffe1' hGameState u8 "Index into the game-state jump table"

The address form replaces the literal address in instruction operands and adds
a `DEF name EQU $addr ;@ type description` line to src/ram.inc. The ROM is
rebuilt afterwards; if it no longer matches, every file is restored.
"""
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import SRC, build  # noqa: E402

RAM_INC = os.path.join(SRC, 'ram.inc')


def src_files():
    return sorted(glob.glob(os.path.join(SRC, '*.asm')) + glob.glob(os.path.join(SRC, '*.inc')))


def rename_label(text, old, new):
    return re.sub(r'(?<![\w.]){}(?![\w])'.format(re.escape(old)), new, text)


def rename_address(text, addr, new):
    pat = re.compile(r'\${:04x}(?![0-9a-fA-F])'.format(addr), re.IGNORECASE)
    out = []
    for line in text.split('\n'):
        code = line.split(';', 1)[0]
        stripped = code.strip().lower()
        if stripped.startswith(('db ', 'dw ', 'def ')) or not stripped:
            out.append(line)
            continue
        out.append(pat.sub(new, code) + line[len(code):])
    return '\n'.join(out)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    old, new = sys.argv[1], sys.argv[2]
    backup = {p: open(p, encoding='utf-8').read() for p in src_files()}
    is_addr = old.startswith('$')
    alias = is_addr and '+' in new   # e.g. 'hBGMapAddr + 1': no new DEF
    if is_addr:
        addr = int(old[1:], 16)
    if is_addr and not alias:
        vtype = sys.argv[3] if len(sys.argv) > 3 else 'u8'
        desc = sys.argv[4] if len(sys.argv) > 4 else ''
        ram = backup.get(RAM_INC, '')
        if re.search(r'^DEF {}\b'.format(re.escape(new)), ram, re.M):
            sys.exit('{} already defined in ram.inc'.format(new))
        line = 'DEF {} EQU ${:04X} ;@ {} {}'.format(new, addr, vtype, desc).rstrip()
        with open(RAM_INC, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    count = 0
    for p, text in backup.items():
        if p == RAM_INC and is_addr:
            continue
        t2 = rename_address(text, addr, new) if is_addr else rename_label(text, old, new)
        if t2 != text:
            count += 1
            open(p, 'w', encoding='utf-8', newline='\n').write(t2)
    ok, msg = build()
    if not ok:
        for p, text in backup.items():
            open(p, 'w', encoding='utf-8', newline='\n').write(text)
        if is_addr and RAM_INC not in backup:
            os.remove(RAM_INC)
        sys.exit('rename reverted - ' + msg)
    print('{} -> {} ({} file(s)); {}'.format(old, new, count, msg))


if __name__ == '__main__':
    main()
