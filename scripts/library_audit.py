#!/usr/bin/env python3
"""Read-only whole-library duplicate audit; Python 3.8+, standard library only.

Input is a catalog JSON list or {games: [...], path_prefix: '/roms'}. Output is
an evidence manifest plus CSV, never a deletion/migration plan. Default mode
hashes only same-platform size/layout candidates; --hash-all reads all eligible
files. A SHA256 is emitted only after every byte was read (or a matching stat
identity was found in this tool's SQLite cache). ZIP CRCs are never fingerprints.
--max-read-gib is a hard limit unless --finish-unit is explicit. That batch mode
admits whole uncached files/ZIP trees and permits the first oversized unit to
finish, so a fixed budget cannot repeatedly restart the same large file.

Examples:
  python3 scripts/library_audit.py --catalog catalog.json --rom-root /volume2/rom/one \
    --cache /tmp/audit/cache.sqlite --output /tmp/audit/report.json --platform fbneo
  # Add --hash-all for full hashing, --rate-mib 16 to bound hashing throughput.

For directory games outside library/<platform>/<source>/<collection>/<game>/,
provide resource_root in each input record. It denotes the complete game folder;
an EXE alone is insufficient. No source files, archives or saves are modified.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import struct
import sys
import tempfile
import time
import zipfile

VERSION = 1
CHUNK = 1024 * 1024
ZIP_SUFFIXES = {'.zip', '.dosz'}
OPAQUE_ARCHIVES = {'.7z', '.rar', '.gz', '.nsz', '.xcz'}
PC_PLATFORMS = {'windows', 'dos', 'teknoparrot', 'openbor', 'scummvm'}
ARCADE_PLATFORMS = {'fbneo', 'mame', 'hbmame', 'lightgun', 'naomi', 'model2', 'model3'}
LINKS = {'.cue', '.gdi', '.m3u', '.m3u8'}
# Deliberately excludes disk/tape formats even on cartridge-capable platforms.
CARTRIDGES = {
    'nes': '.nes .unf .unif', 'fds': '.fds', 'snes': '.sfc .smc .bs',
    'gb': '.gb', 'gbc': '.gb .gbc', 'gba': '.gba', 'nds': '.nds',
    'n64': '.z64 .n64 .v64', 'megadrive': '.md .gen .smd .bin',
    'sega32x': '.32x .bin', 'mastersystem': '.sms', 'gamegear': '.gg',
    'sg1000': '.sg .sc', 'pico': '.md .bin', 'virtualboy': '.vb .vboy',
    'pokemini': '.min', 'atari2600': '.a26 .bin', 'atari5200': '.a52 .bin',
    'atari7800': '.a78 .bin', 'jaguar': '.j64 .jag .rom', 'lynx': '.lnx',
    'wonderswan': '.ws', 'wonderswancolor': '.ws .wsc', 'ngp': '.ngp',
    'ngpc': '.ngp .ngc', 'pce': '.pce .sgx', 'colecovision': '.col .rom',
    'channelf': '.bin .chf', 'vectrex': '.vec .bin', 'odyssey2': '.bin',
    'msx': '.rom', 'msx2': '.rom', 'supervision': '.sv .bin', 'gamewatch': '.mgw',
}
CARTRIDGES = {key: set(value.split()) for key, value in CARTRIDGES.items()}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def signature(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class AuditIssue(Exception):
    def __init__(self, code, message, *, error=False):
        super().__init__(message)
        self.code, self.error = code, error


class BudgetReached(AuditIssue):
    def __init__(self):
        super().__init__('read_budget_reached', 'Read budget exhausted; rerun with the same cache')


def identity(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise AuditIssue('not_regular_file', 'Source is not a regular file', error=True)
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'dev': info.st_dev,
            'ino': info.st_ino, 'ctime_ns': info.st_ctime_ns,
            'executable': info.st_mode & 0o111}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n'); output.flush(); os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


class Progress:
    def __init__(self, rate_mib=32, max_read_bytes=0, callback=None, finish_unit=False):
        self.rate = rate_mib * 1024**2
        self.budget = max_read_bytes
        self.finish_unit = finish_unit
        self.unit_ceiling = None
        self.callback = callback
        self.started = self.last_emit = time.monotonic()
        self.next_read = self.started
        self.state = dict(phase='discover', games_discovered=0, games_total=0,
                          bytes_hashed=0, bytes_from_cache=0, files_hashed=0,
                          cache_hits=0, zip_members_hashed=0, current=None,
                          read_budget_bytes=max_read_bytes,
                          budget_policy='finish_unit' if finish_unit else 'hard',
                          oversized_units_started=0, budget_overrun_bytes=0,
                          deferred_unit_bytes=None)

    @contextmanager
    def unit(self, size):
        """Admit an entire uncached file/ZIP tree before reading it in soft mode.

        A first unit larger than the batch budget may finish. After any actual
        bytes have been read, a unit that cannot fit is deferred without starting.
        Cached units never call this method and consume no read budget.
        """
        previous = self.unit_ceiling
        if self.finish_unit and self.budget and size:
            used = self.state['bytes_hashed']
            if used and used + size > self.budget:
                self.state['deferred_unit_bytes'] = size
                raise BudgetReached()
            if not used and size > self.budget:
                self.unit_ceiling = size
                self.state['oversized_units_started'] += 1
            else:
                self.unit_ceiling = self.budget
        try:
            yield
        finally:
            self.unit_ceiling = previous

    def tick(self, force=False):
        now = time.monotonic()
        if self.callback and (force or now - self.last_emit >= 2):
            self.callback(dict(self.state, elapsed_seconds=round(now - self.started, 3)))
            self.last_emit = now

    def read(self, stream, maximum=CHUNK):
        if self.budget:
            ceiling = self.unit_ceiling if self.unit_ceiling is not None else self.budget
            remaining = ceiling - self.state['bytes_hashed']
            if remaining <= 0:
                raise BudgetReached()
            maximum = min(maximum, remaining)
        if self.rate:
            delay = self.next_read - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        chunk = stream.read(maximum)
        self.state['bytes_hashed'] += len(chunk)
        if self.budget:
            self.state['budget_overrun_bytes'] = max(0, self.state['bytes_hashed'] - self.budget)
        if self.rate:
            self.next_read = max(self.next_read, time.monotonic()) + len(chunk) / self.rate
        self.tick()
        return chunk


class Cache:
    """One writer per cache. Only complete successful hashes are checkpointed."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix(path.suffix + '.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('Another audit is using this cache')
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS audit_hashes_v1(
            path TEXT, identity TEXT, kind TEXT, payload TEXT,
            PRIMARY KEY(path,identity,kind))''')

    def get(self, path, info, kind):
        row = self.db.execute('SELECT payload FROM audit_hashes_v1 WHERE path=? AND identity=? AND kind=?',
                              (str(path), canonical(info), kind)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, path, info, kind, payload):
        self.db.execute('INSERT OR REPLACE INTO audit_hashes_v1 VALUES(?,?,?,?)',
                        (str(path), canonical(info), kind, canonical(payload)))
        self.db.commit()

    def close(self):
        self.db.close(); self.lock.close()


def hash_stream(stream, size, progress):
    digest, count = hashlib.sha256(), 0
    while count < size:
        data = progress.read(stream, min(CHUNK, size - count))
        if not data:
            raise AuditIssue('truncated_file', 'File ended before its declared size', error=True)
        count += len(data); digest.update(data)
    # This read also forces ZipExtFile to finish its CRC check for empty members.
    if stream.read(1):
        raise AuditIssue('size_changed', 'Source grew beyond its declared size', error=True)
    return digest.hexdigest()


def unchanged(path, expected):
    if identity(path) != expected:
        raise AuditIssue('source_changed', 'Source identity changed during audit; rerun', error=True)


def hash_file(path, info, cache, progress):
    unchanged(path, info)
    found = cache.get(path, info, 'raw-sha256')
    if found:
        progress.state['bytes_from_cache'] += info['size']
        progress.state['cache_hits'] += 1
        return found['sha256']
    with progress.unit(info['size']), path.open('rb') as source:
        fd = os.fstat(source.fileno())
        if (fd.st_ino, fd.st_dev, fd.st_size, fd.st_mtime_ns, fd.st_ctime_ns) != tuple(info[k] for k in ('ino', 'dev', 'size', 'mtime_ns', 'ctime_ns')):
            raise AuditIssue('source_changed', 'Source changed before opening', error=True)
        value = hash_stream(source, info['size'], progress)
    unchanged(path, info)
    cache.put(path, info, 'raw-sha256', {'sha256': value})
    progress.state['files_hashed'] += 1
    return value


def zip_directory_bounds(path, max_members, max_directory):
    """Bound ZipFile's central-directory allocation, including ZIP64 archives."""
    with path.open('rb') as source:
        source.seek(0, 2); length = source.tell()
        source.seek(max(0, length - 65557)); tail = source.read(65557)
        offset = tail.rfind(b'PK\x05\x06')
        if offset < 0 or len(tail) - offset < 22:
            raise AuditIssue('invalid_zip', 'ZIP end-of-directory is missing', error=True)
        end = struct.unpack_from('<4s4H2LH', tail, offset)
        if offset + 22 + end[7] != len(tail) or end[1] or end[2] or end[3] != end[4]:
            raise AuditIssue('unsupported_zip_layout', 'ZIP is split, has trailing data, or an ambiguous directory')
        entries, directory_size = end[4], end[5]
        absolute_end = length - len(tail) + offset
        directory_end = absolute_end
        source.seek(max(0, absolute_end - 20)); locator = source.read(20)
        if locator[:4] == b'PK\x06\x07' or entries == 65535 or directory_size == 0xffffffff or end[6] == 0xffffffff:
            if len(locator) != 20 or locator[:4] != b'PK\x06\x07':
                raise AuditIssue('invalid_zip64', 'ZIP64 locator is missing', error=True)
            _, disk, record_offset, disks = struct.unpack('<4sLQL', locator)
            if disk or disks != 1 or record_offset + 56 > absolute_end:
                raise AuditIssue('unsupported_zip_layout', 'Split or inconsistent ZIP64 layout')
            source.seek(record_offset); record = source.read(56)
            if len(record) != 56 or record[:4] != b'PK\x06\x06':
                raise AuditIssue('invalid_zip64', 'ZIP64 directory record is missing', error=True)
            fields = struct.unpack('<4sQ2H2L4Q', record)
            if fields[1] < 44 or fields[1] > 1024**2 or fields[4] or fields[5] or fields[6] != fields[7]:
                raise AuditIssue('unsupported_zip_layout', 'Unsupported ZIP64 directory layout')
            entries, directory_size = fields[7], fields[8]
            directory_end = record_offset
        if entries > max_members or directory_size > max_directory:
            raise AuditIssue('zip_directory_limit', 'ZIP directory exceeds configured inspection limits')
        # Do not trust an adversarial EOCD count: count bounded central records
        # before ZipFile allocates one Python object for each of them.
        start = directory_end - directory_size
        if start < 0:
            raise AuditIssue('invalid_zip', 'ZIP central directory starts outside the file', error=True)
        source.seek(start); actual_entries = 0
        while source.tell() < directory_end:
            header = source.read(46)
            if len(header) != 46 or header[:4] != b'PK\x01\x02':
                raise AuditIssue('unsupported_zip_layout', 'Central directory is not a plain member list')
            variable_size = sum(struct.unpack_from('<3H', header, 28))
            if source.tell() + variable_size > directory_end:
                raise AuditIssue('invalid_zip', 'ZIP central-directory entry is truncated', error=True)
            source.seek(variable_size, 1); actual_entries += 1
            if actual_entries > max_members:
                raise AuditIssue('zip_directory_limit', 'Actual ZIP member count exceeds configured limit')
        if actual_entries != entries:
            raise AuditIssue('invalid_zip', 'ZIP declared and actual member counts differ', error=True)


def member_name(name):
    path = PurePosixPath(name)
    if not name or '\\' in name or '\x00' in name or ':' in name or path.is_absolute() or '..' in path.parts or '.' in name.split('/'):
        raise AuditIssue('unsafe_zip_path', 'ZIP contains an unsafe or ambiguous member path', error=True)
    return path.as_posix()


def inspect_zip(path, limits):
    zip_directory_bounds(path, limits['max_zip_members'], limits['max_zip_directory'])
    members, directories, names, total = [], set(), set(), 0
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            name = member_name(item.orig_filename)
            folded = name.casefold()
            if folded in names:
                raise AuditIssue('ambiguous_zip_names', 'ZIP has duplicate or case-colliding paths')
            names.add(folded)
            mode = item.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise AuditIssue('zip_special_file', 'ZIP contains a link or special file', error=True)
            if item.flag_bits & 1:
                raise AuditIssue('encrypted_zip', 'Encrypted ZIP members are not inspected')
            if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}:
                raise AuditIssue('unsupported_zip_compression', 'ZIP compression method is unsupported')
            for parent in PurePosixPath(name).parents:
                if str(parent) != '.': directories.add(str(parent))
            if item.is_dir():
                directories.add(name)
                continue
            total += item.file_size
            if item.file_size > limits['max_zip_member_bytes'] or total > limits['max_zip_bytes']:
                raise AuditIssue('zip_expansion_limit', 'ZIP expanded size exceeds configured limits')
            if item.file_size > max(item.compress_size, 1) * limits['max_zip_ratio']:
                raise AuditIssue('zip_ratio_limit', 'ZIP expansion ratio exceeds configured limit')
            members.append({'path': name, 'size': item.file_size, 'crc32': item.CRC,
                            'executable': mode & 0o111})
    file_names = {m['path'].casefold() for m in members}
    if any(directory.casefold() in file_names for directory in directories):
        raise AuditIssue('ambiguous_zip_names', 'ZIP contains file/directory path collisions')
    if not members:
        raise AuditIssue('empty_zip', 'ZIP has no content files', error=True)
    return {'members': sorted(members, key=lambda item: item['path']),
            'directories': sorted(directories), 'size': total}


def hash_zip(path, info, inventory, cache, progress):
    unchanged(path, info)
    kind = 'zip-members-sha256:' + signature(inventory)
    found = cache.get(path, info, kind)
    if found:
        progress.state['bytes_from_cache'] += inventory['size']
        progress.state['cache_hits'] += 1
        return found
    members = []
    with progress.unit(inventory['size']), zipfile.ZipFile(path) as archive:
        for item in inventory['members']:
            with archive.open(item['path']) as source:
                digest = hash_stream(source, item['size'], progress)
            members.append(dict(item, sha256=digest))
            progress.state['zip_members_hashed'] += 1
    unchanged(path, info)
    cache.put(path, info, kind, members)
    return members


def filename_labels(name):
    """Extract only explicit filename labels; never infer language from a title."""
    labels = []
    for match in re.finditer(r'\(([^()]*)\)|\[([^\[\]]*)\]', name):
        text = match.group(1) or match.group(2)
        for kind, pattern in (
            ('region', r'\b(usa|japan|europe|world|asia|korea|china|taiwan|hong kong|jpn|eur|us|jp)\b|[美日欧港台韩]版'),
            ('translation', r'\b(translation|translated|english patch)\b|汉化|漢化|简体|簡體|繁体|繁體|中文|^T[+-]'),
            ('revision', r'\b(rev(?:ision)?[ ._-]*[\w.]+|v\d+(?:\.\d+)+)\b|修订|修訂'),
            ('variant', r'\b(hack|beta|proto(?:type)?|demo|unl|pirate)\b|修改|测试|測試|盗版|盜版'),
        ):
            if re.search(pattern, text, re.I): labels.append({'kind': kind, 'evidence': match.group(0)})
    outside = re.sub(r'\([^()]*\)|\[[^\[\]]*\]', '', name)
    for match in re.finditer(r'汉化版|漢化版|简体中文|簡體中文|繁体中文|繁體中文', outside):
        labels.append({'kind': 'translation', 'evidence': match.group(0)})
    return labels


class Auditor:
    def __init__(self, rom_root, cache, *, path_prefix='/roms', hash_all=False,
                 rate_mib=32, max_read_bytes=0, progress=None, finish_unit=False, **limits):
        self.root = Path(rom_root).resolve(strict=True)
        self.prefix = PurePosixPath(path_prefix)
        self.cache = cache
        self.hash_all = hash_all
        self.progress = Progress(rate_mib, max_read_bytes, progress, finish_unit)
        self.limits = dict(max_directory_files=200000, max_zip_members=100000,
                           max_zip_directory=64 * 1024**2, max_zip_bytes=128 * 1024**3,
                           max_zip_member_bytes=64 * 1024**3, max_zip_ratio=10000)
        self.limits.update(limits)

    def path(self, value):
        if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
            raise AuditIssue('invalid_source_path', 'Catalog path is not a valid POSIX path', error=True)
        logical = PurePosixPath(value)
        if '..' in logical.parts:
            raise AuditIssue('unsafe_source_path', 'Catalog path contains parent traversal', error=True)
        if logical.is_absolute():
            try:
                relative = logical.relative_to(self.prefix)
                candidate = self.root / str(relative)
            except ValueError:
                candidate = Path(value)
        else:
            candidate = self.root / value
        if not inside(candidate, self.root):
            raise AuditIssue('outside_rom_root', 'Catalog path is outside the ROM root', error=True)
        current = self.root
        for part in candidate.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise AuditIssue('source_symlink', 'Symlink source requires separate manual review')
        if not candidate.exists():
            raise AuditIssue('missing_source', 'Catalog references a missing source', error=True)
        return candidate

    def relative(self, path):
        return path.relative_to(self.root).as_posix()

    def resource_root(self, game, primary):
        if game.get('resource_root'):
            folder = self.path(game['resource_root'])
            if folder == self.root or not folder.is_dir() or not inside(primary, folder):
                raise AuditIssue('invalid_resource_root', 'resource_root must contain the launch entry', error=True)
            return folder
        if primary.is_dir():
            if primary == self.root:
                raise AuditIssue('unknown_directory_boundary', 'The whole ROM root is not a dedicated game directory')
            return primary
        parts = primary.relative_to(self.root).parts
        # Only a distinct child of a normalized collection is inferred as a game.
        if len(parts) >= 6 and parts[0] == 'library':
            return self.root.joinpath(*parts[:5])
        raise AuditIssue('unknown_directory_boundary', 'Complete game folder is unknown; supply resource_root')

    def walk(self, folder):
        files, dirs, count = [], [], 0
        def failure(error): raise error
        for directory, subdirs, names in os.walk(folder, followlinks=False, onerror=failure):
            for name in sorted(subdirs + names):
                path = Path(directory) / name
                self.path(str(path))  # Reject every symlink, including empty symlink directories.
                count += 1
                if count > self.limits['max_directory_files']:
                    raise AuditIssue('directory_entry_limit', 'Game directory exceeds configured entry limit')
                if path.is_dir(): dirs.append(path.relative_to(folder).as_posix())
                else:
                    identity(path)
                    files.append(path)
            self.progress.tick()
        return sorted(files), sorted(dirs)

    def discover(self, game):
        result = {key: game.get(key) for key in ('id', 'title', 'platform', 'native_core', 'browser_core', 'engine_hint')}
        result.update(relative_rom_path=None, source_manifest=[], errors=[], unverified=[],
                      filename_labels=[], raw={'status': 'unverified'}, content={'status': 'unverified'})
        if 'profile_usage' in game: result['profile_usage'] = game['profile_usage']
        plan = {'result': result, 'game': game}
        try:
            primary = self.path(game.get('rom_path'))
            result['relative_rom_path'] = self.relative(primary)
            result['filename_labels'] = filename_labels(primary.name)
            declared = [self.path(value) for value in game.get('files', [])]
            launches = [self.path(value) for value in game.get('launch_files', [])] or [primary]
            all_paths = set([primary] + declared + launches)
            pc = game['platform'] in PC_PLATFORMS and primary.suffix.lower() not in ZIP_SUFFIXES | OPAQUE_ARCHIVES
            folder_needed = (game.get('resource_root') or pc or primary.is_dir() or
                             primary.suffix.lower() in {'.exe', '.bat', '.com', '.conf'} or
                             len(all_paths) > 1 or primary.suffix.lower() in LINKS)
            if folder_needed:
                folder = self.resource_root(game, primary)
                if any(not inside(path, folder) for path in all_paths):
                    raise AuditIssue('dependency_outside_resource_root', 'Declared resources lie outside the complete game folder')
                files, directories = self.walk(folder)
                if not files:
                    raise AuditIssue('empty_resource_directory', 'Game resource directory has no files', error=True)
                kind, labels = 'directory_tree', [p.relative_to(folder).as_posix() for p in files]
                launch_labels = [p.relative_to(folder).as_posix() for p in launches]
                # References may not escape the audited closure; do not execute launch scripts.
                for path in files:
                    if path.suffix.lower() in LINKS: self.check_links(path, folder)
                result['resource_root'] = self.relative(folder)
            else:
                files, directories, labels, launch_labels = [primary], [], ['@source'], ['@source']
                kind = 'single_file'
                # Adjacent same-stem patches/config/data must never be dropped.
                siblings = [p for p in primary.parent.iterdir() if p != primary and p not in self.catalog_sources and
                            (p.name.casefold().startswith(primary.stem.casefold() + '.') or p.name.casefold() == primary.stem.casefold())]
                if siblings:
                    raise AuditIssue('adjacent_resources', 'Same-stem sidecars exist; supply the complete resource_root')
            source = []
            for path, label in zip(files, labels):
                source.append(dict(relative_path=self.relative(path), path_in_closure=label, **identity(path)))
            plan.update(paths=files, kind=kind, directories=directories, launch_labels=launch_labels)
            result['source_manifest'] = source
            result['scope'] = kind
            result['entry_signature'] = signature({'version': VERSION, 'platform': game['platform'],
                'relative_rom_path': result['relative_rom_path'], 'sources': source,
                'directories': directories, 'launch_files': launch_labels,
                'native_core': game.get('native_core'), 'browser_core': game.get('browser_core'), 'engine_hint': game.get('engine_hint')})
            result['entry_signature_algorithm'] = 'sha256-stat-manifest-v1'
            plan['raw_key'] = self.raw_layout(plan)
            if game['platform'] in ARCADE_PLATFORMS:
                result['unverified'].append({'code': 'external_arcade_dependencies',
                    'message': 'Archive equality does not verify parent ROM sets, BIOS or emulator compatibility'})
            if kind == 'directory_tree':
                plan['content_kind'] = 'directory_tree'
                plan['content_key'] = plan['raw_key']
            elif primary.suffix.lower() in ZIP_SUFFIXES:
                try:
                    inventory = inspect_zip(primary, self.limits)
                    unchanged(primary, {k: source[0][k] for k in identity(primary)})
                    plan['zip'] = inventory
                    # Exactly one file, no additional empty directory, known cartridge suffix.
                    member = inventory['members'][0] if len(inventory['members']) == 1 else None
                    parents = {str(p) for p in PurePosixPath(member['path']).parents if str(p) != '.'} if member else set()
                    single = member and Path(member['path']).suffix.lower() in CARTRIDGES.get(game['platform'], set()) and set(inventory['directories']) == parents
                    plan['content_kind'] = 'single_cartridge_payload' if single else 'zip_tree'
                    plan['content_key'] = (game['platform'], 'single_cartridge_payload', member['size']) if single else (
                        game['platform'], 'zip_tree', canonical([(m['path'], m['size'], m['executable']) for m in inventory['members']]), canonical(inventory['directories']))
                    result['zip_inventory'] = inventory
                except (AuditIssue, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                    self.issue(result, error, 'zip_inspection')
            elif primary.suffix.lower() in CARTRIDGES.get(game['platform'], set()):
                plan['content_kind'] = 'single_cartridge_payload'
                plan['content_key'] = (game['platform'], 'single_cartridge_payload', source[0]['size'])
            else:
                result['unverified'].append({'code': 'opaque_content', 'message': 'Only original-file equality is supported; no decoded-content equivalence claimed'})
        except (AuditIssue, OSError, ValueError, TypeError) as error:
            self.issue(result, error, 'discovery')
            plan.pop('raw_key', None); plan.pop('content_key', None)
        return plan

    def check_links(self, path, folder):
        if path.stat().st_size > 1024**2:
            raise AuditIssue('link_file_limit', 'Disc reference file is too large')
        text = path.read_text(encoding='utf-8-sig', errors='strict')
        if path.suffix.lower() == '.cue':
            refs = [a or b for a, b in re.findall(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+', text, re.I | re.M)]
        elif path.suffix.lower() == '.gdi':
            refs = [a or b for a, b in re.findall(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"([^"]+)"|(\S+))', text, re.M)]
        else:
            refs = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if not refs:
            raise AuditIssue('unparsed_disc_references', 'Disc reference file has no recognized entries')
        for ref in refs:
            if PurePosixPath(ref).is_absolute() or '\\' in ref or ':' in ref:
                raise AuditIssue('unsafe_disc_reference', 'Disc reference is absolute or ambiguous', error=True)
            target = Path(os.path.normpath(str(path.parent / ref)))
            if not inside(target, folder):
                raise AuditIssue('dependency_outside_resource_root', 'Disc reference escapes the audited resource folder')
            self.path(str(target))

    @staticmethod
    def raw_layout(plan):
        result = plan['result']
        if plan['kind'] == 'single_file':
            return (result['platform'], 'single_file', result['source_manifest'][0]['size'])
        return (result['platform'], 'directory_tree', canonical([
            (item['path_in_closure'], item['size'], item['executable']) for item in result['source_manifest']]),
            canonical(plan['directories']), canonical(plan['launch_labels']))

    def issue(self, result, error, stage):
        fatal = error.error if isinstance(error, AuditIssue) else True
        code = error.code if isinstance(error, AuditIssue) else ('invalid_zip' if isinstance(error, zipfile.BadZipFile) else type(error).__name__)
        message = str(error).replace(str(self.root), '<rom_root>')
        result['errors' if fatal else 'unverified'].append({'code': code, 'stage': stage, 'message': message})

    def raw_hash(self, plan):
        result = plan['result']
        for path, item in zip(plan['paths'], result['source_manifest']):
            expected = {key: item[key] for key in ('size', 'mtime_ns', 'dev', 'ino', 'ctime_ns', 'executable')}
            item['raw_sha256'] = hash_file(path, expected, self.cache, self.progress)
        if plan['kind'] == 'single_file':
            fingerprint = result['source_manifest'][0]['raw_sha256']
        else:
            fingerprint = signature({'files': [(i['path_in_closure'], i['size'], i['executable'], i['raw_sha256']) for i in result['source_manifest']],
                                     'directories': plan['directories'], 'launch_files': plan['launch_labels']})
        result['raw'] = {'status': 'verified', 'kind': plan['kind'], 'sha256': fingerprint,
                         'hash_scope': 'whole_source_file' if plan['kind'] == 'single_file' else 'complete_resource_manifest'}

    def content_hash(self, plan):
        result, kind = plan['result'], plan['content_kind']
        if result['raw']['status'] != 'verified': self.raw_hash(plan)
        if 'zip' in plan:
            item = result['source_manifest'][0]
            info = {key: item[key] for key in ('size', 'mtime_ns', 'dev', 'ino', 'ctime_ns', 'executable')}
            members = hash_zip(plan['paths'][0], info, plan['zip'], self.cache, self.progress)
            result['zip_members'] = members
            fingerprint = members[0]['sha256'] if kind == 'single_cartridge_payload' else signature({
                'files': [(m['path'], m['size'], m['executable'], m['sha256']) for m in members], 'directories': plan['zip']['directories']})
        else:
            if result['raw']['status'] != 'verified': self.raw_hash(plan)
            fingerprint = result['raw']['sha256']
        result['content'] = {'status': 'verified', 'kind': kind, 'sha256': fingerprint,
                             'hash_scope': 'single_payload' if kind == 'single_cartridge_payload' else 'complete_resource_manifest'}

    def run(self, records, platforms=None):
        selected = [g for g in records if not platforms or g.get('platform') in platforms]
        ids = [g.get('id') for g in selected]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError('Catalog game IDs must be nonempty and unique')
        if any(not isinstance(g.get('platform'), str) or not g['platform'] for g in selected):
            raise ValueError('Each game needs a platform')
        self.catalog_sources = set()
        for game in selected:
            try: self.catalog_sources.add(self.path(game.get('rom_path')))
            except (AuditIssue, OSError, ValueError, TypeError): pass
        self.progress.state['games_total'] = len(selected)
        plans = []
        for game in selected:
            self.progress.state['current'] = game['id']
            plans.append(self.discover(game))
            self.progress.state['games_discovered'] += 1
            self.progress.tick(self.progress.state['games_discovered'] % 250 == 0)
        candidates = {'raw': defaultdict(list), 'content': defaultdict(list)}
        for plan in plans:
            for kind in candidates:
                if kind + '_key' in plan: candidates[kind][plan[kind + '_key']].append(plan)
        targets = {kind: {p['result']['id'] for group in groups.values() if self.hash_all or len(group) > 1 for p in group}
                   for kind, groups in candidates.items()}
        self.progress.state.update(phase='hash', candidate_games=len(targets['raw'] | targets['content']))
        stopped = False
        for plan in plans:
            result = plan['result']; self.progress.state['current'] = result['id']
            for kind in ('raw', 'content'):
                if kind + '_key' not in plan: continue
                if result['id'] not in targets[kind]:
                    result[kind] = {'status': 'not_hashed', 'reason': 'unique_size_or_layout'}
                    continue
                if stopped:
                    result[kind] = {'status': 'unverified', 'reason': 'read_budget_reached'}
                    continue
                try:
                    (self.raw_hash if kind == 'raw' else self.content_hash)(plan)
                except BudgetReached as error:
                    stopped = True; self.issue(result, error, kind)
                    result[kind] = {'status': 'unverified', 'reason': error.code}
                except (AuditIssue, OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                    self.issue(result, error, kind)
                    result[kind] = {'status': 'error' if result['errors'] else 'unverified'}
            self.progress.tick()
        # Re-stat complete closures before grouping, including additions/removals.
        for plan in plans:
            result = plan['result']
            if not any(result[k]['status'] == 'verified' for k in ('raw', 'content')): continue
            try:
                for path, item in zip(plan['paths'], result['source_manifest']):
                    self.path(str(path))
                    unchanged(path, {key: item[key] for key in ('size', 'mtime_ns', 'dev', 'ino', 'ctime_ns', 'executable')})
                if plan['kind'] == 'directory_tree':
                    files, directories = self.walk(self.root / result['resource_root'])
                    if files != plan['paths'] or directories != plan['directories']:
                        raise AuditIssue('source_changed', 'Resource directory contents changed during audit', error=True)
            except (AuditIssue, OSError) as error:
                self.issue(result, error, 'final_identity')
                result['raw'] = result['content'] = {'status': 'error', 'reason': 'source_changed'}
        games = [p['result'] for p in plans]
        duplicates = {kind + '_exact': self.groups(games, kind) for kind in ('raw', 'content')}
        self.progress.state.update(phase='budget_reached' if stopped else 'complete', current=None)
        self.progress.tick(True)
        return {'schema_version': VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
                'read_only': True, 'deletion_authorized': False, 'catalog_entries': len(records),
                'selected_platforms': sorted(platforms) if platforms else None,
                'hash_policy': 'all_eligible' if self.hash_all else 'same_platform_size_layout_candidates',
                'read_budget': {'limit_bytes': self.progress.budget,
                    'policy': 'finish_unit' if self.progress.finish_unit else 'hard',
                    'actual_hash_bytes': self.progress.state['bytes_hashed'],
                    'overrun_bytes': self.progress.state['budget_overrun_bytes'],
                    'unit': 'one raw source file or all expanded ZIP members',
                    'includes_metadata_reads': False,
                    'explanation': ('Batch limit; a first oversized uncached unit may finish, '
                                    'then the next non-fitting unit is deferred before any reads')
                                   if self.progress.finish_unit else 'Hard limit on raw and expanded hash input bytes'},
                'completed': not stopped, 'all_content_verified': all(g['content']['status'] == 'verified' for g in games),
                'summary': dict(games=len(games), raw_verified=sum(g['raw']['status'] == 'verified' for g in games),
                                content_verified=sum(g['content']['status'] == 'verified' for g in games),
                                games_with_errors=sum(bool(g['errors']) for g in games),
                                duplicate_groups={kind: len(groups) for kind, groups in duplicates.items()}),
                'progress': dict(self.progress.state), 'games': games, 'duplicates': duplicates}

    @staticmethod
    def groups(games, kind):
        buckets = defaultdict(list)
        for game in games:
            value = game[kind]
            if value['status'] == 'verified': buckets[(game['platform'], value['kind'], value['sha256'])].append(game)
        groups = []
        for (platform, scope, digest), members in sorted(buckets.items()):
            if len(members) < 2: continue
            contexts = {(g.get('native_core'), g.get('browser_core'), g.get('engine_hint')) for g in members}
            reasons = []
            if len(contexts) > 1: reasons.append('runtime_context_mismatch')
            if platform in ARCADE_PLATFORMS: reasons.append('dependencies_unverified')
            if any(g['errors'] for g in members): reasons.append('integrity_errors')
            if kind != 'content' or scope != 'single_cartridge_payload':
                reasons.append('game_dependency_closure_not_proven')
            groups.append({'id': signature([kind, platform, scope, digest])[:24], 'platform': platform,
                'scope': scope, 'sha256': digest, 'game_ids': [g['id'] for g in members],
                'relative_rom_paths': [g['relative_rom_path'] for g in members],
                'runtime_context_mismatch': len(contexts) > 1,
                'auto_merge_safe': not reasons, 'auto_merge_scope': 'catalog_aliases_preserving_all_ids_and_saves',
                'merge_blocking_reasons': reasons, 'automatic_file_deletion_allowed': False,
                'proof': 'Complete source bytes match' if kind == 'raw' else 'Complete payload/resource bytes and required layout match',
                'external_dependencies_verified': False})
        return groups


def write_csv(path, report):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['id', 'title', 'platform', 'relative_rom_path', 'entry_signature', 'raw_status',
              'raw_scope', 'raw_sha256', 'content_status', 'content_scope', 'content_sha256',
              'filename_labels', 'errors', 'unverified']
    with tempfile.NamedTemporaryFile('w', encoding='utf-8-sig', newline='', dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader()
            for game in report['games']:
                row = {key: game.get(key, '') for key in fields[:5]}
                for kind in ('raw', 'content'):
                    row.update({kind + '_status': game[kind]['status'], kind + '_scope': game[kind].get('kind', ''), kind + '_sha256': game[kind].get('sha256', '')})
                row.update({key: canonical(game[key]) for key in ('filename_labels', 'errors', 'unverified')})
                # A report opened in a spreadsheet must not execute filename formulas.
                writer.writerow({key: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for key, value in row.items()})
            output.flush(); os.fsync(output.fileno()); os.replace(temp, path)
        finally: temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--rom-root', required=True, type=Path)
    parser.add_argument('--path-prefix', default=None)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--csv', type=Path, default=None)
    parser.add_argument('--progress', type=Path, default=None)
    parser.add_argument('--platform', action='append', help='Repeat or comma-separate platform IDs')
    parser.add_argument('--hash-all', action='store_true')
    parser.add_argument('--rate-mib', type=float, default=32, help='Hashing MiB/s, 0 is unlimited')
    parser.add_argument('--max-read-gib', type=float, default=0, help='Per-run hash read budget, 0 is unlimited')
    parser.add_argument('--finish-unit', action='store_true',
                        help='Use a soft batch budget: finish the first oversized file/ZIP tree; defer other units before reading')
    parser.add_argument('--max-directory-files', type=int, default=200000)
    parser.add_argument('--max-zip-members', type=int, default=100000)
    parser.add_argument('--max-zip-gib', type=float, default=128)
    parser.add_argument('--max-zip-member-gib', type=float, default=64)
    args = parser.parse_args(argv)
    if min(args.rate_mib, args.max_read_gib) < 0 or min(args.max_directory_files, args.max_zip_members, args.max_zip_gib, args.max_zip_member_gib) <= 0:
        parser.error('Rates/budgets must be nonnegative and inspection limits positive')
    root = args.rom_root.resolve(strict=True)
    csv_path = args.csv or args.output.with_suffix('.csv')
    progress_path = args.progress or args.output.with_suffix('.progress.json')
    outputs = [args.cache, args.output, csv_path, progress_path]
    if len({p.resolve() for p in outputs}) != len(outputs) or any(inside(p.resolve(), root) or p.resolve() == args.catalog.resolve() for p in outputs):
        parser.error('Cache and reports must be distinct paths outside the read-only ROM root and catalog input')
    if args.catalog.stat().st_size > 256 * 1024**2: parser.error('Catalog JSON exceeds 256 MiB')
    data = json.loads(args.catalog.read_text(encoding='utf-8-sig'))
    records = data if isinstance(data, list) else data['games']
    platforms = {p for value in args.platform or [] for p in value.split(',') if p} or None
    def progress(state):
        atomic_json(progress_path, state)
        print(json.dumps(state, ensure_ascii=False), file=sys.stderr, flush=True)
    cache = Cache(args.cache)
    try:
        auditor = Auditor(root, cache, path_prefix=args.path_prefix or (data.get('path_prefix', '/roms') if isinstance(data, dict) else '/roms'),
                          hash_all=args.hash_all, rate_mib=args.rate_mib, max_read_bytes=int(args.max_read_gib * 1024**3), progress=progress,
                          finish_unit=args.finish_unit,
                          max_directory_files=args.max_directory_files, max_zip_members=args.max_zip_members,
                          max_zip_bytes=int(args.max_zip_gib * 1024**3), max_zip_member_bytes=int(args.max_zip_member_gib * 1024**3))
        report = auditor.run(records, platforms)
        atomic_json(args.output, report); write_csv(csv_path, report)
        print(json.dumps(report['summary'], ensure_ascii=False))
        return 0 if report['completed'] else 2
    except KeyboardInterrupt:
        progress(dict(phase='interrupted', resumable=True, message='Completed file hashes remain in SQLite; rerun the same command'))
        return 130
    finally: cache.close()


if __name__ == '__main__':
    sys.exit(main())
