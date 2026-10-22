import copy
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading

import pytest

from retroone.game_metadata import GameMetadataError, GameMetadataStore


@pytest.fixture
def setup(tmp_path):
    rom_root = tmp_path / 'roms'
    cartridge = rom_root / 'library/nes/new/FC/game.nes'
    cartridge.parent.mkdir(parents=True)
    cartridge.write_bytes(b'local cartridge fixture')
    cover = cartridge.parent / 'media/game/boxFront.jpg'
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b'cover reference fixture')
    game = {'id': 'a' * 24, 'title': '原始游戏名', 'description': '游戏简介', 'sort_by': 'game',
            'media': {'cover': str(cover)}, 'platform': 'nes', 'rom_path': str(cartridge),
            'files': [str(cartridge)], 'native_core': 'nestopia', 'browser_core': 'fceumm',
            'mode': 'browser', '_save_title': '旧存档名称'}
    return GameMetadataStore(tmp_path / 'library-data/metadata', rom_root), game


def test_generated_and_override_values_are_separate_and_summary_hides_paths(setup):
    store, game = setup
    assert store.revision() == 0
    assert store.import_many([game]) == 1
    summary = store.update_overrides(game['id'], {'title': '我的游戏', 'description': ''})
    assert summary == {
        'id': game['id'], 'revision': 2,
        'effective': {'title': '我的游戏', 'description': '', 'sort_by': 'game'},
        'generated': {'title': '原始游戏名', 'description': '游戏简介', 'sort_by': 'game'},
        'overrides': {'title': '我的游戏', 'description': ''},
        'overridden_fields': ['description', 'title'], 'media_kinds': ['cover'],
    }
    assert str(store.rom_root) not in json.dumps(summary)
    assert 'boxFront' not in json.dumps(summary)
    with sqlite3.connect(store.db_path) as db:
        row = db.execute('SELECT generated,overrides FROM game_metadata').fetchone()
    stored = json.loads(row[0])
    assert stored['media'] == {'cover': 'library/nes/new/FC/media/game/boxFront.jpg'}
    assert set(stored) == {'title', 'description', 'sort_by', 'media'}
    assert 'rom_path' not in row[0] and 'native_core' not in row[0]


def test_apply_is_copy_and_only_changes_presentation_fields(setup):
    store, game = setup
    store.import_many([game]); store.update_overrides(game['id'], {'title': '新名字'})
    before = copy.deepcopy(game)
    result = store.apply(game)
    assert result['title'] == '新名字' and game == before
    for key in game.keys() - {'title', 'description', 'sort_by', 'media'}:
        assert result[key] == game[key]
    assert result['_save_title'] == '旧存档名称'
    result['files'].append('caller-owned change')
    result['media']['cover'] = 'caller-owned change'
    assert game == before
    assert store.apply(game)['media'] == game['media']


def test_rescan_keeps_manual_overrides_and_null_restores_latest_source(setup):
    store, game = setup
    store.import_many([game]); store.update_overrides(game['id'], {'title': '人工标题', 'sort_by': ''})
    changed = dict(game, title='最新源标题', description='新简介', sort_by='new')
    assert store.import_many([changed]) == 3
    assert store.summary(game['id'])['effective'] == {'title': '人工标题', 'description': '新简介', 'sort_by': ''}
    summary = store.update_overrides(game['id'], {'title': None, 'sort_by': None})
    assert summary['overrides'] == {} and summary['effective']['title'] == '最新源标题'
    assert summary['effective']['sort_by'] == 'new' and store.revision() == 4


def test_repeated_import_and_noop_patch_have_no_write_churn(setup):
    store, game = setup
    store.import_many([game])
    with sqlite3.connect(store.db_path) as observer:
        version = observer.execute('PRAGMA data_version').fetchone()[0]
        before = store.db_path.stat().st_mtime_ns
        assert store.import_many([dict(game, rom_path='/ignored/runtime/change', _save_title='ignored')]) == 1
        assert store.update_overrides(game['id'], {})['revision'] == 1
        assert store.update_overrides(game['id'], {'title': None})['revision'] == 1
        assert observer.execute('PRAGMA data_version').fetchone()[0] == version
        assert store.db_path.stat().st_mtime_ns == before
    store.update_overrides(game['id'], {'title': '固定名'})
    assert store.update_overrides(game['id'], {'title': '固定名'})['revision'] == 2


def test_only_missing_bootstrap_does_not_replace_newer_shared_source(setup):
    store, game = setup
    store.import_many([game]); store.update_overrides(game['id'], {'title': '个人编辑'})
    legacy = dict(game, title='过时缓存', description='旧简介', media={'cover': '/outdated/mount/cover.jpg'})
    second = dict(game, id='b' * 24, title='旧库独有的游戏')
    assert store.import_many((item for item in (legacy, second)), only_missing=True) == 3
    assert store.summary(game['id'])['generated']['title'] == '原始游戏名'
    assert store.summary(game['id'])['effective']['title'] == '个人编辑'
    assert store.summary(second['id'])['effective']['title'] == second['title']
    assert store.import_many([legacy], only_missing=True) == 3


def test_batch_is_one_revision_and_invalid_late_item_rolls_back_everything(setup):
    store, game = setup
    second = dict(game, id='b' * 24)
    assert store.import_many([game, second]) == 1
    version, rows = store.changes_since(0)
    assert version == 1 and len(rows) == 2
    invalid = dict(game, id='c' * 24, title=False)
    with pytest.raises(GameMetadataError):
        store.import_many([dict(game, title='must roll back'), invalid])
    assert store.revision() == 1
    assert store.summary(game['id'])['effective']['title'] == game['title']
    assert len(store.changes_since(-1)[1]) == 2


def test_input_generator_failure_rolls_back_batch(setup):
    store, game = setup
    def source():
        yield game
        raise RuntimeError('upstream discovery interrupted')
    with pytest.raises(RuntimeError):
        store.import_many(source())
    assert store.changes_since(0) == (0, [])


def test_change_cursor_supports_full_incremental_no_change_and_backup_rewind(setup):
    store, game = setup
    second = dict(game, id='b' * 24)
    store.import_many([game, second])
    before, rows = store.changes_since(-1)
    assert before == 1 and [row['id'] for row in rows] == [game['id'], second['id']]
    assert all(set(row) == {'id', 'title', 'description', 'sort_by', 'media', 'details', 'first_added_at'} for row in rows)
    store.update_overrides(second['id'], {'description': '修改过'})
    current, changes = store.changes_since(before)
    assert current == 2 and len(changes) == 1 and changes[0]['id'] == second['id']
    assert changes[0]['description'] == '修改过' and changes[0]['media'] == game['media']
    assert store.changes_since(current) == (current, [])
    assert len(store.changes_since(0)[1]) == 2
    assert len(store.changes_since(999)[1]) == 2


def test_change_cursor_and_rows_are_same_read_snapshot_during_write(setup, monkeypatch):
    store, game = setup
    second = dict(game, id='b' * 24)
    store.import_many([game, second])
    reading, committed = threading.Event(), threading.Event()
    original = store._effective
    def interleave(row):
        if row['game_id'] == game['id']:
            reading.set()
            assert committed.wait(10)
        return original(row)
    monkeypatch.setattr(store, '_effective', interleave)
    def writer():
        assert reading.wait(10)
        store.update_overrides(second['id'], {'title': '并发修改'})
        committed.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(writer)
        revision, rows = store.changes_since(0)
        task.result(timeout=10)
    assert revision == 1 and rows[1]['title'] == game['title']
    assert store.changes_since(revision)[1][0]['title'] == '并发修改'


@pytest.mark.parametrize('patch', [
    {'id': 'other'}, {'rom_path': '/etc/passwd'}, {'native_core': 'other'},
    {'media': {}}, {'_save_title': 'other'}, {'title': 7}, {'title': True},
    {'title': []}, {'title': ''}, {'title': '  \n\t'}, {'title': 'x' * 513},
    {'description': False}, {'description': 'x' * 65537}, {'sort_by': {}},
    {'sort_by': 'x' * 1025}, {'description': 'NUL\x00byte'}, {'title': '\ud800'}, [],
])
def test_override_validation_is_strict_atomic_and_cannot_change_runtime(setup, patch):
    store, game = setup
    store.import_many([game]); before = store.summary(game['id'])
    with pytest.raises(GameMetadataError) as failure:
        store.update_overrides(game['id'], patch)
    assert failure.value.status_code == 422
    assert store.summary(game['id']) == before and store.revision() == 1


def test_length_boundaries_and_empty_optional_text_are_supported(setup):
    store, game = setup; store.import_many([game])
    patch = {'title': '汉' * 512, 'description': '字' * 65536, 'sort_by': 'x' * 1024}
    assert store.update_overrides(game['id'], patch)['effective'] == patch
    assert store.update_overrides(game['id'], {'description': '', 'sort_by': ''})['effective']['description'] == ''


@pytest.mark.parametrize('reference', ['/etc/passwd', '../outside.jpg', 'library/../../outside.jpg',
                                        'https://example.com/art.jpg', 'library\\escape.jpg'])
def test_import_rejects_unsafe_media_references(setup, reference):
    store, game = setup
    with pytest.raises(GameMetadataError) as failure:
        store.import_many([dict(game, media={'cover': reference})])
    assert failure.value.status_code == 422 and store.revision() == 0


def test_media_rejects_symlink_and_special_file_but_missing_reference_is_preserved(setup, tmp_path):
    store, game = setup
    cover = Path(game['media']['cover']); cover.unlink()
    outside = tmp_path / 'outside.jpg'; outside.write_bytes(b'outside')
    cover.symlink_to(outside)
    with pytest.raises(GameMetadataError): store.import_many([game])
    cover.unlink()
    store.import_many([game])
    assert store.summary(game['id'])['media_kinds'] == ['cover']
    assert store.apply(game)['media'] == game['media']
    with pytest.raises(GameMetadataError):
        store.import_many([dict(game, media={'cover': str(cover.parent)})])


def test_relative_media_is_movable_between_rom_mounts(setup, tmp_path):
    store, game = setup
    relative = Path(game['media']['cover']).relative_to(store.rom_root).as_posix()
    store.import_many([dict(game, media={'cover': relative})])
    another_mount = tmp_path / 'different-rom-mount'; another_mount.mkdir()
    second = GameMetadataStore(store.root, another_mount)
    assert second.changes_since(0)[1][0]['media']['cover'] == str(another_mount / relative)
    assert second.revision() == 1


def test_missing_id_apply_does_not_generate_and_summary_update_are_404(setup):
    store, game = setup
    assert store.apply(game) == game and store.apply(game) is not game
    assert store.revision() == 0
    for call in (lambda: store.summary(game['id']), lambda: store.update_overrides(game['id'], {'title': 'new'})):
        with pytest.raises(GameMetadataError) as failure: call()
        assert failure.value.status_code == 404


@pytest.mark.parametrize('game_id', ['../escape', '', 'x' * 129, None, 12])
def test_invalid_id_rejected(setup, game_id):
    store, game = setup
    with pytest.raises(GameMetadataError): store.import_many([dict(game, id=game_id)])
    assert store.revision() == 0


@pytest.mark.parametrize('revision', [-2, True, 1.5, '0', None])
def test_invalid_revision_rejected(setup, revision):
    store, _ = setup
    with pytest.raises(GameMetadataError) as failure: store.changes_since(revision)
    assert failure.value.status_code == 422


def test_storage_rejects_rom_root_and_symlink_roots_or_ancestors(setup, tmp_path):
    store, _ = setup
    with pytest.raises(GameMetadataError): GameMetadataStore(store.rom_root / 'metadata', store.rom_root)
    target = tmp_path / 'real'; target.mkdir()
    link = tmp_path / 'link'; link.symlink_to(target, target_is_directory=True)
    with pytest.raises(GameMetadataError): GameMetadataStore(link, store.rom_root)
    with pytest.raises(GameMetadataError): GameMetadataStore(link / 'metadata', store.rom_root)
    assert not (target / 'metadata').exists()


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_storage_rejects_database_and_sidecar_symlinks(setup, tmp_path, suffix):
    store, game = setup; store.import_many([game])
    target = tmp_path / 'untouched'; target.write_bytes(b'do not change')
    path = Path(str(store.db_path) + suffix)
    if path.exists(): path.unlink()
    path.symlink_to(target)
    with pytest.raises(GameMetadataError) as failure: store.revision()
    assert failure.value.code == 'unsafe_metadata_path' and target.read_bytes() == b'do not change'


def test_corrupt_database_is_preserved_and_missing_database_not_recreated(setup):
    store, game = setup; store.import_many([game])
    store.db_path.write_bytes(b'corrupt existing metadata')
    with pytest.raises(GameMetadataError): GameMetadataStore(store.root, store.rom_root)
    assert store.db_path.read_bytes() == b'corrupt existing metadata'
    store.db_path.unlink()
    with pytest.raises(GameMetadataError): store.revision()
    assert not store.db_path.exists()


def test_corrupt_row_never_reset_by_rescan_or_override(setup):
    store, game = setup; store.import_many([game])
    with sqlite3.connect(store.db_path) as db:
        db.execute('UPDATE game_metadata SET generated=?', ('{"command":"bad"}',))
    for call in (lambda: store.summary(game['id']), lambda: store.import_many([game]),
                 lambda: store.update_overrides(game['id'], {'title': 'new'})):
        with pytest.raises(GameMetadataError) as failure: call()
        assert failure.value.code == 'metadata_corrupt'
    assert store.revision() == 1


def _process_write(root, rom_root, game, index):
    store = GameMetadataStore(Path(root), Path(rom_root))
    store.import_many([dict(game, id='process-' + str(index))])
    field = ('title', 'description', 'sort_by')[index]
    store.update_overrides(game['id'], {field: 'process-value-' + str(index)})
    return store.revision()


def test_separate_process_writers_keep_all_fields_ids_and_monotonic_revision(setup):
    store, game = setup; store.import_many([game])
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context('spawn')) as pool:
        tasks = [pool.submit(_process_write, str(store.root), str(store.rom_root), game, index) for index in range(3)]
        assert all(task.result(timeout=30) >= 3 for task in tasks)
    assert store.revision() == 7
    assert store.summary(game['id'])['overrides'] == {
        'title': 'process-value-0', 'description': 'process-value-1', 'sort_by': 'process-value-2',
    }
    assert len(store.changes_since(0)[1]) == 4
