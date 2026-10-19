#!/usr/bin/env python3
"""Validate a staged local-art manifest against a read-only ROM tree (Python 3.8)."""
import argparse
import hashlib
import json
from pathlib import Path
import time


def inspect(manifest, rom_root):
    checked, valid, errors = {}, {}, {}
    rom_root = rom_root.resolve()
    for game_id, entry in manifest['games'].items():
        source = entry['source_path']
        if source not in checked:
            try:
                path = (rom_root / Path(source).relative_to('/roms')).resolve()
                path.relative_to(rom_root / 'library' / entry['platform'])
                if path.stat().st_size > 32 * 1024 * 1024: raise ValueError('local cover exceeds 32 MiB inspection bound')
                with path.open('rb') as stream:
                    header = stream.read(32)
                    image = header.startswith(b'\x89PNG\r\n\x1a\n') or header.startswith(b'\xff\xd8\xff') or header.startswith(b'RIFF') and header[8:12] == b'WEBP'
                    if not image: raise ValueError('unrecognized image signature')
                    digest = hashlib.sha256(header)
                    for chunk in iter(lambda: stream.read(256 * 1024), b''): digest.update(chunk)
                checked[source] = {'valid': True, 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}
            except Exception as error:
                checked[source] = {'valid': False, 'error': str(error)}
        if checked[source]['valid']:
            valid[game_id] = dict(entry, local_validation=checked[source])
        else:
            errors[game_id] = dict(entry, local_validation=checked[source])
    return {'version': 1, 'created_at': time.time(), 'games': valid}, {'checked_sources': len(checked), 'valid_games': len(valid), 'error_games': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--rom-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    validated, report = inspect(json.loads(args.manifest.read_text()), args.rom_root)
    args.output.write_text(json.dumps(validated, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))
