"""Helpers for reworking the pseudo-code <-> assembly grouping.

    python tools/regroup.py extract NAME... > block.asm   # units as apply.py takes them
    python tools/regroup.py check block.asm               # only ;> / ;= lines differ?
    python tools/regroup.py stats [NAME...]                # biggest group per unit

`check` compares every unit in the block with the source, ignoring `;>` and
`;=` lines: instructions, labels, data, comments and headers must be identical.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import apply  # noqa: E402
import build  # noqa: E402

def source_units():
    out = {}
    text = '\n'.join(open(os.path.join(build.SRC, f), encoding='utf-8').read() for f in build.asm_files())
    for name, body in apply.split_units(text):
        while body and not body[-1].strip():
            body.pop()
        out[name] = body
    return out


def is_annotation(line):
    s = line.strip()
    return s.startswith(';>') or s.startswith(';=')


def essence(body):
    return [l.rstrip() for l in body if not is_annotation(l) and l.strip()]


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == 'extract':
        units = source_units()
        for n in args:
            if n not in units:
                sys.exit('no unit ' + n)
            print('\n'.join(units[n]) + '\n')
    elif cmd == 'check':
        units = source_units()
        text = open(args[0], encoding='utf-8').read()
        bad = 0
        for name, body in apply.split_units(text):
            if name not in units:
                print('{}: not in the source'.format(name)); bad += 1; continue
            a, b = essence(units[name]), essence(body)
            if a != b:
                bad += 1
                for i, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        print('{}: differs at line {}:\n  source: {}\n  block:  {}'.format(name, i + 1, x, y)); break
                else:
                    print('{}: {} vs {} non-annotation lines'.format(name, len(a), len(b)))
        print('ok' if not bad else '{} unit(s) differ'.format(bad))
        sys.exit(1 if bad else 0)
    elif cmd == 'stats':
        import gbbolt
        p = gbbolt.Project()
        for u in p.parsed.units:
            if not (u.annotated and u.func.get('def')) or (args and u.name not in args):
                continue
            sizes = {}
            for i in u.lines:
                ln = p.parsed.lines[i]
                if ln.kind == 'insn':
                    g = ln.group or ('x' if ln.xref else 0)    # linked to another unit's line
                    sizes[g] = sizes.get(g, 0) + 1
            for i in u.foreign:
                if p.parsed.lines[i].kind == 'insn':
                    g = p.parsed.lines[i].xref[1]
                    sizes[g] = sizes.get(g, 0) + 1
            print('{:<28} {:3} instructions, {:3} groups, biggest {:3}{}'.format(
                u.name, sum(sizes.values()), len([g for g in sizes if g and g != 'x']), max([v for k, v in sizes.items() if k != 'x'] or [0]),
                '  (%d not linked)' % sizes[0] if sizes.get(0) else ''))


if __name__ == '__main__':
    main()
