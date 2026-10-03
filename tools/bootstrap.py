"""One-time bootstrap: trace the ROM and create the initial disassembly in src/.

    python tools/bootstrap.py [--force]

Runs tools/trace.py to produce tetris.sym (code/data map), then mgbdis
(from ../mgbdis) with the vector labels it normally hard-codes removed - the
tracer knows where code really is, and mgbdis' fixed 8-byte vector blocks
would cut Tetris' serial handler in half.

src/ is hand-edited after this, so the script refuses to overwrite it.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MGBDIS = os.path.join(ROOT, '..', 'mgbdis', 'mgbdis.py')
sys.path.insert(0, HERE)
import build  # noqa: E402
ROM = build.BASEROM
SRC = os.path.join(ROOT, 'src')


def main():
    if os.path.exists(os.path.join(SRC, 'bank_000.asm')) and '--force' not in sys.argv:
        sys.exit('src/ already exists and is hand-edited; pass --force to regenerate it')
    subprocess.check_call([sys.executable, os.path.join(HERE, 'trace.py'), ROM])

    source = open(MGBDIS, encoding='utf-8').read()
    marker = 'gbc_symbols = ['
    patch = ("default_symbols = [s for s in default_symbols\n"
             "                   if int(s.split()[0].split(':')[1], 16) >= 0x104]\n")
    source = source.replace(marker, patch + marker, 1)
    sys.argv = ['mgbdis.py', ROM, '--tiny', '--output-dir', SRC, '--hli', 'hli',
                '--ldh_a8', 'ldh_ffa8', '--indent-tabs', '--overwrite']
    sys.path.insert(0, os.path.dirname(MGBDIS))
    exec(compile(source, MGBDIS, 'exec'), {'__name__': '__main__', '__file__': MGBDIS})
    postprocess()


def postprocess():
    """Jump tables as `dw Label`, and an (empty) ram.inc for named variables."""
    labels = {}
    for line in open(os.path.splitext(ROM)[0] + '.sym'):
        if line.startswith(';') or ' .' in line:
            continue
        loc, name = line.split()
        labels[int(loc.split(':')[1], 16)] = name

    path = os.path.join(SRC, 'bank_000.asm')
    lines = open(path).read().split('\n')
    out, i = [], 0
    while i < len(lines):
        out.append(lines[i])
        if lines[i].startswith('JumpTable_'):
            i += 1
            data = []
            while i < len(lines) and lines[i].strip().startswith('db '):
                data += [int(v.strip()[1:], 16) for v in lines[i].strip()[3:].split(',')]
                i += 1
            for k in range(0, len(data), 2):
                w = data[k] | (data[k + 1] << 8)
                out.append('\tdw {}'.format(labels.get(w, '${:04x}'.format(w))))
            continue
        i += 1
    open(path, 'w', newline='\n').write('\n'.join(out))

    open(os.path.join(SRC, 'ram.inc'), 'w', newline='\n').write(
        '; Named RAM / IO addresses.\n'
        '; Format: DEF name EQU $addr ;@ type description\n'
        ';   type: u8 | u16 (little endian) | u8[N] | code[N] | const\n'
        '; Lines without ";@" are plain constants.\n\n')
    game = os.path.join(SRC, 'game.asm')
    text = open(game).read().replace('INCLUDE "hardware.inc"',
                                     'INCLUDE "hardware.inc"\nINCLUDE "ram.inc"')
    open(game, 'w', newline='\n').write(text)


if __name__ == '__main__':
    main()
