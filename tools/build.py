"""Assemble, link and compare against the original ROM.

The game's project folder (with game.json and src/) is $GBBOLT_ROOT, or else the
current directory. RGBDS comes from $GBBOLT_RGBDS, ../rgbds next to the project,
or the PATH. The original ROM itself is optional: without it the built ROM is
compared with the "sha1" in game.json.
"""
import hashlib
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def find_root():
    root = os.environ.get('GBBOLT_ROOT')
    if root:
        return os.path.abspath(root)
    here = os.getcwd()
    while True:                                    # the nearest folder up from here with a game.json
        if os.path.exists(os.path.join(here, 'game.json')):
            return here
        up = os.path.dirname(here)
        if up == here:
            raise SystemExit('gbbolt: no game.json here or above - run the tools from a game project '
                             'folder or set GBBOLT_ROOT')
        here = up


ROOT = find_root()                                 # the game's project folder
RGBDS = os.environ.get('GBBOLT_RGBDS') or os.path.join(ROOT, '..', 'rgbds')
SRC = os.path.join(ROOT, 'src')
OUT = os.path.join(ROOT, 'out')


def load_game():
    """game.json: what the tools need to know about this game (with defaults)."""
    path = os.path.join(ROOT, 'game.json')
    g = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    g.setdefault('rom', next((f for f in sorted(os.listdir(ROOT)) if f.endswith(('.gb', '.gbc'))), 'game.gb'))
    g.setdefault('main', 'game.asm')
    g.setdefault('link_flags', [])
    g.setdefault('fix_flags', ['-v'])
    g.setdefault('entry', None)
    g.setdefault('graph_root', None)
    jt = g.get('jump_table_rst')
    g['jump_table_rst'] = int(jt, 0) if isinstance(jt, str) else jt
    tm = g.setdefault('test_memory', {})
    num = lambda v, d: int(v, 0) if isinstance(v, str) else (d if v is None else v)  # noqa: E731
    tm['stack_top'] = num(tm.get('stack_top'), 0xDF00)
    tm['stack_zone'] = [num(v, 0) for v in tm.get('stack_zone', ['0xDE00', '0xDF00'])]
    tm['free_ram'] = [num(v, 0) for v in tm.get('free_ram', ['0xC000', '0xDE00'])]
    return g


GAME = load_game()
BASEROM = os.path.join(ROOT, GAME['rom'])
BUILT = os.path.join(OUT, GAME['rom'])
SYM = os.path.join(OUT, 'game.sym')


def rom_title(rom):
    """The title in the cartridge header ($0134-$0143)."""
    return bytes(rom[0x134:0x144]).split(b'\x00')[0].decode('ascii', 'replace').strip() or GAME['rom']


def asm_files():
    """The .asm files of the disassembly, in the order the main file includes them."""
    out = []

    def walk(fname):
        out.append(fname)
        for raw in open(os.path.join(SRC, fname), encoding='utf-8'):
            m = re.match(r'^\s*INCLUDE\s+"([^"]+\.asm)"', raw, re.I)
            if m:
                walk(m.group(1))
    walk(GAME['main'])
    return out


def file_of_label(name):
    """Path of the source file that defines `name::` (or `name:`)."""
    for f in asm_files():
        path = os.path.join(SRC, f)
        for raw in open(path, encoding='utf-8'):
            if raw.rstrip() in (name + '::', name + ':'):
                return path
    raise KeyError('no label ' + name)


def tool(name):
    for exe in (os.path.join(RGBDS, name + '.exe'), os.path.join(RGBDS, name)):
        if os.path.isfile(exe):
            return exe
    return name                                    # from the PATH


def sha1(path):
    return hashlib.sha1(open(path, 'rb').read()).hexdigest()


def build(quiet=False):
    """Returns (ok, message)."""
    os.makedirs(OUT, exist_ok=True)
    obj = os.path.join(OUT, 'game.o')
    steps = [
        [tool('rgbasm'), '-I', SRC, '-o', obj, os.path.join(SRC, GAME['main'])],
        [tool('rgblink')] + GAME['link_flags'] + ['-n', SYM, '-m', os.path.join(OUT, 'game.map'), '-o', BUILT, obj],
        [tool('rgbfix')] + GAME['fix_flags'] + [BUILT],
    ]
    for cmd in steps:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, '{} failed:\n{}{}'.format(os.path.basename(cmd[0]), r.stdout, r.stderr)
    got = sha1(BUILT)
    have_rom = os.path.exists(BASEROM)
    want = sha1(BASEROM) if have_rom else GAME.get('sha1')
    if not want:
        return False, 'no {} to compare with and no "sha1" in game.json'.format(GAME['rom'])
    if want != got:
        where = ''
        if have_rom:
            a, b = open(BASEROM, 'rb').read(), open(BUILT, 'rb').read()
            where = ' (first difference at ${:04X})'.format(
                next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b))))
        return False, 'ROM differs: sha1 {} != {}{}'.format(got, want, where)
    return True, '{} matches: sha1 {}'.format(GAME['rom'], got)


def read_sym():
    """label -> address from rgblink's .sym file."""
    syms = {}
    for line in open(SYM):
        line = line.split(';')[0].strip()
        if not line:
            continue
        loc, name = line.split()
        syms[name] = int(loc.split(':')[1], 16)
    return syms
