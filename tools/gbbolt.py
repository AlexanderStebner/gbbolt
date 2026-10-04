"""gbbolt - annotated disassembly toolkit.

    python tools/gbbolt.py            build, fixheaders, verify, stamp, site - one pass
    python tools/gbbolt.py build      assemble and compare with the original ROM
    python tools/gbbolt.py verify     check every annotation (verify Name... : just those)
    python tools/gbbolt.py stamp      record `;@ sig:` for passing annotations
    python tools/gbbolt.py site       generate out/site/index.html
    python tools/gbbolt.py page       rewrite the viewer only, with the last verify results (layout work)
    python tools/gbbolt.py fixheaders add statically found accesses to ;@ reads/writes

Commands can be chained (`gbbolt.py verify site`); the project is loaded once and
verified at most once. The differential tests run in parallel processes.
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import asmparse  # noqa: E402
import build  # noqa: E402
from analyze import Analysis  # noqa: E402
from verify import verify_all  # noqa: E402

ICON = {'verified': '[ok]  ', 'checked': '[chk] ', 'failing': '[FAIL]', 'stale': '[old] ', 'none': '      '}


class Project:
    def __init__(self, rebuild=True):
        if rebuild:
            ok, msg = build.build()
        else:                       # use the ROM and symbols of the last build
            ok, msg = True, 'not rebuilt'
        self.build_ok, self.build_msg = ok, msg
        if not ok:
            return
        self.rom = open(build.BUILT, 'rb').read()
        self.syms = build.read_sym()
        self.parsed = asmparse.parse(build.SRC, self.rom, self.syms)
        self.analysis = Analysis(self.parsed, self.rom, build.SRC)
        for name, a in self.analysis.io_names.items():
            self.parsed.vars.setdefault(name, {'name': name, 'addr': a, 'type': 'io', 'base': 'u8',
                                               'size': 1, 'desc': 'hardware register'})
        self.results = None

    def verify(self, only=None):
        t = time.time()
        self.results = verify_all(self.parsed, self.analysis, self.syms, self.rom, only)
        self.verify_seconds = time.time() - t
        return self.results


def print_report(project, only=None, problems_only=False):
    res = project.results
    counts = {}
    for u in project.parsed.units:
        r = res[u.name]
        if not u.annotated:
            continue
        counts[r.status] = counts.get(r.status, 0) + 1
        if only and u.name not in only:
            continue
        if problems_only and not (r.errors or r.warnings or r.status in ('failing', 'stale')):
            continue
        tests = '{}/{} trials'.format(r.passed, r.trials) if r.trials else ('skip: ' + r.skip if r.skip else '')
        print('{} ${:04X} {:<24} {}'.format(ICON[r.status], u.start, u.name, tests))
        for e in r.errors:
            print('         error: ' + e)
        if r.example:
            ex = r.example
            print('         inputs: ' + ', '.join('{}={}'.format(k, v) for k, v in
                                               list(ex['args'].items()) + list(ex.get('state', {}).items())))
            for m in ex['memory'][:6]:
                print('         {}: asm {} pseudo {}'.format(m['addr'], m['asm'], m['pseudo']))
            for m in ex['returns']:
                print('         return {}: asm {} pseudo {}'.format(m['reg'], m['asm'], m['pseudo']))
        for w in r.warnings:
            print('         warning: ' + w)
        if r.sig_state == 'stale':
            print('         stale: bytes changed since the annotation was signed')
    print('\n' + ', '.join('{} {}'.format(v, k) for k, v in sorted(counts.items())) +
          ' ({:.1f}s)'.format(project.verify_seconds))


def stamp(project):
    """Write the current byte checksum into `;@ sig:` of every passing annotation."""
    res = project.results
    edits = {}
    for u in project.parsed.units:
        r = res[u.name]
        if not u.annotated or r.status in ('failing', 'none') or r.sig_state == 'ok':
            continue
        label_line = project.parsed.lines[u.first_line]
        edits.setdefault(label_line.file, []).append((u, r.sig))
    for fname, items in edits.items():
        path = os.path.join(build.SRC, fname)
        lines = open(path, encoding='utf-8').read().split('\n')
        for u, sig in sorted(items, key=lambda x: -x[0].first_line):
            # header lines sit directly above the label line
            i = project.parsed.lines[u.first_line].lineno - 1
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith(';@'):
                if lines[j].lstrip().startswith(';@ sig:'):
                    del lines[j]
                    i -= 1
                j -= 1
            lines.insert(i, ';@ sig: ' + sig)
            r = res[u.name]            # keep the results usable for the site in the same run
            r.sig_state = 'ok'
            if r.status == 'stale':
                r.status = 'verified' if r.skip is None and r.trials and r.passed == r.trials else 'checked'
        open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
        print('stamped {} function(s) in {}'.format(len(items), fname))


def fix_headers(project):
    """Add memory accesses found by the static scan to `;@ reads:` / `;@ writes:`."""
    an = project.analysis
    edits = {}
    for u in project.parsed.units:
        if not u.annotated or u.name not in an.refs:
            continue
        refs = an.refs[u.name]
        name = lambda a: an.symbolic(a, u.name)    # noqa: E731
        st_r = {name(a) or '${:04X}'.format(a) for a in refs['reads']}
        st_w = {name(a) or '${:04X}'.format(a) for a in refs['writes']}
        keep = lambda s: not (s.startswith('r') and s in an.io_names)   # noqa: E731 - IO registers stay out
        decl_r, decl_w = set(u.func['reads']), set(u.func['writes'])
        add_r = sorted(s for s in st_r - decl_r - decl_w if keep(s))
        add_w = sorted(s for s in st_w - decl_w if keep(s))
        if add_r or add_w:
            edits[u.name] = (u, add_r, add_w)
    if not edits:
        print('headers complete')
        return
    by_file = {}
    for uname, item in edits.items():
        by_file.setdefault(project.parsed.lines[item[0].first_line].file, {})[uname] = item
    for fname, file_edits in by_file.items():
        fix_header_file(project, os.path.join(build.SRC, fname), file_edits)


def fix_header_file(project, path, edits):
    lines = open(path, encoding='utf-8').read().split('\n')
    for uname, (u, add_r, add_w) in sorted(edits.items(), key=lambda x: -x[1][0].first_line):
        i = project.parsed.lines[u.first_line].lineno - 1      # label line
        top = i
        while top and lines[top - 1].startswith(';@'):
            top -= 1
        for key, add in (('writes', add_w), ('reads', add_r)):
            if not add:
                continue
            idx = next((k for k in range(top, i) if lines[k].startswith(';@ {}:'.format(key))), None)
            if idx is not None:
                lines[idx] = lines[idx].rstrip() + ', ' + ', '.join(add)
            else:
                # after the description, before test/sig lines
                pos = next((k for k in range(top, i) if re.match(r';@ (test|sig):', lines[k])), i)
                lines.insert(pos, ';@ {}: {}'.format(key, ', '.join(add)))
                i += 1
        print('{}: +reads {} +writes {}'.format(uname, add_r, add_w))
    open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))


def show(project, names):
    """Print units' source with addresses, callers and callees (for annotating)."""
    units = {u.name: u for u in project.parsed.units}
    callers, callees = {}, {}
    for a, b, k in project.analysis.edges:
        callers.setdefault(b, []).append('{}({})'.format(a, k) if k != 'call' else a)
        callees.setdefault(a, []).append('{}({})'.format(b, k) if k != 'call' else b)
    for n in names:
        u = units.get(n)
        if u is None:
            print('?? ' + n)
            continue
        print('==== {} ${:04X}-${:04X} {} annotated={}'.format(n, u.start, u.end - 1, u.kind, u.annotated))
        print('  callers: ' + ', '.join(sorted(set(callers.get(n, [])))))
        print('  calls:   ' + ', '.join(sorted(set(callees.get(n, [])))))
        for i in u.lines:
            ln = project.parsed.lines[i]
            if ln.kind in ('blank',):
                continue
            ad = '{:04X}'.format(ln.addr) if ln.addr is not None and ln.kind in ('insn', 'data', 'label') else '    '
            print('{} {}'.format(ad, ln.raw))


def main():
    """Commands run in order on one loaded project, verifying at most once:
    `gbbolt.py` = fixheaders verify stamp site; `gbbolt.py verify Name...` tests only those."""
    args = [a for a in sys.argv[1:] if a not in ('--strict', '--nobuild')] or ['all']
    strict = '--strict' in sys.argv            # exit code 1 if anything fails (for CI)
    steps = [a for a in args if a in COMMANDS]
    names = set(a for a in args if a not in COMMANDS)
    if steps == ['all']:
        steps = ['fixheaders', 'verify', 'stamp', 'site']
    project = Project(rebuild='--nobuild' not in sys.argv)   # --nobuild: use the ROM the last build made
    print(project.build_msg)
    if not project.build_ok:
        sys.exit(1)
    for w in project.parsed.warnings:
        print('warning: ' + w)
    for step in steps:
        if step == 'show':
            show(project, sorted(names))
        elif step == 'fixheaders':
            def source():
                return ''.join(open(os.path.join(build.SRC, f), encoding='utf-8').read() for f in build.asm_files())
            before = source()
            fix_headers(project)
            if source() != before:
                project = Project()           # headers changed: parse again
                print(project.build_msg)
        elif step == 'page':                  # only rewrite the viewer, with the last verify's results
            import pickle
            import site_gen
            saved = os.path.join(build.OUT, 'verify_results.pickle')
            if os.path.exists(saved):
                project.results, project.verify_seconds = pickle.load(open(saved, 'rb'))
            else:
                project.verify(None)
                pickle.dump((project.results, project.verify_seconds), open(saved, 'wb'))
            print('viewer: ' + site_gen.generate(project))
        elif step in ('verify', 'stamp', 'site'):
            if project.results is None:
                project.verify(names or None)
                import pickle
                saved = os.path.join(build.OUT, 'verify_results.pickle')
                if not names:
                    pickle.dump((project.results, project.verify_seconds), open(saved, 'wb'))
                elif os.path.exists(saved):
                    # a partial run: merge these units' results into the saved ones (for `page`)
                    try:
                        old, secs = pickle.load(open(saved, 'rb'))
                        old.update({n: r for n, r in project.results.items() if n in names})
                        pickle.dump((old, secs), open(saved, 'wb'))
                    except Exception:
                        pass
                # with names: those units in full; otherwise only the ones with problems
                print_report(project, names or None, problems_only=not names and 'verify' not in args)
            if step == 'stamp':
                stamp(project)
            elif step == 'site':
                import site_gen
                print('viewer: ' + site_gen.generate(project))
    if strict and project.results is not None:
        bad = [n for n, r in project.results.items() if r.status in ('failing', 'stale')]
        if bad:
            sys.exit('{} failing or stale: {}'.format(len(bad), ', '.join(bad[:10])))


COMMANDS = ('all', 'build', 'show', 'fixheaders', 'verify', 'stamp', 'site', 'page')


if __name__ == '__main__':
    main()
