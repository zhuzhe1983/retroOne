#!/usr/bin/env python3
"""Read-only artwork audit. Python 3.8 compatible for the NAS host.

Emits JSON to stdout. It never changes the catalog, ROM tree, or player data.
Pass the actual host ROM root to translate catalog paths beginning with /roms/.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
COVER_STEMS = {'boxfront', 'box_front', 'cover', 'poster', 'front', 'box'}

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.library_layout import LibraryLayout
from retroone.artwork_match import artwork_collection, generic_artwork_name


def normalized(value):
    return unicodedata.normalize('NFC', value).casefold()


def cache_key(game):
    names = []
    for name in [Path(game['rom_path']).stem, str(game.get('title', ''))]:
        name = re.sub(r'[&*/:`<>?\\|]', '_', name).strip()
        if name and name not in names:
            names.append(name)
    return hashlib.sha256(json.dumps([game['platform'], names], ensure_ascii=False).encode()).hexdigest()


def audit(db_path, rom_root, caches):
    started = time.time()
    rom_root = rom_root.resolve()
    layout_error = None
    try:
        layout = LibraryLayout.load(rom_root)
    except (OSError, ValueError) as error:
        # A broken migration mapping must not turn into a guessed boundary.
        layout = None
        layout_error = str(error)
    db = sqlite3.connect('file:' + str(db_path) + '?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    games = [json.loads(row[0]) for row in db.execute('SELECT payload FROM games')]
    db.close()
    cache_files = {name: set(path.name for path in root.iterdir()) if root.is_dir() else set() for name, root in caches.items()}
    per_platform = collections.defaultdict(collections.Counter)
    collections_roots = {}
    records = []

    def host_path(value):
        path = Path(value)
        if not str(path).startswith('/roms/'):
            return None
        candidate = rom_root / str(path)[len('/roms/'):]
        try:
            candidate.resolve().relative_to(rom_root.resolve())
        except ValueError:
            return None
        return candidate

    for game in games:
        platform = game['platform']
        counter = per_platform[platform]; counter['total'] += 1
        key = cache_key(game)
        cover = game.get('media', {}).get('cover')
        path = host_path(cover) if cover else None
        exists = bool(path and path.is_file())
        state = 'local_valid' if exists else 'local_reference_broken' if cover else 'no_local_reference'
        counter[state] += 1
        cached_in = [name for name, files in cache_files.items() if key + '.png' in files]
        miss_in = [name for name, files in cache_files.items() if key + '.miss' in files]
        if not exists:
            counter['cached_any_player' if cached_in else 'missing_every_player'] += 1
            if miss_in: counter['negative_cache_any_player'] += 1
        rom = host_path(game['rom_path'])
        collection = None
        if rom and layout is not None:
            try:
                collection = artwork_collection(layout, rom, game)
            except (OSError, ValueError) as error:
                layout_error = str(error)
            if collection is not None:
                collections_roots[str(collection)] = platform
        record = {name: game.get(name) for name in ('id', 'title', 'platform', 'source', 'collection', 'rom_path')}
        record.update(local_cover=cover, local_state=state, cache_key=key, cached_in=cached_in, miss_in=miss_in,
                      collection_path='/roms/' + collection.relative_to(rom_root).as_posix() if collection else None)
        records.append(record)

    # Read only recognized media directories, not PC support/driver file trees.
    # Candidate matching is exact after Unicode/case normalization; never fuzzy.
    media = []
    image_keys = collections.defaultdict(list)
    media_collections = {}
    for collection_name, platform in collections_roots.items():
        media_root = Path(collection_name) / 'media'
        if media_root.is_symlink() or not media_root.is_dir():
            continue
        for directory, dirs, files in os.walk(str(media_root), followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(('.', '@')) and not (Path(directory) / name).is_symlink()]
            for filename in files:
                path = Path(directory) / filename
                if path.suffix.lower() not in IMAGE_EXTENSIONS or path.is_symlink():
                    continue
                stem = normalized(path.stem)
                if stem not in COVER_STEMS:
                    continue
                identifier = normalized(path.parent.name)
                item = {'platform': platform, 'match_name': path.parent.name,
                        'path': '/roms/' + path.relative_to(rom_root).as_posix(),
                        'collection_path': '/roms/' + Path(collection_name).relative_to(rom_root).as_posix()}
                media.append(item); image_keys[(platform, identifier)].append(item['path'])
                media_collections[item['path']] = item['collection_path']
    for game in records:
        if game['local_state'] == 'local_valid':
            continue
        candidates = set()
        for name in [Path(game['rom_path']).stem, game['title']]:
            paths = image_keys.get((game['platform'], normalized(name)), [])
            if generic_artwork_name(name):
                paths = [path for path in paths if game['collection_path'] is not None
                         and media_collections[path] == game['collection_path']]
            candidates.update(paths)
        game['exact_local_candidates'] = sorted(candidates)
        if candidates: per_platform[game['platform']]['exact_local_reuse_candidates'] += 1
    return {'started_at': started, 'finished_at': time.time(), 'db': str(db_path), 'rom_root': str(rom_root),
            'total': len(records), 'platforms': dict(per_platform), 'cache_inventory': {name: dict(collections.Counter(Path(path).suffix for path in files)) for name, files in cache_files.items()},
            'recognized_local_covers': len(media), 'games': records, 'local_media': media,
            'layout_error': layout_error,
            'collections': [{'path': '/roms/' + Path(name).relative_to(rom_root).as_posix(), 'platform': platform}
                            for name, platform in sorted(collections_roots.items())]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--rom-root', type=Path, required=True)
    parser.add_argument('--cache', action='append', default=[], help='player-name=/absolute/cache/path')
    args = parser.parse_args()
    caches = {key: Path(value) for key, value in (item.split('=', 1) for item in args.cache)}
    print(json.dumps(audit(args.db, args.rom_root, caches), ensure_ascii=False))
