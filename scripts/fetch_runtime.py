#!/usr/bin/env python3
"""Prepare PRIVATE local runtimes from a read-only reviewed SHA256 lock.

Mutable upstream URLs may change; mismatches fail, never refresh the lock.
Private cache backups must not be attached to public source/image releases.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from urllib.parse import urlparse
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'container/runtime-lock.json'
BROWSER_LOCK = ROOT / 'container/browser-runtime-lock.json'
try:
    from scripts import browser_runtime_assets as browser_assets
except ModuleNotFoundError:  # Direct execution: python scripts/fetch_runtime.py
    import browser_runtime_assets as browser_assets
ASSETS = {
    'Eden-0.2.1.AppImage': 'eden.AppImage',
    'pcsx2-2.8.2.AppImage': 'pcsx2.AppImage',
    'SDLPAL-3.0.2025.0616.AppImage': 'sdlpal.AppImage',
    'PPSSPP.zip': 'runtime-downloads/PPSSPP.zip',
    'EmulatorJS-4.2.3.7z': 'runtime-downloads/EmulatorJS-4.2.3.7z',
    'azahar-libretro-linux-x86_64-2126.2.zip': 'runtime-downloads/azahar-libretro-linux-x86_64-2126.2.zip',
}
MAX_ASSET_BYTES = 512 * 1024 * 1024


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def records(lock):
    result = {}
    for name, entry in lock.items():
        if name == 'runtime_transforms': continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not isinstance(entry, dict): raise ValueError('Invalid runtime lock record')
        if not re.fullmatch('[0-9a-f]{64}', entry.get('sha256', '')): raise ValueError('Missing reviewed SHA256: ' + name)
        if urlparse(entry.get('url', '')).scheme != 'https': raise ValueError('Runtime URL must use HTTPS')
        if name not in ASSETS and not name.endswith('_libretro.so.zip'): raise ValueError('Unrecognized runtime asset: ' + name)
        if name.endswith('_libretro.so.zip') and not re.fullmatch('[0-9a-f]{64}', entry.get('binary_sha256', '')): raise ValueError('Missing core binary hash')
        result[name] = entry
    if not result: raise ValueError('Empty runtime lock')
    return result


def cache_path(vendor, name):
    return vendor / ASSETS.get(name, 'runtime-downloads/' + name)


def verify(path, expected):
    if path.is_symlink() or not path.is_file(): raise ValueError('Missing regular runtime file: ' + str(path))
    if path.stat().st_size > MAX_ASSET_BYTES or digest(path) != expected: raise ValueError('SHA256 mismatch: ' + str(path) + '; restore the reviewed cache, never silently refresh the lock')


def fetch_one(vendor, name, entry, offline):
    path = cache_path(vendor, name)
    if not path.exists():
        if offline: raise ValueError('Offline runtime cache is incomplete: ' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.download')
        try:
            subprocess.run(['curl', '--proto', '=https', '--proto-redir', '=https', '--fail', '--location', '--silent', '--show-error', '--retry', '3', '--connect-timeout', '15', '--max-time', '300', '--output', str(temporary), entry['url']], check=True)
            verify(temporary, entry['sha256']); temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)
    verify(path, entry['sha256'])
    return name, path


def safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or '..' in path.parts or '\\' in name or ':' in name or any(ord(c) < 32 for c in name): raise ValueError('Unsafe archive member: ' + repr(name))
    return path


def write_atomic(path, data):
    if any(parent.is_symlink() for parent in path.parents): raise ValueError('Refusing symlink output directory: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('Refusing symlink output: ' + str(path))
    if path.is_file() and path.read_bytes() == data: return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.runtime-', delete=False) as temporary:
        temporary.write(data); temp_path = Path(temporary.name)
    temp_path.chmod(0o644); temp_path.replace(path)


def extract_zip(path, root):
    total = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            member = safe_member(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1: raise ValueError('Symlink/encrypted runtime ZIP member')
            total += info.file_size
            if info.file_size > MAX_ASSET_BYTES or total > 1024 * 1024 * 1024: raise ValueError('Oversized runtime ZIP')
            if not info.is_dir(): write_atomic(root / member, archive.read(info))


def materialize(paths, entries, vendor):
    for name, source in paths.items():
        if name.endswith('_libretro.so.zip') or name.startswith('azahar-libretro-'):
            member = 'azahar_libretro.so' if name.startswith('azahar-libretro-') else name[:-4]
            with zipfile.ZipFile(source) as archive:
                info = archive.getinfo(member)
                if info.file_size > MAX_ASSET_BYTES or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1: raise ValueError('Unsafe core archive member')
                data = archive.read(info)
            if hashlib.sha256(data).hexdigest() != entries[name]['binary_sha256']: raise ValueError('Core binary hash mismatch: ' + member)
            write_atomic(vendor / 'libretro' / member, data)
    if 'PPSSPP.zip' in paths: extract_zip(paths['PPSSPP.zip'], vendor / 'system')
    if 'EmulatorJS-4.2.3.7z' in paths:
        sevenzip = shutil.which('7zz') or shutil.which('7z')
        if not sevenzip: raise ValueError('Install 7-Zip (7z/7zz) to prepare EmulatorJS')
        source = paths['EmulatorJS-4.2.3.7z']
        listing = subprocess.check_output([sevenzip, 'l', '-slt', '-ba', str(source)], text=True)
        for line in listing.splitlines():
            if line.startswith('Path = '): safe_member(line[7:])
            if line.startswith(('Symbolic Link = ', 'Hard Link = ')): raise ValueError('Runtime archive links are not allowed')
        with tempfile.TemporaryDirectory(prefix='retroone-ejs-') as directory:
            subprocess.run([sevenzip, 'x', '-y', '-o' + directory, str(source)], check=True, stdout=subprocess.DEVNULL)
            for path in Path(directory).rglob('*'):
                if path.is_symlink(): raise ValueError('Symlink in extracted EmulatorJS tree')
                if path.is_file(): write_atomic(vendor / 'emulatorjs' / path.relative_to(directory), path.read_bytes())
        if not (vendor / 'emulatorjs/data/loader.js').is_file(): raise ValueError('Missing EmulatorJS loader after extraction')


def create_bundle(target, lock_path, entries, vendor, *, browser_lock_path=None, browser_paths=None):
    inputs = {'runtime-lock.json': lock_path} if entries else {}
    for name, entry in entries.items():
        path = cache_path(vendor, name); verify(path, entry['sha256']); inputs['archives/' + name] = path
    if browser_lock_path is not None:
        browser_bytes = browser_lock_path.read_bytes()
        inputs['browser-runtime-lock.json'] = browser_lock_path
        for entry in browser_assets.validate_lock(json.loads(browser_bytes)):
            path = browser_paths[entry['path']]
            browser_assets.verify(path, entry)
            inputs['browser/' + entry['path']] = path
    if not inputs: raise ValueError('Cannot create an empty private runtime bundle')
    browser_assets.no_links(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.runtime-bundle-', dir=target.parent)
    import os
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open('wb') as raw, gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, path in sorted(inputs.items()):
                info = tarfile.TarInfo(name); info.size = path.stat().st_size; info.mode = 0o644; info.mtime = 0
                with path.open('rb') as stream: archive.addfile(info, stream)
        temporary.replace(target)
    finally: temporary.unlink(missing_ok=True)
    target.with_suffix(target.suffix + '.sha256').write_text(digest(target) + '  ' + target.name + '\n')


def restore_bundle(bundle, lock_path, entries, vendor, *, browser_lock_path=None):
    allowed = ({'runtime-lock.json'} | {'archives/' + name for name in entries}) if entries else set()
    browser_bytes = browser_lock_path.read_bytes() if browser_lock_path is not None else None
    browser_entries = browser_assets.validate_lock(json.loads(browser_bytes)) if browser_bytes is not None else []
    if browser_bytes is not None: allowed |= {'browser-runtime-lock.json'} | {'browser/' + entry['path'] for entry in browser_entries}
    with tarfile.open(bundle, 'r:gz') as archive:
        members = []
        for member in archive:
            members.append(member)
            if len(members) > len(allowed): raise ValueError('Unexpected private bundle entries')
        if len(members) != len(allowed) or {member.name for member in members} != allowed: raise ValueError('Unexpected, missing or duplicate private bundle entries; match --browser-only/--native-only to the bundle')
        if any(not member.isfile() or member.size > MAX_ASSET_BYTES for member in members): raise ValueError('Unsafe private bundle member')
        if entries and archive.extractfile('runtime-lock.json').read() != lock_path.read_bytes(): raise ValueError('Private bundle belongs to another reviewed lock')
        if browser_bytes is not None and archive.extractfile('browser-runtime-lock.json').read() != browser_bytes: raise ValueError('Private bundle belongs to another reviewed browser lock')
        for name, entry in entries.items():
            data = archive.extractfile('archives/' + name).read()
            if hashlib.sha256(data).hexdigest() != entry['sha256']: raise ValueError('Private bundle checksum mismatch: ' + name)
            write_atomic(cache_path(vendor, name), data)
        for entry in browser_entries:
            member = archive.getmember('browser/' + entry['path'])
            if member.size != entry['size']: raise ValueError('Private browser bundle size mismatch')
            data = archive.extractfile(member).read()
            if hashlib.sha256(data).hexdigest() != entry['sha256']: raise ValueError('Private browser bundle checksum mismatch')
            browser_assets.write_atomic(browser_assets.cache_root(vendor, browser_bytes) / entry['path'], data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=LOCK)
    parser.add_argument('--browser-lock', type=Path, default=BROWSER_LOCK)
    parser.add_argument('--vendor-root', type=Path, default=ROOT / 'vendor')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--browser-only', action='store_true', help='Prepare only the pinned browser runtime')
    scope.add_argument('--native-only', action='store_true', help='Prepare only native assets; also supports matching historical native bundles')
    parser.add_argument('--browser-source', type=Path, help='Seed exact browser files from an already downloaded data directory')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='Validate existing reviewed cache without network or runtime activation')
    parser.add_argument('--create-bundle', type=Path, help='Private local backup only; never attach to a public release')
    parser.add_argument('--from-bundle', type=Path, help='Restore reviewed assets into a clean vendor root from a private backup')
    args = parser.parse_args()
    original = args.lock.read_bytes() if not args.browser_only else None
    entries = records(json.loads(original)) if original is not None else {}
    browser_original = args.browser_lock.read_bytes() if not args.native_only else None
    if browser_original is not None: browser_assets.validate_lock(json.loads(browser_original))
    browser_lock_path = args.browser_lock if browser_original is not None else None
    if args.from_bundle: restore_bundle(args.from_bundle, args.lock, entries, args.vendor_root, browser_lock_path=browser_lock_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = dict(pool.map(lambda item: fetch_one(args.vendor_root, item[0], item[1], args.offline or args.verify_only), entries.items()))
    browser_paths = browser_assets.prepare_cache(args.vendor_root, browser_original, offline=args.offline,
        source=args.browser_source, verify_only=args.verify_only) if browser_original is not None else {}
    if args.create_bundle:
        create_bundle(args.create_bundle, args.lock, entries, args.vendor_root,
            browser_lock_path=browser_lock_path, browser_paths=browser_paths)
    activated = None
    if not args.verify_only:
        materialize(paths, entries, args.vendor_root)
        if browser_original is not None: activated = browser_assets.materialize(args.vendor_root, browser_original, browser_paths)
    if original is not None and args.lock.read_bytes() != original: raise ValueError('Runtime lock changed unexpectedly')
    if browser_original is not None and args.browser_lock.read_bytes() != browser_original: raise ValueError('Browser runtime lock changed unexpectedly')
    print(json.dumps({'assets_verified': len(paths), 'browser_files_verified': len(browser_paths),
        'lock_sha256': hashlib.sha256(original).hexdigest() if original is not None else None,
        'browser_lock_sha256': hashlib.sha256(browser_original).hexdigest() if browser_original is not None else None,
        'vendor_root': str(args.vendor_root), 'materialized': not args.verify_only,
        'browser_activation': activated, 'distribution': 'private-local-only'}))

if __name__ == '__main__': main()
