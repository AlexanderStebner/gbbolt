"""Generate the gbbolt viewer: one self-contained HTML file with the data embedded."""
import base64
import datetime
import json
import os
import re

import build
from pseudo import HELPER_DOCS, parse_signature
from sm83 import CPU

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, 'viewer.html')
OUT_DIR = os.path.join(build.OUT, 'site')

STATUS_CODE = {'verified': 'v', 'checked': 'k', 'failing': 'f', 'stale': 's', 'none': 'c'}


def unit_paths(project):
    """Virtual folder of every unit: `;@ path:` if given; a data block otherwise
    takes the folder most of its users are in (the unit before it if nobody
    refers to it)."""
    p, an = project.parsed, project.analysis
    paths = {u.name: u.path for u in p.units if u.path}
    data = [u for u in p.units if u.kind == 'data' and u.name not in paths]
    users = {u.name: {} for u in data}
    for code in p.units:
        if code.name not in paths or code.name not in an.refs:
            continue
        # RAM accesses plus every 16-bit immediate (ROM pointers like `ld de, MenuObjects`)
        addrs = set().union(*an.refs[code.name].values())
        addrs |= {i.imm for i in an.insns.get(code.name, []) if i.imm is not None and i.imm >= 0x150}
        for d in data:
            if any(d.start <= a < d.end for a in addrs):
                users[d.name][paths[code.name]] = users[d.name].get(paths[code.name], 0) + 1
    order = [u.name for u in p.units]
    for name in an.tables:                           # a jump table belongs with its dispatcher (the unit before it)
        i = order.index(name)
        if name in users and i and order[i - 1] in paths:
            users[name] = {paths[order[i - 1]]: 1}
    prev = ''
    for u in p.units:
        if u.name in users:
            c = users[u.name]
            paths[u.name] = max(sorted(c), key=lambda k: c[k]) if c else prev
        prev = paths.get(u.name, prev)
    return paths


def folder_list(paths):
    """[{path, desc}] in reading order: src/folders.txt first, then any others."""
    out, seen = [], set()
    fname = os.path.join(build.SRC, 'folders.txt')
    if os.path.exists(fname):
        for line in open(fname, encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            path, _, desc = line.partition('|')
            out.append({'path': path.strip(), 'desc': desc.strip()})
            seen.add(path.strip())
    for path in sorted(set(paths.values())):
        parts = path.split('/')
        for k in range(1, len(parts) + 1):
            sub = '/'.join(parts[:k])
            if sub and sub not in seen:
                out.append({'path': sub, 'desc': ''})
                seen.add(sub)
    return out


def insn_row(ln, rom):
    raw = rom[ln.addr:ln.addr + min(ln.size, 16)]
    hexs = ' '.join('{:02X}'.format(b) for b in raw) + (' …' if ln.size > 16 else '')
    return [ln.addr, hexs, 'i' if ln.kind == 'insn' else 'd', ln.text, ln.comment, ln.group]


def host_unit(project, line_index):
    if not hasattr(project, '_host'):
        project._host = {i: u.name for u in project.parsed.units for i in u.lines}
    return project._host.get(line_index)


def unit_data(project, u, res, edges_from, edges_to):
    lines = project.parsed.lines
    rom = project.rom
    out_lines = []
    prev_blank = False
    for i in u.lines:
        ln = lines[i]
        if ln.kind in ('header', 'pseudo'):
            continue
        if ln.kind == 'blank':
            if prev_blank or not out_lines:
                continue
            prev_blank = True
            out_lines.append([None, '', 'b', '', '', ln.group])
            continue
        prev_blank = False
        if ln.kind == 'label':
            out_lines.append([ln.addr, '', 'l', '.' + ln.label.split('.')[-1] if '.' in ln.label else ln.label,
                              '', ln.group])
        elif ln.kind == 'comment':
            out_lines.append([None, '', 'c', '', ln.comment, ln.group])
        elif ln.kind in ('insn', 'data'):
            out_lines.append(insn_row(ln, rom))
        if ln.xref:
            out_lines[-1] = out_lines[-1][:6] + [ln.xref[0]]   # belongs to a line of another unit
    while out_lines and out_lines[-1][2] == 'b':
        out_lines.pop()
    # instructions elsewhere that belong to this unit's lines (`;=@Unit.tag`), by host unit
    foreign = []
    for i in u.foreign:
        ln = lines[i]
        if ln.kind not in ('insn', 'data', 'label'):
            continue
        host = host_unit(project, i)
        if not foreign or foreign[-1]['host'] != host:
            foreign.append({'host': host, 'lines': []})
        row = insn_row(ln, rom) if ln.kind != 'label' else             [ln.addr, '', 'l', '.' + ln.label.split('.')[-1] if '.' in ln.label else ln.label, '', ln.xref[1]]
        row[5] = ln.xref[1]
        foreign[-1]['lines'].append(row)

    d = {
        'n': u.name, 's': u.start, 'e': u.end, 'k': u.kind,
        'st': res.status if u.annotated else 'none',
        'lines': out_lines,
        'file': lines[u.first_line].file,
        'foreign': foreign,
        'out': sorted({(t, k) for t, k in edges_from.get(u.name, [])}),
        'in': sorted({(f, k) for f, k in edges_to.get(u.name, [])}),
    }
    if u.name in project.analysis.tables:
        d['table'] = project.analysis.tables[u.name]
    if u.asset:
        d['asset'] = u.asset['type']
    refs = project.analysis.refs.get(u.name)
    if refs:
        name = project.analysis.var_for
        d['refs'] = {k: sorted({name(a) or '${:04X}'.format(a) for a in v}) for k, v in refs.items()}
    if u.annotated:
        f = u.func
        d['def'] = f['def']
        d['doc'] = f['doc']
        d['hdr'] = {k: f[k] for k in ('reads', 'writes', 'clobbers', 'test')}
        d['groups'] = [g['pseudo'] for g in u.pseudo_groups]
        if f['def']:
            try:
                sig = parse_signature(f['def'])
                d['params'] = sig.params
                d['returns'] = sig.returns
            except SyntaxError:
                pass
        d['res'] = res.as_dict()
    return d


def resolve_value(project, v):
    v = v.strip()
    if v in project.syms:
        return project.syms[v]
    if v in project.parsed.consts:
        return project.parsed.consts[v]
    return int(v[1:], 16) if v.startswith('$') else int(v, 0)


def run_loader(project, spec):
    """Run a tile-loading routine in the SM83 interpreter; return VRAM tile data ($8000-$97FF).

    spec: 'Routine' or 'Routine(hl=Label,bc=$1000)'.
    """
    m = re.match(r'^(\w+)(?:\((.*)\))?$', spec)
    name, args = m.group(1), m.group(2) or ''
    mem = bytearray(0x10000)
    mem[0:0x8000] = project.rom[0:0x8000]
    cpu = CPU(mem)
    for arg in filter(None, args.split(',')):
        reg, _, val = arg.partition('=')
        v = resolve_value(project, val)
        if len(reg) == 2:
            cpu.setp(reg, v)
        else:
            setattr(cpu, reg, v & 0xFF)
    cpu.sp = build.GAME['test_memory']['stack_top']
    cpu.call(project.syms[name], max_steps=2000000)
    return bytes(mem[0x8000:0x9800])


def render_sprites(project, count):
    """Draw every sprite id with the game's own routine (game.json "sprites"): write the
    object record, set the variables, call the routine, read the OAM entries it wrote."""
    spec = build.GAME.get('sprites')
    if not spec:
        return [[] for _ in range(count)]
    syms, v = project.syms, project.parsed.vars

    def addr(name):
        if isinstance(name, int):
            return name
        if name in v:
            return v[name]['addr']
        return syms[name] if name in syms else int(name, 0)
    num = lambda x: int(x, 0) if isinstance(x, str) else x   # noqa: E731
    oam = addr(spec['oam'])
    out = []
    for sid in range(count):
        mem = bytearray(0x10000)
        mem[0:0x8000] = project.rom[0:0x8000]
        rec = [sid if x == 'id' else num(x) for x in spec.get('record', [])]
        at = addr(spec['record_at'])
        mem[at:at + 16] = bytes(rec + [0] * (16 - len(rec)))
        for name, val in spec.get('set', {}).items():
            mem[addr(name)] = num(val) & 0xFF
        cpu = CPU(mem)
        cpu.sp = build.GAME['test_memory']['stack_top']
        if spec.get('hl'):
            cpu.setp('hl', addr(spec['hl']))
        try:
            cpu.call(syms[spec['routine']], max_steps=200000)
        except Exception:  # noqa: BLE001 - a broken entry just renders empty
            out.append([])
            continue
        if spec.get('oam_end'):
            hi, lo = spec['oam_end']
            end = (mem[addr(hi)] << 8) | mem[addr(lo)]
        else:
            end = oam + 0xA0
        out.append([list(mem[a:a + 4]) for a in range(oam, min(end, oam + 0xA0), 4)])
    return out


def collect_assets(project):
    assets, tilesets = [], {}
    rom = project.rom
    users = {}       # address -> code units that load it as an immediate (ld hl/de, Label)
    for uname, insns in project.analysis.insns.items():
        for i in insns:
            if i.imm is not None and i.length == 3:
                users.setdefault(i.imm, set()).add(uname)
    for u in project.parsed.units:
        a = u.asset
        if not a:
            continue
        params = {k: v for k, v in a.items() if k not in ('type', 'doc')}
        if 'range' in params:
            lo, hi = params['range'].split('-')
            start, end = resolve_value(project, lo), resolve_value(project, hi) + 1
        else:
            start = u.start
            end = start + (resolve_value(project, params['length']) if 'length' in params else u.end - u.start)
            if a['type'] == 'tilemap' and 'length' not in params:
                end = start + int(params.get('width', 20)) * int(params.get('height', 18))
        item = {'name': u.name, 'type': a['type'], 'params': params, 'doc': a['doc'],
                'start': start, 'length': end - start,
                'bytes': base64.b64encode(bytes(rom[start:end])).decode(),
                'users': sorted(users.get(u.start, ()))}
        if 'tiles' in params:
            keys = params['tiles'].split('|')
            for key in keys:
                if key not in tilesets:
                    try:
                        tilesets[key] = base64.b64encode(run_loader(project, key)).decode()
                    except Exception as e:  # noqa: BLE001 - report, keep going
                        item['error'] = 'could not run {}: {}'.format(key, e)
            item['tileset'] = keys[0]
            item['tilesets'] = keys
        if a['type'] == 'sprites':
            item['sprites'] = render_sprites(project, resolve_value(project, params.get('count', '1')))
        assets.append(item)
    return assets, tilesets


# What each sound is, from where the game requests it (see the annotated callers)
def other_games():
    """The games of the hub this site is built for ($GBBOLT_GAMES: a games.json), for
    the selector in the viewer's header. Empty for a site built on its own."""
    path = os.environ.get('GBBOLT_GAMES')
    if not path or not os.path.exists(path):
        return []
    return [{'id': g['id'], 'title': g['title']} for g in json.load(open(path, encoding='utf-8'))]


def collect_audio():
    """The rendered sounds (out/site/audio/index.json) with names and labels from src/sound.json."""
    path = os.path.join(OUT_DIR, 'audio', 'index.json')
    if not os.path.exists(path):
        return {'items': [], 'kinds': {}}
    cfg_path = os.path.join(build.SRC, 'sound.json')
    cfg = json.load(open(cfg_path, encoding='utf-8')) if os.path.exists(cfg_path) else {}
    names = cfg.get('names', {})
    items = json.load(open(path))
    for it in items:
        name, used = names.get('{} {}'.format(it['kind'], it['id']), ['{} {}'.format(it['kind'], it['id']), ''])
        it['name'], it['usedBy'] = name, used
    kinds = {k: v.get('label', k) for k, v in cfg.get('kinds', {}).items()}
    return {'items': items, 'kinds': kinds}


def generate(project):
    p = project.parsed
    results = project.results
    edges_from, edges_to = {}, {}
    for a, b, k in project.analysis.edges:
        edges_from.setdefault(a, []).append((b, k))
        edges_to.setdefault(b, []).append((a, k))

    units = [unit_data(project, u, results[u.name], edges_from, edges_to) for u in p.units]
    paths = unit_paths(project)
    for d in units:
        d['p'] = paths.get(d['n'], '')

    rommap = ['d'] * len(project.rom)
    for u in p.units:
        if u.kind != 'code':
            continue
        c = STATUS_CODE[results[u.name].status] if u.annotated else 'c'
        for a in range(u.start, min(u.end, len(rommap))):
            rommap[a] = c
    # bytes inside code units that are data (db lines) stay 'd'
    for ln in p.lines:
        if ln.kind == 'data' and ln.addr is not None:
            for a in range(ln.addr, min(ln.addr + ln.size, len(rommap))):
                rommap[a] = 'd'

    # memory references for the RAM map (static scan of every code unit)
    an = project.analysis
    access = {}
    for uname, refs in an.refs.items():
        for kind, addrs in refs.items():
            for a in addrs:
                access.setdefault(a, {'reads': set(), 'writes': set(), 'ptrs': set()})[kind].add(uname)
    # plus what annotation headers declare (accesses through pointers the static scan misses)
    for u in p.units:
        if not u.annotated:
            continue
        for kind in ('reads', 'writes'):
            for name in u.func[kind]:
                v = p.vars.get(name)
                if v:
                    access.setdefault(v['addr'], {'reads': set(), 'writes': set(), 'ptrs': set()})[kind].add(u.name)

    variables = []
    named = set()
    for v in p.vars.values():
        span = range(v['addr'], v['addr'] + max(1, v['size']))
        named.update(span)
        acc = {'reads': set(), 'writes': set(), 'ptrs': set()}
        for a in span:
            for k in acc:
                acc[k] |= access.get(a, {}).get(k, set())
        if v['type'] == 'io' and not any(acc.values()):
            continue
        variables.append({'name': v['name'], 'addr': v['addr'], 'type': v['type'], 'size': v['size'],
                          'desc': v['desc'], 'reads': sorted(acc['reads']), 'writes': sorted(acc['writes']),
                          'ptrs': sorted(acc['ptrs'])})
    variables.sort(key=lambda v: v['addr'])
    unnamed = []
    # pointers only (no access through them by name), inside a memory region the
    # source names (`WORK_RAM0 + $FFF` etc.: start of a downward fill)
    regions = [(p.consts[c], size) for c, size in (('WORK_RAM0', 0x1000), ('WORK_RAM1', 0x1000),
                                                 ('OAM_START', 0x100), ('HRAM_START', 0x7F))
               if c in p.consts]
    for a in sorted(access):
        if a in named or a < 0xA000 or 0xFF00 <= a < 0xFF80 or a == 0xFFFF:
            continue
        acc = access[a]
        if not acc['reads'] and not acc['writes'] and any(s <= a < s + n for s, n in regions):
            continue
        unnamed.append({'addr': a, 'reads': sorted(acc['reads']), 'writes': sorted(acc['writes']),
                        'ptrs': sorted(acc['ptrs'])})

    consts = {k: {'value': v, 'doc': p.constdoc.get(k, '')} for k, v in p.consts.items()}
    assets, tilesets = collect_assets(project)

    data = {
        'title': build.GAME.get('title') or build.rom_title(project.rom),
        'gameId': build.GAME.get('id'),
        'games': other_games(),
        'game': {'entry': build.GAME.get('entry'), 'graphRoot': build.GAME.get('graph_root'),
                 'jumpRst': build.GAME.get('jump_table_rst'),
                 'sprites': (build.GAME.get('sprites') or {}).get('routine')},
        'generated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'build': {'ok': project.build_ok, 'msg': project.build_msg, 'sha1': build.sha1(build.BUILT)},
        'units': units,
        'rommap': ''.join(rommap),
        'vars': variables,
        'unnamed': unnamed,
        'folders': folder_list(paths),
        'consts': consts,
        'helpers': HELPER_DOCS,
        'assets': assets,
        'audio': collect_audio(),
        'tilesets': tilesets,
        'globalChecksum': sum(b for i, b in enumerate(project.rom) if i not in (0x14E, 0x14F)) & 0xFFFF,
        'verifySeconds': round(project.verify_seconds, 1),
    }
    html = open(TEMPLATE, encoding='utf-8').read()
    payload = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    html = html.replace('/*__GBBOLT_DATA__*/null', payload)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, 'index.html')
    open(path, 'w', encoding='utf-8', newline='\n').write(html)
    # a short summary for the hub page that lists all games
    funcs = [u for u in units if u['k'] == 'code']
    summary = {'id': build.GAME.get('id'), 'title': data['title'], 'sha1': data['build']['sha1'],
               'functions': len(funcs), 'annotated': sum(1 for u in funcs if u.get('def')),
               'verified': sum(1 for u in funcs if u.get('st') == 'verified'),
               'checked': sum(1 for u in funcs if u.get('st') == 'checked'),
               'sounds': len(data['audio']['items']), 'assets': len(data['assets']),
               'generated': data['generated']}
    json.dump(summary, open(os.path.join(OUT_DIR, 'summary.json'), 'w', encoding='utf-8'), indent=1)
    return path
