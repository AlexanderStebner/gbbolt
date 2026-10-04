"""Compile and run gbbolt pseudo-code.

Pseudo-code is a small Python dialect:

* RAM variables from ram.inc are plain names: `hGameState = 3` writes memory,
  `if hJoyHeld & 1:` reads it. u16 variables read/write two bytes.
* Arrays (u8[N]) index like lists: `wScore[0]`.
* Constants (`TILE_BLANK`) and labels (`GameStateTable`, `vBGMap0`) are numbers.
* `mem[addr]` / `mem16[addr]` access any address, `mem[a:b]` gives a list.
* Other annotated functions are called like Python functions.
* Helpers (listed in HELPERS) cover BCD maths, copying, and hardware waits.

Parameters and return values are tied to registers in the header:
`def CopyBytes(src: hl, dest: de, count: bc)`, `-> carry`, `-> (a, hl)`.
"""
import ast
import os

REGS8 = ('a', 'b', 'c', 'd', 'e', 'h', 'l')
REGS16 = ('bc', 'de', 'hl', 'af')
FLAGS = ('carry', 'zero')
REGISTERS = REGS8 + REGS16 + FLAGS


class NotModeled(Exception):
    """Pseudo-code reached something that has no executable model."""


class Hardware(NotModeled):
    pass


class Memory:
    def __init__(self, data, mbc=None):
        self.data = data  # bytearray(0x10000)
        self.mbc = mbc    # sm83.MBC over the same bytes, for banked games
        self.reads = set()
        self.writes = set()

    def __getitem__(self, a):
        if isinstance(a, slice):
            return [self[i] for i in range(a.start, a.stop)]
        a = int(a) & 0xFFFF          # int(): array names stand for their address
        self.reads.add(a)
        return self.data[a]

    def __setitem__(self, a, v):
        if isinstance(a, slice):
            for i, x in zip(range(a.start, a.stop), v):
                self[i] = x
            return
        a = int(a) & 0xFFFF
        if a < 0x8000:
            if self.mbc is not None and self.mbc.kind:
                self.writes.add(a)
                self.mbc.write(a, v & 0xFF)
                return
            raise Hardware('write to ROM/MBC register ${:04X}'.format(a))
        self.writes.add(a)
        self.data[a] = v & 0xFF


class Mem16:
    def __init__(self, mem):
        self.mem = mem

    def __getitem__(self, a):
        return self.mem[a] | (self.mem[a + 1] << 8)

    def __setitem__(self, a, v):
        self.mem[a] = v & 0xFF
        self.mem[a + 1] = (v >> 8) & 0xFF


class View:
    """An array variable: indexes relative to its base address."""

    def __init__(self, mem, addr, size):
        self.mem, self.addr, self.size = mem, addr, size

    def __getitem__(self, i):
        if isinstance(i, slice):
            return self.mem[self.addr + (i.start or 0):self.addr + (self.size if i.stop is None else i.stop)]
        return self.mem[self.addr + i]

    def __setitem__(self, i, v):
        if isinstance(i, slice):
            for k, x in zip(range(i.start or 0, self.size if i.stop is None else i.stop), v):
                self.mem[self.addr + k] = x
            return
        self.mem[self.addr + i] = v

    def __len__(self):
        return self.size

    def __iter__(self):
        return iter(self[0:self.size])

    # in arithmetic an array name stands for its address: wBGMap0Copy + 0x22
    def __index__(self):
        return self.addr

    __int__ = __index__

    def __add__(self, other):
        return self.addr + other

    __radd__ = __add__

    def __sub__(self, other):
        return self.addr - other

    def __rsub__(self, other):
        return other - self.addr

    # bitwise operators also see the address (`(dest & 0xFF00) | lo(dest + 16)`)
    def __and__(self, other):
        return self.addr & other

    def __or__(self, other):
        return self.addr | other

    def __xor__(self, other):
        return self.addr ^ other

    def __rshift__(self, other):
        return self.addr >> other

    def __lshift__(self, other):
        return self.addr << other

    __rand__, __ror__, __rxor__ = __and__, __or__, __xor__

    def __eq__(self, other):
        return self.addr == other if isinstance(other, int) else self is other

    def __hash__(self):
        return hash(self.addr)


class Stub(int):
    """A label without pseudo-code: calling it is not modelled, but in arithmetic
    (`mem[Label + i]`) it is simply its address."""

    def __new__(cls, name, address, bank=None):
        obj = int.__new__(cls, address)
        obj.label = name
        obj.bank = bank
        return obj

    def __call__(self, *a, **k):
        raise NotModeled('calls {} which has no pseudo-code yet'.format(self.label))


class Function(int):
    """An annotated function as other pseudo-code sees it: calling it runs its pseudo-code;
    in arithmetic it is its address, and BANK() gives its bank."""

    def __new__(cls, name, address, bank, pyfunc):
        obj = int.__new__(cls, address)
        obj.label, obj.bank, obj.pyfunc = name, bank, pyfunc
        return obj

    def __call__(self, *a, **k):
        return self.pyfunc(*a, **k)


class JumpTableView(int):
    """A table of function addresses: `Table[i]` is entry i as a callable; in arithmetic it is the table's
    address (`mem16[Table + 2 * i]`, or passed on as an address)."""

    def __new__(cls, address, entries, resolve):
        obj = int.__new__(cls, address)
        obj.entries, obj.resolve = entries, resolve
        return obj

    def __getitem__(self, i):
        return self.resolve(self.entries[i])

    def __len__(self):
        return len(self.entries)


def _hw(name, what):
    def f(*args, **kw):
        raise Hardware('{} ({})'.format(name, what))
    f.__name__ = name
    return f


def _bcd_to_int(x):
    n, mul = 0, 1
    while x:
        n += (x & 0xF) * mul
        x >>= 4
        mul *= 10
    return n


def make_helpers(mem):
    m16 = Mem16(mem)

    def set_rom_bank(bank):
        """Switch ROM bank `bank` in at $4000-$7FFF (a write to the MBC's bank register)"""
        if mem.mbc is None or not mem.mbc.kind:
            raise Hardware('set_rom_bank (MBC write)')
        mem[0x2000] = bank

    def rom_bank():
        """The ROM bank switched in at $4000-$7FFF right now"""
        if mem.mbc is None or not mem.mbc.kind:
            raise Hardware('rom_bank (MBC state)')
        return mem.mbc.rom_bank

    def bank_of_label(label):
        """The ROM bank a label is in, like the assembler's BANK(): `set_rom_bank(BANK(Foo))`"""
        bank = getattr(label, 'bank', None)
        if bank is None:
            raise NotModeled('BANK() of something that is not a label')
        return bank

    def bcd_read(addr, nbytes):
        """Read a little-endian packed BCD number of nbytes bytes."""
        n = 0
        for i in reversed(range(nbytes)):
            b = mem[addr + i]
            n = n * 100 + (b >> 4) * 10 + (b & 0xF)
        return n

    def bcd_write(addr, nbytes, value):
        """Write value as little-endian packed BCD (2 digits per byte)."""
        for i in range(nbytes):
            d = value % 100
            value //= 100
            mem[addr + i] = ((d // 10) << 4) | (d % 10)

    def copy(dest, src, count):
        """Copy count bytes from src to dest, front to back."""
        for i in range(count):
            mem[dest + i] = mem[src + i]

    def fill(dest, value, count):
        """Set count bytes starting at dest to value."""
        for i in range(count):
            mem[dest + i] = value

    helpers = {
        'mem': (mem, 'Any byte of the 64 KiB address space: mem[addr], mem[a:b]'),
        'mem16': (m16, 'Little-endian 16-bit word at an address'),
        'lo': (lambda x: x & 0xFF, 'Low byte of a 16-bit value'),
        'u8': (lambda x: x & 0xFF, 'Wrap to 8 bits, like the CPU does (u8(3 - 5) == 254)'),
        'u16': (lambda x: x & 0xFFFF, 'Wrap to 16 bits'),
        'hi': (lambda x: (x >> 8) & 0xFF, 'High byte of a 16-bit value'),
        'swap': (lambda x: ((x & 0xF) << 4) | ((x >> 4) & 0xF), 'Swap the two nibbles of a byte'),
        'bcd_to_int': (_bcd_to_int, 'Packed BCD value (e.g. $1234) to a number (1234)'),
        'to_bcd': (lambda n: ((n // 10 % 10) << 4) | (n % 10), 'Number 0-99 to a packed BCD byte (42 -> $42)'),
        'bcd_read': (bcd_read, bcd_read.__doc__),
        'bcd_write': (bcd_write, bcd_write.__doc__),
        'copy': (copy, copy.__doc__),
        'fill': (fill, fill.__doc__),
        'wait_ly': (_hw('wait_ly', 'polls rLY'), 'Busy-wait until the LCD reaches scanline n (rLY == n)'),
        'wait_hblank': (_hw('wait_hblank', 'polls rSTAT'), 'Busy-wait until the LCD is in HBlank (rSTAT mode 0)'),
        'wait_vblank': (_hw('wait_vblank', 'polls rSTAT'), 'Busy-wait until the LCD is in VBlank (rSTAT mode 1)'),
        'wait_serial': (_hw('wait_serial', 'polls rSC'), 'Busy-wait until the link cable transfer is done (rSC bit 7 clear)'),
        'wait_div': (_hw('wait_div', 'polls rDIV'), 'Busy-wait on the divider rDIV, which counts up 16384 times a second'),
        'wait_vblank_flag': (_hw('wait_vblank_flag', 'waits for an interrupt'),
                             'Sleep until the VBlank interrupt has set hVBlankDone'),
        'read_buttons': (_hw('read_buttons', 'reads rP1'),
                         'Select a button group through rP1 and read it back (bit set = pressed)'),
        'enable_interrupts': (lambda: None, 'ei (no effect on memory; not modelled further)'),
        'disable_interrupts': (lambda: None, 'di (no effect on memory; not modelled further)'),
        'set_rom_bank': (set_rom_bank, set_rom_bank.__doc__),
        'rom_bank': (rom_bank, rom_bank.__doc__),
        'BANK': (bank_of_label, bank_of_label.__doc__),
        'pop_return_address': (_hw('pop_return_address', 'stack manipulation'),
                               'Remove the return address from the stack and return it'),
        'goto': (_hw('goto', 'tail jump'), 'Jump to an address (does not return here)'),
        'link_transfer': (_hw('link_transfer', 'link cable'),
                          'Send a byte over the link cable and wait for the byte coming back '
                          '(internal clock: we drive the transfer); returns the received byte'),
        'return_from_caller': (_hw('return_from_caller', 'pop hl / ret'),
                               "Drop the caller's return address and return to its caller (pop hl / ret)"),
        'reset_stack': (_hw('reset_stack', 'ld sp'), 'Point the stack pointer at a new address'),
        'forever': (lambda: iter(int, 1), 'Infinite loop: `for _ in forever():`'),
        'range': (range, 'Python range'),
        'len': (len, 'Python len'),
        'abs': (abs, 'Python abs'),
        'enumerate': (enumerate, 'Python enumerate'),
        'next': (next, 'Python next'),
        'sorted': (sorted, 'Python sorted'),
        'list': (list, 'Python list'),
        'sum': (sum, 'Python sum'),
        'any': (any, 'Python any'),
        'all': (all, 'Python all'),
        'zip': (zip, 'Python zip'),
        'reversed': (reversed, 'Python reversed'),
        'bool': (bool, 'Python bool'),
        'min': (min, 'Python min'),
        'max': (max, 'Python max'),
        'True': (True, ''),
        'False': (False, ''),
        'None': (None, ''),
    }
    helpers.update(game_helpers(mem))
    return helpers


def game_helpers(mem):
    """Helpers a game adds for its own idioms (game.json "helpers": a Python file in
    the project whose `helpers(mem, addr)` returns {name: (function, description)};
    addr('wTileMap') is a label's address)."""
    import build
    path = build.GAME.get('helpers')
    if not path:
        return {}
    if 'mod' not in _GAME_HELPERS:           # loaded once: verify asks for fresh helpers every trial
        import importlib.util
        spec = importlib.util.spec_from_file_location('game_helpers', os.path.join(build.ROOT, path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GAME_HELPERS['mod'] = mod
    cache = _GAME_HELPERS.setdefault('syms', {})

    def addr(name):
        if not cache:
            cache.update(build.read_sym())
        return cache[name]
    return _GAME_HELPERS['mod'].helpers(mem, addr)


_GAME_HELPERS = {}


HELPER_DOCS = {k: v[1] for k, v in make_helpers(Memory(bytearray(0x10000))).items()}


class Signature:
    def __init__(self, name, params, returns):
        self.name = name
        self.params = params      # [(pname, reg)]
        self.returns = returns    # [reg]


def parse_signature(def_line):
    """Parse ';@ def Name(x: hl, n: b) -> carry' into a Signature."""
    tree = ast.parse(def_line.rstrip(':') + ':\n    pass')
    fn = tree.body[0]
    params = []
    for arg in fn.args.args:
        reg = arg.annotation.id if isinstance(arg.annotation, ast.Name) else None
        params.append((arg.arg, reg))
    returns = []
    r = fn.returns
    if isinstance(r, ast.Name):
        returns = [r.id]
    elif isinstance(r, ast.Tuple):
        returns = [e.id for e in r.elts]
    return Signature(fn.name, params, returns)


class Rewriter(ast.NodeTransformer):
    """Turn RAM variable names into memory accesses."""

    def __init__(self, env):
        self.env = env

    def scalar(self, name):
        v = self.env.vars.get(name)
        if v and v['base'] in ('u8', 'u16') and v['size'] in (1, 2) and '[' not in v['type']:
            return v
        return None

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            v = self.scalar(node.id)
            if v:
                return ast.copy_location(ast.Call(ast.Name('__rd', ast.Load()),
                                                  [ast.Constant(v['addr']), ast.Constant(v['size'])], []), node)
        return node

    def _store(self, name, value, node):
        v = self.scalar(name)
        return ast.copy_location(ast.Expr(ast.Call(ast.Name('__wr', ast.Load()),
                                                   [ast.Constant(v['addr']), ast.Constant(v['size']), value],
                                                   [])), node)

    def visit_Assign(self, node):
        node.value = self.visit(node.value)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and self.scalar(node.targets[0].id):
            return self._store(node.targets[0].id, node.value, node)
        if len(node.targets) > 1:
            # a = b = value: evaluate once, then store into every target
            tmp = ast.Name('__chain', ast.Store())
            out = [ast.copy_location(ast.Assign([tmp], node.value), node)]
            for t in node.targets:
                val = ast.Name('__chain', ast.Load())
                if isinstance(t, ast.Name) and self.scalar(t.id):
                    out.append(self._store(t.id, val, node))
                else:
                    out.append(ast.copy_location(ast.Assign([self.visit(t)], val), node))
            return out
        for t in ast.walk(node.targets[0]):
            if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store) and self.scalar(t.id):
                raise SyntaxError('tuple assignment to RAM variable {} is not supported'.format(t.id))
        node.targets = [self.visit(t) for t in node.targets]
        return node

    def visit_AugAssign(self, node):
        node.value = self.visit(node.value)
        if isinstance(node.target, ast.Name) and self.scalar(node.target.id):
            cur = self.visit_Name(ast.Name(node.target.id, ast.Load()))
            return self._store(node.target.id, ast.BinOp(cur, node.op, node.value), node)
        node.target = self.visit(node.target)
        return node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == 'addr' and len(node.args) == 1 \
                and isinstance(node.args[0], ast.Name):
            a = self.env.address_of(node.args[0].id)
            if a is not None:
                return ast.copy_location(ast.Constant(a), node)
        self.generic_visit(node)
        return node


def function_source(unit):
    lines = [unit.func['def'].rstrip(':') + ':']
    body = [ln for g in unit.pseudo_groups for ln in g['pseudo']]
    if not any(ln.strip() and not ln.strip().startswith('#') for ln in body):
        body.append('raise NotModeled("no pseudo-code")')
    lines += ['    ' + ln for ln in body]
    return '\n'.join(lines) + '\n'


def free_names(fn):
    """Names a function reads that it does not bind itself."""
    bound = {a.arg for a in fn.args.args}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.comprehension,)):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    bound.add(n.id)
        elif isinstance(node, (ast.FunctionDef, ast.Lambda)) and node is not fn:
            if isinstance(node, ast.FunctionDef):     # a helper defined inside the pseudo-code
                bound.add(node.name)
            bound.update(a.arg for a in node.args.args + node.args.kwonlyargs)
    used = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound:
            used.append((node.id, getattr(node, 'lineno', 0)))
    return used


class Env:
    """Everything pseudo-code can see: variables, constants, labels, functions."""

    def __init__(self, parsed, syms, mem_data, tables):
        self.parsed = parsed
        self.vars = parsed.vars
        self.consts = parsed.consts
        self.syms = syms
        self.mem = Memory(mem_data)
        self.helpers = make_helpers(self.mem)
        self.units = {u.name: u for u in parsed.units}
        self.tables = tables      # unit name -> [target unit names]
        self.compiled = {}
        self.errors = {}

    def address_of(self, name):
        if name in self.vars:
            return self.vars[name]['addr']
        if name in self.syms:
            return self.syms[name]
        if name in self.consts:
            return self.consts[name]
        return None

    def known(self, name):
        return (name in self.vars or name in self.consts or name in self.syms
                or name in self.helpers or name in REGISTERS or name in ('NotModeled', 'addr'))

    def compile_all(self):
        g = {}
        g.update({k: v[0] for k, v in self.helpers.items()})
        g['NotModeled'] = NotModeled

        def rd(a, size):
            return self.mem[a] if size == 1 else self.mem[a] | (self.mem[a + 1] << 8)

        def wr(a, size, v):
            self.mem[a] = v
            if size == 2:
                self.mem[a + 1] = v >> 8

        g['__rd'], g['__wr'] = rd, wr
        for name, v in self.vars.items():
            if not Rewriter(self).scalar(name):
                g[name] = View(self.mem, v['addr'], v['size'])
        g.update(self.consts)
        for name, a in self.syms.items():
            if '.' not in name:
                # a number in arithmetic; calling it (code not modelled yet) says so instead of crashing
                g.setdefault(name, self.stub(name) if name not in self.vars else a)

        def resolve(target):
            return g.get(target) if callable(g.get(target)) else self.stub(target)

        for uname, entries in self.tables.items():
            g[uname] = JumpTableView(self.syms.get(uname, 0), entries, resolve)
        for u in self.parsed.units:
            if u.kind == 'code' and not (u.annotated and u.func.get('def')):
                g[u.name] = self.stub(u.name)
        self.globals = g
        for u in self.parsed.units:
            if u.annotated and u.func.get('def'):
                self.compile(u)

    def stub(self, name):
        import build
        return Stub(name, self.syms.get(name, 0), build.SYM_BANK.get(name))

    def compile(self, unit):
        src = function_source(unit)
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            self.errors[unit.name] = 'syntax error in pseudo-code: {} (line {})'.format(e.msg, e.lineno)
            return None
        fn = tree.body[0]
        sig = parse_signature(unit.func['def'])
        for arg in fn.args.args:
            arg.annotation = None
        fn.returns = None
        unknown = sorted({n for n, _ in free_names(fn) if not self.known(n)})
        try:
            tree = Rewriter(self).visit(tree)
        except SyntaxError as e:
            self.errors[unit.name] = str(e)
            return None
        ast.fix_missing_locations(tree)
        code = compile(tree, '<pseudo {}>'.format(unit.name), 'exec')
        exec(code, self.globals)
        pyfn = self.globals[fn.name]
        self.compiled[unit.name] = (pyfn, sig, unknown)
        # other pseudo-code sees the function as its label too: callable, and a number in
        # arithmetic or BANK() (`hl = GetTileAndCoordsInFrontOfPlayer`, `BANK(PlaySound)`)
        import build
        if fn.name in self.syms:
            self.globals[fn.name] = Function(fn.name, self.syms[fn.name], build.SYM_BANK.get(fn.name), pyfn)
        return self.compiled[unit.name]
