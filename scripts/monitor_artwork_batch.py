#!/usr/bin/env python3
"""Finish this isolated NAS artwork job and publish completed files periodically.

No service/container operations. All child commands are fixed local batch tools;
only the explicitly supplied artwork directory is published, never ROMs/saves.
Exits after all batches settle, or six hours with a diagnostic handoff status.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from retroone.profiles import atomic_json
from scripts.publish_artwork import publish


def read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {}


def coverage(root, destination):
    inventory = read(root / 'nas-inventory.json')
    files = {path.stem for path in destination.glob('*.png')}
    local = read(destination / 'local-overrides.json').get('games', {})
    plans = {}
    for name in ('candidates', 'extra-platforms', 'hash-candidates'):
        plans.update(read(root / name / 'manifest.json').get('games', {}))
    counts, platforms, missing = Counter(), defaultdict(Counter), []
    for game in inventory['games']:
        covered = game['local_state'] == 'local_valid' or game['id'] in local or game['cache_key'] in files
        prior = game['local_state'] == 'local_valid' or bool(game['cached_in'])
        counts['total'] += 1; counts['covered'] += covered; counts['newly_covered'] += covered and not prior
        platforms[game['platform']]['total'] += 1; platforms[game['platform']]['covered'] += covered
        if not covered:
            planned = plans.get(game['id'], {})
            reason = planned.get('status', 'no_deterministic_match')
            if reason == 'download': reason = 'source_fetch_failed'
            record = read(root / 'hash-candidates/records' / (game['id'] + '.json'))
            missing.append({'id': game['id'], 'platform': game['platform'], 'title': game['title'], 'reason': reason, 'hash_status': record.get('hash_status')})
    document = {'version': 1, 'updated_at': time.time(), 'inventory_at': inventory['finished_at'], 'counts': dict(counts),
                'covered_percent': round(counts['covered'] / counts['total'] * 100, 2), 'cache_pngs': len(files),
                'local_overrides': len(local), 'platforms': dict(platforms), 'missing_reasons': dict(Counter(game['reason'] for game in missing)), 'missing': missing}
    atomic_json(root / 'coverage.json', document)
    return document


def monitor(project_root, destination, interval):
    os.nice(10)
    root = project_root / 'artifacts/artwork-audit'
    state_path = root / 'monitor-state.json'
    state = read(state_path) or {'started_at': time.time(), 'retries': {}, 'spawned': {}}
    def launch(name):
        args = [sys.executable, 'scripts/prepare_artwork.py', '--inventory', str(root / 'nas-inventory.json'), '--output', str(root / name), '--reuse-plan', '--fetch', '--workers', '2', '--rate', '2']
        with (project_root / (name + '-monitor-download.log')).open('ab') as log:
            process = subprocess.Popen(args, cwd=str(project_root), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
        state['spawned'][name] = {'pid': process.pid, 'at': time.time()}
    run_started = time.time()
    while time.time() - run_started < 6 * 3600:
        settled, statuses = {}, {}
        hash_ready = bool(read(root / 'hash-candidates/status.json').get('complete'))
        if hash_ready and not (root / 'hash-candidates/download-status.json').exists() and 'hash-candidates' not in state['spawned']:
            if read(root / 'extra-platforms/download-status.json').get('complete'):
                launch('hash-candidates')
        for name in ('candidates', 'extra-platforms', 'hash-candidates'):
            folder = root / name
            status = statuses[name] = read(folder / 'download-status.json')
            completed = bool(status and status.get('complete'))
            spawned = state['spawned'].get(name)
            if spawned and status.get('started_at', 0) < spawned['at']:
                completed = False
            if completed and status.get('failed', 0) and state['retries'].get(name, 0) < 1:
                state['retries'][name] = state['retries'].get(name, 0) + 1
                launch(name); completed = False
            if (folder / 'cache').is_dir():
                local = folder / 'local-overrides.verified.json'
                result = publish(folder / 'cache', destination, local if name == 'candidates' and local.is_file() else None)
                state.setdefault('publications', {})[name] = dict(result, at=time.time())
            settled[name] = completed
        snapshot = coverage(root, destination)
        state.update(updated_at=time.time(), batches=statuses, counts=snapshot['counts'], settled=settled)
        state['complete'] = all(settled.values())
        atomic_json(state_path, state)
        print(json.dumps({'at': time.time(), 'counts': state['counts'], 'settled': settled}), flush=True)
        if state['complete']: return
        time.sleep(interval)
    state['timed_out'] = True
    atomic_json(state_path, state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--interval', type=int, default=300)
    args = parser.parse_args()
    with (args.project_root / 'artwork-monitor.lock').open('a+b') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monitor(args.project_root.resolve(), args.destination.resolve(), max(60, args.interval))
