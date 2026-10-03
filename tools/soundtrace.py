"""Dynamic trace of the sound engine: which addresses execute as code.

Runs the sound engine (src/sound.json) on every song / sound effect, plus pause
and resume if the config names a pause variable, for a while in the emulator and
records each executed instruction address.

    python tools/soundtrace.py         prints db lines in the engine that executed
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import asmparse  # noqa: E402
import build  # noqa: E402
from audio import Engine, load_config  # noqa: E402


def trace(rom, syms):
    cfg = load_config()
    executed = set()
    pause = cfg.get('pause')                 # {"var": name, "on": value, "off": value}
    pause_addr = None
    if pause:
        import asmparse as ap
        p = ap.Parsed()
        ap.parse_ram_inc(os.path.join(build.SRC, 'ram.inc'), p)
        pause_addr = p.vars[pause['var']]['addr']
    items = [(kind, n, 1500 if k.get('loops') else 300) for kind, k in cfg['kinds'].items()
             for n in range(k['ids'][0], k['ids'][1] + 1)]
    for kind, n, frames in items:
        eng = Engine(rom, syms, cfg)
        cpu = eng.cpu
        orig = cpu.step

        def step():
            executed.add(cpu.pc)
            orig()
        cpu.step = step
        eng.mem[cfg['kinds'][kind]['request']] = n
        try:
            for f in range(frames):
                if pause_addr is not None and f == 200:
                    eng.mem[pause_addr] = pause['on']
                if pause_addr is not None and f == 260:
                    eng.mem[pause_addr] = pause['off']
                eng.call(cfg['update'])
        except Exception:  # noqa: BLE001 - invalid request numbers run off into the weeds
            pass
    return executed


def main():
    ok, msg = build.build()
    rom = open(build.BUILT, 'rb').read()
    syms = build.read_sym()
    executed = trace(rom, syms)
    engine = sorted(a for a in executed if a >= 0x6400)
    json.dump(engine, open(os.path.join(build.OUT, 'soundtrace.json'), 'w'))
    parsed = asmparse.parse(build.SRC, rom, syms)
    hits = []
    for ln in parsed.lines:
        if ln.kind == 'data' and ln.addr is not None and ln.addr >= 0x6400:
            inside = [a for a in engine if ln.addr <= a < ln.addr + ln.size]
            if inside:
                hits.append((ln.addr, ln.size, inside))
    print('{} executed addresses in the engine; {} db lines contain executed code'.format(len(engine), len(hits)))
    for addr, size, inside in hits:
        print('${:04X} +{:<3} executed at {}'.format(addr, size, ' '.join('{:04X}'.format(a) for a in inside[:8])))


if __name__ == '__main__':
    main()
