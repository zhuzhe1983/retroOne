"""Physical collection locations with immutable legacy identity prefixes.

Standard library only; usable by the Python 3.8 audit/import helpers as well as
the server. A malformed declared layout must never fall back to new game IDs.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MAX_LAYOUT_BYTES = 2 * 1024**2
MAX_COLLECTIONS = 4096
LEGACY_SOURCES = frozenset({'new', 'old', 'pegasus', 'import', 'manual'})


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _relative_collection(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('游戏库布局的集合路径无效')
    path = PurePosixPath(value)
    if (path.is_absolute() or '..' in path.parts or str(path) != value or len(path.parts) < 3
            or path.parts[0] != 'library' or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', path.parts[1])):
        raise ValueError('游戏库布局的集合路径必须位于 library/平台 内，且不能包含路径穿越')
    return path


def _check_symlinks(root, relative):
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('游戏库布局路径不能经过符号链接')


def canonical_manifest(document):
    """Validate and normalize bindings for the offline migration's identity seal."""
    if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
            or document['schema_version'] != 1 or not isinstance(document.get('collections'), list)
            or len(document['collections']) > MAX_COLLECTIONS):
        raise ValueError('游戏库布局版本或集合列表无效')
    bindings, current_roots, original_roots = [], [], []
    for entry in document['collections']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'original_path'}:
            raise ValueError('游戏库布局的集合记录无效')
        current = _relative_collection(entry['path'])
        original = _relative_collection(entry['original_path'])
        if current.parts[1] != original.parts[1]:
            raise ValueError('游戏库布局不能跨平台重定位集合')
        for paths, candidate in ((current_roots, current), (original_roots, original)):
            if any(_inside(candidate, previous) or _inside(previous, candidate) for previous in paths):
                raise ValueError('游戏库布局的集合路径或原始身份路径重叠')
            paths.append(candidate)
        bindings.append({'path': str(current), 'original_path': str(original)})
    return {'schema_version': 1, 'collections': sorted(bindings, key=lambda entry: (entry['path'], entry['original_path']))}


def manifest_digest(document):
    payload = json.dumps(canonical_manifest(document), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class CollectionLocation:
    path: Path
    original_path: PurePosixPath

    @property
    def platform(self):
        return self.original_path.parts[1]


class LibraryLayout:
    def __init__(self, root, collections=(), declared=False):
        self.root = Path(root).resolve()
        self.collections = tuple(sorted(collections, key=lambda entry: (-len(entry.path.parts), str(entry.path))))
        self.declared = declared
        self._by_platform = {}
        for entry in self.collections:
            self._by_platform.setdefault(entry.platform, []).append(entry)

    @property
    def document(self):
        return canonical_manifest({'schema_version': 1, 'collections': [
            {'path': entry.path.relative_to(self.root).as_posix(), 'original_path': str(entry.original_path)}
            for entry in self.collections
        ]})

    @classmethod
    def load(cls, rom_root):
        root = Path(rom_root).resolve()
        if not root.is_dir():
            raise ValueError('ROM 根目录尚未就绪')
        relative = PurePosixPath('.retroone/library-layout.json')
        _check_symlinks(root, relative)
        sentinel = PurePosixPath('.retroone/library-layout.required')
        _check_symlinks(root, sentinel)
        manifest = root / str(relative)
        if not manifest.exists():
            if (root / str(sentinel)).exists():
                raise ValueError('游戏库已启用固定身份布局，但布局文件缺失；保留现有索引')
            return cls(root)
        if not manifest.is_file() or manifest.stat().st_size > MAX_LAYOUT_BYTES:
            raise ValueError('游戏库布局文件类型无效或超过大小限制')
        try:
            with manifest.open('rb') as source:
                payload = source.read(MAX_LAYOUT_BYTES + 1)
            if len(payload) > MAX_LAYOUT_BYTES:
                raise ValueError('游戏库布局文件超过大小限制')
            document = json.loads(payload)
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError('无法读取游戏库布局；保留现有索引') from error
        document = canonical_manifest(document)
        sentinel_path = root / str(sentinel)
        if sentinel_path.exists():
            try:
                if not sentinel_path.is_file() or sentinel_path.stat().st_size > 4096:
                    raise ValueError('invalid sentinel')
                with sentinel_path.open('rb') as source:
                    seal_bytes = source.read(4097)
                if len(seal_bytes) > 4096:
                    raise ValueError('oversized sentinel')
                seal = json.loads(seal_bytes)
                if (not isinstance(seal, dict) or set(seal) != {'schema_version', 'manifest_sha256'}
                        or type(seal.get('schema_version')) is not int or seal['schema_version'] != 1
                        or seal.get('manifest_sha256') != manifest_digest(document)):
                    raise ValueError('layout seal mismatch')
            except (OSError, UnicodeError, ValueError) as error:
                raise ValueError('游戏库布局校验不一致；请通过目录迁移工具更新布局和身份校验，保留现有索引') from error
        bindings = []
        for entry in document['collections']:
            current = _relative_collection(entry['path'])
            original = _relative_collection(entry['original_path'])
            _check_symlinks(root, current)
            _check_symlinks(root, original)
            actual = root / str(current)
            if not actual.is_dir():
                raise ValueError('游戏库布局声明的集合目录尚未就绪：' + str(current))
            if current != original and (root / str(original)).exists():
                raise ValueError('游戏库布局的新旧集合同时存在，可能产生重复游戏 ID：' + str(original))
            bindings.append(CollectionLocation(actual, original))
        return cls(root, bindings, declared=True)

    def _relative(self, path):
        path = Path(path)
        if path.is_absolute():
            path = path.relative_to(self.root)
        if '..' in path.parts or '\\' in str(path) or '\x00' in str(path):
            raise ValueError('路径不属于游戏库')
        return PurePosixPath(path.as_posix())

    def original_relative(self, path):
        relative = self._relative(path)
        actual = self.root / str(relative)
        for binding in self._by_platform.get(relative.parts[1] if len(relative.parts) > 1 else '', ()):
            if _inside(actual, binding.path):
                return (binding.original_path / actual.relative_to(binding.path).as_posix()).as_posix()
        return relative.as_posix()

    def collection_for(self, path):
        """Resolve a collection boundary, never infer a whole game closure."""
        try:
            relative = self._relative(path)
        except ValueError:
            return None
        actual = self.root / str(relative)
        for binding in self._by_platform.get(relative.parts[1] if len(relative.parts) > 1 else '', ()):
            if _inside(actual, binding.path):
                return binding.path
        parts = relative.parts
        if len(parts) < 3 or parts[0] != 'library':
            return None
        if len(parts) == 3:
            return self.root.joinpath(*parts[:2])
        depth = 4 if parts[2] in LEGACY_SOURCES and len(parts) >= 5 else 3
        return self.root.joinpath(*parts[:depth])
