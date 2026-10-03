"""Game Boy sound -> audio files and timelines.

The game's own sound engine runs in the SM83 interpreter: its init routine once,
then a request (song / sound effect), then its update routine once per frame -
just like the main loop. Every write to the sound registers goes into a small APU
model (2 square channels with sweep and envelope, the wave channel, the noise
LFSR). Per item this writes, into out/site/audio/:

  <item>.mp3            the mix
  <item>_ch1..4.mp3     each channel alone (for mute / solo in the Music view)
  <item>.js             the timeline: notes per channel with their frequency and
                        volume frame by frame, duty, envelope, sweep, waveform,
                        noise settings, panning, which pattern byte played each
                        note and who owned the channel (music or an effect)

Songs are rendered once through, up to the point where they loop (the player
loops from there). How to drive the engine is game specific and lives in
src/sound.json (entry points, request bytes, the music channel structs).

    python tools/audio.py                 everything
    python tools/audio.py music 5         one item (music / sfx / wave / noise)
"""
import json
import os
import subprocess
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import asmparse  # noqa: E402
import build  # noqa: E402
from analyze import load_io_names  # noqa: E402
from sm83 import CPU  # noqa: E402

RATE = 44100
FRAME_HZ = 4194304 / 70224          # 59.7275 frames per second
DUTY = [0.125, 0.25, 0.5, 0.75]
TRIGGER_REGS = {0xFF14: 0, 0xFF19: 1, 0xFF1E: 2, 0xFF23: 3}
WAVE_LEVEL = {4: 0, 0: 15, 1: 8, 2: 4}      # wave output shift -> volume 0-15 for the timeline


# ---------------------------------------------------------------- game config
def load_config():
    """src/sound.json with RAM / IO names resolved to addresses."""
    cfg = json.load(open(os.path.join(build.SRC, 'sound.json'), encoding='utf-8'))
    p = asmparse.Parsed()
    asmparse.parse_ram_inc(os.path.join(build.SRC, 'ram.inc'), p)
    io, _ = load_io_names(build.SRC)
    names = {v['name']: v['addr'] for v in p.vars.values() if isinstance(v, dict) and 'addr' in v}
    names.update({n: a for a, n in io.items()})

    def addr(x):
        return int(x, 0) if isinstance(x, str) and x[:2].lower() == '0x' else names[x]
    cfg['power_on'] = [(addr(a), v) for a, v in cfg['power_on']]
    cfg['stack'] = addr(cfg.get('stack', '0xCFFF'))
    for k in cfg['kinds'].values():
        k['request'], k['playing'] = addr(k['request']), addr(k['playing'])
    mc = cfg.get('music_channels')
    if mc:
        mc['structs'] = [addr(s) for s in mc['structs']]
    return cfg


# ---------------------------------------------------------------- engine
class Engine:
    """The ROM's sound engine running in the emulator."""

    def __init__(self, rom, syms, cfg):
        self.mem = bytearray(0x10000)
        self.mem[0:0x8000] = rom[0:0x8000]
        self.cpu = CPU(self.mem)
        self.syms, self.cfg = syms, cfg
        self.call(cfg['init'])

    def call(self, name):
        cpu = self.cpu
        cpu.sp = self.cfg['stack']
        cpu.io_log = []
        cpu.call(self.syms[name], max_steps=500000)
        log, cpu.io_log = cpu.io_log, None
        return log

    def word(self, a):
        return self.mem[a] | self.mem[a + 1] << 8

    def frames(self, kind, number, max_frames):
        """Request a sound, then yield the register writes of each frame until it ends."""
        k = self.cfg['kinds'][kind]
        self.mem[k['request']] = number
        started = False
        for f in range(max_frames):
            writes = self.call(self.cfg['update'])
            playing = self.mem[k['playing']]
            started = started or bool(playing)
            yield writes
            if (started and not playing) or (not started and f > 3):
                break


# ---------------------------------------------------------------- APU model
class Channel:
    def __init__(self):
        self.on = False
        self.dac = False
        self.length = 0
        self.length_enable = False
        self.vol = 0
        self.env_vol = 0
        self.env_up = False
        self.env_period = 0
        self.env_timer = 0
        self.freq = 0
        self.phase = 0.0

    def env_write(self, v):
        self.env_vol, self.env_up, self.env_period = v >> 4, bool(v & 8), v & 7
        self.dac = (v & 0xF8) != 0
        if not self.dac:
            self.on = False

    def trigger(self, max_length):
        self.on = self.dac
        if self.length == 0:
            self.length = max_length
        self.vol = self.env_vol
        self.env_timer = self.env_period

    def tick_length(self):
        if self.length_enable and self.length > 0:
            self.length -= 1
            if self.length == 0:
                self.on = False

    def tick_env(self):
        if self.env_period == 0:
            return
        self.env_timer -= 1
        if self.env_timer <= 0:
            self.env_timer = self.env_period
            if self.env_up and self.vol < 15:
                self.vol += 1
            elif not self.env_up and self.vol > 0:
                self.vol -= 1

    def level(self):
        """Current volume 0-15 (0 = silent), for the timeline."""
        return self.vol if self.on and self.dac else 0


class Square(Channel):
    def __init__(self, sweep):
        super().__init__()
        self.has_sweep = sweep
        self.duty = 2
        self.sweep_period = self.sweep_shift = 0
        self.sweep_neg = False
        self.sweep_timer = 0
        self.sweep_on = False
        self.shadow = 0

    def write(self, reg, v):
        if reg == 0:
            self.sweep_period, self.sweep_neg, self.sweep_shift = (v >> 4) & 7, bool(v & 8), v & 7
        elif reg == 1:
            self.duty, self.length = v >> 6, 64 - (v & 0x3F)
        elif reg == 2:
            self.env_write(v)
        elif reg == 3:
            self.freq = (self.freq & 0x700) | v
        elif reg == 4:
            self.freq = (self.freq & 0xFF) | ((v & 7) << 8)
            self.length_enable = bool(v & 0x40)
            if v & 0x80:
                self.trigger(64)
                self.shadow = self.freq
                self.sweep_timer = self.sweep_period or 8
                self.sweep_on = bool(self.sweep_period or self.sweep_shift)
                if self.sweep_shift:
                    self.sweep_calc()

    def sweep_calc(self):
        delta = self.shadow >> self.sweep_shift
        new = self.shadow - delta if self.sweep_neg else self.shadow + delta
        if new > 2047:
            self.on = False
        return new

    def tick_sweep(self):
        if not self.has_sweep:
            return
        self.sweep_timer -= 1
        if self.sweep_timer <= 0:
            self.sweep_timer = self.sweep_period or 8
            if self.sweep_on and self.sweep_period:
                new = self.sweep_calc()
                if new <= 2047 and self.sweep_shift:
                    self.shadow = self.freq = new
                    self.sweep_calc()

    def render(self, n):
        if not self.on:
            return np.zeros(n)
        hz = 131072 / (2048 - self.freq)
        t = self.phase + np.arange(1, n + 1) * hz / RATE
        self.phase = t[-1] % 1.0
        return np.where((t % 1.0) < DUTY[self.duty], self.vol, 0).astype(float)


class Wave(Channel):
    def __init__(self, regs):
        super().__init__()
        self.regs = regs
        self.shift = 0

    def write(self, reg, v):
        if reg == 0:
            self.dac = bool(v & 0x80)
            if not self.dac:
                self.on = False
        elif reg == 1:
            self.length = 256 - v
        elif reg == 2:
            self.shift = [4, 0, 1, 2][(v >> 5) & 3]
        elif reg == 3:
            self.freq = (self.freq & 0x700) | v
        elif reg == 4:
            self.freq = (self.freq & 0xFF) | ((v & 7) << 8)
            self.length_enable = bool(v & 0x40)
            if v & 0x80:
                self.on = self.dac
                if self.length == 0:
                    self.length = 256
                self.phase = 0.0

    def level(self):
        return WAVE_LEVEL[self.shift] if self.on and self.dac else 0

    def render(self, n):
        if not self.on or self.shift == 4:
            return np.zeros(n)
        table = []
        for i in range(16):
            b = self.regs[0x30 + i]
            table += [b >> 4, b & 0xF]
        table = np.array(table, dtype=float)
        hz = 65536 / (2048 - self.freq)
        t = self.phase + np.arange(1, n + 1) * hz / RATE
        self.phase = t[-1] % 1.0
        return np.floor(table[(np.floor((t % 1.0) * 32)).astype(int) % 32] / (1 << self.shift))


def _lfsr(width7):
    reg, out = 0x7FFF, []
    period = 127 if width7 else 32767
    for _ in range(period):
        bit = (reg ^ (reg >> 1)) & 1
        reg = (reg >> 1) | (bit << 14)
        if width7:
            reg = (reg & ~0x40) | (bit << 6)
        out.append(1 - (reg & 1))
    return np.array(out, dtype=float)


LFSR15, LFSR7 = _lfsr(False), _lfsr(True)


class Noise(Channel):
    def __init__(self):
        super().__init__()
        self.shift = self.div = 0
        self.width7 = False
        self.pos = 0.0

    def write(self, reg, v):
        if reg == 1:
            self.length = 64 - (v & 0x3F)
        elif reg == 2:
            self.env_write(v)
        elif reg == 3:
            self.shift, self.width7, self.div = v >> 4, bool(v & 8), v & 7
        elif reg == 4:
            self.length_enable = bool(v & 0x40)
            if v & 0x80:
                self.trigger(64)
                self.pos = 0.0

    def render(self, n):
        if not self.on:
            return np.zeros(n)
        hz = 524288 / (self.div or 0.5) / (2 ** (self.shift + 1))
        seq = LFSR7 if self.width7 else LFSR15
        t = self.pos + np.arange(1, n + 1) * hz / RATE
        self.pos = t[-1] % len(seq)
        return seq[(np.floor(t)).astype(np.int64) % len(seq)] * self.vol


class APU:
    def __init__(self):
        self.regs = bytearray(0x40)
        self.ch = [Square(True), Square(False), Wave(self.regs), Noise()]
        self.seq_pos = 0.0
        self.seq_step = 0

    def write(self, addr, v):
        r = addr - 0xFF00
        self.regs[r] = v
        if 0x10 <= r <= 0x14:
            self.ch[0].write(r - 0x10, v)
        elif 0x16 <= r <= 0x19:
            self.ch[1].write(r - 0x15, v)
        elif 0x1A <= r <= 0x1E:
            self.ch[2].write(r - 0x1A, v)
        elif 0x20 <= r <= 0x23:
            self.ch[3].write(r - 0x1F, v)

    def sequencer_tick(self):
        s = self.seq_step
        if s % 2 == 0:
            for c in self.ch:
                c.tick_length()
        if s in (2, 6):
            self.ch[0].tick_sweep()
        if s == 7:
            for c in (self.ch[0], self.ch[1], self.ch[3]):
                c.tick_env()
        self.seq_step = (s + 1) & 7

    def render(self, n):
        """n samples: [(left, right)] per channel, panned and scaled by the master volume."""
        out = [(np.zeros(n), np.zeros(n)) for _ in self.ch]
        done = 0
        per_tick = RATE / 512
        while done < n:
            chunk = min(n - done, max(1, int(per_tick - self.seq_pos)))
            nr51, nr50 = self.regs[0x25], self.regs[0x24]
            vl, vr = (((nr50 >> 4) & 7) + 1) / 8, ((nr50 & 7) + 1) / 8
            powered = bool(self.regs[0x26] & 0x80)
            for i, c in enumerate(self.ch):
                if not (c.dac and powered):
                    c.render(chunk)                 # keep the phase moving
                    continue
                analog = c.render(chunk) / 15.0 - 0.5
                if nr51 & (1 << (i + 4)):
                    out[i][0][done:done + chunk] = analog * vl
                if nr51 & (1 << i):
                    out[i][1][done:done + chunk] = analog * vr
            self.seq_pos += chunk
            if self.seq_pos >= per_tick:
                self.seq_pos -= per_tick
                self.sequencer_tick()
            done += chunk
        return out


def highpass(x, a=0.9985, block=2048):
    """y[n] = x[n] - x[n-1] + a*y[n-1] (removes the DC of the channel outputs), in numpy blocks."""
    y = np.empty_like(x)
    pin = pout = 0.0
    k = np.arange(block)
    for s in range(0, len(x), block):
        xb = x[s:s + block]
        m = len(xb)
        d = np.diff(xb, prepend=pin)
        powers = a ** k[:m]
        # y[i] = a^(i+1) pout + sum_{j<=i} a^(i-j) d[j]
        y[s:s + m] = powers * a * pout + powers * np.cumsum(d / powers)
        pin, pout = xb[-1], y[s + m - 1]
    return y


# ---------------------------------------------------------------- timeline
class Timeline:
    """Per channel: notes (from a trigger until silence or the next trigger) with
    frame-by-frame frequency and volume, plus panning, master volume, waveforms,
    who owns each channel and which pattern byte played each music note."""

    def __init__(self, cfg, eng):
        self.cfg, self.eng = cfg, eng
        self.notes = [[] for _ in range(4)]
        self.open = [None] * 4
        self.pan, self.master = [], []
        self.waves, self.wave_index = [], {}
        mc = cfg.get('music_channels')
        self.sections = [[] for _ in range(4)] if mc else None
        self.last_list = [None] * 4
        self.last_pat = [0] * 4
        self.looped = {}
        self.list_seen = [{} for _ in range(4)]
        self.loop = None                  # (loop start frame, frame where it jumped back)

    def owner(self, i):
        """'music' or the effect that holds channel i."""
        mc, mem = self.cfg.get('music_channels'), self.eng.mem
        if mc:
            off, bit = mc['effect_flag']
            if not mem[mc['structs'][i] + off] & bit:
                return 'music'
        for kind, chans in self.cfg.get('effect_channels', {}).items():
            n = mem[self.cfg['kinds'][kind]['playing']]
            if i in chans and n:
                return '{} {}'.format(kind, n)
        return 'music'

    def source(self, i):
        mc = self.cfg.get('music_channels')
        if not mc:
            return None
        return (self.eng.word(mc['structs'][i] + mc['pattern_ptr']) - 1) & 0xFFFF

    def frame(self, f, apu, writes):
        trig = {TRIGGER_REGS[a] for a, v in writes if a in TRIGGER_REGS and v & 0x80}
        regs = apu.regs
        if not self.pan or self.pan[-1][1] != regs[0x25]:
            self.pan.append([f, regs[0x25]])
        if not self.master or self.master[-1][1] != regs[0x24]:
            self.master.append([f, regs[0x24]])
        for i, c in enumerate(apu.ch):
            lvl = c.level()
            note = self.open[i]
            if note and (i in trig or lvl == 0 and not (c.env_up and c.on)):
                self.close(i, f)
                note = None
            if note is None and lvl:
                note = self.start(i, f, c)
            if note:
                k = f - note['t']
                if note['f'][-1][1] != c.freq:
                    note['f'].append([k, c.freq])
                if note['v'][-1][1] != lvl:
                    note['v'].append([k, lvl])
        if self.sections is not None:
            self.track_sections(f)

    def start(self, i, f, c):
        own = self.owner(i)
        note = {'t': f, 'f': [[0, c.freq]], 'v': [[0, c.level()]], 'own': own}
        if own == 'music' and self.sections is not None:
            note['src'] = self.source(i)
        if i < 2:
            note['duty'] = c.duty
            note['env'] = [c.env_vol, int(c.env_up), c.env_period]
        if i == 0 and (c.sweep_period or c.sweep_shift):
            note['sweep'] = [c.sweep_period, int(c.sweep_neg), c.sweep_shift]
        if i == 2:
            w = bytes(c.regs[0x30:0x40]).hex()
            if w not in self.wave_index:
                self.wave_index[w] = len(self.waves)
                self.waves.append(w)
            note['wave'] = self.wave_index[w]
        if i == 3:
            note['env'] = [c.env_vol, int(c.env_up), c.env_period]
            note['noise'] = [c.shift, int(c.width7), c.div]
        self.open[i] = note
        return note

    def close(self, i, f):
        note = self.open[i]
        note['d'] = f - note['t']
        self.notes[i].append(note)
        self.open[i] = None

    def track_sections(self, f):
        mc, eng = self.cfg['music_channels'], self.eng
        for i, base in enumerate(mc['structs']):
            lst = eng.word(base + mc['list_ptr'])
            if eng.mem[base + 1] == 0 and eng.mem[base] == 0:
                continue
            prev = self.last_list[i]
            pat = eng.word(base + mc['pattern_ptr'])
            # a pattern list entry that loops onto itself: the pattern pointer jumps back
            restart = lst == prev and pat < self.last_pat[i]
            if lst != prev or restart:
                if prev is not None and (lst < prev or restart) and self.loop is None:
                    # the song repeats once every channel has looped (a drum part may loop early)
                    self.looped[i] = self.list_seen[i].get(lst, 0)
                    active = [k for k, b in enumerate(mc['structs']) if eng.word(b + mc['list_ptr'])]
                    if all(k in self.looped for k in active):
                        self.loop = (self.looped[i], f)
                self.list_seen[i].setdefault(lst, f)
                self.sections[i].append([f, eng.word(lst)])
                self.last_list[i] = lst
            self.last_pat[i] = pat

    def finish(self, frames):
        for i in range(4):
            if self.open[i]:
                self.close(i, frames)
            self.notes[i] = [n for n in self.notes[i] if n['d'] > 0]
        out = {'frames': frames, 'fps': FRAME_HZ, 'ch': self.notes, 'pan': self.pan,
               'master': self.master, 'waves': self.waves}
        if self.sections is not None:
            out['sections'] = self.sections
        return out


# ---------------------------------------------------------------- rendering
def render(rom, syms, cfg, kind, number):
    """(mix, [4 channel stems], timeline, seconds, loop start in seconds or None)."""
    k = cfg['kinds'][kind]
    eng = Engine(rom, syms, cfg)
    apu = APU()
    for a, v in cfg['power_on']:
        apu.write(a, v)
    tl = Timeline(cfg, eng)
    chans = [([], []) for _ in range(4)]
    acc, count = 0.0, 0
    for writes in eng.frames(kind, number, int(k['max_seconds'] * FRAME_HZ)):
        for a, v in writes:
            apu.write(a, v)
        tl.frame(count, apu, writes)
        if k.get('loops') and tl.loop:             # once through: stop where it jumps back
            break
        acc += RATE / FRAME_HZ
        n = int(acc)
        acc -= n
        for i, (l, r) in enumerate(apu.render(n)):
            chans[i][0].append(l)
            chans[i][1].append(r)
        count += 1
    if not count:
        return None
    stems = []
    for l, r in chans:
        stems.append(np.stack([highpass(np.concatenate(l)), highpass(np.concatenate(r))], axis=1))
    mix = sum(stems)
    timeline = tl.finish(count)
    loop = tl.loop[0] / FRAME_HZ if tl.loop else None
    if k.get('loops') and not tl.loop and count >= int(k['max_seconds'] * FRAME_HZ) - 1:
        fade = min(len(mix), int(RATE * 4))       # never looped within the limit: fade out
        ramp = np.linspace(1.0, 0.0, fade)[:, None]
        for s in stems + [mix]:
            s[-fade:] *= ramp
    pcm = lambda x: np.clip(x * 0.45 * 32767, -32768, 32767).astype(np.int16)  # noqa: E731
    timeline['loopStart'] = tl.loop[0] if tl.loop else None
    return pcm(mix), [pcm(s) for s in stems], timeline, count / FRAME_HZ, loop


def write_mp3(pcm, path, quality=3):
    wav = path[:-4] + '.wav'
    with wave.open(wav, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', wav, '-codec:a', 'libmp3lame',
                    '-q:a', str(quality), path], check=True)
    os.remove(wav)


def main():
    ok, msg = build.build()                      # render what the source builds to (also on a fresh clone)
    if not ok:
        sys.exit(msg)
    rom = open(build.BUILT, 'rb').read()
    syms = build.read_sym()
    cfg = load_config()
    outdir = os.path.join(build.OUT, 'site', 'audio')
    os.makedirs(outdir, exist_ok=True)
    if len(sys.argv) == 3:
        items = [(sys.argv[1], int(sys.argv[2], 0))]
    else:
        items = [(kind, n) for kind, k in cfg['kinds'].items() for n in range(k['ids'][0], k['ids'][1] + 1)]
    index_path = os.path.join(outdir, 'index.json')
    index = {(e['kind'], e['id']): e for e in json.load(open(index_path))} if len(items) == 1 and os.path.exists(index_path) else {}
    for kind, n in items:
        res = render(rom, syms, cfg, kind, n)
        if res is None:
            continue
        mix, stems, timeline, secs, loop = res
        base = '{}_{:02X}'.format(kind, n)
        write_mp3(mix, os.path.join(outdir, base + '.mp3'))
        for i, s in enumerate(stems):
            write_mp3(s, os.path.join(outdir, '{}_ch{}.mp3'.format(base, i + 1)), quality=6)
        key = '{} {}'.format(kind, n)
        with open(os.path.join(outdir, base + '.js'), 'w', encoding='utf-8') as fh:
            fh.write('(window.GB_TIMELINES = window.GB_TIMELINES || {{}})[{}] = {};\n'.format(
                json.dumps(key), json.dumps(timeline, separators=(',', ':'))))
        index[(kind, n)] = {'kind': kind, 'id': n, 'file': 'audio/' + base + '.mp3',
                            'stems': ['audio/{}_ch{}.mp3'.format(base, i + 1) for i in range(4)],
                            'timeline': 'audio/' + base + '.js', 'seconds': round(secs, 2),
                            'loop': round(loop, 3) if loop is not None else None}
        print('{:<6} ${:02X}  {:6.1f}s{}  {} notes'.format(kind, n, secs, '  loops from {:.1f}s'.format(loop) if loop is not None else '',
                                                    sum(len(c) for c in timeline['ch'])))
    order = {k: i for i, k in enumerate(cfg['kinds'])}
    json.dump(sorted(index.values(), key=lambda e: (order[e['kind']], e['id'])),
              open(index_path, 'w'), indent=1)


if __name__ == '__main__':
    main()
