"""Static analysis over the parsed disassembly: call graph and memory references."""
import os
import re

import build

from sm83 import decode

TERMINAL = ('jump', 'ret', 'jphl')


def load_io_names(src_dir):
    """hardware.inc register names: (address -> preferred name, name -> address)."""
    by_addr, by_name = {}, {}
    path = os.path.join(src_dir, 'hardware.inc')
    if os.path.exists(path):
        for line in open(path, encoding='utf-8', errors='replace'):
            m = re.match(r'^\s*DEF\s+(r\w+)\s+EQU\s+\$([0-9A-Fa-f]{4})\b', line)
            if m:
                a = int(m.group(2), 16)
                by_addr.setdefault(a, m.group(1))
                by_name[m.group(1)] = a
    return by_addr, by_name


# `rst $xx` followed by a table of addresses (game.json: jump_table_rst; None = not used)
JUMP_RST = build.GAME['jump_table_rst']

class Analysis:
    def __init__(self, parsed, rom, src_dir):
        self.p = parsed
        self.rom = rom
        self.io, self.io_names = load_io_names(src_dir)
        self.by_start = {u.start: u for u in parsed.units}
        self.unit_at = {}
        for u in parsed.units:
            for a in range(u.start, u.end):
                self.unit_at.setdefault(a, u)
        self.tables = {}     # table unit name -> [target unit names]
        self.edges = []      # (from, to, kind)
        self.refs = {}       # unit -> {'reads': set(addr), 'writes': set(addr), 'ptrs': set(addr)}
        self.insns = {}      # unit -> [Insn]
        self.run()

    def owner(self, a):
        """The most specific (smallest) variable containing an address."""
        best = None
        for v in self.p.vars.values():
            if v['type'] != 'io' and v['addr'] <= a < v['addr'] + v['size']:
                if best is None or v['size'] < best['size']:
                    best = v
        return best

    def var_for(self, a):
        v = self.owner(a)
        if v:
            return v['name'] if v['addr'] == a else '{}+{}'.format(v['name'], a - v['addr'])
        return self.io.get(a)

    def symbolic(self, a):
        """Owner name for an address: variable, IO register, or None."""
        v = self.owner(a)
        return v['name'] if v else self.io.get(a)

    def run(self):
        lines = self.p.lines
        code_units = {u.name for u in self.p.units if u.kind == 'code'}
        for u in self.p.units:
            if u.kind != 'data':
                continue
            targets = []
            for i in u.lines:
                ln = lines[i]
                if ln.kind == 'data':
                    m = re.match(r'^dw\s+(.+)$', ln.text)
                    if not m:
                        targets = None
                        break
                    targets += [t.strip() for t in m.group(1).split(',')]
            # a jump table: dw entries that are all code (not e.g. pointers to song data)
            if targets and all(t in code_units for t in targets):
                self.tables[u.name] = targets

        units = self.p.units
        for idx, u in enumerate(units):
            if u.kind != 'code':
                continue
            code_lines = [lines[i] for i in u.lines if lines[i].kind == 'insn']
            ins = [decode(self.rom, ln.addr) for ln in code_lines]
            self.insns[u.name] = ins
            blank = lambda: {'reads': set(), 'writes': set(), 'ptrs': set()}
            r = self.refs.setdefault(u.name, blank())
            seen = set()

            def edge(to, kind):
                if (to, kind) not in seen and to != u.name:
                    seen.add((to, kind))
                    self.edges.append((u.name, to, kind))

            for k, insn in enumerate(ins):
                t = insn.target
                if insn.kind in ('call', 'ccall', 'rst') and t in self.by_start:
                    edge(self.by_start[t].name, 'call')
                elif insn.kind in ('jump', 'cjump') and t is not None and not (u.start <= t < u.end):
                    tu = self.unit_at.get(t)
                    if tu:
                        edge(tu.name, 'jump')
                if insn.kind == 'rst' and t == JUMP_RST and k == len(ins) - 1 and idx + 1 < len(units):
                    nxt = units[idx + 1]
                    for target in self.tables.get(nxt.name, []):
                        edge(target, 'table')
                text = insn.text
                # a fragment linked to another unit's line (`;=@Unit.tag`) counts for that unit
                xr = code_lines[k].xref
                rr = self.refs.setdefault(xr[0], blank()) if xr else r
                if insn.imm is not None and insn.imm >= 0x8000:
                    a = insn.imm
                    if text.startswith(('ld a, (', 'ldh a, (')):
                        rr['reads'].add(a)
                    elif text.startswith(('ld (', 'ldh (')):
                        rr['writes'].add(a)
                    elif re.match(r'^ld (bc|de|hl|sp), \$', text):
                        rr['ptrs'].add(a)
            if ins and ins[-1].kind not in TERMINAL and idx + 1 < len(units):
                nxt = units[idx + 1]
                if nxt.kind == 'code':
                    edge(nxt.name, 'fallthrough')
