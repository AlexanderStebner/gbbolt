"""Set the type/description of a variable in src/ram.inc.

    python tools/describe.py hLevel u8 "Current level (binary, 0-20)"
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import SRC  # noqa: E402

path = os.path.join(SRC, 'ram.inc')
name, vtype, desc = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ''
text = open(path, encoding='utf-8').read()
pat = re.compile(r'^(DEF {} EQU \S+)(?:\s*;@.*)?$'.format(re.escape(name)), re.M)
if not pat.search(text):
    sys.exit('{} not found in ram.inc'.format(name))
text = pat.sub(lambda m: '{} ;@ {} {}'.format(m.group(1), vtype, desc).rstrip(), text)
open(path, 'w', encoding='utf-8', newline='\n').write(text)
print('{}: {} {}'.format(name, vtype, desc))
