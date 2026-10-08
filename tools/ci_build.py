"""Build the whole site: every game of games.json plus the hub page.

    python tools/ci_build.py games.json SITE_DIR [--local DIR] [--no-audio]

Each game repo is cloned into games/<id> (or taken from --local DIR/<repo name>),
built from source (no ROM needed - the result is checked against the sha1 in its
game.json), verified, its audio rendered and its viewer written to SITE_DIR/<id>/.
Then SITE_DIR/index.html lists them all. Used by .github/workflows/pages.yml.

A game whose commit, games.json and engine are the same as at its last build is not built
again: its viewer comes from .sitecache/<repo name> (CI keeps it between runs). --full
builds every game anyway.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)


def run(cmd, cwd, env):
    print('$', ' '.join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def engine_hash(games_json):
    """What a game's viewer depends on besides the game: the engine's tools (not the hub and
    this script, which don't change a game's pages) and games.json."""
    h = hashlib.sha256(open(games_json, 'rb').read())
    for base, dirs, files in sorted(os.walk(HERE)):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for f in sorted(files):
            if f in ('hub.py', 'ci_build.py') or f.endswith('.pyc'):
                continue
            path = os.path.join(base, f)
            h.update(os.path.relpath(path, HERE).replace('\\', '/').encode())
            h.update(open(path, 'rb').read())
    return h.hexdigest()


def main():
    args = sys.argv[1:]
    games_json, site = os.path.abspath(args[0]), os.path.abspath(args[1])
    local = args[args.index('--local') + 1] if '--local' in args else None
    audio = '--no-audio' not in args
    full = '--full' in args
    games = json.load(open(games_json, encoding='utf-8'))
    engine = engine_hash(games_json)
    os.makedirs(site, exist_ok=True)
    for g in games:
        name = g['repo'].split('/')[-1]
        if local:
            root = os.path.join(local, name)
        else:
            root = os.path.join(ENGINE, 'games', name)
            if not os.path.exists(root):
                run(['git', 'clone', '--depth', '1', 'https://github.com/{}.git'.format(g['repo']), root], ENGINE, os.environ)
        dest = os.path.join(site, g['id'])
        if os.path.exists(dest):
            shutil.rmtree(dest)
        built = os.path.join(ENGINE, '.sitecache', name)
        stamp_file = os.path.join(built, 'STAMP')
        stamp = None
        if not local:                                  # a local working tree has no commit to key on
            commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True,
                                    text=True, check=True).stdout.strip()
            stamp = hashlib.sha256((commit + engine + json.dumps(g, sort_keys=True)).encode()).hexdigest()
            if not full and os.path.exists(stamp_file) and open(stamp_file).read() == stamp:
                print('{}: unchanged since its last build ({}), viewer from the cache'.format(name, commit[:7]), flush=True)
                shutil.copytree(os.path.join(built, 'site'), dest)
                continue
        env = dict(os.environ, GBBOLT_ROOT=root, GBBOLT_GAMES=games_json)
        # asset plugin results from earlier runs (CI keeps .gencache between builds): a plugin
        # that has not changed is not run again
        cache = os.path.join(ENGINE, '.gencache', name)
        gen = os.path.join(root, 'out', 'site', 'gen')
        if os.path.isdir(cache):
            shutil.copytree(cache, gen, dirs_exist_ok=True)
        game_src = json.load(open(os.path.join(root, 'game.json'), encoding='utf-8')).get('src', 'src')
        if audio and os.path.exists(os.path.join(root, game_src, 'sound.json')):   # games without a sound config yet
            run([sys.executable, os.path.join(HERE, 'audio.py')], root, env)
        # "strict": false in games.json: verify failures are shown on the page but don't stop the build
        strict = ['--strict'] if g.get('strict', True) else []
        run([sys.executable, os.path.join(HERE, 'gbbolt.py'), 'verify', 'site'] + strict, root, env)
        if os.path.isdir(gen):
            shutil.copytree(gen, cache, dirs_exist_ok=True)
        shutil.copytree(os.path.join(root, 'out', 'site'), dest,
                        ignore=shutil.ignore_patterns('_test.html'))
        if stamp:
            if os.path.exists(built):
                shutil.rmtree(built)
            shutil.copytree(dest, os.path.join(built, 'site'))
            open(stamp_file, 'w').write(stamp)
    run([sys.executable, os.path.join(HERE, 'hub.py'), games_json, site], ENGINE, os.environ)


if __name__ == '__main__':
    main()
