"""Port names and annotations from a sibling game that shares code.

    cd new-game && python ../gbbolt/tools/port.py ../other-game [--names-only] [--dry-run]

Every annotated function of the other game is looked for in this game's ROM: same
instructions, with addresses (16-bit operands, ldh addresses) allowed to differ.
For each unique match:

  * the matched code's addresses tell what is what: the function itself, the labels
    it calls or jumps to, the RAM / HRAM variables it uses (a name is taken only if
    all matches agree on its address and this game has no name for it yet);
  * those names are given here (rename.py: labels, then RAM with type and
    description from the other game's ram.inc);
  * if this game's unit starts at the match and covers the same instructions, the
    other game's annotated unit text (header, pseudo-code, local labels) replaces it
    (apply.py - the ROM must still match, otherwise that unit is left alone).

Afterwards run gbbolt.py: the differential tests check the ported pseudo-code
against this game's code; tests that use the other game's addresses need fixing.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def masked(rom, start, end, decode):
    """[(offset, bytes-with-None-for-operands, operand kind, operand value)] per instruction."""
    out, a = [], start
    while a < end:
        i = decode(rom, a)
        b = list(rom[a:a + i.length])
        op = None
        if i.length == 3:
            op = ('w', rom[a + 1] | rom[a + 2] << 8)
            b[1] = b[2] = None
        elif b[0] in (0xE0, 0xF0):                     # ldh (a8),a / ldh a,(a8)
            op = ('h', 0xFF00 | rom[a + 1])
            b[1] = None
        out.append((a - start, b, op))
        a += i.length
    return out


def dump():
    """(run in the source project) its annotated functions as JSON."""
    import gbbolt
    from sm83 import decode
    import regroup
    p = gbbolt.Project()
    units = regroup.source_units()
    names = {}
    for n, a in p.syms.items():
        if '.' not in n:
            names.setdefault(a, n)
    var_by_addr = {v['addr']: v for v in p.parsed.vars.values() if isinstance(v, dict) and v.get('type') != 'io'}
    out = []
    for u in p.parsed.units:
        if not (u.annotated and u.func.get('def')):
            continue
        code = [p.parsed.lines[i] for i in u.lines if p.parsed.lines[i].kind == 'insn']
        if not code or code[0].addr != u.start:
            continue
        end = max(ln.addr + ln.size for ln in code)
        if end - u.start < 6:
            continue
        ins = masked(p.rom, u.start, end, decode)
        ops = []
        for off, b, op in ins:
            if op:
                v = var_by_addr.get(op[1])
                ops.append([off, op[0], op[1], names.get(op[1]),
                            {'name': v['name'], 'type': v['type'], 'desc': v['desc']} if v else None])
        out.append({'name': u.name, 'start': u.start, 'end': end, 'unit_end': u.end,
                    'pattern': [x for _, b, _ in ins for x in b], 'ops': ops,
                    'text': '\n'.join(units.get(u.name, []))})
    json.dump(out, sys.stdout)


def find(pat, rom):
    first = next(k for k, x in enumerate(pat) if x is not None)
    hits = []
    for s in range(0x150, len(rom) - len(pat)):
        if rom[s + first] == pat[first] and all(x is None or rom[s + k] == x for k, x in enumerate(pat)):
            hits.append(s)
    return hits


def main():
    if '--dump' in sys.argv:
        return dump()
    source = os.path.abspath(sys.argv[1])
    dry = '--dry-run' in sys.argv
    names_only = '--names-only' in sys.argv
    env = dict(os.environ, GBBOLT_ROOT=source)
    funcs = json.loads(subprocess.run([sys.executable, __file__, '--dump'], env=env, capture_output=True,
                                      text=True, check=True).stdout)
    import build
    import gbbolt
    from sm83 import decode
    p = gbbolt.Project()
    rom = p.rom
    have_names = set(n for n in p.syms if '.' not in n and not re.match(r'^(Call|Jump|jr|JumpTable)_', n))
    have_vars = {v['addr'] for v in p.parsed.vars.values() if isinstance(v, dict) and v.get('type') != 'io'}
    io_addrs = set(p.analysis.io_names.values())

    matches = []
    for f in funcs:
        hits = find(f['pattern'], rom)
        if len(hits) == 1:
            matches.append((f, hits[0]))
    print('{} of {} functions of {} found once in this ROM'.format(len(matches), len(funcs), os.path.basename(source)))

    # names: label / variable name -> this game's address, kept only if every match agrees
    label_votes, var_votes, var_info = {}, {}, {}
    for f, hit in matches:
        label_votes.setdefault(f['name'], set()).add(hit)
        tins = {off: op for off, _, op in masked(rom, hit, hit + f['end'] - f['start'], decode) if op}
        for off, kind, src_val, src_name, var in f['ops']:
            tgt = tins.get(off)
            if not tgt:
                continue
            if var and 0x8000 <= src_val:
                var_votes.setdefault(var['name'], set()).add(tgt[1])
                var_info[var['name']] = var
            elif src_name and src_val < 0x8000 and not src_name.startswith(('jr_', 'Call_', 'Jump_')):
                label_votes.setdefault(src_name, set()).add(tgt[1])
    labels = {n: a.pop() for n, a in label_votes.items() if len(a) == 1}
    labels = {n: a for n, a in labels.items() if a < 0x8000}     # a ROM label must be in ROM here too
    labels = {n: a for n, a in labels.items() if n not in have_names and list(labels.values()).count(a) == 1}
    variables = {n: a.pop() for n, a in var_votes.items() if len(a) == 1}
    variables = {n: a for n, a in variables.items() if n not in have_names and a not in have_vars
                 and a not in io_addrs and list(variables.values()).count(a) == 1}
    print('{} labels and {} variables to name'.format(len(labels), len(variables)))
    by_addr = {}
    for n, a in p.syms.items():
        if '.' not in n:
            by_addr.setdefault(a, n)
    if dry:
        for n, a in sorted(labels.items(), key=lambda x: x[1]):
            print('  label ${:04X} {} -> {}'.format(a, by_addr.get(a, '(no label)'), n))
        for n, a in sorted(variables.items(), key=lambda x: x[1]):
            print('  var   ${:04X} -> {}'.format(a, n))
        return
    tool = lambda *args: subprocess.run([sys.executable, os.path.join(HERE, args[0])] + list(args[1:]),  # noqa: E731
                                        capture_output=True, text=True)
    done = 0
    for n, a in sorted(labels.items(), key=lambda x: x[1]):
        old = by_addr.get(a)
        if old and re.match(r'^(Call|Jump|jr|JumpTable)_', old):
            r = tool('rename.py', old, n)
        elif old is None:
            r = tool('addlabel.py', '{:04X}'.format(a), n)
        else:
            continue
        done += r.returncode == 0
    print('named {} labels'.format(done))
    # a routine that is a function there becomes a function (unit) here too: `Name:` -> `Name::`
    funcs_named = {f['name'] for f in funcs}
    for fname in build.asm_files():
        path = os.path.join(build.SRC, fname)
        text = open(path, encoding='utf-8').read()
        new = re.sub(r'^(\w+):$', lambda m: m.group(1) + '::' if m.group(1) in funcs_named else m.group(0), text, flags=re.M)
        if new != text:
            open(path, 'w', encoding='utf-8', newline='\n').write(new)
    ok, msg = build.build()
    if not ok:
        sys.exit('turning labels into units broke the build: ' + msg)
    done = 0
    for n, a in sorted(variables.items(), key=lambda x: x[1]):
        v = var_info[n]
        r = tool('rename.py', '${:04x}'.format(a), n, v['type'], v['desc'] or '')
        done += r.returncode == 0
    print('named {} variables'.format(done))
    if names_only:
        return
    # annotations: units that start at the match and cover the same instructions
    p = gbbolt.Project()
    ported, failed = [], []
    by_name = {u.name: u for u in p.parsed.units}
    tmp = os.path.join(build.OUT, 'port_block.asm')
    for f, hit in matches:
        u = by_name.get(f['name'])
        if not u or u.start != hit or u.annotated or not f['text']:
            continue
        if u.end - u.start != f['unit_end'] - f['start']:
            continue
        text = '\n'.join(l for l in f['text'].split('\n') if not l.startswith(';@ sig:'))
        open(tmp, 'w', encoding='utf-8', newline='\n').write(text + '\n')
        r = tool('apply.py', tmp)
        (ported if r.returncode == 0 else failed).append(f['name'])
    if os.path.exists(tmp):
        os.remove(tmp)
    print('ported {} annotated units: {}'.format(len(ported), ', '.join(ported)))
    if failed:
        print('not ported (the ROM would differ - names it uses are missing here?): ' + ', '.join(failed))


if __name__ == '__main__':
    main()
