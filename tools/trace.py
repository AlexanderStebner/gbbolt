"""Recursive code tracer for a 32 KiB (single bank) Game Boy ROM.

Finds which bytes are code by following control flow from the entry points,
including jump tables: a table of addresses right after an `rst` (game.json
"jump_table_rst") or a call to a dispatcher routine ("jump_table_calls"). Writes an mgbdis .sym file that marks code and data blocks
and names the discovered entry points.

    python tools/trace.py tetris.gb  ->  tetris.sym
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import build  # noqa: E402
from sm83 import decode  # noqa: E402

JUMPTABLE_RST = build.GAME['jump_table_rst']   # `rst $xx` + table of addresses, or None
# routines that are called with a table of addresses right after the call (game.json:
# "jump_table_calls": ["0x0229"]) - they pop it as their return address and jump through it
JUMPTABLE_TARGETS = ({JUMPTABLE_RST} if JUMPTABLE_RST is not None else set()) |     {int(a, 0) for a in build.GAME.get('jump_table_calls', [])}
ENTRY_POINTS = {
    0x0100: 'Boot',
    0x0000: 'RST_00',
    0x0008: 'RST_08',
    0x0028: 'RST_28',
    0x0040: 'VBlankInterrupt',
    0x0048: 'LCDCInterrupt',
    0x0050: 'TimerOverflowInterrupt',
    0x0058: 'SerialTransferCompleteInterrupt',
}
# code nothing refers to, but that is clearly code (game.json: "trace_entries": {"0x0153": "Name"})
ENTRY_POINTS.update({int(a, 0): n for a, n in build.GAME.get('trace_entries', {}).items()})


def trace(rom, table_limits):
    """One tracing pass. table_limits: {table_addr: max_entries or None}."""
    size = len(rom)
    code = {}            # addr -> Insn
    calls = set()        # call/rst targets
    jumps = set()        # jump targets
    tables = {}          # table_addr -> [targets]
    work = list(ENTRY_POINTS)
    seen = set()
    while work:
        pc = work.pop()
        while 0 <= pc < size and pc not in seen:
            insn = decode(rom, pc)
            if insn.kind == 'invalid':
                break
            seen.add(pc)
            code[pc] = insn
            nxt = pc + insn.length
            k = insn.kind
            if k in ('jump', 'cjump'):
                jumps.add(insn.target)
                work.append(insn.target)
            elif k in ('call', 'ccall', 'rst'):
                calls.add(insn.target)
                work.append(insn.target)
                if insn.target in JUMPTABLE_TARGETS:
                    t = nxt
                    limit = table_limits.get(t)
                    entries = []
                    a = t
                    while a + 1 < size and (limit is None or len(entries) < limit):
                        w = rom[a] | (rom[a + 1] << 8)
                        if not (0x0040 <= w < 0x8000) or 0x0104 <= w < 0x0150:
                            break
                        entries.append(w)
                        a += 2
                    tables[t] = entries
                    for w in entries:
                        jumps.add(w)
                        work.append(w)
                    break
            if k in ('jump', 'ret', 'jphl', 'stop'):
                break
            pc = nxt
    return code, calls, jumps, tables


def code_bytes(code):
    s = set()
    for a, i in code.items():
        s.update(range(a, a + i.length))
    return s


def run(rom):
    limits = {}
    for _ in range(50):
        code, calls, jumps, tables = trace(rom, limits)
        cb = code_bytes(code)
        starts = set(code)
        changed = False
        for t, entries in tables.items():
            # a table ends where code (or another table) begins
            n = 0
            for i in range(len(entries)):
                a = t + 2 * i
                if a in cb or a + 1 in cb or a in starts:
                    break
                if i and any(a == t2 for t2 in tables if t2 != t):
                    break
                n += 1
            if limits.get(t) != n:
                limits[t] = n
                changed = True
        if not changed:
            break
    return code, calls, jumps, tables


# --- banked ROMs ---------------------------------------------------------------
# Places are linear addresses (offsets in the ROM file). Code in bank n sees bank 0 at $0000-$3FFF and
# itself at $4000-$7FFF, so its jumps into $4000-$7FFF stay in bank n; a jump from bank 0 into
# $4000-$7FFF depends on the bank switched in at run time and is not followed. Code in other banks is
# reached through the far call (game.json "farcall": {"rst": "0x10", "table": "0x4001"}: `ld hl, $BBII`
# + `rst $10` calls entry II of the table of addresses at BB:table; every entry of every bank's table is
# traced), and through the addresses the game was seen running (--coverage FILE: one linear address in
# hex per line, from running the game in an emulator).

def banked_view(rom, bank, cache={}):
    v = cache.get(bank)
    if v is None:
        v = bytearray(0x10000)
        v[0:0x4000] = rom[0:0x4000]
        o = bank * 0x4000
        v[0x4000:0x8000] = rom[o:o + 0x4000].ljust(0x4000, b'\xff')
        cache[bank] = v
    return v


def lin_of(bank, addr):
    if addr < 0x4000:
        return addr
    if addr < 0x8000 and bank:
        return bank * 0x4000 + addr - 0x4000
    return None


def far_tables(rom, table):
    """bank -> [linear entry addresses] of the far-call table at the start of each bank (a bank has one
    when its first byte is its own number and the words after it point into the bank)."""
    out = {}
    for bank in range(1, len(rom) // 0x4000):
        o = bank * 0x4000
        if rom[o] != bank:
            continue
        entries, a, lowest = [], table, 0x8000
        while a + 1 < 0x8000 and a < lowest:
            w = rom[o + a - 0x4000] | rom[o + a - 0x3FFF] << 8
            if not table + 2 <= w < 0x8000:
                break
            entries.append(w)
            lowest = min(lowest, w)
            a += 2
        if entries:
            out[bank] = [lin_of(bank, w) for w in entries]
    return out


def trace_banked(rom, table_limits, extra):
    far = build.GAME.get('farcall') or {}
    far_rst = int(far['rst'], 0) if 'rst' in far else None
    far_table = int(far.get('table', '0x4001'), 0)
    ftables = far_tables(rom, far_table) if far_rst is not None else {}
    size = len(rom)
    code, calls, jumps, tables, unresolved = {}, set(), set(), {}, set()
    work = list(ENTRY_POINTS) + list(extra)
    far_used = set()                                   # (bank, entry) the code calls
    seen = set()
    while work:
        lin = work.pop()
        while lin is not None and 0 <= lin < size and lin not in seen:
            bank = lin // 0x4000
            view = banked_view(rom, bank)
            pc = lin if bank == 0 else 0x4000 + (lin & 0x3FFF)
            insn = decode(view, pc)
            if insn.kind == 'invalid' or pc + insn.length > (0x4000 if bank == 0 else 0x8000):
                break
            seen.add(lin)
            code[lin] = insn
            nxt = lin + insn.length
            k = insn.kind
            if k in ('jump', 'cjump', 'call', 'ccall', 'rst'):
                t = lin_of(bank, insn.target)
                if t is None:
                    if insn.target < 0x8000:
                        unresolved.add((lin, insn.target))
                else:
                    (jumps if k in ('jump', 'cjump') else calls).add(t)
                    work.append(t)
            if k == 'rst' and insn.target == far_rst and view[pc - 3] == 0x21 and lin - 3 in code:
                fb, fi = view[pc - 1], view[pc - 2]                  # ld hl, $BBII just before
                ft = ftables.get(fb, [])
                if fi < len(ft):
                    far_used.add((fb, fi))
                    calls.add(ft[fi])
                    work.append(ft[fi])
            if k in ('call', 'ccall', 'rst') and insn.target in JUMPTABLE_TARGETS:
                limit = table_limits.get(nxt)
                entries, a = [], nxt
                while a + 1 < size and (limit is None or len(entries) < limit):
                    ca = pc + (a - lin)                                  # CPU address of the entry
                    w = view[ca] | view[ca + 1] << 8 if ca + 1 < 0x8000 else None
                    if w is None or not (0x0040 <= w < 0x8000) or 0x0104 <= w < 0x0150:
                        break
                    t = lin_of(bank, w)
                    if t is None:
                        break
                    entries.append(t)
                    a += 2
                tables[nxt] = entries
                for t in entries:
                    jumps.add(t)
                    work.append(t)
                break
            if k in ('jump', 'ret', 'jphl', 'stop'):
                break
            lin = nxt
    trace_banked.far_used = far_used
    return code, calls, jumps, tables, unresolved, ftables


RARE_OPS = {0x00, 0x40, 0x49, 0x52, 0x5B, 0x64, 0x6D, 0x7F, 0x76, 0x10, 0xFF, 0x08, 0xE8, 0xF8, 0xF9,
            0x37, 0x3F, 0x27, 0x2F, 0xD9}


def looks_like_code(rom, lin, known, limit=300):
    """Does the code from `lin` (a far-table entry nothing calls by number) look like a routine? No invalid
    or rare opcodes (nop, ld b,b, halt, ...: common in data, rare in code), not mostly register moves (the
    high bytes of a pointer table), it ends (ret / jp), and its calls into bank 0 land on routines already
    found."""
    bank = lin // 0x4000
    view = banked_view(rom, bank)
    work, seen, rare, moves, n, ends = [0x4000 + (lin & 0x3FFF)], set(), 0, 0, 0, False
    while work and n < limit:
        pc = work.pop()
        while 0x4000 <= pc < 0x8000 and pc not in seen:
            insn = decode(view, pc)
            if insn.kind == 'invalid' or bank * 0x4000 + pc - 0x4000 in known.get('data', ()):
                return False
            seen.add(pc)
            n += 1
            rare += view[pc] in RARE_OPS
            moves += 0x40 <= view[pc] < 0x80          # ld r, r': a table of pointers into $4000-$7FFF reads so
            k = insn.kind
            if k in ('jump', 'cjump', 'call', 'ccall', 'rst') and insn.target is not None:
                t = insn.target
                if t < 0x4000 and k != 'rst' and t not in known['starts']:
                    return False
                if 0x4000 <= t < 0x8000:
                    work.append(t)
            if k in ('ret', 'jump', 'jphl'):
                ends = True
            if k in ('jump', 'ret', 'jphl', 'stop'):
                break
            pc += insn.length
    return ends and n >= 2 and rare * 12 <= n and moves * 5 <= n * 2


def run_banked(rom, extra):
    extra = list(extra)
    for _ in range(10):                            # far-table entries that look like code join the trace
        code, calls, jumps, tables, unresolved, ftables = run_banked_once(rom, extra)
        known = {'starts': set(code)}
        used = {b for b, _ in trace_banked.far_used}
        new = [e for b in used for e in ftables[b] if e not in code and looks_like_code(rom, e, known)]
        if not new:
            break
        extra += new
        run_banked.guessed = getattr(run_banked, 'guessed', set()) | set(new)
    return code, calls, jumps, tables, unresolved, ftables


def run_banked_once(rom, extra):
    limits = {}
    for _ in range(50):
        code, calls, jumps, tables, unresolved, ftables = trace_banked(rom, limits, extra)
        cb = code_bytes(code)
        starts = set(code)
        changed = False
        for t, entries in tables.items():
            n = 0
            for i in range(len(entries)):
                a = t + 2 * i
                if a in cb or a + 1 in cb or a in starts:
                    break
                if i and any(a == t2 for t2 in tables if t2 != t):
                    break
                n += 1
            if limits.get(t) != n:
                limits[t] = n
                changed = True
        if not changed:
            break
    return code, calls, jumps, tables, unresolved, ftables


def main_banked(rom_path, rom, extra):
    code, calls, jumps, tables, unresolved, ftables = run_banked(rom, extra)
    cb = code_bytes(code)
    size = len(rom)
    loc = lambda a: '{:02x}:{:04x}'.format(a // 0x4000, a if a < 0x4000 else 0x4000 + (a & 0x3FFF))  # noqa: E731
    tag = lambda a: ('{:04X}'.format(a) if a < 0x4000 else  # noqa: E731
                     '{:02X}_{:04X}'.format(a // 0x4000, 0x4000 + (a & 0x3FFF)))
    labels = dict(ENTRY_POINTS)
    data_blocks = {}
    used_banks = {b for b, _ in trace_banked.far_used}
    for bank, entries in ftables.items():
        if bank not in used_banks:
            continue
        o = bank * 0x4000
        labels.setdefault(o, 'BankNumber_{:02X}'.format(bank))
        labels.setdefault(o + 1, 'FarTable_{:02X}'.format(bank))
        data_blocks[o] = 1
        data_blocks[o + 1] = 2 * len(entries)
        for e in entries:                          # entries that are not code: data the bank hands out
            if e not in code:
                labels.setdefault(e, 'Data_' + tag(e))
    for t in tables:
        labels.setdefault(t, 'JumpTable_' + tag(t))
    for c in calls | getattr(run_banked, 'guessed', set()):
        labels.setdefault(c, 'Call_' + tag(c))
    for t in jumps:
        if t in cb and t not in labels:
            pass                                   # mgbdis names jump targets itself
    for e in tables.values():
        for w in e:
            labels.setdefault(w, 'Jump_' + tag(w))
    out = [';; generated by tools/trace.py - code/data map for mgbdis']
    for bank in range(size // 0x4000):
        base = bank * 0x4000
        a = base
        while a < base + 0x4000:
            if a in data_blocks:
                out.append('{} .data:{:x}'.format(loc(a), data_blocks[a]))
                a += data_blocks[a]
                continue
            is_code = a in cb
            b = a
            while b < base + 0x4000 and (b in cb) == is_code and (b == a or b not in data_blocks):
                b += 1
            out.append('{} .{}:{:x}'.format(loc(a), 'code' if is_code else 'data', b - a))
            a = b
    for a in sorted(labels):
        out.append('{} {}'.format(loc(a), labels[a]))
    open(os.path.splitext(rom_path)[0] + '.sym', 'w').write('\n'.join(out) + '\n')
    per_bank = {}
    for a in cb:
        per_bank[a // 0x4000] = per_bank.get(a // 0x4000, 0) + 1
    print(json.dumps({'code_bytes': len(cb), 'functions': len(calls), 'tables': len(tables),
                      'far_tables': len(used_banks), 'far_entries': len(trace_banked.far_used), 'unresolved_far_jumps': len(unresolved)}))
    for bank in sorted(used_banks):
        missing = [i for i in range(len(ftables[bank])) if (bank, i) not in trace_banked.far_used
                   and ftables[bank][i] not in code]
        if missing:
            print('bank {:02X}: far entries never called: {}'.format(bank, missing))
    print('code bytes per bank:', ' '.join('{:02X}:{}'.format(b, n) for b, n in sorted(per_bank.items())))


def main():
    rom_path = sys.argv[1]
    rom = open(rom_path, 'rb').read()
    if len(rom) > 0x8000:
        extra = []
        if '--coverage' in sys.argv:
            extra = [int(x, 16) for x in open(sys.argv[sys.argv.index('--coverage') + 1]).read().split()]
        return main_banked(rom_path, rom, extra)
    code, calls, jumps, tables = run(rom)
    cb = code_bytes(code)
    size = len(rom)

    # contiguous code / data ranges
    blocks = []
    a = 0
    while a < size:
        is_code = a in cb
        b = a
        while b < size and (b in cb) == is_code:
            b += 1
        blocks.append((a, b - a, 'code' if is_code else 'data'))
        a = b

    table_bytes = set()
    for t, e in tables.items():
        table_bytes.update(range(t, t + 2 * len(e)))

    labels = dict(ENTRY_POINTS)
    for t in tables:
        labels.setdefault(t, 'JumpTable_{:04X}'.format(t))
    for c in calls:
        labels.setdefault(c, 'Call_{:04X}'.format(c))
    for e in tables.values():
        for w in e:
            labels.setdefault(w, 'Jump_{:04X}'.format(w))

    out = [';; generated by tools/trace.py - code/data map for mgbdis']
    for a, n, kind in blocks:
        out.append('00:{:04x} .{}:{:x}'.format(a, kind, n))
    for a in sorted(labels):
        out.append('00:{:04x} {}'.format(a, labels[a]))
    sym_path = os.path.splitext(rom_path)[0] + '.sym'
    open(sym_path, 'w').write('\n'.join(out) + '\n')

    info = {
        'code_bytes': len(cb),
        'data_bytes': size - len(cb),
        'functions': len(calls),
        'tables': {'{:04X}'.format(t): ['{:04X}'.format(w) for w in e] for t, e in tables.items()},
    }
    print(json.dumps({k: v for k, v in info.items() if k != 'tables'}))
    for t, e in sorted(tables.items()):
        print('table ${:04X}: {} entries'.format(t, len(e)))


if __name__ == '__main__':
    main()
