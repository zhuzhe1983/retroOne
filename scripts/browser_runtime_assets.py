"""Pinned browser assets: bounded downloads, whole-tree activation, private backups."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024**3
MAX_FILES = 1024
REQUIRED = {'loader.js', 'emulator.min.js', 'version.json'}


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def safe_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or
            any(ord(char) < 32 for char in value) or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ValueError('Unsafe browser asset path')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError('Unsafe browser asset path')
    return path


def validate_lock(document):
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise ValueError('Unsupported browser runtime lock')
    version = document.get('version', '')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?', version):
        raise ValueError('Browser runtime version must be explicit, not latest/stable')
    base = 'https://cdn.emulatorjs.org/' + version + '/data/'
    if document.get('source') != base:
        raise ValueError('Browser runtime source must be the pinned official version directory')
    entries = document.get('files')
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
        raise ValueError('Invalid browser asset count')
    seen, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'path', 'url', 'size', 'sha256'}:
            raise ValueError('Invalid browser runtime file record')
        name = safe_path(entry['path']).as_posix()
        if name.casefold() in seen or entry['url'] != base + name:
            raise ValueError('Duplicate browser asset or mismatched source URL')
        if (type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE_BYTES or
                not isinstance(entry['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
            raise ValueError('Invalid browser asset size or reviewed SHA256')
        seen.add(name.casefold()); total += entry['size']
    if total > MAX_TOTAL_BYTES or not REQUIRED <= {entry['path'] for entry in entries}:
        raise ValueError('Incomplete or oversized browser runtime lock')
    return entries


def no_links(path):
    path = Path(path).absolute()
    if any(item.is_symlink() for item in [path, *path.parents]):
        raise ValueError('Browser runtime paths cannot contain symlinks: ' + str(path))
    return path


def verify(path, entry):
    no_links(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size != entry['size']:
        raise ValueError('Missing/wrong-sized browser asset: ' + str(path))
    if sha256(path) != entry['sha256']:
        raise ValueError('Browser asset SHA256 mismatch; restore the reviewed cache, never refresh the lock: ' + str(path))


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def write_atomic(path, source):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.browser-', delete=False) as output:
            temporary = Path(output.name)
            if isinstance(source, bytes): output.write(source)
            else:
                with source.open('rb') as stream: shutil.copyfileobj(stream, output, 1024 * 1024)
            output.flush(); os.fsync(output.fileno())
        temporary.chmod(0o644); temporary.replace(path); sync_directory(path.parent)
    finally:
        if temporary: temporary.unlink(missing_ok=True)


def cache_root(vendor, lock_bytes):
    return Path(vendor) / 'runtime-downloads/browser' / hashlib.sha256(lock_bytes).hexdigest()


def prepare_cache(vendor, lock_bytes, *, offline=False, source=None, workers=4, verify_only=False):
    from concurrent.futures import ThreadPoolExecutor
    entries = validate_lock(json.loads(lock_bytes))
    cache = no_links(cache_root(vendor, lock_bytes))
    if source is not None: source = no_links(source)
    def prepare(entry):
        target = cache / entry['path']
        no_links(target)
        if target.exists(): verify(target, entry); return entry['path'], target
        local = source / entry['path'] if source is not None else None
        # A fully matching installed runtime is also a valid offline cache seed.
        installed = Path(vendor) / 'emulatorjs/data' / entry['path']
        if local is None and installed.exists():
            try: verify(installed, entry)
            except ValueError: pass
            else: local = installed
        if local is not None:
            verify(local, entry)
            if verify_only: return entry['path'], local
            write_atomic(target, local)
        else:
            if offline or verify_only: raise ValueError('Offline browser cache is incomplete: ' + entry['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix='.download-', dir=target.parent)
            os.close(descriptor); temporary = Path(name)
            try:
                subprocess.run(['curl', '--proto', '=https', '--proto-redir', '=https', '--fail', '--location',
                    '--silent', '--show-error', '--retry', '3', '--connect-timeout', '15', '--max-time', '300',
                    '--max-filesize', str(MAX_FILE_BYTES), '--output', str(temporary), entry['url']], check=True)
                verify(temporary, entry)
                with temporary.open('rb') as downloaded: os.fsync(downloaded.fileno())
                temporary.replace(target); sync_directory(target.parent)
            finally: temporary.unlink(missing_ok=True)
        verify(target, entry)
        return entry['path'], target
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        return dict(pool.map(prepare, entries))


def runtime_marker(lock_bytes):
    return {'version': json.loads(lock_bytes)['version'], 'lock_sha256': hashlib.sha256(lock_bytes).hexdigest()}


def verify_tree(data, entries, lock_bytes):
    no_links(data)
    expected = {entry['path'] for entry in entries} | {'.retroone-runtime.json'}
    actual = set()
    for path in data.rglob('*'):
        if path.is_symlink(): raise ValueError('Symlink in browser runtime tree')
        if path.is_file(): actual.add(path.relative_to(data).as_posix())
        elif not path.is_dir(): raise ValueError('Non-file in browser runtime tree')
    if actual != expected: raise ValueError('Browser runtime tree contains missing or extra files')
    for entry in entries: verify(data / entry['path'], entry)
    marker = data / '.retroone-runtime.json'
    if marker.stat().st_size > 4096 or json.loads(marker.read_text()) != runtime_marker(lock_bytes):
        raise ValueError('Browser runtime activation marker differs from reviewed lock')


@contextmanager
def installation_lock(vendor):
    no_links(vendor); vendor.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(vendor / '.browser-runtime.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 60
        while True:
            try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise ValueError('Another browser runtime activation is still running')
                time.sleep(.05)
        yield
    finally: os.close(descriptor)


def recover_activation(vendor):
    journal = vendor / '.browser-runtime-activation.json'
    no_links(journal)
    if not journal.exists(): return
    if not journal.is_file() or journal.stat().st_size > 4096: raise ValueError('Invalid activation journal; inspect manually')
    record = json.loads(journal.read_text())
    if (not isinstance(record, dict) or set(record) != {'version', 'stage', 'backup'} or record['version'] != 1 or
            not isinstance(record['stage'], str) or not re.fullmatch(r'\.emulatorjs\.stage-[a-zA-Z0-9_-]+', record['stage']) or
            (record['backup'] is not None and (not isinstance(record['backup'], str) or not re.fullmatch(r'emulatorjs-[0-9a-f]{32}', record['backup'])))):
        raise ValueError('Invalid activation journal; inspect manually')
    target = no_links(vendor / 'emulatorjs')
    if not target.exists() and record['backup']:
        backup = no_links(vendor / 'runtime-backups' / record['backup'])
        if not backup.is_dir(): raise ValueError('Prior runtime backup is missing; inspect activation journal')
        backup.rename(target)
        sync_directory(backup.parent); sync_directory(vendor)
    # A leftover stage is intentionally kept for review; it is never merged.
    journal.unlink(); sync_directory(vendor)


def materialize(vendor, lock_bytes, paths):
    vendor = Path(vendor).absolute(); entries = validate_lock(json.loads(lock_bytes))
    with installation_lock(vendor):
        recover_activation(vendor)
        target = no_links(vendor / 'emulatorjs')
        if target.exists() and not target.is_dir(): raise ValueError('Browser runtime destination must be a directory')
        if target.exists():
            try: verify_tree(target / 'data', entries, lock_bytes)
            except ValueError: pass
            else: return {'changed': False, 'backup': None}
        stage = Path(tempfile.mkdtemp(prefix='.emulatorjs.stage-', dir=vendor))
        backup = no_links(vendor / 'runtime-backups' / ('emulatorjs-' + uuid4().hex)) if target.exists() else None
        journal = vendor / '.browser-runtime-activation.json'
        try:
            for entry in entries:
                source = paths[entry['path']]; verify(source, entry)
                write_atomic(stage / 'data' / entry['path'], source)
            write_atomic(stage / 'data/.retroone-runtime.json', json.dumps(runtime_marker(lock_bytes), sort_keys=True).encode())
            verify_tree(stage / 'data', entries, lock_bytes)
            write_atomic(stage / 'browser-runtime-lock.json', lock_bytes)
            # Persist newly created directory entries before the activation journal.
            for directory in sorted((item for item in stage.rglob('*') if item.is_dir()),
                                    key=lambda item: len(item.parts), reverse=True):
                sync_directory(directory)
            sync_directory(stage)
            if backup: backup.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(journal, json.dumps({'version': 1, 'stage': stage.name, 'backup': backup.name if backup else None}).encode())
            if backup:
                target.rename(backup)
                sync_directory(backup.parent); sync_directory(vendor)
            stage.rename(target)
            sync_directory(vendor)
            journal.unlink(); sync_directory(vendor)
            return {'changed': True, 'backup': str(backup) if backup else None}
        except BaseException:
            recover_activation(vendor)
            raise
        finally:
            if stage.exists(): shutil.rmtree(stage)
