"""Decode the song data of the Tetris sound engine into labelled structures.

    python tools/songdata.py

Format (this engine): the song table holds a pointer per song to an 11-byte
header: flags, a pointer to the note length table, and a pattern list pointer per
channel (0 = channel unused). A pattern list is a list of pattern pointers; the engine only
looks at the high byte of each entry: $00xx ends the song, $FFxx is followed by
the address to continue at (loop). Short jingles have no end marker at all: their
lists run on into the next channel's list (and further into whatever bytes
follow) until such a byte turns up - the decoded list stops where other data
starts and says so.
A pattern is a byte stream ending in $00: $9D + 3 bytes sets the instrument, $Ax
sets the note length, $01 is a rest, other bytes are notes.

The region from the first song header up to the next unit after the song data is
rewritten as `SongNNHeader` / `SongNNSq1` ... / `SongNNPatK` with `dw` pointers.
Bytes no structure claims stay as `db`. The ROM is rebuilt; if it differs,
nothing changes. Settings (song table label, song count, names) come from
src/sound.json: "song_data": {"table": ..., "count": ...}.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402

CH = ['Sq1', 'Sq2', 'Wave', 'Noise']


def main():
    ok, msg = build.build()
    if not ok:
        sys.exit(msg)
    rom = open(build.BUILT, 'rb').read()
    syms = build.read_sym()
    cfg = json.load(open(os.path.join(build.SRC, 'sound.json'), encoding='utf-8'))
    sd = cfg['song_data']
    # pattern commands and their length in bytes (default: the Tetris engine's $9D instrument + 3 bytes)
    cmd_len = {int(k, 16): v for k, v in sd.get('commands', {'9D': 4}).items() if not k.startswith('_')}
    names = cfg.get('names', {})
    w = lambda a: rom[a] | rom[a + 1] << 8   # noqa: E731
    table, count = syms[sd['table']], sd['count']

    labels = {}          # addr -> name
    structs = {}         # addr -> (kind, size, song)
    order = []

    def claim(addr, name, kind, size, song):
        if addr not in labels:
            labels[addr] = name
            structs[addr] = (kind, size, song)
            order.append(addr)

    end = syms[sd.get('region_end', 'UpdateSound')]
    heads = [w(table + 2 * (s - 1)) for s in range(1, count + 1)]
    start = min(heads)
    list_starts = {}
    for s, h in enumerate(heads, 1):
        claim(h, 'Song{:02X}Header'.format(s), 'header', 11, s)
        for c in range(4):
            lst = w(h + 3 + 2 * c)
            if lst:
                list_starts.setdefault(lst, (s, c))
    # patterns named by lists that end properly: a list without an end marker that runs
    # into one of these stops there (its bytes are that pattern, not more entries)
    known_patterns = set()
    for lst in list_starts:
        a, entries = lst, []
        while a not in list_starts or a == lst:
            p = w(a)
            if p >> 8 in (0, 0xFF):
                known_patterns.update(entries)
                break
            if not start <= p < end:
                break
            entries.append(p)
            a += 2
    runs_on = {}                                         # list -> the label it runs into
    for s, h in enumerate(heads, 1):
        pat_no = sum(1 for a in structs if structs[a][0] == 'pattern' and structs[a][2] == s)
        for c in range(4):
            lst = w(h + 3 + 2 * c)
            if not lst or lst in structs:
                continue
            claim(lst, 'Song{:02X}{}'.format(s, CH[c]), 'list', None, s)
            a = lst
            while True:
                if a != lst and (a in list_starts or a in structs or a in known_patterns):
                    runs_on[lst] = a                     # no end marker: continues into the next list
                    break
                p = w(a)
                if p >> 8 == 0:
                    a += 2
                    break
                if p >> 8 == 0xFF:
                    target = w(a + 2)
                    if target not in labels:
                        labels[target] = 'Song{:02X}{}Loop'.format(s, CH[c])
                    a += 4
                    break
                if not start <= p < end:                 # not a pattern: other data follows
                    runs_on[lst] = None
                    break
                if p not in labels:
                    pat_no += 1
                    claim(p, 'Song{:02X}Pat{}'.format(s, pat_no), 'pattern', None, s)
                a += 2
            structs[lst] = ('list', a - lst, s)
    # pattern sizes: up to and including the $00 that ends them; a pattern that
    # starts inside another one becomes a label inside it
    inner = {}
    others = {x for x in structs if structs[x][0] != 'pattern'}
    for a in sorted(x for x in structs if structs[x][0] == 'pattern'):
        p = a
        while rom[p] != 0 and p not in others:
            p += cmd_len.get(rom[p], 1)
        if p in others:                                  # no $00: the bytes after it are another list
            runs_on[a] = p
            structs[a] = ('pattern', p - a, structs[a][2])
        else:
            structs[a] = ('pattern', p + 1 - a, structs[a][2])
    pats = sorted(x for x in structs if structs[x][0] == 'pattern')
    for a in pats:
        for b in pats:
            if b < a < b + structs[b][1] and b in structs:
                inner[a] = b
                break
    for a in inner:
        del structs[a]
    covered = {}
    for a, (kind, size, s) in structs.items():
        for b in range(a, a + size):
            if b in covered and covered[b] != a:
                sys.exit('structures overlap at ${:04X}: {} ${:04X} and {} ${:04X}'.format(b, structs[covered[b]][0], covered[b], kind, a))
            covered[b] = a
    if max(covered) >= end:
        sys.exit('song data runs past the region end')
    name_of = {a: n for n, a in syms.items() if '.' not in n}
    name_of.update(labels)

    def ref(a):
        return name_of.get(a, '${:04x}'.format(a))

    out = ['; Song data of the sound engine (decoded by tools/songdata.py): per song a header,',
           '; a pattern list per channel and the patterns. Lists end with dw $0000 (song over)',
           '; or dw $FFFF, Target (continue there - the loop). Pattern bytes: $9D + 3 = instrument,',
           '; $Ax = note length, $01 = rest, $00 = end, others = notes.']
    a = start
    while a < end:
        if a in labels and a not in structs:          # a loop target inside a list
            pass
        if a in structs:
            kind, size, s = structs[a]
            out.append('')
            if kind == 'header':
                nm = names.get('music {}'.format(s))
                out.append('; Song ${:02X}{}'.format(s, ': ' + nm[0] if nm else ''))
            if sd.get('path'):
                out.append(';@ path: ' + sd['path'])
            out.append(labels[a] + '::')
            if kind == 'header':
                out.append('\tdb ${:02x}'.format(rom[a]))
                out.append('\tdw ' + ref(w(a + 1)))
                out.append('\tdw ' + ', '.join(ref(w(a + 3 + 2 * c)) if w(a + 3 + 2 * c) else '$0000' for c in range(4)))
            elif kind == 'list':
                b, words = a, []
                if a in runs_on:
                    nxt = runs_on[a]
                    out.append('; (no end marker: the engine reads on into {})'.format(labels.get(nxt, 'the bytes after it') if nxt else 'the bytes after it'))
                while b < a + size:
                    if b != a and b in labels:          # loop target label inside the list
                        if words:
                            out.append('\tdw ' + ', '.join(words))
                            words = []
                        out.append(labels[b] + ':')
                    p = w(b)
                    if p == 0xFFFF:
                        words += ['$FFFF', ref(w(b + 2))]
                        b += 4
                        continue
                    words.append(ref(p) if p else '$0000')
                    b += 2
                out.append('\tdw ' + ', '.join(words))
            else:
                if a in runs_on:
                    out.append('; (no $00 at the end: the engine reads on into {})'.format(labels.get(runs_on[a], 'the next bytes')))
                cuts = sorted([a] + [x for x in inner if a < x < a + size] + [a + size])
                for k in range(len(cuts) - 1):
                    if k:
                        out.append(labels[cuts[k]] + ':')     # another pattern starts here
                    data = rom[cuts[k]:cuts[k + 1]]
                    for j in range(0, len(data), 16):
                        out.append('\tdb ' + ', '.join('${:02x}'.format(x) for x in data[j:j + 16]))
            a += size
        else:                                            # bytes nothing refers to
            b = a
            while b < end and b not in structs:
                b += 1
            data = rom[a:b]
            out.append('')
            out.append('; (not referenced by any song)')
            for k in range(0, len(data), 16):
                out.append('\tdb ' + ', '.join('${:02x}'.format(x) for x in data[k:k + 16]))
            a = b
    out.append('')

    PATH = build.file_of_label(sd.get('region_end', 'UpdateSound'))
    lines = open(PATH, encoding='utf-8').read().split('\n')
    backup = '\n'.join(lines)
    first = syms_line = next(i for i, l in enumerate(lines) if l.strip().endswith('::') and syms.get(l.strip()[:-2]) == start)
    while first > 0 and lines[first - 1].startswith(';'):
        first -= 1                                       # its comment and header lines go too
    k = first - 1                                        # and the explanation block of an earlier run
    while k > 0 and not lines[k].strip():
        k -= 1
    if lines[k].startswith('; ') and any(l.startswith('; Song data of the sound engine') for l in lines[max(0, k - 6):k + 1]):
        while k > 0 and lines[k - 1].startswith('; '):
            k -= 1
        first = k
    last = next(i for i, l in enumerate(lines) if l.strip() == sd.get('region_end', 'UpdateSound') + '::')
    while lines[last - 1].startswith(';@'):
        last -= 1
    del syms_line
    lines[first:last] = out
    open(PATH, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
    ok, msg = build.build()
    if not ok:
        open(PATH, 'w', encoding='utf-8', newline='\n').write(backup)
        sys.exit('reverted - ' + msg)
    print('{} songs, {} structures, {} labels; {}'.format(count, len(structs), len(labels), msg))


if __name__ == '__main__':
    main()
