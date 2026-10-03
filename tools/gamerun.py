"""Run a game's own code frame by frame in the SM83 interpreter and look at the screen.

For asset plugins (see assets.py): demo replays, cutscenes, anything the game draws.
There is no PPU and no interrupts: a plugin calls the game's routines itself (a state
handler, the VBlank handler) once per frame, and `screen()` renders the background,
window and sprites from VRAM / OAM the way the LCD would at the end of that frame.

    run = GameRunner(project)
    run.set('hGameState', 0)
    for f in range(600):
        run.call('RunGameState')
        run.call('VBlankHandler')
        frames.append(run.screen(oam='wShadowOAM'))
    write_video(frames, path)
"""
import os
import subprocess

from sm83 import CPU

DMG_RGB = [(0xE0, 0xF8, 0xD0), (0x88, 0xC0, 0x70), (0x34, 0x68, 0x56), (0x08, 0x18, 0x20)]


class GameRunner:
    def __init__(self, project, stack=0xCFFF):
        self.project = project
        self.syms = project.syms
        self.vars = {n: v['addr'] for n, v in project.parsed.vars.items()
                     if isinstance(v, dict) and 'addr' in v}
        self.mem = bytearray(0x10000)
        self.mem[0:0x8000] = bytes(project.rom[0:0x8000])
        self.cpu = CPU(self.mem)
        self.stack = stack
        self.frame = 0
        # the LCD as busy-wait loops want to see it: in VBlank at line $91, in HBlank (mode 0)
        self.mem[0xFF44] = 0x91
        self.mem[0xFF41] = 0x00
        self.sound = None                          # list of per-frame sound register writes, when recording

    def record_sound(self):
        """From now on collect the sound register writes, frame by frame (see end_frame)."""
        self.sound, self._pending = [], []

    def end_frame(self):
        """Close the current frame (for the sound recording)."""
        if self.sound is not None:
            self.sound.append(self._pending)
            self._pending = []

    # ---- memory by name
    def addr(self, name):
        if isinstance(name, int):
            return name
        if name in self.vars:
            return self.vars[name]
        if name in self.syms:
            return self.syms[name]
        return int(name, 0)

    def get(self, name):
        return self.mem[self.addr(name)]

    def set(self, name, value):
        self.mem[self.addr(name)] = value & 0xFF

    def copy_in(self, dest, data):
        a = self.addr(dest)
        self.mem[a:a + len(data)] = bytes(data)

    # ---- running code
    def call(self, name, max_steps=2000000, **regs):
        cpu = self.cpu
        for r, v in regs.items():
            if len(r) == 2:
                cpu.setp(r, v)
            else:
                setattr(cpu, r, v & 0xFF)
        cpu.sp = self.stack
        cpu.reads, cpu.writes = set(), set()       # the interpreter's own bookkeeping, not needed here
        cpu.rom_writes = []
        self.mem[0xFF44] = 0x91
        if self.sound is not None:
            cpu.io_log = []
        steps = cpu.call(self.addr(name), max_steps=max_steps)
        if self.sound is not None:
            self._pending += cpu.io_log
            cpu.io_log = None
        if 0xFF46 in cpu.writes:                   # OAM DMA: 160 bytes from page [$FF46] to OAM
            src = self.mem[0xFF46] << 8
            self.mem[0xFE00:0xFEA0] = self.mem[src:src + 0xA0]
        return steps

    # ---- the screen
    def screen(self, oam=0xFE00):
        """160x144 shade indices (0-3, after the palettes) as a list of bytearrays."""
        m = self.mem
        lcdc = m[0xFF40]
        rows = [bytearray(160) for _ in range(144)]
        if not lcdc & 0x80:
            return rows
        bgp, obp = m[0xFF47], (m[0xFF48], m[0xFF49])
        pal = lambda p, c: (p >> (2 * c)) & 3   # noqa: E731
        signed = not lcdc & 0x10

        def tile_row(tile, y):
            if signed and tile < 128:
                base = 0x9000 + tile * 16
            elif signed:
                base = 0x8800 + (tile - 128) * 16
            else:
                base = 0x8000 + tile * 16
            return m[base + 2 * y], m[base + 2 * y + 1]
        colour = [bytearray(160) for _ in range(144)]   # raw BG colour (0 = behind sprites)
        scx, scy, wx, wy = m[0xFF43], m[0xFF42], m[0xFF4B] - 7, m[0xFF4A]
        bgmap = 0x9C00 if lcdc & 0x08 else 0x9800
        winmap = 0x9C00 if lcdc & 0x40 else 0x9800
        win = lcdc & 0x20 and wy < 144 and wx < 160
        for y in range(144):
            row, raw = rows[y], colour[y]
            if lcdc & 0x01:
                for x in range(160):
                    if win and y >= wy and x >= wx:
                        mx, my, base = x - wx, y - wy, winmap
                    else:
                        mx, my, base = (x + scx) & 0xFF, (y + scy) & 0xFF, bgmap
                    lo, hi = tile_row(m[base + (my >> 3) * 32 + (mx >> 3)], my & 7)
                    b = 7 - (mx & 7)
                    c = ((hi >> b) & 1) << 1 | ((lo >> b) & 1)
                    raw[x] = c
                    row[x] = pal(bgp, c)
        if lcdc & 0x02:
            tall = lcdc & 0x04
            oam = self.addr(oam)
            for i in range(39, -1, -1):              # lower index drawn last = on top
                sy, sx, tile, attr = m[oam + 4 * i: oam + 4 * i + 4]
                sy -= 16
                sx -= 8
                h = 16 if tall else 8
                if tall:
                    tile &= 0xFE
                for ty in range(h):
                    y = sy + ty
                    if not 0 <= y < 144:
                        continue
                    r = (h - 1 - ty) if attr & 0x40 else ty
                    base = 0x8000 + tile * 16 + 2 * r
                    lo, hi = m[base], m[base + 1]
                    for tx in range(8):
                        x = sx + tx
                        if not 0 <= x < 160:
                            continue
                        b = tx if attr & 0x20 else 7 - tx
                        c = ((hi >> b) & 1) << 1 | ((lo >> b) & 1)
                        if not c or (attr & 0x80 and colour[y][x]):
                            continue
                        rows[y][x] = pal(obp[(attr >> 4) & 1], c)
        return rows


def to_rgb(frame, scale=1):
    out = bytearray()
    for row in frame:
        line = b''.join(bytes(DMG_RGB[c]) * scale for c in row)
        out += line * scale
    return bytes(out)


def write_video(frames, path, fps=60, scale=3, audio=None, audio_start=0.0):
    """Encode shade-index frames as an H.264 MP4 (optionally with an audio track)."""
    if not frames:
        raise ValueError('no frames')
    h, w = len(frames[0]), len(frames[0][0])
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-s', '{}x{}'.format(w, h), '-r', str(fps), '-i', '-']
    if audio:
        cmd += ['-ss', str(audio_start), '-i', audio]
    cmd += ['-vf', 'scale=iw*{0}:ih*{0}:flags=neighbor'.format(scale), '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'veryfast', '-movflags', '+faststart']
    if audio:
        cmd += ['-c:a', 'aac', '-b:a', '128k', '-shortest']
    cmd.append(path)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fr in frames:
        proc.stdin.write(to_rgb(fr))
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError('ffmpeg failed for ' + os.path.basename(path))


def write_png_frame(frame, path, scale=2):
    from site_gen import write_png
    rows = []
    for row in frame:
        line = [c for v in row for c in DMG_RGB[v] * scale]
        rows += [line] * scale
    write_png(path, len(frame[0]) * scale, len(frame) * scale, rows)
