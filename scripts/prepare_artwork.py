#!/usr/bin/env python3
"""Plan and prefetch verified-name covers into an isolated resumable cache.

The inventory is produced by audit_artwork.py; no catalog/ROM/save is modified.
Official directory indexes are cached, and unavailable indexes remain errors,
never evidence that a title has no artwork. Rerun --fetch to resume downloads.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import ssl
import struct
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.artwork import MAX_IMAGE_BYTES, REPOS, thumbnail_names
from retroone.artwork_match import NameIndex, collection_path
from retroone.profiles import atomic_json

# Offline-only index expansion. The live fallback retains its original allowlist;
# these platforms receive a cover only after this audited batch verifies it.
REPOS = dict(REPOS, **{
    'pico': 'Sega_-_PICO', 'odyssey2': 'Magnavox_-_Odyssey2',
    'supervision': 'Watara_-_Supervision', 'vectrex': 'GCE_-_Vectrex',
    'channelf': 'Fairchild_-_Channel_F', 'fbneo': 'FBNeo_-_Arcade_Games',
    'mame': 'MAME', 'naomi': 'Sega_-_Naomi', 'ps3': 'Sony_-_PlayStation_3',
    'gamewatch': 'Handheld_Electronic_Game',
})

HOST = 'thumbnails.libretro.com'
MAX_INDEX_BYTES = 64 * 1024 * 1024
SSL = ssl.create_default_context(cafile=os.getenv('SSL_CERT_FILE') or ('/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None))


class Directory(HTMLParser):
    def __init__(self):
        super().__init__(); self.names = set()
    def handle_starttag(self, tag, attrs):
        if tag != 'a': return
        value = dict(attrs).get('href', '')
        name = unquote(value)
        if name.endswith('.png') and '/' not in name and '\\' not in name and not urlparse(value).scheme and not any(ord(c) < 32 for c in name):
            self.names.add(name[:-4])


class Fetcher:
    def __init__(self, requests_per_second):
        self.interval = 1 / requests_per_second
        self.next_at = 0
        self.lock = threading.Lock()
    def read(self, url, maximum, timeout=20):
        if urlparse(url).scheme != 'https' or urlparse(url).hostname != HOST:
            raise ValueError('Only the official HTTPS thumbnail host is allowed')
        with self.lock:
            slot = max(time.monotonic(), self.next_at)
            self.next_at = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0: time.sleep(delay)
        request = Request(url, headers={'User-Agent': 'RetroOne/0.1 personal-library-artwork', 'Accept': '*/*'})
        with urlopen(request, timeout=timeout, context=SSL) as response:
            if urlparse(response.geturl()).hostname != HOST: raise ValueError('Unexpected redirect host')
            data = response.read(maximum + 1)
        if len(data) > maximum: raise ValueError('Response exceeded bounded size')
        return data


def index_url(platform):
    return 'https://' + HOST + '/' + quote(REPOS[platform].replace('_', ' '), safe='') + '/Named_Boxarts/'


def load_index(platform, root, fetcher):
    target = root / (platform + '.json')
    if target.is_file():
        cached = json.loads(target.read_text())
        if cached.get('status') == 'ok' and time.time() - cached.get('fetched_at', 0) < 30 * 86400:
            return platform, cached
    result = {'platform': platform, 'url': index_url(platform), 'fetched_at': time.time()}
    try:
        data = fetcher.read(result['url'], MAX_INDEX_BYTES)
        parser = Directory(); parser.feed(data.decode('utf-8'))
        if not parser.names: raise ValueError('Directory returned no recognized PNG entries')
        result.update(status='ok', names=sorted(parser.names), index_bytes=len(data))
    except Exception as error:
        result.update(status='unavailable', error=str(error)[:400])
    atomic_json(target, result)
    return platform, result


def prepare(inventory, output, fetcher, workers):
    index_root = output / 'indexes'; index_root.mkdir(parents=True, exist_ok=True)
    local_names = defaultdict(lambda: defaultdict(list))
    for media in inventory['local_media']:
        local_names[media['platform']][media['match_name']].append(media['path'])
    local_indexes = {platform: NameIndex(names) for platform, names in local_names.items()}
    platforms = sorted({game['platform'] for game in inventory['games'] if game['local_state'] != 'local_valid' and game['platform'] in REPOS})
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(load_index, platform, index_root, fetcher) for platform in platforms]):
            platform, result = future.result(); results[platform] = result
            print(json.dumps({'index': platform, 'status': result['status'], 'names': len(result.get('names', []))}), flush=True)
    official = {platform: NameIndex(result['names']) for platform, result in results.items() if result['status'] == 'ok'}
    entries, overrides, counts = {}, {}, Counter()
    for game in inventory['games']:
        if game['local_state'] == 'local_valid': counts['existing_local'] += 1; continue
        platform = game['platform']
        names = list(dict.fromkeys([Path(game['rom_path']).stem, game['title']]))
        local = local_indexes.get(platform)
        matched = None
        same_collection = collection_path(game['rom_path'])
        if local:
            remaining = list(names)
            while remaining:
                candidate = local.match(remaining)
                if candidate:
                    paths = local_names[platform][candidate['matched_name']]
                    # Numbered bundles are meaningful inside their collection;
                    # "001" in another bundle proves no game identity.
                    name = candidate['original_name']
                    generic = name.isdecimal() or name.casefold() in {'game', 'boot', 'eboot', 'default', 'start'}
                    if generic: paths = [path for path in paths if collection_path(path) == same_collection]
                    if paths:
                        matched = candidate; break
                    remaining.remove(name)
                else:
                    break
        entry = {key: game[key] for key in ('id', 'platform', 'title', 'rom_path', 'cache_key')}
        if matched:
            selected = min(paths, key=lambda path: (collection_path(path) != same_collection, '/new/' not in path, path))
            entry.update(matched, status='local', source_path=selected, source_candidates=len(paths))
            overrides[game['id']] = entry
            counts['local_' + matched['method']] += 1
        elif game['cached_in']:
            entry.update(status='existing_cache', players=game['cached_in']); counts['existing_cache'] += 1
        elif platform not in REPOS:
            entry.update(status='no_official_platform_index'); counts[entry['status']] += 1
        elif platform not in official:
            entry.update(status='index_unavailable', error=results[platform]['error']); counts[entry['status']] += 1
        else:
            matched = official[platform].match(thumbnail_names(game))
            if matched:
                entry.update(matched, status='download', source=index_url(platform) + quote(matched['matched_name'] + '.png', safe=''))
                counts['official_' + matched['method']] += 1
            else:
                entry.update(status='no_deterministic_match'); counts[entry['status']] += 1
        entries[game['id']] = entry
    manifest = {'version': 1, 'created_at': time.time(), 'inventory_at': inventory['finished_at'], 'counts': dict(counts),
                'index_status': {key: {k: v for k, v in value.items() if k != 'names'} for key, value in results.items()}, 'games': entries}
    atomic_json(output / 'manifest.json', manifest)
    atomic_json(output / 'local-overrides.json', {'version': 1, 'created_at': time.time(), 'games': overrides})
    print(json.dumps({'plan': dict(counts)}, ensure_ascii=False), flush=True)
    return manifest


def valid_png(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) < 33 or data[12:16] != b'IHDR': return False
    width, height = struct.unpack('>II', data[16:24])
    # These are cover thumbnails, not unbounded image-decoder inputs.
    return 0 < width <= 8192 and 0 < height <= 8192 and b'IEND' in data[-32:]


def atomic_bytes(path, data):
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.' + str(threading.get_ident()) + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def download(manifest, output, fetcher, workers, maximum):
    cache = output / 'cache'; cache.mkdir(parents=True, exist_ok=True)
    objects = output / 'objects'; objects.mkdir(parents=True, exist_ok=True)
    jobs = defaultdict(list)
    for game in manifest['games'].values():
        if game['status'] == 'download': jobs[game['source']].append(game)
    jobs = list(jobs.items())
    if maximum: jobs = jobs[:maximum]
    status = {'started_at': time.time(), 'unique_sources': len(jobs), 'done': 0, 'failed': 0, 'games_cached': 0, 'bytes_fetched': 0, 'complete': not jobs}
    atomic_json(output / 'download-status.json', status)
    lock = threading.Lock()
    def work(item):
        url, games = item
        object_id = hashlib.sha256(url.encode()).hexdigest()
        path = objects / (object_id + '.png')
        fetched = False
        result = {'source': url, 'game_ids': [game['id'] for game in games], 'finished_at': time.time()}
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_IMAGE_BYTES:
                data = path.read_bytes()
            else:
                data = fetcher.read(url, MAX_IMAGE_BYTES, timeout=15); fetched = True
            if not valid_png(data): raise ValueError('Invalid or oversized PNG dimensions')
            if fetched: atomic_bytes(path, data)
            digest = hashlib.sha256(data).hexdigest()
            for game in games:
                target = cache / (game['cache_key'] + '.png')
                descriptor = os.open(cache / (game['cache_key'] + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'a+b') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    metadata_path = target.with_suffix('.json')
                    if target.is_file() and metadata_path.is_file():
                        previous = json.loads(metadata_path.read_text())
                        if previous.get('source') == url and previous.get('sha256') == digest:
                            continue
                    if not target.is_file():
                        # Content files are immutable; hard-link only inside this task-owned staging directory.
                        temporary = target.with_name(target.name + '.' + str(threading.get_ident()) + '.tmp')
                        temporary.unlink(missing_ok=True)
                        os.link(path, temporary); temporary.replace(target)
                    atomic_json(metadata_path, {**game, 'fetched_at': time.time(), 'sha256': digest})
            result.update(status='cached', bytes=len(data), sha256=digest)
        except Exception as error:
            result.update(status='retryable_error', error=str(error)[:400])
        result_path = objects / (object_id + '.json')
        previous = json.loads(result_path.read_text()) if result_path.is_file() else {}
        if result['status'] != 'cached' or previous.get('sha256') != result.get('sha256') or previous.get('game_ids') != result['game_ids']:
            atomic_json(result_path, result)
        with lock:
            status['done'] += 1
            if result['status'] == 'cached':
                status['games_cached'] += len(games)
                if fetched: status['bytes_fetched'] += len(data)
            else: status['failed'] += 1
            status['updated_at'] = time.time()
            status['complete'] = status['done'] == len(jobs)
            if status['done'] % 20 == 0 or result['status'] != 'cached' or status['complete']:
                atomic_json(output / 'download-status.json', status)
            if status['done'] % 50 == 0 or status['done'] == len(jobs): print(json.dumps(status), flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, jobs))
    shutil.copyfile(output / 'local-overrides.json', cache / 'local-overrides.json')
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--reuse-plan', action='store_true')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--rate', type=float, default=3, help='Maximum requests/second, shared between all threads')
    parser.add_argument('--max-sources', type=int, default=0, help='Optional bounded sample before a full resumable batch')
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or not 0.1 <= args.rate <= 10: parser.error('workers 1..8, request rate 0.1..10')
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / 'batch.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fetcher = Fetcher(args.rate)
        manifest = json.loads((args.output / 'manifest.json').read_text()) if args.reuse_plan else prepare(json.loads(args.inventory.read_text()), args.output, fetcher, args.workers)
        if args.fetch: download(manifest, args.output, fetcher, args.workers, args.max_sources)
