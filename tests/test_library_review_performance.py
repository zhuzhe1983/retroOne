"""Batching may save directory work, never source freshness or scope checks."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from retroone.library_review import ReviewIndex


def make_review(tmp_path, names=('one/Alpha.nes', 'one/Beta.nes', 'two/Copy.nes', 'two/Other.nes')):
    root = tmp_path / 'roms'
    records = {}
    for number, name in enumerate(names):
        source = root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'synthetic cartridge')
        info = source.stat()
        records[f'{number:024x}'] = dict(
            primary=name, platform='nes',
            sources=[dict(relative_path=name, **{key: getattr(info, 'st_' + key)
                         for key in ('size', 'mtime_ns', 'ctime_ns', 'ino')})],
            fingerprint=dict(scope='single_cartridge_payload', sha256='a' * 64, complete=True),
            verification=dict(status='fingerprinted'))
    data = dict(schema_version=1, games=records, known_sources=list(names), groups=[dict(
        id='f' * 24, game_ids=list(records), scope='single_cartridge_payload', auto_merge_safe=True)])
    index = tmp_path / 'review.json'
    index.write_text(json.dumps(data))
    review = ReviewIndex(root, index)
    review.load()
    return review, data


@pytest.mark.parametrize('method', ['rows', 'changed_groups'])
def test_batch_rechecks_each_source_but_shares_directory_work(tmp_path, monkeypatch, method):
    review, data = make_review(tmp_path)
    sources = {review.root / name for name in data['known_sources']}
    parents = {source.parent for source in sources}
    stats, resolutions, listings = Counter(), Counter(), Counter()
    resolving = set()
    original_stat, original_resolve, original_scandir = Path.stat, Path.resolve, os.scandir

    def measured_stat(path, *args, **kwargs):
        # Python versions differ in whether resolve() itself uses Path.stat.
        if path not in resolving:
            stats[path] += 1
        return original_stat(path, *args, **kwargs)

    def measured_resolve(path, *args, **kwargs):
        resolutions[path] += 1
        resolving.add(path)
        try:
            return original_resolve(path, *args, **kwargs)
        finally:
            resolving.remove(path)

    def measured_scandir(path):
        listings[Path(path)] += 1
        return original_scandir(path)

    monkeypatch.setattr(Path, 'stat', measured_stat)
    monkeypatch.setattr(Path, 'resolve', measured_resolve)
    monkeypatch.setattr(os, 'scandir', measured_scandir)
    for _ in range(2):
        result = getattr(review, method)()
        if method == 'changed_groups':
            assert result == set()
        else:
            assert all(row[1] == 'f' * 24 for row in result)
    assert {path: stats[path] for path in sources} == dict.fromkeys(sources, 2)
    assert {path: stats[path] for path in parents} == dict.fromkeys(parents, 2)
    assert resolutions == Counter(dict.fromkeys(parents, 2))
    assert listings == Counter(dict.fromkeys(parents, 1))


@pytest.mark.parametrize('change', ['same_size_edit', 'delete', 'replace', 'source_symlink', 'parent_symlink'])
def test_next_batch_invalidates_changed_hidden_sources(tmp_path, change):
    review, data = make_review(tmp_path)
    assert review.changed_groups() == set()
    source = review.root / data['known_sources'][0]
    original = source.stat()
    parent_stamp = source.parent.stat().st_mtime_ns
    if change == 'same_size_edit':
        source.write_bytes(b'X' * original.st_size)
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert source.parent.stat().st_mtime_ns == parent_stamp
    elif change == 'delete':
        source.unlink()
    elif change == 'replace':
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(source.read_bytes())
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, source)
    elif change == 'source_symlink':
        moved = review.root / 'original'
        source.rename(moved)
        source.symlink_to(moved)
    else:
        # Preserve all file inodes and contents: only the parent now escapes.
        moved = tmp_path / 'outside'
        source.parent.rename(moved)
        source.parent.symlink_to(moved, target_is_directory=True)
    assert review.changed_groups() == {'f' * 24}
    entry = next(iter(review.entries.values()))
    assert not review.current(entry)
    assert review.public(dict(rom_path=str(source)), entry)['verification']['status'] == 'stale'


def test_parent_symlink_inside_root_remains_supported_and_rechecked(tmp_path):
    review, data = make_review(tmp_path)
    parent = review.root / 'one'
    destination = review.root / 'relocated'
    parent.rename(destination)
    parent.symlink_to(destination, target_is_directory=True)
    assert review.changed_groups() == set()
    (destination / 'Alpha.ips').write_bytes(b'patch')
    assert review.changed_groups() == {'f' * 24}
    (destination / 'Alpha.ips').unlink()
    assert review.changed_groups() == set()


@pytest.mark.parametrize(('sidecar', 'invalid'), [
    ('GAME PACK.V1.ips', True), ('game pack.v1', True),
    ('Game Pack.v1.patch/extra', True), ('Game Pack.v10.ips', False),
    ('Game Pack.v1 extra.ips', False), ('Game Pack.v1.sfc', False),
])
def test_sidecar_names_and_known_sources_preserve_original_matching(tmp_path, sidecar, invalid):
    names = ('one/Game Pack.v1.nes', 'one/Game Pack.v1.sfc')
    review, _ = make_review(tmp_path, names)
    assert review.changed_groups() == set()
    extra = review.root / 'one' / sidecar
    if sidecar != 'Game Pack.v1.sfc':
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b'patch')
    assert review.changed_groups() == ({'f' * 24} if invalid else set())
    if invalid:
        extra.unlink()
        if extra.parent != review.root / 'one':
            extra.parent.rmdir()
        assert review.changed_groups() == set()


@pytest.mark.parametrize(('siblings', 'valid'), [
    (['Game.nes'], True), (['Game.nes', 'GAME.NES'], False),
    (['Game.nes', 'GameExtra.ips'], True), (['Game.nes', 'GAME'], False),
])
def test_legacy_missing_known_source_excludes_only_own_exact_name(tmp_path, monkeypatch, siblings, valid):
    review, _ = make_review(tmp_path, ('one/Game.nes',))
    review.known_sources = set()

    @contextmanager
    def names_only(_):
        # Case-fold collisions also work on case-insensitive test filesystems.
        yield iter(SimpleNamespace(name=name) for name in siblings)

    monkeypatch.setattr(os, 'scandir', names_only)
    assert review.current(next(iter(review.entries.values()))) is valid


def test_index_reload_cannot_race_with_directory_cache_population(tmp_path, monkeypatch):
    review, data = make_review(tmp_path)
    entry = next(iter(review.entries.values()))
    (review.root / 'one/Alpha.ips').write_bytes(b'patch')
    data['known_sources'].append('one/Alpha.ips')
    review.path.write_text(json.dumps(data))
    entered, release, loading, loaded = (threading.Event() for _ in range(4))
    original_scandir = os.scandir

    @contextmanager
    def blocked_scandir(path):
        entered.set()
        assert release.wait(3)
        with original_scandir(path) as iterator:
            yield iterator

    def reload():
        loading.set()
        review.load()
        loaded.set()

    monkeypatch.setattr(os, 'scandir', blocked_scandir)
    with ThreadPoolExecutor(max_workers=2) as executor:
        validation = executor.submit(review.current, entry)
        try:
            assert entered.wait(3)
            reloading = executor.submit(reload)
            assert loading.wait(3)
            assert not loaded.wait(.05)
        finally:
            release.set()
        assert validation.result(timeout=3) is False
        reloading.result(timeout=3)
    assert review.current(entry) is True


def test_resource_directory_still_checks_added_unrelated_files(tmp_path):
    review, _ = make_review(tmp_path, ('one/Game.bin', 'one/Other.bin'))
    entry = next(iter(review.entries.values()))
    entry['sources'].extend(list(review.entries.values())[1]['sources'])
    entry['resource_root'] = 'one'
    entry['fingerprint']['scope'] = 'complete_resource_directory'
    assert review.current(entry)
    (review.root / 'one/new-resource.dat').write_bytes(b'new resource')
    assert not review.current(entry)
