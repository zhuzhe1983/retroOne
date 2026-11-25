#!/usr/bin/env python3
"""Derive only ROM-set identifiers and CRC/size signatures; never reads game files."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

SOURCE = 'https://raw.githubusercontent.com/libretro/FBNeo/2e6605c4a08b86455e3aee03294fe35e7618fa91/dats/FinalBurn%20Neo%20%28ClrMame%20Pro%20XML%2C%20Arcade%20only%29.dat'
EXPECTED_SHA256 = '0031b6557d493bc52d601e25b640e8a2d830bd6c78d4f3fff227463d2c62ae88'


def build(path):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED_SHA256:
        raise ValueError('Unreviewed DAT revision; review and update the pinned digest first')
    root = ET.fromstring(data)
    sets = {}
    for game in root.findall('game'):
        name = game.get('name')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name or ''):
            continue
        roms = game.findall('rom')
        if not roms or any(not r.get('crc') or not r.get('size') for r in roms):
            continue
        members = sorted(f'{r.get("crc").lower()}:{int(r.get("size"))}' for r in roms)
        signature = hashlib.sha256(('\n'.join(members)+'\n').encode()).hexdigest()
        sets.setdefault(signature, []).append([name, game.get('romof', '')])
    return {'source': SOURCE, 'source_sha256': EXPECTED_SHA256,
            'version': root.findtext('header/version'), 'sets': sets}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('dat', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    a.output.write_text(json.dumps(build(a.dat), ensure_ascii=True, sort_keys=True,
                                  separators=(',', ':'))+'\n')
