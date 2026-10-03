"""SM83 (Game Boy CPU) decoder and interpreter.

Used by the tracer (instruction lengths / control flow) and by the
differential tester (executing the original code).
"""

R8 = ['b', 'c', 'd', 'e', 'h', 'l', '(hl)', 'a']
RP = ['bc', 'de', 'hl', 'sp']
RP2 = ['bc', 'de', 'hl', 'af']
CC = ['nz', 'z', 'nc', 'c']
ALU = ['add', 'adc', 'sub', 'sbc', 'and', 'xor', 'or', 'cp']
ROT = ['rlc', 'rrc', 'rl', 'rr', 'sla', 'sra', 'swap', 'srl']

FZ, FN, FH, FC = 0x80, 0x40, 0x20, 0x10

INVALID = {0xD3, 0xDB, 0xDD, 0xE3, 0xE4, 0xEB, 0xEC, 0xED, 0xF4, 0xFC, 0xFD}


class Insn:
    """Decoded instruction with control-flow info for the tracer."""
    __slots__ = ('addr', 'op', 'length', 'text', 'kind', 'target', 'imm')

    def __init__(self, addr, op, length, text, kind='normal', target=None, imm=None):
        self.addr, self.op, self.length, self.text = addr, op, length, text
        # kind: normal | jump | cjump | call | ccall | ret | cret | jphl | rst | stop | invalid
        self.kind, self.target, self.imm = kind, target, imm


def s8(v):
    return v - 256 if v & 0x80 else v


def decode(mem, addr):
    """Decode the instruction at addr. mem is indexable (bytes)."""
    op = mem[addr]
    n = mem[(addr + 1) & 0xFFFF]
    nn = n | (mem[(addr + 2) & 0xFFFF] << 8)
    x, y, z, p, q = op >> 6, (op >> 3) & 7, op & 7, (op >> 4) & 3, (op >> 3) & 1

    if op in INVALID:
        return Insn(addr, op, 1, 'db ${:02x}'.format(op), 'invalid')
    if op == 0xCB:
        cb = n
        cx, cy, cz = cb >> 6, (cb >> 3) & 7, cb & 7
        if cx == 0:
            t = '{} {}'.format(ROT[cy], R8[cz])
        else:
            t = '{} {}, {}'.format(['', 'bit', 'res', 'set'][cx], cy, R8[cz])
        return Insn(addr, op, 2, t)

    if x == 0:
        if z == 0:
            if y == 0:
                return Insn(addr, op, 1, 'nop')
            if y == 1:
                return Insn(addr, op, 3, 'ld (${:04x}), sp'.format(nn), imm=nn)
            if y == 2:
                return Insn(addr, op, 2, 'stop', 'stop')
            tgt = (addr + 2 + s8(n)) & 0xFFFF
            if y == 3:
                return Insn(addr, op, 2, 'jr ${:04x}'.format(tgt), 'jump', tgt)
            return Insn(addr, op, 2, 'jr {}, ${:04x}'.format(CC[y - 4], tgt), 'cjump', tgt)
        if z == 1:
            if q == 0:
                return Insn(addr, op, 3, 'ld {}, ${:04x}'.format(RP[p], nn), imm=nn)
            return Insn(addr, op, 1, 'add hl, {}'.format(RP[p]))
        if z == 2:
            m = ['(bc)', '(de)', '(hl+)', '(hl-)'][p]
            return Insn(addr, op, 1, 'ld {}, a'.format(m) if q == 0 else 'ld a, {}'.format(m))
        if z == 3:
            return Insn(addr, op, 1, '{} {}'.format('inc' if q == 0 else 'dec', RP[p]))
        if z == 4:
            return Insn(addr, op, 1, 'inc ' + R8[y])
        if z == 5:
            return Insn(addr, op, 1, 'dec ' + R8[y])
        if z == 6:
            return Insn(addr, op, 2, 'ld {}, ${:02x}'.format(R8[y], n), imm=n)
        return Insn(addr, op, 1, ['rlca', 'rrca', 'rla', 'rra', 'daa', 'cpl', 'scf', 'ccf'][y])
    if x == 1:
        if op == 0x76:
            return Insn(addr, op, 1, 'halt')
        return Insn(addr, op, 1, 'ld {}, {}'.format(R8[y], R8[z]))
    if x == 2:
        return Insn(addr, op, 1, '{} {}'.format(ALU[y], R8[z]))
    # x == 3
    if z == 0:
        if y < 4:
            return Insn(addr, op, 1, 'ret ' + CC[y], 'cret')
        if y == 4:
            return Insn(addr, op, 2, 'ldh ($ff{:02x}), a'.format(n), imm=0xFF00 | n)
        if y == 5:
            return Insn(addr, op, 2, 'add sp, {}'.format(s8(n)))
        if y == 6:
            return Insn(addr, op, 2, 'ldh a, ($ff{:02x})'.format(n), imm=0xFF00 | n)
        return Insn(addr, op, 2, 'ld hl, sp{:+d}'.format(s8(n)))
    if z == 1:
        if q == 0:
            return Insn(addr, op, 1, 'pop ' + RP2[p])
        if p == 0:
            return Insn(addr, op, 1, 'ret', 'ret')
        if p == 1:
            return Insn(addr, op, 1, 'reti', 'ret')
        if p == 2:
            return Insn(addr, op, 1, 'jp hl', 'jphl')
        return Insn(addr, op, 1, 'ld sp, hl')
    if z == 2:
        if y < 4:
            return Insn(addr, op, 3, 'jp {}, ${:04x}'.format(CC[y], nn), 'cjump', nn)
        if y == 4:
            return Insn(addr, op, 1, 'ld ($ff00+c), a')
        if y == 5:
            return Insn(addr, op, 3, 'ld (${:04x}), a'.format(nn), imm=nn)
        if y == 6:
            return Insn(addr, op, 1, 'ld a, ($ff00+c)')
        return Insn(addr, op, 3, 'ld a, (${:04x})'.format(nn), imm=nn)
    if z == 3:
        if y == 0:
            return Insn(addr, op, 3, 'jp ${:04x}'.format(nn), 'jump', nn)
        if y == 6:
            return Insn(addr, op, 1, 'di')
        return Insn(addr, op, 1, 'ei')
    if z == 4:
        return Insn(addr, op, 3, 'call {}, ${:04x}'.format(CC[y], nn), 'ccall', nn)
    if z == 5:
        if q == 0:
            return Insn(addr, op, 1, 'push ' + RP2[p])
        return Insn(addr, op, 3, 'call ${:04x}'.format(nn), 'call', nn)
    if z == 6:
        return Insn(addr, op, 2, '{} ${:02x}'.format(ALU[y], n), imm=n)
    return Insn(addr, op, 1, 'rst ${:02x}'.format(y * 8), 'rst', y * 8)


class StepLimit(Exception):
    pass


class CPU:
    """A plain SM83 interpreter over a flat 64 KiB memory.

    No PPU/timers/interrupts: hardware registers behave like RAM. Memory
    accesses are recorded so the verifier can see what code really touches.
    """

    def __init__(self, mem):
        self.mem = mem  # bytearray(0x10000)
        self.a = self.f = self.b = self.c = self.d = self.e = self.h = self.l = 0
        self.sp = 0xDFF0
        self.pc = 0
        self.ime = False
        self.reads = set()
        self.writes = set()
        self.reg_read = set()     # registers read before being written
        self.reg_written = set()
        self.trace = False
        self.rom_writes = []      # writes to $0000-$7FFF (MBC registers)
        self.push_marks = []      # (register pair, sp, untouched halves) per push
        self.io_log = None        # list: collect (address, value) of sound register writes

    # --- register tracking -------------------------------------------------
    def _rr(self, name):
        if name not in self.reg_written:
            self.reg_read.add(name)

    def _rw(self, name):
        self.reg_written.add(name)

    def get8(self, i):
        if i == 6:
            return self.rd(self.hl)
        name = R8[i]
        self._rr(name)
        return getattr(self, name)

    def set8(self, i, v):
        if i == 6:
            self.wr(self.hl, v)
            return
        name = R8[i]
        self._rw(name)
        setattr(self, name, v & 0xFF)

    def getp(self, name):
        if name == 'sp':
            return self.sp
        if name == 'af':
            self._rr('a')
            self._rr('f')
            return (self.a << 8) | (self.f & 0xF0)
        hi, lo = name[0], name[1]
        self._rr(hi)
        self._rr(lo)
        return (getattr(self, hi) << 8) | getattr(self, lo)

    def setp(self, name, v):
        v &= 0xFFFF
        if name == 'sp':
            self.sp = v
            return
        if name == 'af':
            self._rw('a')
            self._rw('f')
            self.a, self.f = v >> 8, v & 0xF0
            return
        hi, lo = name[0], name[1]
        self._rw(hi)
        self._rw(lo)
        setattr(self, hi, v >> 8)
        setattr(self, lo, v & 0xFF)

    hl = property(lambda s: s.getp('hl'), lambda s, v: s.setp('hl', v))

    def flag(self, m):
        self._rr('f')
        return bool(self.f & m)

    def setf(self, z, n, h, c):
        self._rw('f')
        self.f = (FZ if z else 0) | (FN if n else 0) | (FH if h else 0) | (FC if c else 0)

    # --- memory ------------------------------------------------------------
    def rd(self, a):
        a &= 0xFFFF
        self.reads.add(a)
        return self.mem[a]

    def wr(self, a, v):
        a &= 0xFFFF
        if self.io_log is not None and 0xFF10 <= a <= 0xFF3F:
            self.io_log.append((a, v & 0xFF))
        if a < 0x8000:
            self.rom_writes.append((a, v & 0xFF))
            return
        self.writes.add(a)
        self.mem[a] = v & 0xFF

    def fetch(self):
        v = self.mem[self.pc]
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self):
        lo = self.fetch()
        return lo | (self.fetch() << 8)

    def push(self, v):
        self.sp = (self.sp - 1) & 0xFFFF
        self.wr(self.sp, v >> 8)
        self.sp = (self.sp - 1) & 0xFFFF
        self.wr(self.sp, v & 0xFF)

    def pop(self):
        lo = self.rd(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        hi = self.rd(self.sp)
        self.sp = (self.sp + 1) & 0xFFFF
        return lo | (hi << 8)

    def cond(self, y):
        if y == 0:
            return not self.flag(FZ)
        if y == 1:
            return self.flag(FZ)
        if y == 2:
            return not self.flag(FC)
        return self.flag(FC)

    # --- ALU ---------------------------------------------------------------
    def alu(self, y, v):
        a = self.a
        self._rr('a')
        if y in (1, 3):
            cin = 1 if self.flag(FC) else 0
        else:
            cin = 0
        if y in (0, 1):
            r = a + v + cin
            self.setf((r & 0xFF) == 0, 0, (a & 0xF) + (v & 0xF) + cin > 0xF, r > 0xFF)
        elif y in (2, 3, 7):
            r = a - v - cin
            self.setf((r & 0xFF) == 0, 1, (a & 0xF) - (v & 0xF) - cin < 0, r < 0)
            if y == 7:
                return
        elif y == 4:
            r = a & v
            self.setf(r == 0, 0, 1, 0)
        elif y == 5:
            r = a ^ v
            self.setf(r == 0, 0, 0, 0)
        else:
            r = a | v
            self.setf(r == 0, 0, 0, 0)
        self._rw('a')
        self.a = r & 0xFF

    def cbop(self, cb):
        x, y, z = cb >> 6, (cb >> 3) & 7, cb & 7
        v = self.get8(z)
        if x == 1:
            self._rr('f')
            c = self.f & FC
            self._rw('f')
            self.f = (0 if v & (1 << y) else FZ) | FH | c
            return
        if x == 2:
            self.set8(z, v & ~(1 << y))
            return
        if x == 3:
            self.set8(z, v | (1 << y))
            return
        c = self.flag(FC) if y in (2, 3) else False
        if y == 0:
            co = v >> 7
            r = (v << 1 | co)
        elif y == 1:
            co = v & 1
            r = (v >> 1) | (co << 7)
        elif y == 2:
            co = v >> 7
            r = (v << 1) | int(c)
        elif y == 3:
            co = v & 1
            r = (v >> 1) | (int(c) << 7)
        elif y == 4:
            co = v >> 7
            r = v << 1
        elif y == 5:
            co = v & 1
            r = (v >> 1) | (v & 0x80)
        elif y == 6:
            co = 0
            r = ((v & 0xF) << 4) | (v >> 4)
        else:
            co = v & 1
            r = v >> 1
        r &= 0xFF
        self.set8(z, r)
        self.setf(r == 0, 0, 0, co)

    # --- execution ---------------------------------------------------------
    def step(self):
        pc0 = self.pc
        op = self.fetch()
        x, y, z, p, q = op >> 6, (op >> 3) & 7, op & 7, (op >> 4) & 3, (op >> 3) & 1
        if op in INVALID:
            raise RuntimeError('invalid opcode ${:02x} at ${:04x}'.format(op, pc0))
        if op == 0xCB:
            self.cbop(self.fetch())
            return
        if x == 0:
            if z == 0:
                if y == 0:
                    return
                if y == 1:
                    a = self.fetch16()
                    self.wr(a, self.sp & 0xFF)
                    self.wr(a + 1, self.sp >> 8)
                    return
                if y == 2:
                    self.fetch()
                    raise RuntimeError('stop at ${:04x}'.format(pc0))
                d = s8(self.fetch())
                if y == 3 or self.cond(y - 4):
                    self.pc = (self.pc + d) & 0xFFFF
                return
            if z == 1:
                if q == 0:
                    self.setp(RP[p], self.fetch16())
                else:
                    hl = self.getp('hl')
                    v = self.getp(RP[p])
                    r = hl + v
                    self._rr('f')
                    z_ = self.f & FZ
                    self.setf(0, 0, (hl & 0xFFF) + (v & 0xFFF) > 0xFFF, r > 0xFFFF)
                    self.f |= z_
                    self.setp('hl', r)
                return
            if z == 2:
                if p == 0:
                    addr = self.getp('bc')
                elif p == 1:
                    addr = self.getp('de')
                else:
                    addr = self.getp('hl')
                    self.setp('hl', addr + (1 if p == 2 else -1))
                if q == 0:
                    self._rr('a')
                    self.wr(addr, self.a)
                else:
                    self._rw('a')
                    self.a = self.rd(addr)
                return
            if z == 3:
                self.setp(RP[p], self.getp(RP[p]) + (1 if q == 0 else -1))
                return
            if z in (4, 5):
                v = self.get8(y)
                self._rr('f')
                c = self.f & FC
                if z == 4:
                    r = (v + 1) & 0xFF
                    self.setf(r == 0, 0, (v & 0xF) == 0xF, 0)
                else:
                    r = (v - 1) & 0xFF
                    self.setf(r == 0, 1, (v & 0xF) == 0, 0)
                self.f |= c
                self.set8(y, r)
                return
            if z == 6:
                self.set8(y, self.fetch())
                return
            # z == 7
            a = self.a
            self._rr('a')
            if y == 0:
                c = a >> 7
                self.a = ((a << 1) | c) & 0xFF
                self.setf(0, 0, 0, c)
            elif y == 1:
                c = a & 1
                self.a = (a >> 1) | (c << 7)
                self.setf(0, 0, 0, c)
            elif y == 2:
                c = a >> 7
                self.a = ((a << 1) | int(self.flag(FC))) & 0xFF
                self.setf(0, 0, 0, c)
            elif y == 3:
                c = a & 1
                self.a = (a >> 1) | (int(self.flag(FC)) << 7)
                self.setf(0, 0, 0, c)
            elif y == 4:
                n, h, c = self.flag(FN), self.flag(FH), self.flag(FC)
                if not n:
                    if c or a > 0x99:
                        a += 0x60
                        c = True
                    if h or (a & 0xF) > 9:
                        a += 6
                else:
                    if c:
                        a -= 0x60
                    if h:
                        a -= 6
                a &= 0xFF
                self.a = a
                self.setf(a == 0, n, 0, c)
            elif y == 5:
                self.a = a ^ 0xFF
                self._rr('f')
                self._rw('f')
                self.f |= FN | FH
            elif y == 6:
                self._rr('f')
                self.setf(self.f & FZ, 0, 0, 1)
            else:
                self._rr('f')
                self.setf(self.f & FZ, 0, 0, not (self.f & FC))
            if y < 6:
                self._rw('a')
            return
        if x == 1:
            if op == 0x76:
                raise RuntimeError('halt at ${:04x}'.format(pc0))
            self.set8(y, self.get8(z))
            return
        if x == 2:
            if z == 7 and y in (2, 5):   # sub a / xor a: result does not depend on a
                self._rw('a')
            self.alu(y, self.get8(z))
            return
        # x == 3
        if z == 0:
            if y < 4:
                if self.cond(y):
                    self.pc = self.pop()
                return
            if y == 4:
                self._rr('a')
                self.wr(0xFF00 | self.fetch(), self.a)
                return
            if y == 6:
                self._rw('a')
                self.a = self.rd(0xFF00 | self.fetch())
                return
            d = s8(self.fetch())
            sp = self.sp
            r = (sp + d) & 0xFFFF
            self.setf(0, 0, (sp & 0xF) + (d & 0xF) > 0xF, (sp & 0xFF) + (d & 0xFF) > 0xFF)
            if y == 5:
                self.sp = r
            else:
                self.setp('hl', r)
            return
        if z == 1:
            if q == 0:
                name, sp = RP2[p], self.sp
                self.setp(name, self.pop())
                if self.push_marks and self.push_marks[-1][:2] == (name, sp):
                    for r in self.push_marks.pop()[2]:
                        self.reg_written.discard(r)
                return
            if p == 0:
                self.pc = self.pop()
            elif p == 1:
                self.pc = self.pop()
                self.ime = True
            elif p == 2:
                self.pc = self.getp('hl')
            else:
                self.sp = self.getp('hl')
            return
        if z == 2:
            if y < 4:
                a = self.fetch16()
                if self.cond(y):
                    self.pc = a
                return
            if y == 4:
                self._rr('a')
                self._rr('c')
                self.wr(0xFF00 | self.c, self.a)
            elif y == 5:
                self._rr('a')
                self.wr(self.fetch16(), self.a)
            elif y == 6:
                self._rr('c')
                self._rw('a')
                self.a = self.rd(0xFF00 | self.c)
            else:
                self._rw('a')
                self.a = self.rd(self.fetch16())
            return
        if z == 3:
            if y == 0:
                self.pc = self.fetch16()
            elif y == 6:
                self.ime = False
            elif y == 7:
                self.ime = True
            return
        if z == 4:
            a = self.fetch16()
            if self.cond(y):
                self.push(self.pc)
                self.pc = a
            return
        if z == 5:
            if q == 0:
                # saving a register on the stack is not treated as reading it
                name = RP2[p]
                v = (self.a << 8 | (self.f & 0xF0)) if name == 'af' else                     (getattr(self, name[0]) << 8 | getattr(self, name[1]))
                self.push(v)
                # remember which halves were still untouched, so the matching pop
                # (restoring the saved value) does not count as a write either
                parts = ('a', 'f') if name == 'af' else (name[0], name[1])
                self.push_marks.append((name, self.sp, [r for r in parts if r not in self.reg_written]))
            else:
                a = self.fetch16()
                self.push(self.pc)
                self.pc = a
            return
        if z == 6:
            self.alu(y, self.fetch())
            return
        self.push(self.pc)
        self.pc = y * 8

    def call(self, addr, max_steps=200000):
        """Call a subroutine and run until it returns to us."""
        sentinel = 0xFEA0  # unused memory area, never executed
        self.push(sentinel)
        self.pc = addr
        steps = 0
        while self.pc != sentinel:
            self.step()
            steps += 1
            if steps > max_steps:
                raise StepLimit('no return after {} steps (pc=${:04x})'.format(steps, self.pc))
        return steps
