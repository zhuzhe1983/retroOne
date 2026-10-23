"""Generated fixtures only; no emulator, downloaded ROM, or NAS is used."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest

from scripts import consolidate_library as migration
from retroone.game_config import GameConfigStore
from retroone.game_metadata import GameMetadataStore
from retroone.runner import Runner


def write(path, data=b'generated game bytes'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def dump(path, document):
    write(path, json.dumps(document, ensure_ascii=False).encode())


def database_rows(path, table):
    with sqlite3.connect(path) as db:
        return db.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / 'roms'
    data = tmp_path / 'shared'
    artwork = tmp_path / 'artwork'
    data.mkdir(); artwork.mkdir()
    source = write(root / 'library/nes/new/FC/game.nes')
    other = write(root / 'library/nes/old/FC/game.nes', b'another revision')
    cover = write(source.parent / 'media/cover.png', b'generated cover')
    write(root / 'library/switch/artwork/review/cover.png', b'unmoved artwork')
    archive = root / 'library/dos/new/DOS/game.zip'
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('PLAY.BAT', b'echo synthetic DOS')
    games = []
    for identifier, path, platform in [('a' * 24, source, 'nes'), ('b' * 24, other, 'nes'), ('c' * 24, archive, 'dos')]:
        name = '/roms/' + path.relative_to(root).as_posix()
        games.append(dict(id=identifier, title='Generated title', platform=platform,
            collection=path.parent.name, source=path.parts[-3], rom_path=name,
            files=[name], launch_files=[name], media={'cover': '/roms/' + cover.relative_to(root).as_posix()},
            _save_title='immutable save title', native_core='nestopia', browser_core='fceumm',
            engine_hint=None, browser_eligible=platform == 'nes', unpacked_size=path.stat().st_size,
            mode='browser' if platform == 'nes' else 'stream', blocked_reason=None))
    physical = lambda game: {**game, **{key: str(root / value.removeprefix('/roms/'))
        if isinstance(value, str) else [str(root / p.removeprefix('/roms/')) for p in value]
        for key, value in game.items() if key in ('rom_path', 'files', 'launch_files')},
        'media': {'cover': str(cover)}}
    configs = GameConfigStore(data / 'runtime', root)
    for game in games:
        configs.generate(physical(game))
    configs.update_overrides(physical(games[0]), {'mode': 'stream', 'notes': 'keep this setting'})
    metadata = GameMetadataStore(data / 'metadata', root)
    metadata.import_many([physical(g) for g in games])
    metadata.update_overrides(games[0]['id'], {'title': 'User override'})
    metadata.import_details_many([{'id': games[0]['id'], 'details': {'publisher': 'Synthetic publisher'}}])
    info = migration.file_identity(source)
    relative = source.relative_to(root).as_posix()
    review_entry = dict(platform='nes', native_core='nestopia', browser_core='fceumm', engine_hint=None,
        primary=relative, resource_root=None, sources=[dict(relative_path=relative,
        **{key: info[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'ino')})],
        fingerprint={'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'scope': 'single_cartridge_payload', 'complete': True},
        verification={'status': 'fingerprinted'})
    stale = copy.deepcopy(review_entry)
    stale['verification']['status'] = 'stale'
    review = {'schema_version': 1, 'games': {games[0]['id']: review_entry, 'stale': stale},
              'known_sources': [relative], 'groups': [{'id': 'group', 'game_ids': [games[0]['id'], 'stale'], 'auto_merge_safe': True}]}
    dump(data / 'metadata/library-review.json', review)
    profiles = []
    for number in range(2):
        profile = tmp_path / ('player' + str(number)); profile.mkdir()
        with sqlite3.connect(profile / 'catalog.sqlite3') as db:
            db.executescript('''CREATE TABLE games(id TEXT PRIMARY KEY,collection TEXT,payload TEXT);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE favorites(game_id TEXT PRIMARY KEY,favorite INTEGER);
                CREATE TABLE history(game_id TEXT PRIMARY KEY,played_at REAL);
                CREATE TABLE game_play_counts(game_id TEXT PRIMARY KEY,play_count INTEGER);
                CREATE TABLE game_reviews(game_id TEXT PRIMARY KEY,group_id TEXT,status TEXT,payload TEXT);''')
            for game in games:
                db.execute('INSERT INTO games VALUES(?,?,?)', (game['id'], game['collection'], json.dumps(game)))
            db.execute('INSERT INTO favorites VALUES(?,1)', (games[0]['id'],))
            db.execute('INSERT INTO history VALUES(?,123.5)', (games[0]['id'],))
            db.execute('INSERT INTO game_play_counts VALUES(?,7)', (games[0]['id'],))
            db.execute('INSERT INTO settings VALUES(?,?)', ('language', 'zh-CN'))
            db.execute('INSERT INTO game_reviews VALUES(?,?,?,?)', (games[0]['id'], None, 'fingerprinted', json.dumps(review_entry)))
        # Real extraction creates Runner's actual marker layout, then a game save.
        runner = Runner(profile, root)
        workspace = profile / 'work' / games[2]['id']
        entry = runner._prepare_content(archive, 'dos', workspace)
        save = write(entry.parent / 'USER.SAV', b'irreplaceable synthetic progress')
        runner._prepare_content(archive, 'dos', workspace / 'disc-0')
        bad = profile / 'work/stale/content/.retroone-extracted.json'
        dump(bad, {'path': str(archive), 'size': archive.stat().st_size + 1, 'mtime_ns': archive.stat().st_mtime_ns})
        profiles.append(profile)
    dump(artwork / 'local-overrides.json', {'version': 1, 'games': {games[0]['id']: {'source_path': '/roms/' + cover.relative_to(root).as_posix()}}})
    cache = data / 'metadata/audit/hashes.sqlite'; cache.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache) as db:
        db.execute('CREATE TABLE audit_hashes_v1(path TEXT,identity TEXT,kind TEXT,payload TEXT,PRIMARY KEY(path,identity,kind))')
        db.execute('INSERT INTO audit_hashes_v1 VALUES(?,?,?,?)', ('/roms/' + relative, migration.encoded(info), 'raw-sha256', '"complete hash"'))
        bad_info = dict(info, ctime_ns=info['ctime_ns'] + 1)
        db.execute('INSERT INTO audit_hashes_v1 VALUES(?,?,?,?)', ('/roms/' + relative, migration.encoded(bad_info), 'raw-sha256', '"stale hash"'))
    return dict(root=root, data=data, artwork=artwork, profiles=profiles, source=source, other=other,
                archive=archive, games=games, configs=configs, cache=cache, metadata=metadata)


def apply(lib, **kwargs):
    return migration.consolidate(lib['root'], lib['data'], lib['profiles'], lib['artwork'],
                                 apply=True, workers_stopped=True, **kwargs)


def snapshot_files(lib):
    paths = [lib['data'] / 'metadata/library-review.json', lib['artwork'] / 'local-overrides.json']
    paths += list((lib['data'] / 'runtime/game-configs').glob('*.json'))
    paths += [p for profile in lib['profiles'] for p in (profile / 'work').rglob('*') if p.is_file()]
    return {str(path): path.read_bytes() for path in paths}


def test_default_dry_run_never_writes_and_uses_neutral_collision_suffix(library):
    lib = library; before = snapshot_files(lib)
    report = migration.consolidate(lib['root'], lib['data'], lib['profiles'], lib['artwork'])
    assert report['dry_run'] and report['collection_count'] == 3
    moves = {x['original_path']: x['path'] for x in report['moves']}
    assert moves['library/nes/new/FC'] == 'library/nes/FC'
    assert moves['library/nes/old/FC'] == 'library/nes/FC（合集 2）'
    assert not any('/artwork/' in name for name in moves)
    assert not (lib['data'] / 'private-migrations').exists()
    assert snapshot_files(lib) == before and lib['source'].exists()


def test_apply_preserves_ids_saves_overrides_and_only_valid_evidence(library):
    from retroone.library_layout import canonical_manifest, manifest_digest
    lib = library; before = migration.file_identity(lib['source'])
    details = database_rows(lib['metadata'].db_path, 'game_details')
    overrides = [(r[0], r[2]) for r in database_rows(lib['metadata'].db_path, 'game_metadata')]
    revision = lib['metadata'].revision()
    histories = {str(p): {t: database_rows(p / 'catalog.sqlite3', t) for t in ['favorites', 'history', 'game_play_counts']} for p in lib['profiles']}
    report = apply(lib)
    new = lib['root'] / 'library/nes/FC/game.nes'
    assert report['collections_moved'] == 3 and report['source_directories_removed'] == 3
    assert migration.file_identity(new) == before and not lib['source'].exists()
    assert (lib['root'] / 'library/nes/FC（合集 2）/game.nes').read_bytes() == b'another revision'
    manifest = migration.read_json(lib['root'] / migration.MANIFEST)
    assert manifest == canonical_manifest(manifest)
    assert migration.read_json(lib['root'] / migration.REQUIRED)['manifest_sha256'] == manifest_digest(manifest)
    for profile in lib['profiles']:
        rows = database_rows(profile / 'catalog.sqlite3', 'games')
        game = json.loads(rows[0][2])
        assert game['id'] == lib['games'][0]['id'] and game['_save_title'] == 'immutable save title'
        assert game['rom_path'] == '/roms/library/nes/FC/game.nes'
        assert game['source'] == '' and game['collection'] == 'FC'
        settings = dict(database_rows(profile / 'catalog.sqlite3', 'settings'))
        assert settings['language'] == 'zh-CN' and settings['library_layout_required'] == '1'
        assert json.loads(settings['library_layout_bindings']) == manifest['collections']
        for table, original in histories[str(profile)].items():
            assert database_rows(profile / 'catalog.sqlite3', table) == original
        workspace = profile / 'work' / lib['games'][2]['id']
        before_save = (workspace / 'content/USER.SAV').read_bytes()
        new_archive = lib['root'] / 'library/dos/DOS/game.zip'
        runner = Runner(profile, lib['root'])
        assert runner._prepare_content(new_archive, 'dos', workspace).name == 'PLAY.BAT'
        assert runner._prepare_content(new_archive, 'dos', workspace / 'disc-0').name == 'PLAY.BAT'
        assert (workspace / 'content/USER.SAV').read_bytes() == before_save
        assert migration.read_json(profile / 'work/stale/content/.retroone-extracted.json')['path'] == str(lib['archive'])
    assert database_rows(lib['metadata'].db_path, 'game_details') == details
    assert [(r[0], r[2]) for r in database_rows(lib['metadata'].db_path, 'game_metadata')] == overrides
    assert lib['metadata'].revision() == revision + 1
    document = lib['configs'].get(lib['games'][0])
    assert document['overrides'] == {'mode': 'stream', 'notes': 'keep this setting'}
    assert document['generated']['source_signature'] == migration.digest(document['generated']['asset_identity'])
    actual_game = dict(lib['games'][0], rom_path=str(new), files=[str(new)], launch_files=[str(new)])
    assert not lib['configs'].summary(actual_game)['stale']
    review = migration.read_json(lib['data'] / 'metadata/library-review.json')
    assert 'fingerprint' in review['games'][lib['games'][0]['id']]
    assert 'fingerprint' not in review['games']['stale']
    assert review['games']['stale']['verification']['status'] == 'stale'
    assert review['groups'] == []
    with sqlite3.connect(lib['cache']) as db:
        new_rows = db.execute("SELECT identity,payload FROM audit_hashes_v1 WHERE path='/roms/library/nes/FC/game.nes'").fetchall()
    assert new_rows == [(migration.encoded(before), '"complete hash"')]
    assert migration.read_json(lib['artwork'] / 'local-overrides.json')['games'][lib['games'][0]['id']]['source_path'] == '/roms/library/nes/FC/media/cover.png'


def test_explicit_rollback_and_repeat_are_safe(library):
    lib = library; files = snapshot_files(lib)
    catalogs = {str(p): {t: database_rows(p / 'catalog.sqlite3', t) for t in ['games', 'settings', 'favorites', 'history', 'game_play_counts', 'game_reviews']} for p in lib['profiles']}
    metadata = database_rows(lib['metadata'].db_path, 'game_metadata')
    cache = database_rows(lib['cache'], 'audit_hashes_v1')
    result = apply(lib)
    for _ in range(2):
        restored = migration.rollback(result['backup_path'], workers_stopped=True)
        assert restored['status'] == 'rolled_back'
        assert snapshot_files(lib) == files
        assert lib['source'].exists() and lib['other'].exists()
        assert not (lib['root'] / migration.MANIFEST).exists()
        assert not (lib['root'] / migration.REQUIRED).exists()
        assert database_rows(lib['metadata'].db_path, 'game_metadata') == metadata
        assert database_rows(lib['cache'], 'audit_hashes_v1') == cache
        for profile in lib['profiles']:
            for table, original in catalogs[str(profile)].items():
                assert database_rows(profile / 'catalog.sqlite3', table) == original


def test_failed_store_rewrite_automatically_restores_everything(library, monkeypatch):
    lib = library; files = snapshot_files(lib); records = database_rows(lib['profiles'][0] / 'catalog.sqlite3', 'games')
    original = migration._rewrite_stores
    def fail(*args):
        original(*args)
        raise RuntimeError('injected after storage writes')
    monkeypatch.setattr(migration, '_rewrite_stores', fail)
    with pytest.raises(RuntimeError, match='injected'):
        apply(lib)
    assert lib['source'].exists() and snapshot_files(lib) == files
    assert database_rows(lib['profiles'][0] / 'catalog.sqlite3', 'games') == records
    journals = list((lib['data'] / 'private-migrations').glob('*/transaction.json'))
    assert len(journals) == 1 and migration.read_json(journals[0])['status'] == 'rolled_back'


@pytest.mark.parametrize('kind', ['worker', 'cache'])
def test_cooperative_worker_locks_prevent_any_rename(library, kind):
    lib = library
    lock = lib['data'] / 'metadata/audit/worker.lock' if kind == 'worker' else lib['cache'].with_suffix('.sqlite.lock')
    with migration.exclusive_lock(lock), pytest.raises(migration.MigrationError, match='busy'):
        apply(lib)
    assert lib['source'].exists() and not (lib['root'] / migration.MANIFEST).exists()


def test_no_replace_rename_does_not_overwrite_empty_directory(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir(); write(a / 'payload')
    with pytest.raises(OSError):
        migration.no_replace_rename(a, b)
    assert (a / 'payload').is_file() and b.is_dir()


def test_existing_casefold_destination_and_source_leftovers_are_preserved(library):
    lib = library
    existing = lib['root'] / 'library/nes/fc'; existing.mkdir()
    leftover = write(lib['root'] / 'library/nes/new/readme.txt', b'keep source-level material')
    result = apply(lib)
    assert existing.is_dir() and leftover.is_file()
    assert (lib['root'] / 'library/nes/FC（合集 2）/game.nes').is_file()
    assert (lib['root'] / 'library/nes/FC（合集 3）/game.nes').is_file()
    assert 'library/nes/new/readme.txt' in result['source_files_retained']


def test_symlink_collection_content_is_rejected_without_mutation(library):
    lib = library
    (lib['source'].parent / 'escape').symlink_to(lib['data'], target_is_directory=True)
    with pytest.raises(migration.MigrationError, match='symlink'):
        migration.plan_layout(lib['root'])
    assert lib['source'].is_file()


def test_apply_and_rollback_require_stopped_workers(library):
    lib = library
    with pytest.raises(migration.MigrationError, match='workers-stopped'):
        migration.consolidate(lib['root'], lib['data'], lib['profiles'], apply=True)
    with pytest.raises(migration.MigrationError, match='workers-stopped'):
        migration.rollback(lib['data'])


def test_new_layout_is_idempotent(library):
    lib = library; first = apply(lib)
    manifest = (lib['root'] / migration.MANIFEST).read_bytes()
    assert migration.plan_layout(lib['root'])['moves'] == []
    assert apply(lib)['status'] == 'unchanged'
    assert (lib['root'] / migration.MANIFEST).read_bytes() == manifest
    assert len(list((lib['data'] / 'private-migrations').glob('*/transaction.json'))) == 1


def test_path_prefix_rewrite_does_not_touch_similar_names_or_user_text(tmp_path):
    mapping = migration.PathMap(tmp_path, [{'original_path': 'library/nes/new/FC', 'path': 'library/nes/FC'}])
    assert mapping.rewrite('/roms/library/nes/new/FC/game.nes') == '/roms/library/nes/FC/game.nes'
    assert mapping.rewrite('/roms/library/nes/new/FC-HD/game.nes') == '/roms/library/nes/new/FC-HD/game.nes'
    assert mapping.rewrite('https://example.com/library/nes/new/FC/game') == 'https://example.com/library/nes/new/FC/game'


@pytest.mark.parametrize('name,text', [
    ('metadata.pegasus.txt', 'game: Test\nfile: ../Shared/game.nes\n'),
    ('metadata.pegasus.txt', 'game: Test\nfiles:\n  game.nes\n  ../Shared/game.nes\n'),
    ('metadata.pegasus.txt', 'game: Test\nassets.box_front: /roms/library/nes/new/FC/media/cover.png\n'),
    ('disc.cue', 'FILE "../Shared/track.bin" BINARY\nTRACK 01 MODE1/2352\n'),
    ('disc.m3u', '# comment\n../Shared/disc.cue\n'),
    ('disc.gdi', '1\n1 0 4 2352 "../Shared/track.bin" 0\n'),
])
def test_collection_reference_cannot_escape_before_any_mutation(library, name, text):
    lib = library
    write(lib['source'].parent / name, text.encode())
    with pytest.raises(migration.MigrationError, match='Reference leaves its collection'):
        apply(lib)
    assert lib['source'].exists() and not (lib['data'] / 'private-migrations').exists()


def test_nested_references_stay_inside_the_same_intact_collection(library):
    lib = library
    write(lib['source'].parent / 'nested/game.m3u', b'../game.nes\n')
    write(lib['source'].parent / 'metadata.pegasus.txt', b'game: Test\nfile: nested/game.m3u\nassets.box_front: https://example.com/cover.png\n')
    apply(lib)
    assert (lib['root'] / 'library/nes/FC/nested/game.m3u').read_bytes() == b'../game.nes\n'


@pytest.mark.parametrize('name,text', [
    ('metadata.pegasus.txt', 'game: Test\nfile: new/FC/game.nes\n'),
    ('METADATA.PEGASUS.TXT', 'game: Test\nFILE: "new/FC/game.nes"\n'),
    ('disc.m3u', 'new/FC/game.nes\n'),
])
def test_platform_metadata_referring_to_moved_collection_is_rejected(library, name, text):
    lib = library
    write(lib['root'] / 'library/nes' / name, text.encode())
    with pytest.raises(migration.MigrationError, match='External metadata'):
        apply(lib)
    assert lib['source'].exists()


@pytest.mark.parametrize('target', ['favorites', 'history', 'settings', 'metadata', 'runtime'])
def test_manual_rollback_refuses_later_user_writes_before_moving_files(library, target):
    lib = library; report = apply(lib)
    if target in ('favorites', 'history', 'settings'):
        with sqlite3.connect(lib['profiles'][0] / 'catalog.sqlite3') as db:
            if target == 'settings':
                db.execute("UPDATE settings SET value='en' WHERE key='language'")
            elif target == 'favorites':
                db.execute('UPDATE favorites SET favorite=0')
            else:
                db.execute('UPDATE history SET played_at=999')
    elif target == 'metadata':
        lib['metadata'].update_overrides(lib['games'][0]['id'], {'title': 'A new user title'})
    else:
        path = lib['data'] / ('runtime/game-configs/' + lib['games'][0]['id'] + '.json')
        document = migration.read_json(path)
        document['overrides']['notes'] = 'new edit'
        dump(path, document)
    with pytest.raises(migration.MigrationError, match='Storage changed since migration'):
        migration.rollback(report['backup_path'], workers_stopped=True)
    assert not lib['source'].exists() and (lib['root'] / 'library/nes/FC/game.nes').exists()


def test_manual_rollback_ignores_scan_cache_changes_and_is_a_noop_afterward(library):
    lib = library; report = apply(lib)
    with sqlite3.connect(lib['profiles'][0] / 'catalog.sqlite3') as db:
        db.execute("INSERT INTO settings VALUES('last_scan','changed')")
        db.execute('DELETE FROM game_reviews')
    migration.rollback(report['backup_path'], workers_stopped=True)
    with sqlite3.connect(lib['profiles'][0] / 'catalog.sqlite3') as db:
        db.execute('UPDATE favorites SET favorite=0')
    assert migration.rollback(report['backup_path'], workers_stopped=True)['already_rolled_back']
    assert database_rows(lib['profiles'][0] / 'catalog.sqlite3', 'favorites')[0][1] == 0


def test_tampered_manifest_is_not_resealed(library):
    lib = library; apply(lib)
    path = lib['root'] / migration.MANIFEST
    manifest = migration.read_json(path)
    manifest['collections'][0]['original_path'] += '-tampered'
    dump(path, manifest)
    write(lib['root'] / 'library/nes/new/Other/game.nes')
    with pytest.raises(ValueError, match='校验不一致'):
        apply(lib)
    assert (lib['root'] / 'library/nes/new/Other/game.nes').exists()


def test_profile_binding_deletion_cannot_be_authorized_by_missing_manifest(library):
    lib = library; apply(lib)
    (lib['root'] / migration.MANIFEST).unlink()
    (lib['root'] / migration.REQUIRED).unlink()
    write(lib['root'] / 'library/nes/new/Other/game.nes')
    with pytest.raises(migration.MigrationError, match='requires the missing layout seal'):
        apply(lib)


def test_unknown_source_is_reported_and_never_guessed(library):
    lib = library
    file = write(lib['root'] / 'library/nes/manual/Personal/game.nes')
    plan = migration.plan_layout(lib['root'])
    assert 'library/nes/manual' in plan['unhandled_directories']
    apply(lib)
    assert file.exists()


def test_sqlite_wal_commit_is_backed_up_and_restored(library):
    lib = library
    db = sqlite3.connect(lib['profiles'][0] / 'catalog.sqlite3')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('UPDATE favorites SET favorite=0'); db.commit()
        report = apply(lib)
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        migration.rollback(report['backup_path'], workers_stopped=True)
        assert db.execute('SELECT favorite FROM favorites').fetchone()[0] == 0
    finally:
        db.close()


def test_artwork_state_must_not_live_inside_a_moving_collection(library):
    lib = library
    with pytest.raises(migration.MigrationError, match='artwork state must remain outside'):
        migration.consolidate(lib['root'], lib['data'], lib['profiles'], lib['source'].parent,
                              apply=True, workers_stopped=True)
    assert lib['source'].exists()
