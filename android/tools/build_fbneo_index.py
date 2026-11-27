#!/usr/bin/env python3
"""Build bounded ROM-set metadata from pinned DAT/source; never reads game/BIOS bytes."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

COMMIT = '2e6605c4a08b86455e3aee03294fe35e7618fa91'
SOURCE = f'https://raw.githubusercontent.com/libretro/FBNeo/{COMMIT}/dats/FinalBurn%20Neo%20%28ClrMame%20Pro%20XML%2C%20Arcade%20only%29.dat'
EXPECTED_SHA256 = '0031b6557d493bc52d601e25b640e8a2d830bd6c78d4f3fff227463d2c62ae88'
BIOS_SOURCE = f'https://raw.githubusercontent.com/libretro/FBNeo/{COMMIT}/src/burn/drv/neogeo/d_neogeo.cpp'
BIOS_SOURCE_SHA256 = '7e51b413aab7da1965ba2d003d0b76fd7e5dfac5a9f82ea356fc74b0680602b7'
MATCHER_VERSION = 'fbneo-romsets-v2'
# Reviewed non-BRF_OPT rows in the pinned neogeoRomDesc, not all 38 alternatives.
REQUIRED_BIOS = {
    ('sp-s3.sp1', '91b64be3', 131072),
    ('sm1.sm1', '94416d67', 131072),
    ('sfix.sfix', 'c2ea0cfd', 131072),
    ('000-lo.lo', '5a86cff2', 131072),
}


def signature(members):
    rows = sorted(f'{crc}:{size}' for _, crc, size in members)
    return hashlib.sha256(('\n'.join(rows) + '\n').encode()).hexdigest()


def source_bios_flags(data):
    """Read only named rows in the exact core's BIOS descriptor, not another driver."""
    if hashlib.sha256(data).hexdigest() != BIOS_SOURCE_SHA256:
        raise ValueError('Unreviewed NeoGeo source revision')
    text = data.decode('utf-8')
    block = re.search(r'neogeoRomDesc\[\]\s*=\s*\{(.*?)\n\};', text, re.S)
    if not block:
        raise ValueError('Missing neogeoRomDesc')
    rows = re.findall(r'\{\s*"([^"\n]+)"\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,([^}]+)\}', block[1])
    bios = [(name, f'{int(crc, 16):08x}', int(size, 16), 'BRF_OPT' not in flags)
            for name, size, crc, flags in rows]
    if len(bios) != 38 or {(n, c, s) for n, c, s, required in bios if required} != REQUIRED_BIOS:
        raise ValueError('Reviewed BIOS flags changed')
    return bios


def valid_leaf(name):
    return bool(name) and len(name) <= 255 and name not in ('.', '..') and not any(
        ord(c) < 32 or ord(c) > 126 or c in '/\\:' for c in name)


def derive(root, required_bios):
    """Pure derivation used by original XML fixtures as well as the pinned production build."""
    games = {}
    for game in root.findall('game'):
        name = game.get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) or name in games:
            raise ValueError('Invalid or duplicate DAT set name')
        games[name] = game
    if 'neogeo' not in games or games['neogeo'].get('isbios') != 'yes':
        raise ValueError('Missing NeoGeo BIOS definition')

    def read_members(game):
        rows = []
        seen = set()
        for rom in game.findall('rom'):
            name, crc, size = rom.get('name', ''), rom.get('crc', '').lower(), rom.get('size', '')
            if not valid_leaf(name) or name.lower() in seen:
                return None  # Do not admit sets whose DAT names cannot map unambiguously.
            seen.add(name.lower())
            if not re.fullmatch(r'[0-9a-f]{8}', crc) or not size.isdigit():
                return None  # Unknown dump/size sets never get an admission signature.
            size = int(size)
            if size < 0 or size > 512 * 1024 * 1024:
                return None
            rows.append((name, crc, size))
        return rows or None

    bios_members = read_members(games['neogeo'])
    if not bios_members or not required_bios.issubset(set(bios_members)):
        raise ValueError('Required BIOS members absent from DAT')
    known_bios = Counter((crc, size) for _, crc, size in bios_members)
    if any(v != 1 for v in known_bios.values()):
        raise ValueError('Ambiguous NeoGeo BIOS signature')
    bios_ancestors = {}

    def ancestry(name, visiting):
        if name in bios_ancestors:
            return bios_ancestors[name]
        if name not in games or name in visiting:
            raise ValueError('Missing or cyclic ROM dependency')
        game = games[name]
        found = {name} if game.get('isbios') == 'yes' else set()
        for attr in ('cloneof', 'romof'):
            dependency = game.get(attr)
            if dependency:
                found |= ancestry(dependency, visiting | {name})
        bios_ancestors[name] = found
        return found

    pool, pool_ids, catalog, signatures = [], {}, {}, {}
    for name in sorted(games):
        game = games[name]
        bios = ','.join(sorted(ancestry(name, set())))
        parent = game.get('cloneof', '')
        if not parent and game.get('romof') and games[game.get('romof')].get('isbios') != 'yes':
            parent = game.get('romof')
        rows = read_members(game)
        flags = (1 if game.get('isbios') == 'yes' else 0) | (2 if rows is None else 0)
        if rows is not None and bios == 'neogeo' and not (flags & 1):
            remaining_bios = known_bios.copy()
            content = []
            for row in rows:
                key = row[1:]
                if remaining_bios[key]:
                    remaining_bios[key] -= 1
                else:
                    content.append(row)
            if any(remaining_bios.values()):
                # DAT should expand all base BIOS alternatives; refuse an unexpected split form.
                flags |= 2
            rows = content
        member_ids = []
        # Only descendant entries need names for merged-subdirectory admission. Root signatures
        # retain every CRC/size multiplicity without duplicating base-only member metadata.
        if rows is not None and parent:
            for row in rows:
                if row not in pool_ids:
                    pool_ids[row] = len(pool)
                    pool.append(row)
                member_ids.append(pool_ids[row])
        catalog[name] = [parent, bios, flags, member_ids]
        if rows and not (flags & 2):
            signatures.setdefault(signature(rows), []).append(name)
    return {
        'schema_version': 2, 'matcher_version': MATCHER_VERSION,
        'source': SOURCE, 'source_sha256': EXPECTED_SHA256,
        'version': root.findtext('header/version'),
        'bios_source': BIOS_SOURCE, 'bios_source_sha256': BIOS_SOURCE_SHA256,
        'neogeo_bios': [[n, c, s, (n, c, s) in required_bios] for n, c, s in bios_members],
        'members': pool, 'catalog': catalog, 'signatures': signatures,
    }


def build(path, neogeo_source=None):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED_SHA256:
        raise ValueError('Unreviewed DAT revision; review and update the pinned digest first')
    result = derive(ET.fromstring(data), REQUIRED_BIOS)
    if len(result['neogeo_bios']) != 38:
        raise ValueError('Reviewed BIOS catalog changed')
    if neogeo_source is not None:
        flags = source_bios_flags(neogeo_source.read_bytes())
        if sorted(map(tuple, result['neogeo_bios'])) != sorted(flags):
            raise ValueError('DAT and pinned core BIOS flags disagree')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dat', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--neogeo-source', type=Path)
    args = parser.parse_args()
    text = json.dumps(build(args.dat, args.neogeo_source), ensure_ascii=True,
                      sort_keys=True, separators=(',', ':')) + '\n'
    if len(text.encode()) > 4 * 1024 * 1024:
        raise ValueError('Index exceeds reviewed 4 MiB APK asset bound')
    args.output.write_text(text)
