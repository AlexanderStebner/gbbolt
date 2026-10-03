"""Replace whole units in the disassembly with annotated versions.

    python tools/apply.py block.asm [more.asm ...]

Each file holds one or more units: optional `;@` header lines, then the unit's
label line, then its body. Every unit replaces the source of the unit with the
same label (from its header/label down to the next unit). The ROM is rebuilt;
if it no longer matches, all changes are reverted.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asmparse  # noqa: E402
import build  # noqa: E402

LABEL = re.compile(r'^([A-Za-z_]\w*)(::?)\s*$')


def split_units(text):
    units, cur, name = [], [], None
    for line in text.rstrip('\n').split('\n'):
        m = LABEL.match(line)
        if m and asmparse.is_unit_label(m.group(1), m.group(2)):
            if name is not None:
                # header lines at the end of the previous chunk belong to this unit
                k = len(cur)
                while k and cur[k - 1].startswith(';@'):
                    k -= 1
                units.append((name, cur[:k]))
                cur = cur[k:]
            name = m.group(1)
        cur.append(line)
    if name is not None:
        units.append((name, cur))
    return units


def unit_span(lines, name):
    """(start, end) line indexes of a unit in the source, header included."""
    start = next(i for i, l in enumerate(lines) if LABEL.match(l) and LABEL.match(l).group(1) == name)
    s = start
    while s and lines[s - 1].startswith(';@'):
        s -= 1
    e = start + 1
    while e < len(lines):
        m = LABEL.match(lines[e])
        if m and asmparse.is_unit_label(m.group(1), m.group(2)):
            break
        e += 1
    while e > start + 1 and lines[e - 1].startswith(';@'):
        e -= 1           # next unit's header
    while e > start + 1 and not lines[e - 1].strip():
        e -= 1           # keep the blank separator lines
    return s, e


def block_file(text):
    """The source file holding the units of a block (the file of the first one that exists)."""
    for name, _ in split_units(re.sub(r'^;! .*\n', '', text, flags=re.M)):
        try:
            return build.file_of_label(name)
        except KeyError:
            continue
    sys.exit('none of the units in the block exists yet')


def main():
    originals, done = {}, []
    for f in sys.argv[1:]:
        text = open(f, encoding='utf-8').read()
        path = block_file(text)
        if path not in originals:
            originals[path] = open(path, encoding='utf-8').read()
        lines = open(path, encoding='utf-8').read().split('\n')
        # `;! absorb Label` removes another unit whose code the new version takes over
        for name in re.findall(r'^;! absorb (\w+)\s*$', text, re.M):
            s, e = unit_span(lines, name)
            del lines[s:e]
        text = re.sub(r'^;! .*\n', '', text, flags=re.M)
        last_end = None
        for name, body in split_units(text):
            while body and not body[-1].strip():
                body.pop()
            if any(LABEL.match(l) and LABEL.match(l).group(1) == name for l in lines):
                s, e = unit_span(lines, name)
            elif last_end is not None:
                # a new label splitting off part of the previous unit: insert after it
                s = e = last_end
                body = [''] + body
            else:
                sys.exit('{}: label not found and no previous unit to insert after'.format(name))
            lines[s:e] = body
            last_end = s + len(body)
            done.append(name)
        open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
    ok, msg = build.build()
    if not ok:
        for path, text in originals.items():
            open(path, 'w', encoding='utf-8', newline='\n').write(text)
        sys.exit('reverted - ' + msg)
    print('applied {} unit(s): {}\n{}'.format(len(done), ', '.join(done), msg))


if __name__ == '__main__':
    main()
