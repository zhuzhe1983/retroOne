#!/usr/bin/env python3
"""Read-only NSP/XCI content comparison. No keys, decryption or ROM rewriting.

Accepts a private catalogue snapshot (``games`` list) and optional candidate IDs.
Only byte-for-byte identical, fully hashed NCA sets are equivalent. Container
names, title IDs and the hashes embedded in filenames are never proof alone.
Tickets/certificates are recorded as packaging, never included in public output.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import time


def table(handle, offset, magic, width, bound):
    if offset < 0 or offset + 16 > bound:
        raise ValueError('Container header outside file')
    handle.seek(offset)
    header = handle.read(16)
    if len(header) != 16 or header[:4] != magic:
        raise ValueError('Invalid container header')
    count, length, _ = struct.unpack('<III', header[4:])
    if not 0 < count < 4096 or length > 4194304:
        raise ValueError('Container table exceeds limits')
    base = offset + 16 + count * width + length
    if base > bound:
        raise ValueError('Truncated container table')
    entries, names = handle.read(count * width), handle.read(length)
    result, seen, ranges = [], set(), []
    for index in range(count):
        pos, size, namepos = struct.unpack_from('<QQI', entries, index * width)
        if namepos >= length or base + pos + size > bound:
            raise ValueError('Container entry outside file')
        end = names.find(b'\0', namepos)
        if end < 0:
            raise ValueError('Unterminated entry name')
        name = names[namepos:end].decode('utf-8')
        if not name or '/' in name or '\\' in name or name in ('.', '..') or name in seen:
            raise ValueError('Unsafe or duplicate entry name')
        seen.add(name)
        ranges.append((base + pos, base + pos + size))
        result.append((name, base + pos, size))
    ranges.sort()
    if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
        raise ValueError('Overlapping container entries')
    return result


def entries(handle, extension, size):
    if extension == '.nsp':
        return table(handle, 0, b'PFS0', 24, size)
    if extension != '.xci':
        raise ValueError('Only NSP/XCI packages are supported')
    handle.seek(0x100)
    if handle.read(4) != b'HEAD':
        raise ValueError('Invalid XCI header')
    handle.seek(0x130)
    data = handle.read(8)
    if len(data) != 8:
        raise ValueError('Truncated XCI header')
    partitions = table(handle, struct.unpack('<Q', data)[0], b'HFS0', 64, size)
    secure = [p for p in partitions if p[0] == 'secure']
    if len(secure) != 1:
        raise ValueError('Missing secure partition')
    _, offset, length = secure[0]
    return table(handle, offset, b'HFS0', 64, offset + length)


def identity(path):
    info = path.stat()
    return {key: getattr(info, 'st_' + key) for key in ('size', 'mtime_ns', 'ctime_ns', 'ino')}


def manifest(path, hash_content=False):
    before = identity(path)
    content, auxiliary = [], []
    with path.open('rb') as handle:
        for name, offset, size in entries(handle, path.suffix.lower(), before['size']):
            if not name.lower().endswith('.nca'):
                auxiliary.append({'name': name, 'size': size})
                continue
            if size < 1024:
                raise ValueError('Truncated NCA')
            item = {'name': name, 'size': size, 'offset': offset}
            if hash_content:
                digest, remaining = hashlib.sha256(), size
                handle.seek(offset)
                while remaining:
                    chunk = handle.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Truncated NCA during hashing')
                    digest.update(chunk)
                    remaining -= len(chunk)
                item['sha256'] = digest.hexdigest()
            content.append(item)
    if not content:
        raise ValueError('No NCA content')
    if identity(path) != before:
        raise ValueError('Source changed during audit')
    result = {'identity': before, 'ncas': content, 'auxiliary': auxiliary}
    if hash_content:
        canonical = sorted((item['sha256'], item['size']) for item in content)
        result['fingerprint'] = hashlib.sha256(json.dumps(canonical, separators=(',', ':')).encode()).hexdigest()
    return result


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--ids', type=Path, help='JSON array of candidate game IDs')
    parser.add_argument('--hash', action='store_true', help='Read every NCA byte; can take hours')
    args = parser.parse_args()
    games = json.loads(args.catalog.read_text())['games']
    allowed = set(json.loads(args.ids.read_text())) if args.ids else None
    games = [g for g in games if g['platform'] == 'switch' and (allowed is None or g['id'] in allowed)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(args.output.read_text()).get('games', {}) if args.output.exists() else {}
    report = {'schema_version': 1, 'read_only': True, 'generated_at': time.time(), 'complete': False, 'games': {}}
    for index, game in enumerate(games):
        path = Path(game['rom_path'])
        record = {'id': game['id'], 'title': game['title'], 'platform': 'switch'}
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(args.root.resolve()):
                raise ValueError('ROM outside configured root')
            record['relative_path'] = path.relative_to(args.root).as_posix()
            cached = previous.get(game['id'], {})
            if cached.get('identity') == identity(path) and (not args.hash or cached.get('fingerprint')):
                record.update(cached)
            else:
                record.update(manifest(path, args.hash))
        except (OSError, ValueError, KeyError, UnicodeError, struct.error) as error:
            record['error'] = str(error)[:300]
        report['games'][game['id']] = record
        save(args.output, report)
        print(json.dumps({'checked': index + 1, 'total': len(games), 'id': game['id'], 'hashed': bool(record.get('fingerprint')), 'error': record.get('error') or None}), flush=True)
    buckets = {}
    for game in report['games'].values():
        if game.get('fingerprint'):
            buckets.setdefault(game['fingerprint'], []).append(game['id'])
    report['identical_nca_sets'] = [dict(sha256=key, game_ids=value) for key, value in buckets.items() if len(value) > 1]
    report['complete'] = True
    save(args.output, report)


if __name__ == '__main__':
    main()
