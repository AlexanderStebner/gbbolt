"""Give units a virtual folder (`;@ path: folder/sub`).

    python tools/setpath.py game/piece SpawnNextPiece LockPiece ...
    python tools/setpath.py --file mapping.txt      # lines "folder: Name Name ..."

The `;@ path:` line goes right after the `;@ def` line (or replaces the old one).
A unit without any `;@` header gets a header of just the path. Only comment lines
change; the ROM is rebuilt and checked.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402


def set_paths(mapping):
    by_file = {}
    for name, folder in mapping.items():
        try:
            by_file.setdefault(build.file_of_label(name), {})[name] = folder
        except KeyError:
            sys.exit('no unit: ' + name)
    backups = {p: open(p, encoding='utf-8').read() for p in by_file}
    for path, file_mapping in by_file.items():
        set_paths_in(path, file_mapping)
    ok, msg = build.build()
    if not ok:
        for p, text in backups.items():
            open(p, 'w', encoding='utf-8', newline='\n').write(text)
        sys.exit('reverted - ' + msg)
    print('{} path(s) set; {}'.format(len(mapping), msg))


def set_paths_in(path, mapping):
    lines = open(path, encoding='utf-8').read().split('\n')
    index = {}
    for i, l in enumerate(lines):
        m = re.match(r'^([A-Za-z_]\w*)::\s*$', l)
        if m:
            index[m.group(1)] = i
    missing = [n for n in mapping if n not in index]
    if missing:
        sys.exit('no unit: ' + ', '.join(missing))
    for name in sorted(mapping, key=lambda n: -index[n]):     # bottom up: indices stay valid
        i = index[name]
        top = i
        while top > 0 and lines[top - 1].startswith(';@'):
            top -= 1
        header = list(range(top, i))
        for k in reversed(header):
            if lines[k].startswith(';@ path:'):
                del lines[k]
                i -= 1
        header = list(range(top, i))
        defline = next((k for k in header if lines[k].startswith(';@ def ')), None)
        lines.insert(defline + 1 if defline is not None else i, ';@ path: ' + mapping[name])
    open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))


def main():
    mapping = {}
    if sys.argv[1] == '--file':
        for line in open(sys.argv[2], encoding='utf-8'):
            if ':' in line and not line.startswith('#'):
                folder, names = line.split(':', 1)
                for n in names.split():
                    mapping[n] = folder.strip()
    else:
        for n in sys.argv[2:]:
            mapping[n] = sys.argv[1]
    set_paths(mapping)


if __name__ == '__main__':
    main()
