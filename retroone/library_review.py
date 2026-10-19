"""Private audit evidence, separate from game identity and player saves.

Only complete, standalone cartridge matches can collapse list entries. All
original IDs remain addressable. Evidence is invalidated when source stats
change; importing a report is never evidence that an emulator was playable.
"""
from __future__ import annotations

import json
import re
import threading
import os
import tempfile
from bisect import bisect_left
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


def publish_report(report: dict, destination: Path) -> dict:
    """Reduce a private, full audit into the runtime's metadata index."""
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
        'region': r'\b(USA|Japan|Europe|World|Asia|Korea|China|Taiwan|JPN|EUR|US|JP)\b|[美日欧港台韩]版',
        'translation': r'\b(translation|translated|english patch)\b|汉化|漢化|简体|繁体|中文|^T[+-]',
        'revision': r'\b(rev(?:ision)?[ ._-]*[\w.]+|v\d+(?:\.\d+)+)\b|修订',
        'variant': r'\b(hack|beta|proto(?:type)?|demo|unl|pirate)\b|修改|测试|盗版',
    }
    for match in re.finditer(r'\(([^()]*)\)|\[([^\[\]]*)\]', name):
        value = match.group(1) or match.group(2)
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
                    # Publisher and reader both enforce the restricted proof scope.
                    if (group.get('auto_merge_safe') is True and group.get('scope') == 'single_cartridge_payload'
                            and ID.fullmatch(str(group.get('id', ''))) and len(members) > 1
                            and len(set(members)) == len(members) and all(m in entries for m in members)):
                        records = [entries[m] for m in members]
                        contexts = {(r.get('platform'), r.get('native_core'), r.get('browser_core'), r.get('engine_hint')) for r in records}
                        fingerprints = {r.get('fingerprint', {}).get('sha256') for r in records}
                        if len(contexts) == 1 and len(fingerprints) == 1 and SHA.fullmatch(str(next(iter(fingerprints)))) and all(
                            r.get('fingerprint', {}).get('complete') is True and r.get('fingerprint', {}).get('scope') == 'single_cartridge_payload'
                            and len(r.get('sources', [])) == 1 for r in records):
                            groups[group['id']] = members
                self.entries, self.groups = entries, groups
                self.known_sources = set(data.get('known_sources', [])) | {entry.get('primary') or (entry.get('sources') or [{}])[0].get('relative_path') for entry in entries.values()}
                self.directory_cache = {}
                self.key, self.generated_at = key, data.get('generated_at')
            except (OSError, ValueError, TypeError, KeyError):
                # Do not leave stale deduplication enabled if the index vanished.
                self.entries, self.groups, self.key = {}, {}, None
                self.known_sources, self.directory_cache = set(), {}

    def _sidecars(self, path: Path) -> bool:
        parent = path.parent
        stem = path.stem.casefold()
        if path.relative_to(self.root).as_posix() not in self.known_sources:
            # Legacy/noncanonical indexes may omit the current source. Preserve
            # the original explicit exclusion, including case-colliding files.
            return any(p != path and p.relative_to(self.root).as_posix() not in self.known_sources
                       and (p.name.casefold() == stem or p.name.casefold().startswith(stem + '.'))
                       for p in parent.iterdir())
        info = parent.stat()
        stamp = (info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
        cached = self.directory_cache.get(parent)
        if not cached or cached[0] != stamp:
            # A collection can contain thousands of known ROMs. Filter those
            # once per directory change, then search only unowned sidecar names.
            # Rewalking all siblings for every hidden alias makes each library
            # request quadratic in large cartridge collections.
            unknown = frozenset(p.name.casefold() for p in parent.iterdir()
                                if p.relative_to(self.root).as_posix() not in self.known_sources)
            cached = (stamp, unknown, sorted(unknown))
            self.directory_cache[parent] = cached
        prefix = stem + '.'
        index = bisect_left(cached[2], prefix)
        return stem in cached[1] or (index < len(cached[2]) and cached[2][index].startswith(prefix))

    def current(self, entry: dict) -> bool:
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
                if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    return False
                info = path.stat()
                if not path.is_file() or any(getattr(info, 'st_' + key) != source[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'ino')):
                    return False
                if entry.get('fingerprint', {}).get('scope') == 'single_cartridge_payload' and self._sidecars(path):
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
        self.load()
        membership = {member: group for group, members in self.groups.items() for member in members}
        valid = {key: self.current(value) for key, value in self.entries.items()}
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
        return {g for g, members in self.groups.items() if not all(self.current(self.entries[m]) for m in members)}

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
        return review
