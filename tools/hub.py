"""The hub page that lists all games.

    python tools/hub.py games.json SITE_DIR

SITE_DIR holds one folder per game (its viewer, built with tools/gbbolt.py site);
each has a summary.json (and a thumb.png if game.json names a "thumbnail" tilemap).
Writes SITE_DIR/index.html.
"""
import html
import json
import os
import sys

ICONS = {   # 24x24 line icons
    'code': '<path d="M8 6l-6 6 6 6M16 6l6 6-6 6M14 4l-4 16"/>',
    'book': '<path d="M4 4h6a3 3 0 013 3v13a2 2 0 00-2-2H4zM20 4h-6a3 3 0 00-3 3v13a2 2 0 012-2h7z"/>',
    'graph': '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7.5 8l3.5 7.5M16.5 8L13 15.5M8.5 6h7"/>',
    'ram': '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 6v12M11 6v12M15 6v12M7 3v3M12 3v3M17 3v3M7 18v3M12 18v3M17 18v3"/>',
    'assets': '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 15l5-5 4 4 3-3 6 6"/><circle cx="15.5" cy="8.5" r="1.5"/>',
    'music': '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    'github': '<path d="M9 19c-4 1.5-4-2-6-2.5m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 00-1.3-3.2 4.2 4.2 0 00-.1-3.2s-1.1-.3-3.5 1.3a12.3 12.3 0 00-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 00-.1 3.2A4.6 4.6 0 004 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/>',
}


def icon(name, size=22):
    return ('<svg class="ic" width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{p}</svg>').format(s=size, p=ICONS[name])


FEATURES = [
    ('code', 'Code', 'Pseudo-code beside the assembly, linked line by line.'),
    ('book', 'Book', 'Every function as one readable document, in chapters.'),
    ('graph', 'Call graph', 'Who calls whom, filtered by folder.'),
    ('ram', 'RAM map', 'What every byte of memory means.'),
    ('assets', 'Assets', 'Tiles, screens and sprites, drawn by the game\'s own code.'),
    ('music', 'Music', 'Every song as a piano roll, mute and solo per channel.'),
]

STEPS = [
    ('Disassemble', 'A matching disassembly: it rebuilds the original ROM byte for byte.'),
    ('Annotate', 'Python pseudo-code written next to the instructions, as plain comments.'),
    ('Verify', 'An emulator runs the original code and the pseudo-code on random states and compares.'),
]

PAGE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gbbolt</title>
<meta name="description" content="Game Boy games, read like source code: matching disassemblies with checked pseudo-code">
<style>
:root {
  --bg: #f4f6f3; --panel: #ffffff; --panel2: #eef2ec; --border: #d8ded5; --text: #18211c; --muted: #5b675f;
  --accent: #2f6b4f; --accent2: #88c070; --ok: #2f8a4f; --chk: #b07d12;
  --screen: #e0f8d0; --bezel: #545b6b; --hero1: #e3f2d9; --hero2: #f4f6f3;
  --mono: ui-monospace, "Cascadia Code", "JetBrains Mono", Consolas, Menlo, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e1310; --panel: #151c18; --panel2: #1b241f; --border: #27332c; --text: #dbe5de; --muted: #8b9a90;
    --accent: #88c070; --accent2: #5fa36f; --ok: #4fbf74; --chk: #d6a23a;
    --bezel: #3b4150; --hero1: #13201a; --hero2: #0e1310;
  }
}
:root[data-theme="dark"] {
  --bg: #0e1310; --panel: #151c18; --panel2: #1b241f; --border: #27332c; --text: #dbe5de; --muted: #8b9a90;
  --accent: #88c070; --accent2: #5fa36f; --ok: #4fbf74; --chk: #d6a23a;
  --bezel: #3b4150; --hero1: #13201a; --hero2: #0e1310;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 var(--sans); }
a { color: var(--accent); text-decoration: none; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 16px; }
.ic { flex: none; }

/* top bar */
.top { display: flex; align-items: center; justify-content: space-between; padding: 18px 0; }
.logo { display: flex; align-items: center; gap: 10px; font: 700 18px var(--sans); color: var(--text); letter-spacing: -.01em; }
.logo i { width: 26px; height: 26px; border-radius: 7px; background: var(--accent); display: grid; place-items: center; font: 700 15px var(--mono); color: var(--screen); font-style: normal; }
.top a.gh { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 14px; }
.top a.gh:hover { color: var(--text); }

/* hero */
.hero { background: linear-gradient(180deg, var(--hero1), var(--hero2)); border-bottom: 1px solid var(--border); }
.hero .wrap { padding-top: 34px; padding-bottom: 46px; }
.hero h1 { font: 800 clamp(30px, 5vw, 46px)/1.1 var(--sans); margin: 0; letter-spacing: -.02em; max-width: 720px; }
.hero h1 em { font-style: normal; color: var(--accent); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
.chip { display: inline-flex; align-items: center; gap: 7px; padding: 5px 12px; border-radius: 99px; background: var(--panel); border: 1px solid var(--border); font-size: 13px; color: var(--muted); }
.chip b { color: var(--text); font-weight: 600; }

/* sections */
section { padding: 40px 0 8px; }
h2 { font: 700 13px var(--sans); text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 16px; }

/* game cards */
.games { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 18px; }
.game { background: var(--panel); border: 1px solid var(--border); border-radius: 18px; overflow: hidden; display: flex; flex-direction: column; transition: transform .15s, box-shadow .15s, border-color .15s; color: var(--text); }
.game:hover { transform: translateY(-2px); box-shadow: 0 10px 30px rgba(0, 0, 0, .08); border-color: var(--accent2); }
.shot { background: var(--bezel); padding: 18px 26px 20px; display: grid; place-items: center; }
/* exactly 2x (320x288): whole-pixel scaling keeps the pixel art crisp */
.shot img, .shot .blank { width: 320px; max-width: 100%; aspect-ratio: 10 / 9; image-rendering: pixelated; border-radius: 4px; display: block; background: var(--screen); }
.body { padding: 18px 20px 20px; display: flex; flex-direction: column; gap: 12px; flex: 1; }
.title { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.title h3 { margin: 0; font: 700 21px var(--sans); }
.title span { color: var(--muted); font-size: 13px; white-space: nowrap; }
.desc { color: var(--muted); font-size: 14px; margin: 0; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.progress { display: flex; align-items: center; gap: 12px; }
.pct { font: 800 26px var(--sans); font-variant-numeric: tabular-nums; min-width: 3.2em; }
.pct small { display: block; font: 500 11.5px var(--sans); color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.bar { flex: 1; height: 10px; border-radius: 5px; background: var(--panel2); overflow: hidden; display: flex; }
.bar i { display: block; height: 100%; }
.stats { display: flex; flex-wrap: wrap; gap: 6px; }
.stat { font: 12.5px var(--mono); padding: 3px 9px; border-radius: 6px; background: var(--panel2); color: var(--muted); }
.stat b { color: var(--text); font-weight: 600; }
.actions { display: flex; gap: 10px; align-items: center; margin-top: auto; padding-top: 4px; }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 10px; font-weight: 600; font-size: 14px; }
.btn.main { background: var(--accent); color: var(--panel); }
.btn.main:hover { filter: brightness(1.08); }
.btn.ghost { color: var(--muted); border: 1px solid var(--border); }
.btn.ghost:hover { color: var(--text); border-color: var(--muted); }

/* features */
.features { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 860px) { .features { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .features { grid-template-columns: minmax(0, 1fr); } .games { grid-template-columns: minmax(0, 1fr); } }
.feature { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 16px; display: flex; gap: 12px; align-items: flex-start; }
.feature .ic { color: var(--accent); background: var(--panel2); border-radius: 10px; padding: 8px; width: 40px; height: 40px; }
.feature b { display: block; font-size: 15px; }
.feature span { color: var(--muted); font-size: 13.5px; }

/* steps */
.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; counter-reset: step; }
.step { padding: 16px 18px; border-left: 3px solid var(--accent2); background: var(--panel); border-radius: 0 12px 12px 0; border-top: 1px solid var(--border); border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.step b { display: block; font-size: 15px; }
.step b::before { counter-increment: step; content: counter(step); display: inline-grid; place-items: center; width: 22px; height: 22px; border-radius: 50%; background: var(--accent); color: var(--panel); font: 700 12px var(--sans); margin-right: 8px; }
.step span { color: var(--muted); font-size: 13.5px; }

footer { margin: 48px 0 0; padding: 22px 0 40px; border-top: 1px solid var(--border); color: var(--muted); font-size: 12.5px; }
footer .wrap { display: flex; flex-wrap: wrap; gap: 8px 24px; justify-content: space-between; }
</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <div class="top">
      <span class="logo"><i>g</i>gbbolt</span>
      <a class="gh" href="https://github.com/{engine}">{github} GitHub</a>
    </div>
    <h1>Game Boy games, <em>read like source code.</em></h1>
    <div class="chips">
      <span class="chip"><b>Matching</b> rebuilds the ROM byte for byte</span>
      <span class="chip"><b>Checked</b> pseudo-code verified in an emulator</span>
      <span class="chip"><b>Complete</b> graphics, music and sound</span>
    </div>
  </div>
</header>
<main class="wrap">
  <section>
    <h2>Games</h2>
    <div class="games">
{cards}
    </div>
  </section>
  <section>
    <h2>In every game</h2>
    <div class="features">
{features}
    </div>
  </section>
  <section>
    <h2>How it works</h2>
    <div class="steps">
{steps}
    </div>
  </section>
</main>
<footer>
  <div class="wrap">
    <span>Built with <a href="https://github.com/{engine}">gbbolt</a> · MIT</span>
    <span>The games belong to their publishers. No ROMs are included anywhere.</span>
  </div>
</footer>
</body>
</html>
'''

CARD = '''      <a class="game" href="{id}/">
        <div class="shot">{shot}</div>
        <div class="body">
          <div class="title"><h3>{title}</h3><span>{meta}</span></div>
          <p class="desc">{desc}</p>
          <div class="progress">
            <div class="pct">{pct}%<small>verified</small></div>
            <div class="bar" title="{verified} verified, {checked} checked of {functions} functions"><i style="width:{pv}%;background:var(--ok)"></i><i style="width:{pc}%;background:var(--chk)"></i></div>
          </div>
          <div class="stats"><span class="stat"><b>{annotated}</b>/{functions} functions</span><span class="stat"><b>{sounds}</b> sounds</span><span class="stat"><b>{assets}</b> assets</span></div>
          <div class="actions"><span class="btn main">Open</span><span class="btn ghost" data-href="https://github.com/{repo}">Source</span></div>
        </div>
      </a>'''


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
        shot = ('<img src="{}/{}" alt="{} title screen" loading="lazy">'.format(html.escape(g['id']), s['thumbnail'], html.escape(g['title']))
                if s.get('thumbnail') else '<div class="blank"></div>')
        meta = ' · '.join(str(x) for x in (s.get('publisher'), s.get('year')) if x)
        cards.append(CARD.format(
            id=html.escape(g['id']), title=html.escape(g['title']), desc=html.escape(g.get('description', '')),
            repo=html.escape(g['repo']), shot=shot, meta=html.escape(meta), pct=round(100 * s['verified'] / n),
            pv=100 * s['verified'] / n, pc=100 * s['checked'] / n,
            **{k: v for k, v in s.items() if k in ('verified', 'checked', 'functions', 'annotated', 'sounds', 'assets')}))
    features = '\n'.join('      <div class="feature">{}<div><b>{}</b><span>{}</span></div></div>'.format(
        icon(i), html.escape(t), html.escape(d)) for i, t, d in FEATURES)
    steps = '\n'.join('      <div class="step"><b>{}</b><span>{}</span></div>'.format(html.escape(t), html.escape(d)) for t, d in STEPS)
    out = (PAGE.replace('{cards}', '\n'.join(cards)).replace('{features}', features).replace('{steps}', steps)
           .replace('{github}', icon('github', 18)).replace('{engine}', html.escape(engine)))
    # the "Source" button sits inside the card link: open the repository instead of the game
    out = out.replace('</body>', '<script>document.querySelectorAll("[data-href]").forEach(b => b.addEventListener("click", '
                      'e => { e.preventDefault(); e.stopPropagation(); window.open(b.dataset.href, "_blank", "noopener"); }));</script>\n</body>')
    open(os.path.join(site, 'index.html'), 'w', encoding='utf-8', newline='\n').write(out)
    print('hub: {} game(s)'.format(len(cards)))


if __name__ == '__main__':
    main()
