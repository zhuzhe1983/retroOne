#!/usr/bin/env python3
"""Atomically publish an audited artwork batch, without ROM/DB/save changes.

Can run repeatedly while the resumable downloader is still adding completed
files. Each PNG requires a complete source record and matching SHA256. Existing
cache entries win; new local overrides merge rather than erase another batch.
"""
from __future__ import annotations
import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.profiles import atomic_json


def publish(source, destination, local_manifest=None):
    source, destination = source.resolve(), destination.resolve()
    if source == destination: raise ValueError('staging and published caches must differ')
    destination.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    for image in source.glob('*.png'):
        key = image.stem
        if not re.fullmatch('[a-f0-9]{64}', key) or image.is_symlink(): counts['invalid_name'] += 1; continue
        metadata_path = image.with_suffix('.json')
        if not metadata_path.is_file() or metadata_path.is_symlink(): counts['not_complete'] += 1; continue
        descriptor = os.open(destination / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a+b') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: counts['busy_retry'] += 1; continue
            target = destination / image.name
            if target.exists(): counts['existing'] += 1; continue
            try:
                if metadata_path.stat().st_size > 64 * 1024 or image.stat().st_size > 4 * 1024 * 1024: raise ValueError('bounded file size')
                metadata = json.loads(metadata_path.read_text())
                url = urlparse(metadata['source'])
                if url.scheme != 'https' or url.hostname != 'thumbnails.libretro.com': raise ValueError('untrusted source')
                data = image.read_bytes()
                if not data.startswith(b'\x89PNG\r\n\x1a\n') or hashlib.sha256(data).hexdigest() != metadata['sha256']: raise ValueError('content hash mismatch')
                temporary = destination / ('.' + image.name + '.' + uuid4().hex + '.tmp')
                try:
                    try: os.link(image, temporary)
                    except OSError: shutil.copyfile(image, temporary)
                    os.replace(temporary, target)
                finally: temporary.unlink(missing_ok=True)
                atomic_json(target.with_suffix('.json'), metadata)
                target.with_suffix('.miss').unlink(missing_ok=True)
                counts['published'] += 1
            except (ValueError, KeyError, OSError): counts['invalid'] += 1
    if local_manifest:
        document = json.loads(local_manifest.read_text())
        if document.get('version') != 1 or not isinstance(document.get('games'), dict): raise ValueError('invalid local manifest')
        if not all(game.get('local_validation', {}).get('valid') for game in document['games'].values()): raise ValueError('local files require completed validation first')
        with (destination / 'local-overrides.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            target = destination / 'local-overrides.json'
            if target.is_symlink(): raise ValueError('unsafe local manifest')
            current = json.loads(target.read_text()) if target.is_file() else {'version': 1, 'games': {}}
            if current.get('version') != 1 or not isinstance(current.get('games'), dict): raise ValueError('existing local manifest is invalid; preserving it')
            counts['local_added'] = len(document['games'].keys() - current['games'].keys())
            current['games'].update(document['games'])
            atomic_json(target, current)
    return dict(counts)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--local-manifest', type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.source, args.destination, args.local_manifest)))
