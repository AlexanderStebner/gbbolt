"""Game-defined assets: plugins in <game>/assets/*.py.

The engine's built-in asset types (tiles, tilemap, sprites, ...) come from `;@ asset:`
lines in the disassembly. Anything else a game wants to show - a world map, a table of
monsters, a replay of its demo, a chart of its speed curve - is a plugin: a Python file
in the game's assets/ folder with

    TITLE = 'Demo replay'                 # optional
    def build(ctx):
        return [ {asset}, ... ]

Each asset is a dict made of the viewer's display parts (the `type`):

    image      {'width', 'height', 'pixels': base64 shade indices (0-3, 255 = transparent)}
               'packed': 'zlib' with ctx.packed_pixels(rows) for big pictures; 'scale' (large view),
               'scroll': True (scrolls sideways), 'marks': [{'x', 'y', 'w', 'h', 'label', 'text'}]
               (pixels of the picture: boxes to hover / click, text may name labels)
    video      {'file': url from ctx.video(...), 'width', 'height', 'fps',
                'lanes': [{'name', 'spans': [[from, to], ...]}]}   frame spans, drawn under the video
    chart      {'kind': 'line' | 'step' | 'bar', 'x': label, 'y': label, 'xticks': [...] optional,
                'series': [{'name', 'points': [[x, y], ...]}], 'marks': [{'x', 'label'}]}
    table      {'columns': [...], 'rows': [[cell, ...], ...]}  cells: text, number, or
                {'text', 'code': label} (links into the disassembly) / {'image': asset-like dict}
    tracks     {'tracks': [{'title', 'note', 'file', 'seconds'}]}  audio from ctx.audio(...)

plus the common fields: 'name' (unique), 'group' (the Assets menu entry, e.g. 'replays'),
'title', 'doc' (list of paragraphs, may name labels), 'users' (labels of the code involved).

`ctx` gives the plugin the ROM, its symbols and RAM names, a GameRunner (gamerun.py) to
run the game's own code, the tile loaders, video and audio writers. Results are cached
under out/site/gen/: a plugin runs again only when it, the ROM or the engine changes.
"""
import base64
import glob
import hashlib
import importlib.util
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
import build  # noqa: E402

# Bump when engine changes alter what plugins produce (the runner, the audio renderer):
# plugins are rebuilt only when this, the plugin, its helpers (_*.py) or the ROM change.
ASSET_ENGINE_VERSION = 2
FRAME_HZ = 4194304 / 70224        # the Game Boy's frame rate (59.73)


class AssetContext:
    def __init__(self, project, gen_dir, plugin):
        self.project = project
        self.rom = bytes(project.rom)
        self.syms = project.syms
        self.vars = {n: v['addr'] for n, v in project.parsed.vars.items() if isinstance(v, dict) and 'addr' in v}
        self.gen_dir = gen_dir
        self.plugin = plugin
        self.files = []

    # ---- names
    def addr(self, name):
        return name if isinstance(name, int) else self.vars.get(name, self.syms.get(name))

    def word(self, a):
        return self.rom[a] | self.rom[a + 1] << 8

    # ---- the game's code
    def runner(self):
        from gamerun import GameRunner
        return GameRunner(self.project)

    def tileset(self, spec):
        from site_gen import run_loader
        return run_loader(self.project, spec)

    # ---- files the viewer loads (out/site/gen/...)
    def file(self, name):
        """(path to write, url for the viewer)."""
        fn = '{}_{}'.format(self.plugin, name)
        self.files.append(fn)
        return os.path.join(self.gen_dir, fn), 'gen/' + fn

    def video(self, frames, name, fps=FRAME_HZ, scale=3, audio=None, audio_start=0.0, sound=None):
        """An MP4 of the frames. audio: a sound file to put under it; sound: a GameRunner
        that recorded the game's own sound register writes (record_sound) - rendered here."""
        from gamerun import write_video
        path, url = self.file(name + '.mp4')
        tmp = None
        if sound is not None and sound.sound:
            import audio as A
            cfg = A.load_config()
            tmp = path[:-4] + '.wav'
            A.write_wav(A.render_writes(sound.sound, fps, cfg['power_on']), tmp)
            audio, audio_start = tmp, 0.0
        try:
            write_video(frames, path, fps=fps, scale=scale, audio=audio, audio_start=audio_start)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return url

    def poster(self, frame, name):
        from gamerun import write_png_frame
        path, url = self.file(name + '.png')
        write_png_frame(frame, path)
        return url

    def audio(self, kind, number, name, pokes=None, hz=None, max_seconds=None, loops=None, wav=False):
        """Render a sound with the game's engine (audio.py). Returns {'file', 'seconds', 'path'}."""
        import audio as A
        cfg = A.load_config()
        pokes = {self.addr(k): v for k, v in (pokes or {}).items()}
        res = A.render(self.rom, self.syms, cfg, kind, number, pokes=pokes, hz=hz,
                       max_seconds=max_seconds, loops=loops)
        mix, _, _, secs, _ = res
        path, url = self.file(name + '.mp3')
        A.write_mp3(mix, path)
        return {'file': url, 'seconds': round(secs, 2), 'path': path}

    @staticmethod
    def pixels(rows):
        """base64 of shade-index rows (for an 'image' part)."""
        return base64.b64encode(b''.join(bytes(r) for r in rows)).decode()

    @staticmethod
    def packed_pixels(rows):
        """Deflated shade-index rows, base64 (an 'image' part with 'packed': 'zlib'):
        for big pictures like whole level maps."""
        import zlib
        return base64.b64encode(zlib.compress(b''.join(bytes(r) for r in rows), 9)).decode()


def _key(project, src, plugin_dir):
    h = hashlib.sha1(bytes(project.rom))
    h.update(src.encode())
    for f in sorted(glob.glob(os.path.join(plugin_dir, '_*.py'))):     # shared helpers of the plugins
        h.update(open(f, 'rb').read())
    h.update(str(ASSET_ENGINE_VERSION).encode())
    return h.hexdigest()


def collect(project, site_dir):
    """All plugin assets of the current game (a list of dicts), building what is out of date."""
    plugin_dir = os.path.join(build.ROOT, 'assets')
    out = []
    files = sorted(glob.glob(os.path.join(plugin_dir, '*.py')))
    if not files:
        return out
    gen_dir = os.path.join(site_dir, 'gen')
    os.makedirs(gen_dir, exist_ok=True)
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        if name.startswith('_'):
            continue
        src = open(path, encoding='utf-8').read()
        key = _key(project, src, plugin_dir)
        cache = os.path.join(gen_dir, name + '.json')
        if os.path.exists(cache):
            c = json.load(open(cache, encoding='utf-8'))
            if c.get('key') == key and all(os.path.exists(os.path.join(gen_dir, f)) for f in c['files']):
                out += c['assets']
                continue
        spec = importlib.util.spec_from_file_location('gbbolt_asset_' + name, path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            ctx = AssetContext(project, gen_dir, name)
            assets = mod.build(ctx) or []
        except Exception:  # noqa: BLE001 - one broken plugin must not stop the site
            err = traceback.format_exc()
            print('asset plugin {} failed:\n{}'.format(name, err), file=sys.stderr)
            out.append({'name': name, 'type': 'error', 'group': 'errors', 'error': err.strip().splitlines()[-1]})
            continue
        for a in assets:
            a.setdefault('group', getattr(mod, 'GROUP', 'more'))
            a.setdefault('doc', [])
            a.setdefault('users', [])
            a['generated'] = 'assets/{}.py'.format(name)
        json.dump({'key': key, 'files': ctx.files, 'assets': assets}, open(cache, 'w', encoding='utf-8'))
        print('asset plugin {}: {} asset(s)'.format(name, len(assets)))
        out += assets
    return out
