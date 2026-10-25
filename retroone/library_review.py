"""Private audit evidence, separate from game identity and player saves.

Only complete cartridge matches or fully hashed Switch NCA sets can collapse list entries. All
original IDs remain addressable. Evidence is invalidated when source stats
change; importing a report is never evidence that an emulator was playable.
"""
from __future__ import annotations

import json
import re
import threading
import os
import stat
import tempfile
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

LABELS = {
    'unverified': '尚未检测', 'fingerprinted': '内容指纹已记录',
    'archive_verified': '压缩包校验通过', 'dependency_error': '缺少或损坏游戏文件',
    'load_failed': '启动检测失败', 'boot_observed': '已观察到启动画面',
    'input_observed': '已观察到输入响应', 'gameplay_observed': '已观察到游戏画面',
    'runtime_unavailable': '检测环境暂不可用', 'stale': '文件已变化，待重新检测',
}
ID = re.compile(r'^[a-f0-9]{24}$')
SHA = re.compile(r'^[a-f0-9]{64}$')


@dataclass(frozen=True)
class _Sidecars:
    stamp: tuple
    prefix: str
    names: frozenset[str]
    folded: frozenset[str]
    ordered: list[str]


@dataclass
class _ValidationBatch:
    # Only directory work is shared. Every source is still lstat'ed on every
    # validation, and each public validation call starts a fresh batch.
    parents: dict[Path, bool] = field(default_factory=dict)
    sidecars: dict[Path, _Sidecars] = field(default_factory=dict)


def publish_report(report: dict, destination: Path) -> dict:
    """Publish audit evidence, preserving only explicitly untargeted platforms."""
    if report.get('schema_version') != 1 or report.get('read_only') is not True:
        raise ValueError('Unsupported audit report')
    games = {}
    for game in report['games']:
        fingerprint = game.get('content', {})
        if fingerprint.get('status') != 'verified':
            fingerprint = game.get('raw', {})
        errors = game.get('errors', [])
        record = {key: game.get(key) for key in ('platform', 'native_core', 'browser_core', 'engine_hint', 'resource_root')}
        record['primary'] = game.get('relative_rom_path')
        record['sources'] = [{k: s[k] for k in ('relative_path', 'size', 'mtime_ns', 'ctime_ns', 'ino')} for s in game.get('source_manifest', [])]
        status = 'unverified'
        if fingerprint.get('status') == 'verified':
            status = 'fingerprinted'
            record['fingerprint'] = {'sha256': fingerprint['sha256'], 'scope': fingerprint['kind'], 'complete': True}
        if errors:
            status = 'dependency_error'
        record['verification'] = {'status': status, 'checked_at': report.get('generated_at'),
                                  'notes': ['指纹检测不代表已启动或已通过游戏测试'],
                                  'errors': [str(e.get('message', e.get('code', '')))[:300] for e in errors]}
        if record.get('fingerprint') or errors:
            games[game['id']] = record
    groups = [{key: group[key] for key in ('id', 'scope', 'game_ids', 'auto_merge_safe')}
              for group in report.get('duplicates', {}).get('content_exact', []) if group.get('auto_merge_safe') is True]
    result = {'schema_version': 1, 'generated_at': report.get('generated_at'), 'games': games, 'groups': groups,
              'known_sources': [g['relative_rom_path'] for g in report['games'] if g.get('relative_rom_path')],
              'summary': report.get('summary', {}), 'hash_policy': report.get('hash_policy'), 'completed': report.get('completed')}
    # An explicit platform filter is a partition update, not a whole-library
    # replacement. Reported IDs are always authoritative for ordinary evidence,
    # even if the filter and the record's platform disagree. A budget-limited
    # unfiltered report remains a replacement; absence alone is not a partition.
    selected = report.get('selected_platforms')
    selected = set(selected) if (isinstance(selected, list) and selected and
                                all(isinstance(value, str) and value for value in selected)) else None
    # The general audit cannot decode Switch containers. Retain separately
    # verified full-NCA proofs across a later general scan, only while the new
    # source manifest and launch context still agree. Reader rechecks disk stats.
    try:
        if destination.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Previous index exceeds limit')
        old = json.loads(destination.read_text())
        if old.get('schema_version') != 1:
            raise ValueError('Unsupported previous index')
        latest = {game['id']: game for game in report['games']}
        retained, untouched = set(), set()
        for identifier, entry in old.get('games', {}).items():
            if not isinstance(entry, dict):
                continue
            new = latest.get(identifier)
            if (selected is not None and new is None and isinstance(entry.get('platform'), str)
                    and entry['platform'] not in selected):
                games[identifier] = entry
                retained.add(identifier)
                untouched.add(identifier)
                continue
            if entry.get('fingerprint', {}).get('scope') != 'switch_nca_set':
                continue
            if new is not None:
                sources = [{key: source.get(key) for key in ('relative_path', 'size', 'mtime_ns', 'ctime_ns', 'ino')}
                           for source in new.get('source_manifest', [])]
                if (new.get('errors') or sources != entry.get('sources') or any(new.get(key) != entry.get(key)
                        for key in ('platform', 'native_core', 'browser_core', 'engine_hint'))):
                    continue
            games[identifier] = entry
            retained.add(identifier)
        new_group_ids = {group['id'] for group in groups}
        groups.extend(group for group in old.get('groups', [])
                      if group.get('id') not in new_group_ids and group.get('game_ids')
                      and (set(group['game_ids']).issubset(untouched)
                           or (group.get('scope') == 'switch_nca_set'
                               and set(group['game_ids']).issubset(retained))))
        result['known_sources'] = sorted(set(result['known_sources']) | {
            source['relative_path'] for identifier in retained for source in games[identifier].get('sources', [])})
    except (OSError, ValueError, TypeError, KeyError):
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=destination.parent, encoding='utf-8', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(result, handle, ensure_ascii=False, separators=(',', ':'))
            handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def filename_tags(name: str) -> list[dict]:
    tags = []
    patterns = {
        'region': r'\b(USA|Japan|Europe|World|Asia|Korea|China|Taiwan|JPN|EUR|US|JP|EU|CN|CHN)\b|[美日欧中港台韩]版',
        'translation': r'\b(translation|translated|english patch|CHS|CHT)\b|汉化|漢化|简体|繁体|中文|^T[+-]',
        'revision': r'\b(rev(?:ision)?[ ._-]*[\w.]+|v\d+(?:\.\d+)+)\b|修订',
        'variant': r'\b(hack|mod|patched|beta|proto(?:type)?|demo|unl|pirate)\b|修改|测试|盗版|魔改',
    }
    for match in re.finditer(r'\(([^()]*)\)|\[([^\[\]]*)\]|（([^（）]*)）|【([^【】]*)】', name):
        value = next(value for value in match.groups() if value is not None)
        for kind, pattern in patterns.items():
            if re.search(pattern, value, re.I):
                tags.append(dict(kind=kind, label=value[:100], raw=match.group(0)[:120], source='filename'))
    return tags[:24]


class ReviewIndex:
    def __init__(self, root: Path, index_path: Path | None = None):
        self.root = root.resolve()
        self.path = index_path or self.root / '.retroone' / 'library-review.json'
        self.lock = threading.RLock()
        self.key = None
        self.entries: dict[str, dict] = {}
        self.groups: dict[str, list[str]] = {}
        self.generated_at = None
        self.known_sources = set()
        self.directory_cache = {}

    def load(self):
        with self.lock:
            try:
                stat = self.path.stat()
                key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                if key == self.key:
                    return
                if stat.st_size > 64 * 1024 * 1024:
                    raise ValueError('Audit index exceeds limit')
                data = json.loads(self.path.read_text())
                if data.get('schema_version') != 1 or not isinstance(data.get('games'), dict):
                    raise ValueError('Unsupported audit index')
                entries = {k: v for k, v in data['games'].items() if ID.fullmatch(k) and isinstance(v, dict)}
                groups = {}
                for group in data.get('groups', []):
                    members = group.get('game_ids', [])
                    scope = group.get('scope')
                    # Publisher and reader both enforce the restricted proof scope.
                    if (group.get('auto_merge_safe') is True and scope in ('single_cartridge_payload', 'switch_nca_set')
                            and ID.fullmatch(str(group.get('id', ''))) and len(members) > 1
                            and len(set(members)) == len(members) and all(m in entries for m in members)):
                        records = [entries[m] for m in members]
                        contexts = {(r.get('platform'), r.get('native_core'), r.get('browser_core'), r.get('engine_hint')) for r in records}
                        fingerprints = {r.get('fingerprint', {}).get('sha256') for r in records}
                        if len(contexts) == 1 and len(fingerprints) == 1 and SHA.fullmatch(str(next(iter(fingerprints)))) and all(
                            r.get('fingerprint', {}).get('complete') is True and r.get('fingerprint', {}).get('scope') == scope
                            and len(r.get('sources', [])) == 1
                            and (scope != 'switch_nca_set' or (r.get('platform') == 'switch'
                                 and str(r['sources'][0].get('relative_path', '')).lower().endswith(('.nsp', '.xci'))
                                 and r.get('fingerprint', {}).get('algorithm') == 'sha256_full_nca_set_v1'))
                            for r in records):
                            groups[group['id']] = members
                self.entries, self.groups = entries, groups
                self.known_sources = set(data.get('known_sources', [])) | {entry.get('primary') or (entry.get('sources') or [{}])[0].get('relative_path') for entry in entries.values()}
                self.directory_cache = {}
                self.key, self.generated_at = key, data.get('generated_at')
            except (OSError, ValueError, TypeError, KeyError):
                # Do not leave stale deduplication enabled if the index vanished.
                self.entries, self.groups, self.key = {}, {}, None
                self.known_sources, self.directory_cache = set(), {}

    def _sidecars(self, path: Path, batch: _ValidationBatch) -> bool:
        parent = path.parent
        stem = path.stem.casefold()
        cached = batch.sidecars.get(parent)
        if cached is None:
            info = parent.stat()
            stamp = (info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)
            cached = self.directory_cache.get(parent)
            if cached is None or cached.stamp != stamp:
                # A collection can contain thousands of known ROMs. Compute
                # its relative prefix once, without constructing a Path for
                # every sibling. Entries of any type can be sidecars.
                relative = parent.relative_to(self.root).as_posix()
                prefix = '' if relative == '.' else relative + '/'
                with os.scandir(parent) as entries:
                    names = frozenset(item.name for item in entries
                                      if prefix + item.name not in self.known_sources)
                folded = frozenset(name.casefold() for name in names)
                cached = _Sidecars(stamp, prefix, names, folded, sorted(folded))
                self.directory_cache[parent] = cached
            batch.sidecars[parent] = cached
        if cached.prefix + path.name not in self.known_sources:
            # Legacy/noncanonical indexes may omit the current source. Preserve
            # the original explicit exclusion, including case-colliding files.
            return any(name != path.name and (name.casefold() == stem or name.casefold().startswith(stem + '.'))
                       for name in cached.names)
        prefix = stem + '.'
        index = bisect_left(cached.ordered, prefix)
        return stem in cached.folded or (index < len(cached.ordered) and cached.ordered[index].startswith(prefix))

    def current(self, entry: dict) -> bool:
        with self.lock:
            return self._current(entry, _ValidationBatch())

    def _current(self, entry: dict, batch: _ValidationBatch) -> bool:
        sources = entry.get('sources', [])
        if not sources:
            return False
        for source in sources:
            try:
                name = source['relative_path']
                relative = PurePosixPath(name)
                if relative.is_absolute() or '..' in relative.parts or '\\' in name:
                    return False
                path = self.root / name
                # One non-following stat both rejects source symlinks and
                # checks regular-file identity. Parent symlinks remain allowed
                # when their resolved destination stays within the ROM root.
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or any(getattr(info, 'st_' + key) != source[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'ino')):
                    return False
                parent = path.parent
                if parent not in batch.parents:
                    batch.parents[parent] = parent.resolve().is_relative_to(self.root)
                if not batch.parents[parent]:
                    return False
                if entry.get('fingerprint', {}).get('scope') in ('single_cartridge_payload', 'switch_nca_set') and self._sidecars(path, batch):
                    return False
            except (OSError, KeyError, TypeError, ValueError):
                return False
        if entry.get('resource_root'):
            try:
                folder = self.root / entry['resource_root']
                if not folder.resolve().is_relative_to(self.root) or folder.is_symlink():
                    return False
                expected = {item['relative_path'] for item in sources}
                actual = set()
                for directory, subdirs, files in os.walk(folder, followlinks=False):
                    if any((Path(directory) / name).is_symlink() for name in subdirs + files):
                        return False
                    actual.update((Path(directory) / name).relative_to(self.root).as_posix() for name in files)
                    if len(actual) > len(expected):
                        return False
                if actual != expected:
                    return False
            except (OSError, ValueError, TypeError):
                return False
        return True

    def rows(self) -> list[tuple]:
        """Called on index change or explicit scan; page/detail recheck evidence."""
        with self.lock:
            self.load()
            return self._rows()

    def _rows(self) -> list[tuple]:
        batch = _ValidationBatch()
        membership = {member: group for group, members in self.groups.items() for member in members}
        valid = {key: self._current(value, batch) for key, value in self.entries.items()}
        invalid_groups = {group for group, members in self.groups.items() if not all(valid[m] for m in members)}
        result = []
        for game_id, entry in self.entries.items():
            group = membership.get(game_id)
            status = (entry.get('verification') or {}).get('status', 'unverified')
            if status not in LABELS:
                status = 'unverified'
            if not valid[game_id] and entry.get('sources') and (entry.get('fingerprint') or status != 'unverified'):
                status = 'stale'
            if not valid[game_id] or group in invalid_groups:
                group = None
            result.append((game_id, group, status, json.dumps(entry, ensure_ascii=False)))
        return result

    def changed_groups(self) -> set[str]:
        """Check every hidden alias, so a changed alias cannot stay concealed."""
        with self.lock:
            batch = _ValidationBatch()
            return {g for g, members in self.groups.items() if not all(self._current(self.entries[m], batch) for m in members)}

    def public(self, game: dict, entry: dict | None, status: str | None = None) -> dict:
        review = {'variant_tags': filename_tags(Path(game.get('rom_path', '')).name),
                  'verification': {'status': 'unverified', 'label': LABELS['unverified']}}
        if not entry:
            return review
        verification = entry.get('verification', {})
        actual = status or verification.get('status', 'unverified')
        if actual not in LABELS:
            actual = 'unverified'
        current = self.current(entry)
        if not current and entry.get('sources') and (entry.get('fingerprint') or actual != 'unverified'):
            actual = 'stale'
        review['verification'] = {key: verification[key] for key in ('checked_at', 'mode', 'engine', 'notes', 'errors') if key in verification}
        review['verification'].update(status=actual, label=LABELS[actual])
        if current and entry.get('fingerprint'):
            review['fingerprint'] = entry['fingerprint']
        if current:
            for tag in entry.get('variant_tags', [])[:24]:
                if (isinstance(tag, dict) and tag.get('kind') in ('region', 'translation', 'revision', 'variant', 'package')
                        and isinstance(tag.get('label'), str) and tag.get('source') == 'package_manifest'):
                    review['variant_tags'].append({'kind': tag['kind'], 'label': tag['label'][:150], 'source': 'package_manifest'})
        return review
