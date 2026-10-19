"""Bounded, resumable read-only library jobs; never control player emulators."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from .library_review import publish_report


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
            handle.flush(); os.fsync(handle.fileno()); os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class LibraryManager:
    def __init__(self, catalog):
        self.catalog = catalog
        self.root = catalog.library_data_root
        self.jobs = self.root / 'metadata' / 'audit'
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.status_path = self.jobs / 'status.json'
        self.lock_path = self.jobs / 'worker.lock'
        self.script = Path(__file__).resolve().parents[1] / 'scripts' / 'library_audit.py'
        self.thread = None

    def _lock(self):
        handle = self.lock_path.open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            handle.close()
            return None

    def job(self):
        try:
            value = json.loads(self.status_path.read_text())
            progress = self.jobs / 'report.progress.json'
            if progress.exists():
                value['progress'] = json.loads(progress.read_text())
            if value['status'] == 'running':
                lock = self._lock()
                if lock:
                    try:
                        value.update(status='interrupted', error='上次检测已中断，可以继续；已完成的指纹会复用')
                        atomic_json(self.status_path, value)
                    finally:
                        lock.close()
            return value
        except (OSError, ValueError, KeyError):
            return None

    def summary(self):
        display = self.catalog.count()
        with self.catalog.connect() as db:
            total = db.execute('SELECT count(*) FROM games').fetchone()[0]
            reviewed = db.execute("SELECT count(*) FROM game_reviews r JOIN games g ON g.id=r.game_id WHERE r.status NOT IN ('unverified','stale','runtime_unavailable')").fetchone()[0]
            attention = db.execute("SELECT count(*) FROM game_reviews r JOIN games g ON g.id=r.game_id WHERE r.status IN ('dependency_error','load_failed')").fetchone()[0]
        return {'catalog_entries': total, 'display_entries': display, 'duplicate_copies': total - display,
                'reviewed': reviewed, 'needs_attention': attention, 'pending': total - reviewed}

    def view(self):
        return {'scan': dict(self.catalog.scan_status), 'summary': self.summary(), 'job': self.job(),
                'capabilities': {'audit': self.script.is_file(), 'observe': False}, 'storage': {'separated': True}}

    def start(self):
        if not self.script.is_file():
            raise ValueError('镜像尚未包含内容检测工具')
        if self.catalog.scan_status['running']:
            raise ValueError('目录扫描正在进行，请等扫描完成后再核验内容')
        lock = self._lock()
        if not lock:
            raise ValueError('已有内容检测任务正在运行')
        value = {'id': uuid4().hex, 'kind': 'audit', 'status': 'running', 'started_at': time.time()}
        try:
            (self.jobs / 'report.progress.json').unlink(missing_ok=True)
            atomic_json(self.status_path, value)
            self.thread = threading.Thread(target=self._work, args=(lock, value), name='retroone-library-audit', daemon=True)
            self.thread.start()
        except Exception:
            lock.close()
            raise
        return value

    def _work(self, lock, value):
        try:
            with self.catalog.connect() as db:
                games = [json.loads(row[0]) for row in db.execute('SELECT payload FROM games ORDER BY platform,id')]
            atomic_json(self.jobs / 'catalog.json', {'schema_version': 1, 'path_prefix': str(self.catalog.rom_root), 'games': games})
            command = [sys.executable, str(self.script), '--catalog', str(self.jobs / 'catalog.json'),
                       '--rom-root', str(self.catalog.rom_root), '--cache', str(self.jobs / 'hashes.sqlite'),
                       '--output', str(self.jobs / 'report.json'), '--rate-mib', '8', '--max-read-gib', '2', '--finish-unit']
            # No emulator, shell, display, network, or player-save access is used.
            with (self.jobs / 'worker.log').open('w') as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
            if result.returncode not in (0, 2):
                raise ValueError('内容检测未完成，可重试；详细原因已记录在管理日志')
            report = json.loads((self.jobs / 'report.json').read_text())
            publish_report(report, self.root / 'metadata' / 'library-review.json')
            value.update(status='complete' if result.returncode == 0 else 'paused', finished_at=time.time(),
                         summary=report['summary'])
        except Exception as error:
            value.update(status='failed', finished_at=time.time(), error=str(error)[:300])
        finally:
            atomic_json(self.status_path, value)
            lock.close()
