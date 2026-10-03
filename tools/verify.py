"""Check annotations against the real code.

For every annotated function:
  1. sig      - checksum of the bytes the annotation was written for (stale?)
  2. names    - every name in the pseudo-code resolves (variable, label, helper...)
  3. header   - declared registers/memory vs what the code really touches
  4. difftest - run the original asm (SM83 interpreter) and the pseudo-code on
                identical random machine states and compare the results
"""
import os
import random
import re

from asmparse import unit_sig
from pseudo import Env, Memory, NotModeled, REGS16
from sm83 import CPU, StepLimit, FC, FZ

import build  # noqa: E402

# where the tests put the stack, and the WRAM rand_ram() hands out (game.json: test_memory)
STACK_TOP = build.GAME['test_memory']['stack_top']
STACK_ZONE = range(*build.GAME['test_memory']['stack_zone'])    # never compared
FREE_RAM = build.GAME['test_memory']['free_ram']
TRIALS = 64


def reg_value(cpu, reg):
    if reg == 'carry':
        return bool(cpu.f & FC)
    if reg == 'zero':
        return bool(cpu.f & FZ)
    if reg in REGS16:
        return cpu.getp(reg)
    return getattr(cpu, reg)


def norm(reg, v):
    if reg in ('carry', 'zero'):
        return bool(v)
    return int(v) & (0xFFFF if reg in REGS16 else 0xFF)


def fmt(v):
    return str(v) if isinstance(v, bool) else '${:X}'.format(v)


def split_regs(regs):
    out = set()
    for r in regs:
        if r in REGS16:
            out.update(r)
        elif r in ('carry', 'zero'):
            out.add('f')
        elif r:
            out.add(r)
    return out


def scalar_vars(env):
    return {n: v for n, v in env.vars.items() if v['size'] in (1, 2) and '[' not in v['type']
            and v['base'] in ('u8', 'u16')}


def setup_namespace(data, rng, env):
    mem = Memory(data)
    ns = {'rng': rng, 'mem': mem}
    ns.update(env.consts)
    for n, v in env.vars.items():
        ns[n] = v['addr'] if n not in scalar_vars(env) else None

    def rand_ram(n=1, lo=FREE_RAM[0], hi=FREE_RAM[1]):
        """Random address with n bytes of free WRAM behind it."""
        return rng.randrange(lo, hi - n)

    def rand_bcd(nbytes):
        v = 0
        for i in range(nbytes):
            v |= (rng.randrange(10) << 4 | rng.randrange(10)) << (8 * i)
        return v

    def fill_bcd(addr, nbytes):
        for i in range(nbytes):
            mem[addr + i] = rng.randrange(10) << 4 | rng.randrange(10)

    def rand(lo, hi):
        return rng.randrange(lo, hi + 1)

    def play(kind, number, frames):
        """Put the sound engine into a realistic state: run its init routine, request
        a sound, then run its update routine `frames` times on this memory (src/sound.json)."""
        from audio import load_config
        cfg = load_config()
        cpu = CPU(data)
        cpu.sp = cfg['stack']
        cpu.call(env.syms[cfg['init']])
        data[cfg['kinds'][kind]['request']] = number
        if 'count' in cfg['kinds'][kind]:
            data[cfg['kinds'][kind]['count']] = 1
        for _ in range(frames):
            cpu.sp = cfg['stack']
            cpu.call(env.syms[cfg['update']])

    ns.update(to_bcd=lambda n: int(str(n), 16), rand_ram=rand_ram, rand_bcd=rand_bcd, fill_bcd=fill_bcd, rand=rand, play=play)
    return ns


class Result:
    def __init__(self, unit):
        self.unit = unit
        self.status = 'none'
        self.sig = None
        self.sig_state = None
        self.errors = []
        self.warnings = []
        self.info = []
        self.trials = 0
        self.passed = 0
        self.skip = None
        self.example = None
        self.observed = {}

    def as_dict(self):
        return {'status': self.status, 'sig': self.sig, 'sig_state': self.sig_state,
                'errors': self.errors, 'warnings': self.warnings, 'info': self.info,
                'trials': self.trials, 'passed': self.passed, 'skip': self.skip,
                'example': self.example, 'observed': self.observed}


def difftest(env, unit, fn, sig, rom, res, trials=TRIALS):
    obs_in, obs_out, obs_rd, obs_wr = set(), set(), set(), set()
    params = sig.params
    for t in range(trials):
        rng = random.Random('{}:{}'.format(unit.name, t))
        data = bytearray(rng.randbytes(0x10000))
        data[0:0x8000] = rom[0:0x8000]
        ns = setup_namespace(data, rng, env)
        svars = scalar_vars(env)
        try:
            for stmt in unit.func['test']:
                exec(re.sub(r'\$([0-9A-Fa-f]+)\b', r'0x\1', stmt), ns)   # $13 -> 0x13
        except Exception as e:
            res.errors.append('test setup failed: {}: {}'.format(type(e).__name__, e))
            return
        for n, v in svars.items():      # `hGameState = 0` in a test line sets memory
            if ns.get(n) is not None:
                data[v['addr']] = ns[n] & 0xFF
                if v['size'] == 2:
                    data[v['addr'] + 1] = (ns[n] >> 8) & 0xFF
        state = {}
        for n in unit.func['reads']:
            v = env.vars.get(n)
            if v:
                state[n] = '$' + ''.join('{:02X}'.format(data[v['addr'] + i]) for i in reversed(range(min(v['size'], 4))))
        regs = {r: rng.randrange(256) for r in 'abcdehl'}
        regs['f'] = rng.randrange(16) << 4
        args = {}
        for pname, reg in params:
            if pname in ns:
                v = ns[pname]
            else:
                v = rng.randrange(0x10000 if reg in REGS16 else 0x100)
            if reg in REGS16:
                v &= 0xFFFF
                regs[reg[0]], regs[reg[1]] = v >> 8, v & 0xFF
            elif reg in ('carry', 'zero'):
                bit = FC if reg == 'carry' else FZ
                regs['f'] = (regs['f'] | bit) if v else (regs['f'] & ~bit)
                v = bool(v)
            elif reg:
                v &= 0xFF
                regs[reg] = v
            args[pname] = v

        # pseudo-code first: if it reaches something unmodelled we stop early
        env.mem.data = bytearray(data)
        env.mem.reads, env.mem.writes = set(), set()
        try:
            ret = fn(**args)
        except NotModeled as e:
            res.skip = str(e)
            return
        except Exception as e:
            res.errors.append('pseudo-code raised {}: {}'.format(type(e).__name__, e))
            return

        cpu = CPU(bytearray(data))
        for r, v in regs.items():
            setattr(cpu, r, v)
        cpu.sp = STACK_TOP
        try:
            cpu.call(unit.start)
        except (StepLimit, RuntimeError) as e:
            res.errors.append('original code did not return: {}'.format(e))
            return
        obs_in |= cpu.reg_read
        obs_out |= cpu.reg_written
        obs_rd |= {a for a in cpu.reads if a >= 0x8000 and a not in STACK_ZONE}
        obs_wr |= {a for a in cpu.writes if a not in STACK_ZONE}
        res.trials += 1

        diffs = []
        for a in range(0x8000, 0x10000):
            if a in STACK_ZONE:
                continue
            if cpu.mem[a] != env.mem.data[a]:
                diffs.append(a)
        rets = sig.returns
        if len(rets) == 1:
            ret = (ret,)
        mism = []
        for i, reg in enumerate(rets):
            want = norm(reg, reg_value(cpu, reg))
            got = norm(reg, ret[i] if ret is not None and i < len(ret) else 0)
            if want != got:
                mism.append((reg, want, got))
        if diffs or mism:
            res.example = {
                'trial': t,
                'args': {k: (v if isinstance(v, bool) else '${:X}'.format(v)) for k, v in args.items()},
                'state': state,
                'memory': [{'addr': '${:04X}'.format(a), 'asm': '${:02X}'.format(cpu.mem[a]),
                            'pseudo': '${:02X}'.format(env.mem.data[a])} for a in diffs[:12]],
                'memory_total': len(diffs),
                'returns': [{'reg': r, 'asm': fmt(w), 'pseudo': fmt(g)} for r, w, g in mism],
            }
            res.errors.append('differs from the original in trial {}: {} byte(s) of memory, {} return value(s)'
                              .format(t, len(diffs), len(mism)))
            break
        res.passed += 1
    res.observed = {'inputs': sorted(obs_in), 'written': sorted(obs_out),
                    'reads': sorted(obs_rd), 'writes': sorted(obs_wr)}


def header_check(unit, sig, analysis, res):
    f = unit.func
    declared_in = split_regs(r for _, r in sig.params)
    observed = res.observed
    if observed:
        undeclared = set(observed['inputs']) - declared_in - {'f'}
        if undeclared:
            res.warnings.append('reads registers not declared as parameters: {}'.format(', '.join(sorted(undeclared))))
        unused = declared_in - set(observed['inputs'])
        if unused:
            res.warnings.append('declared parameter register(s) never read: {}'.format(', '.join(sorted(unused))))
        ret_regs = split_regs(sig.returns)
        clob = set(observed['written']) - ret_regs - {'f'}
        if f['clobbers']:
            extra = clob - split_regs(f['clobbers'])
            if extra:
                res.warnings.append('clobbers undeclared register(s): {}'.format(', '.join(sorted(extra))))
        elif clob:
            res.info.append('observed clobbers: {}'.format(', '.join(sorted(clob))))

    refs = analysis.refs.get(unit.name, {'reads': set(), 'writes': set(), 'ptrs': set()})
    name = analysis.symbolic
    st_reads = {name(a) or '${:04X}'.format(a) for a in refs['reads']}
    st_writes = {name(a) or '${:04X}'.format(a) for a in refs['writes']}
    st_ptrs = {name(a) or '${:04X}'.format(a) for a in refs['ptrs']}
    dyn_reads = {name(a) for a in observed.get('reads', [])} - {None}
    dyn_writes = {name(a) for a in observed.get('writes', [])} - {None}
    decl_r, decl_w = set(f['reads']), set(f['writes'])
    for s in sorted(st_reads - decl_r - decl_w):
        if not s.startswith('r'):
            res.warnings.append('reads {} but the header does not declare it'.format(s))
    for s in sorted(st_writes - decl_w):
        if not s.startswith('r'):
            res.warnings.append('writes {} but the header does not declare it'.format(s))
    sink = res.warnings if observed else res.info
    for s in sorted(decl_r - st_reads - st_ptrs - dyn_reads - st_writes):
        sink.append('declares reads: {} but {}'.format(s, 'never reads it' if observed else
                                                     'no direct access found (via a pointer or a callee?)'))
    for s in sorted(decl_w - st_writes - st_ptrs - dyn_writes):
        sink.append('declares writes: {} but {}'.format(s, 'never writes it' if observed else
                                                      'no direct access found (via a pointer or a callee?)'))


# ---- parallel differential tests: each worker process loads the project once
_W = {}


def _worker_init():
    import gbbolt
    p = gbbolt.Project(rebuild=False)
    env = Env(p.parsed, p.syms, bytearray(0x10000), p.analysis.tables)
    env.compile_all()
    _W.update(env=env, rom=p.rom, units={u.name: u for u in p.parsed.units})


def _worker_test(name):
    env, u = _W['env'], _W['units'][name]
    fn, sig, _ = env.compiled[name]
    res = Result(u)
    difftest(env, u, fn, sig, _W['rom'], res)
    return name, {k: getattr(res, k) for k in ('errors', 'trials', 'passed', 'skip', 'example', 'observed')}


def run_difftests(names, jobs):
    """{name: result fields} for the given units, spread over `jobs` processes."""
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init) as ex:
        return dict(ex.map(_worker_test, names, chunksize=1))


def verify_all(parsed, analysis, syms, rom, only=None, jobs=None):
    env = Env(parsed, syms, bytearray(0x10000), analysis.tables)
    env.compile_all()
    results = {}
    # which units get a differential test
    todo = [u.name for u in parsed.units
            if u.annotated and u.func.get('def') and u.name not in env.errors
            and not env.compiled[u.name][2] and not any(t.startswith('skip') for t in u.func['test'])
            and (only is None or u.name in only)]
    jobs = jobs if jobs is not None else min(os.cpu_count() or 1, 16)
    pre = run_difftests(todo, jobs) if jobs > 1 and len(todo) > 8 else {}
    for u in parsed.units:
        res = Result(u)
        results[u.name] = res
        if not u.annotated:
            continue
        if not u.func.get('def'):
            res.status = 'failing'
            res.errors.append('header has no `;@ def` line')
            continue
        res.sig = unit_sig(u)
        res.sig_state = 'unsigned' if not u.func['sig'] else ('ok' if u.func['sig'] == res.sig else 'stale')
        if u.name in env.errors:
            res.errors.append(env.errors[u.name])
            res.status = 'failing'
            continue
        fn, sig, unknown = env.compiled[u.name]
        for n in unknown:
            res.errors.append('unknown name in pseudo-code: {}'.format(n))
        skip = [t for t in u.func['test'] if t.startswith('skip')]
        if skip:
            res.skip = skip[0][4:].strip() or 'skipped'
        elif u.name in pre:
            for k, v in pre[u.name].items():
                setattr(res, k, list(res.errors) + v if k == 'errors' else v)
        elif not unknown and (only is None or u.name in only):
            difftest(env, u, fn, sig, rom, res)
        header_check(u, sig, analysis, res)
        if res.errors:
            res.status = 'failing'
        elif res.sig_state == 'stale':
            res.status = 'stale'
        elif res.skip is None and res.trials and res.passed == res.trials:
            res.status = 'verified'
        else:
            res.status = 'checked'
    return results
