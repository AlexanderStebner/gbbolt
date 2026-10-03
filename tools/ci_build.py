"""Build the whole site: every game of games.json plus the hub page.

    python tools/ci_build.py games.json SITE_DIR [--local DIR] [--no-audio]

Each game repo is cloned into games/<id> (or taken from --local DIR/<repo name>),
built from source (no ROM needed - the result is checked against the sha1 in its
game.json), verified, its audio rendered and its viewer written to SITE_DIR/<id>/.
Then SITE_DIR/index.html lists them all. Used by .github/workflows/pages.yml.
"""
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


def main():
    args = sys.argv[1:]
    games_json, site = os.path.abspath(args[0]), os.path.abspath(args[1])
    local = args[args.index('--local') + 1] if '--local' in args else None
    audio = '--no-audio' not in args
    games = json.load(open(games_json, encoding='utf-8'))
    os.makedirs(site, exist_ok=True)
    for g in games:
        name = g['repo'].split('/')[-1]
        if local:
            root = os.path.join(local, name)
        else:
            root = os.path.join(ENGINE, 'games', name)
            if not os.path.exists(root):
                run(['git', 'clone', '--depth', '1', 'https://github.com/{}.git'.format(g['repo']), root], ENGINE, os.environ)
        env = dict(os.environ, GBBOLT_ROOT=root, GBBOLT_GAMES=games_json)
        # asset plugin results from earlier runs (CI keeps .gencache between builds): a plugin
        # that has not changed is not run again
        cache = os.path.join(ENGINE, '.gencache', name)
        gen = os.path.join(root, 'out', 'site', 'gen')
        if os.path.isdir(cache):
            shutil.copytree(cache, gen, dirs_exist_ok=True)
        if audio:
            run([sys.executable, os.path.join(HERE, 'audio.py')], root, env)
        run([sys.executable, os.path.join(HERE, 'gbbolt.py'), 'verify', 'site', '--strict'], root, env)
        if os.path.isdir(gen):
            shutil.copytree(gen, cache, dirs_exist_ok=True)
        dest = os.path.join(site, g['id'])
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(os.path.join(root, 'out', 'site'), dest,
                        ignore=shutil.ignore_patterns('_test.html'))
    run([sys.executable, os.path.join(HERE, 'hub.py'), games_json, site], ENGINE, os.environ)


if __name__ == '__main__':
    main()
