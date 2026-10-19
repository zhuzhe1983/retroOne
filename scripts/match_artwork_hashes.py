#!/usr/bin/env python3
"""Resolve renamed cartridge ROMs with official No-Intro hashes, read-only.

ZIP central CRC+size is only a shortlist. Every final match requires a full
streamed SHA1 comparison to the official DAT, with bounded bytes and I/O rate.
The resumable output is isolated from the live catalog and save directories.
"""
from __future__ import annotations

import argparse
from collections import defaultdict, Counter
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import quote
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.artwork import REPOS
from retroone.artwork_match import NameIndex
from retroone.profiles import atomic_json

MAX_ROM_BYTES = 64 * 1024 * 1024
EXTENSIONS = {'gba': {'.gba', '.bin'}, 'atari2600': {'.a26', '.bin', '.rom'}}


def dat_entries(text):
    result = defaultdict(list)
    for block in re.findall(r'^game\s*\(\s*(.*?)^\)', text, re.M | re.S):
        name = re.search(r'^\s*name\s+("(?:\\.|[^"\\])*")', block, re.M)
        rom = re.search(r'rom\s*\(.*?\bsize\s+(\d+)\s+crc\s+([A-Fa-f0-9]{8})\s+md5\s+[A-Fa-f0-9]+\s+sha1\s+([A-Fa-f0-9]{40})', block)
        if name and rom:
            result[(int(rom[1]), int(rom[2], 16))].append({'title': json.loads(name[1]), 'sha1': rom[3].lower()})
    if not result: raise ValueError('No usable official DAT records')
    return result


def archive_member(path, extensions):
    # Inspect EOCD bounds before ZipFile can allocate its central-directory list.
    if path.stat().st_size > MAX_ROM_BYTES: raise ValueError('archive larger than cartridge inspection bound')
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 65557)); tail = stream.read(65557)
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or len(tail) - position < 22: raise ValueError('missing ZIP directory')
    values = struct.unpack('<4s4H2LH', tail[position:position + 22])
    if values[3] != values[4] or values[4] > 128 or values[5] > 128 * 1024:
        raise ValueError('unsupported or oversized ZIP directory')
    archive = zipfile.ZipFile(path)
    members = [info for info in archive.infolist() if Path(info.filename).suffix.lower() in extensions and not info.is_dir()]
    if len(members) != 1:
        archive.close(); raise ValueError('requires exactly one cartridge ROM')
    member = members[0]
    if member.flag_bits & 1 or member.file_size > MAX_ROM_BYTES:
        archive.close(); raise ValueError('encrypted or oversized ROM')
    return archive, member


def match(manifest, dat_root, index_root, rom_root, output, rate):
    os.nice(10)
    output.mkdir(parents=True, exist_ok=True)
    records_root = output / 'records'; records_root.mkdir(exist_ok=True)
    databases = {platform: dat_entries((dat_root / (platform + '.dat')).read_text()) for platform in EXTENSIONS}
    indexes = {platform: NameIndex(json.loads((index_root / (platform + '.json')).read_text())['names']) for platform in EXTENSIONS}
    targets = [game for game in manifest['games'].values() if game['platform'] in EXTENSIONS and game['status'] == 'no_deterministic_match']
    counts, entries = Counter(), {}
    for game in targets:
        record = records_root / (game['id'] + '.json')
        if record.is_file():
            result = json.loads(record.read_text())
        else:
            result = dict(game)
            started = time.monotonic()
            bytes_read = 0
            try:
                path = (rom_root / Path(game['rom_path']).relative_to('/roms')).resolve()
                path.relative_to(rom_root.resolve() / 'library' / game['platform'])
                if path.suffix.lower() != '.zip': raise ValueError('only ZIP cartridge input is currently supported')
                archive, member = archive_member(path, EXTENSIONS[game['platform']])
                with archive:
                    candidates = databases[game['platform']].get((member.file_size, member.CRC), [])
                    if not candidates:
                        result.update(hash_status='no_crc_size_match')
                    else:
                        digest = hashlib.sha1()
                        with archive.open(member) as source:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk: break
                                bytes_read += len(chunk)
                                if bytes_read > MAX_ROM_BYTES: raise ValueError('unpacked ROM exceeds bound')
                                digest.update(chunk)
                                delay = bytes_read / rate - (time.monotonic() - started)
                                if delay > 0: time.sleep(delay)
                        matching = [item for item in candidates if item['sha1'] == digest.hexdigest()]
                        if matching:
                            matched = indexes[game['platform']].match([item['title'] for item in matching])
                            if matched:
                                url = 'https://thumbnails.libretro.com/' + quote(REPOS[game['platform']].replace('_', ' '), safe='') + '/Named_Boxarts/' + quote(matched['matched_name'] + '.png', safe='')
                                result.update(matched, status='download', hash_status='sha1_verified', method='official-rom-sha1',
                                              original_name=game['title'], canonical_rom_title=matched['original_name'],
                                              content_sha1=digest.hexdigest(), content_crc32='%08X' % member.CRC,
                                              content_size=member.file_size, source=url,
                                              database_source='https://github.com/libretro/libretro-database/tree/master/metadat/no-intro')
                            else: result.update(hash_status='sha1_known_no_cover', canonical_rom_title=matching[0]['title'])
                        else: result.update(hash_status='crc_candidate_sha1_mismatch')
            except Exception as error:
                result.update(hash_status='inspection_error', hash_error=str(error)[:400])
            result['checked_at'] = time.time()
            atomic_json(record, result)
        counts[result['hash_status']] += 1
        if result.get('hash_status') == 'sha1_verified': entries[game['id']] = result
        if sum(counts.values()) % 100 == 0:
            atomic_json(output / 'status.json', {'total': len(targets), 'checked': sum(counts.values()), 'counts': dict(counts), 'updated_at': time.time()})
            print(json.dumps(dict(counts)), flush=True)
    atomic_json(output / 'manifest.json', {'version': 1, 'games': entries, 'counts': dict(counts)})
    atomic_json(output / 'local-overrides.json', {'version': 1, 'games': {}})
    atomic_json(output / 'status.json', {'total': len(targets), 'checked': sum(counts.values()), 'counts': dict(counts), 'complete': True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('manifest', 'dat-root', 'index-root', 'rom-root', 'output'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--mib-per-second', type=float, default=16)
    args = parser.parse_args()
    if not 1 <= args.mib_per_second <= 64: parser.error('I/O rate must be 1..64 MiB/sec')
    match(json.loads(args.manifest.read_text()), args.dat_root, args.index_root, args.rom_root, args.output, args.mib_per_second * 1024 * 1024)
