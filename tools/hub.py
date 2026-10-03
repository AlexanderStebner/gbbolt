"""The hub page that lists all games.

    python tools/hub.py games.json SITE_DIR

SITE_DIR holds one folder per game (its viewer, built with tools/gbbolt.py site);
each has a summary.json. Writes SITE_DIR/index.html.
"""
import html
import json
import os
import sys

PAGE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gbbolt</title>
<meta name="description" content="Game Boy disassemblies with checked pseudo-code next to the assembly">
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --panel2: #f0f2f5; --border: #d9dde4; --text: #1d2330; --muted: #5d6678;
  --accent: #2563eb; --ok: #1a7f37; --chk: #9a6700;
  --mono: ui-monospace, "Cascadia Code", "JetBrains Mono", Consolas, Menlo, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1115; --panel: #161a21; --panel2: #1c212b; --border: #2a303c; --text: #d7dce5; --muted: #8a93a6;
    --accent: #6aa9ff; --ok: #3fb950; --chk: #d29922;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1115; --panel: #161a21; --panel2: #1c212b; --border: #2a303c; --text: #d7dce5; --muted: #8a93a6;
  --accent: #6aa9ff; --ok: #3fb950; --chk: #d29922;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 var(--sans); }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
main { max-width: 1040px; margin: 0 auto; padding: 48px 16px 80px; }
h1 { font: 700 34px var(--sans); margin: 0; letter-spacing: -.01em; }
h1 span { color: var(--muted); font-weight: 500; }
.lead { font-size: 17px; color: var(--muted); max-width: 720px; margin: 10px 0 28px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; display: flex; flex-direction: column; gap: 10px; }
.card:hover { border-color: var(--accent); }
.card h2 { margin: 0; font: 700 20px var(--sans); }
.card h2 a { color: var(--text); }
.card p { margin: 0; color: var(--muted); font-size: 14px; }
.bar { height: 8px; border-radius: 4px; background: var(--panel2); overflow: hidden; display: flex; }
.bar i { display: block; height: 100%; }
.stats { font: 12.5px var(--mono); color: var(--muted); display: flex; gap: 14px; flex-wrap: wrap; }
.links { display: flex; gap: 14px; font-size: 14px; margin-top: auto; }
.open { display: inline-block; background: var(--accent); color: var(--panel); padding: 7px 14px; border-radius: 8px; font-weight: 600; }
.open:hover { text-decoration: none; filter: brightness(1.08); }
h3 { margin: 40px 0 8px; font-size: 16px; }
ul { color: var(--muted); padding-left: 20px; max-width: 760px; }
li { margin: 4px 0; }
footer { margin-top: 48px; color: var(--muted); font-size: 13px; border-top: 1px solid var(--border); padding-top: 16px; }
</style>
</head>
<body>
<main>
  <h1>gbbolt <span>· Game Boy disassemblies</span></h1>
  <p class="lead">Matching disassemblies of Game Boy games with <b>pseudo-code written next to the assembly</b>,
  checked against the real code in an emulator, and a Godbolt-style viewer: hover a line of Python to see the
  instructions behind it. Plus the game's graphics, music and sound effects, rendered from the code itself.</p>
  <div class="grid">
{cards}
  </div>
  <h3>What you get for each game</h3>
  <ul>
    <li><b>Code</b> - every function as pseudo-code (Python or C-style) beside its assembly, linked line by line, in a folder tree.</li>
    <li><b>Book</b> - all functions as one readable document, chapter by chapter.</li>
    <li><b>Call graph, RAM map, Coverage</b> - how it fits together and what each byte of memory means.</li>
    <li><b>Assets</b> - tiles, tilemaps, sprites, the cartridge header, drawn from the ROM data.</li>
    <li><b>Music</b> - every song and sound effect as a piano roll with the sound chip's settings, mute / solo per channel.</li>
  </ul>
  <footer>
    Built with <a href="https://github.com/{engine}">gbbolt</a>. The games and their code are the property of their
    publishers; no ROMs are included anywhere - each disassembly rebuilds the original ROM byte for byte from source.
  </footer>
</main>
</body>
</html>
'''

CARD = '''    <div class="card">
      <h2><a href="{id}/">{title}</a></h2>
      <p>{desc}</p>
      <div class="bar" title="{verified} verified, {checked} checked of {functions} functions"><i style="width:{pv}%;background:var(--ok)"></i><i style="width:{pc}%;background:var(--chk)"></i></div>
      <div class="stats"><span>{annotated}/{functions} functions</span><span>{verified} verified</span><span>{sounds} sounds</span><span>{assets} assets</span></div>
      <div class="links"><a class="open" href="{id}/">Open</a><a href="https://github.com/{repo}">source</a></div>
    </div>'''


def main():
    games = json.load(open(sys.argv[1], encoding='utf-8'))
    site = sys.argv[2]
    engine = os.environ.get('GBBOLT_ENGINE_REPO', 'AlexanderStebner/gbbolt')
    cards = []
    for g in games:
        path = os.path.join(site, g['id'], 'summary.json')
        if not os.path.exists(path):
            print('skipping {} (not built)'.format(g['id']))
            continue
        s = json.load(open(path, encoding='utf-8'))
        n = max(1, s['functions'])
        cards.append(CARD.format(id=html.escape(g['id']), title=html.escape(g['title']), desc=html.escape(g.get('description', '')),
                                 repo=html.escape(g['repo']), pv=100 * s['verified'] / n, pc=100 * s['checked'] / n,
                                 **{k: v for k, v in s.items() if k not in ('id', 'title')}))
    out = PAGE.replace('{cards}', '\n'.join(cards)).replace('{engine}', html.escape(engine))
    open(os.path.join(site, 'index.html'), 'w', encoding='utf-8', newline='\n').write(out)
    print('hub: {} game(s)'.format(len(cards)))


if __name__ == '__main__':
    main()
