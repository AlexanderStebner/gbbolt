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
    g.setdefault('src', 'src')                  # the folder the assembler runs in ('.' for pret projects)
    g.setdefault('objects', [g['main']])        # one rgbasm run per object file, then one link
    g.setdefault('asm_flags', [])
    g.setdefault('prebuild', None)              # a command run in the project folder before assembling
    jt = g.get('jump_table_rst')
    g['jump_table_rst'] = int(jt, 0) if isinstance(jt, str) else jt
    tm = g.setdefault('test_memory', {})
    num = lambda v, d: int(v, 0) if isinstance(v, str) else (d if v is None else v)  # noqa: E731
    tm['stack_top'] = num(tm.get('stack_top'), 0xDF00)
    tm['stack_zone'] = [num(v, 0) for v in tm.get('stack_zone', ['0xDE00', '0xDF00'])]
    tm['free_ram'] = [num(v, 0) for v in tm.get('free_ram', ['0xC000', '0xDE00'])]
    return g


GAME = load_game()
SRC = os.path.normpath(os.path.join(ROOT, GAME['src']))
BASEROM = os.path.join(ROOT, GAME['rom'])
BUILT = os.path.join(OUT, GAME['rom'])
SYM = os.path.join(OUT, 'game.sym')


def rom_title(rom):
    """The title in the cartridge header ($0134-$0143)."""
    return bytes(rom[0x134:0x144]).split(b'\x00')[0].decode('ascii', 'replace').strip() or GAME['rom']


def asm_files():
    """The .asm files of the disassembly, in the order the object files include them."""
    out = []

    def walk(fname):
        if fname in out or not os.path.exists(os.path.join(SRC, fname)):
            return
        out.append(fname)
        for raw in open(os.path.join(SRC, fname), encoding='utf-8', errors='replace'):
            m = re.match(r'^\s*INCLUDE\s+"([^"]+\.asm)"', raw, re.I)
            if m:
                walk(m.group(1))
    for o in GAME['objects']:
        walk(o)
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
    if GAME['prebuild']:
        r = subprocess.run(GAME['prebuild'], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            return False, 'prebuild failed:\n{}{}'.format(r.stdout, r.stderr)
    objs = [os.path.join(OUT, os.path.splitext(o)[0].replace('/', '_') + '.o') for o in GAME['objects']]
    steps = [[tool('rgbasm')] + GAME['asm_flags'] + ['-I', SRC, '-s', 'equ:' + obj[:-2] + '.state', '-o', obj, o]
             for o, obj in zip(GAME['objects'], objs)]
    steps += [
        [tool('rgblink')] + GAME['link_flags'] + ['-n', SYM, '-m', os.path.join(OUT, 'game.map'), '-o', BUILT] + objs,
        [tool('rgbfix')] + GAME['fix_flags'] + [BUILT],
    ]
    for cmd in steps:
        r = subprocess.run(cmd, cwd=SRC, capture_output=True, text=True)
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
            where = ' (first difference at {})'.format(
                fmt_rom(next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))))
        return False, 'ROM differs: sha1 {} != {}{}'.format(got, want, where)
    return True, '{} matches: sha1 {}'.format(GAME['rom'], got)


# --- banks ---------------------------------------------------------------------
# A place in the ROM is its offset in the ROM file ("linear address"): bank n's
# $4000-$7FFF is n * $4000 + (addr - $4000). For a 32 KiB game that is simply the
# CPU address. Symbols keep CPU addresses (what the code sees); SYM_BANK has the bank.

SYM_BANK = {}                                      # label -> bank (ROM: ROM bank, RAM: WRAM/SRAM bank)
SYM_CONST = {}                                     # exported constants found in the .sym file


def rom_size():
    return os.path.getsize(BUILT) if os.path.exists(BUILT) else 0x8000


BANKED = None                                      # set by read_sym(): more than 32 KiB?


def linear(bank, addr):
    """ROM file offset of CPU address `addr` with ROM bank `bank` mapped in."""
    return addr if addr < 0x4000 or bank == 0 else bank * 0x4000 + (addr - 0x4000)   # bank 0: a --tiny ROM


def bank_of(lin):
    return 0 if lin < 0x4000 else lin // 0x4000


def cpu_addr(lin):
    return lin if lin < 0x4000 else 0x4000 + (lin & 0x3FFF)


def fmt_rom(lin):
    """$1234 for a 32 KiB game; 0E:5A2F (bank:address) for a banked one."""
    if not BANKED:
        return '${:04X}'.format(lin)
    return '{:02X}:{:04X}'.format(bank_of(lin), cpu_addr(lin))


def sym_linear(name, syms):
    """Linear address of a ROM label."""
    return linear(SYM_BANK.get(name, 0), syms[name])


def read_sym():
    """label -> CPU address from rgblink's .sym file (banks in SYM_BANK)."""
    global BANKED
    syms = {}
    SYM_BANK.clear()
    SYM_CONST.clear()
    for line in open(SYM):
        line = line.split(';')[0].strip()
        if not line:
            continue
        loc, name = line.split()
        if ':' not in loc:                         # an exported constant: `value NAME`
            SYM_CONST[name] = int(loc, 16)
            continue
        bank, addr = loc.split(':')
        syms[name] = int(addr, 16)
        SYM_BANK[name] = int(bank, 16)
    BANKED = rom_size() > 0x8000
    return syms


def read_consts():
    """Every numeric constant the assembler knew (`rgbasm -s equ:`), name -> value."""
    out = {}
    for o in GAME['objects']:
        path = os.path.join(OUT, os.path.splitext(o)[0].replace('/', '_') + '.state')
        if not os.path.exists(path):
            continue
        for line in open(path, encoding='utf-8', errors='replace'):
            m = re.match(r'^def\s+(\w+)\s+equ\s+\$([0-9A-Fa-f]+)\s*$', line, re.I)
            if m:
                out.setdefault(m.group(1), int(m.group(2), 16))
    return out


def read_map():
    """Sections from rgblink's .map file: [(type, bank, start, end, name)], end exclusive."""
    out, kind, bank = [], None, 0
    path = os.path.join(OUT, 'game.map')
    if not os.path.exists(path):
        return out
    for line in open(path, encoding='utf-8', errors='replace'):
        m = re.match(r'^(ROM0|ROMX|VRAM|SRAM|WRAM0|WRAMX|OAM|HRAM) bank #(\d+):', line)
        if m:
            kind, bank = m.group(1), int(m.group(2))
            continue
        m = re.match(r'^\s+SECTION: \$([0-9a-f]+)(?:-\$([0-9a-f]+))? \(\$[0-9a-f]+ bytes?\) \["(.*)"\]', line, re.I)
        if m and kind:
            start = int(m.group(1), 16)
            end = int(m.group(2), 16) + 1 if m.group(2) else start
            out.append((kind, bank, start, end, m.group(3)))
    return out
