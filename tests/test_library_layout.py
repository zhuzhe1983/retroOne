"""Generated fixtures: moving a collection must not create new player identities."""
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from retroone.catalog import Catalog
from retroone.library_layout import LibraryLayout, canonical_manifest, manifest_digest


def write(root, name, data=b'generated cartridge'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def manifest(root, collections):
    write(root, '.retroone/library-layout.json', json.dumps(dict(schema_version=1, collections=collections)).encode())


def seal(root):
    document = json.loads((root / '.retroone/library-layout.json').read_text())
    write(root, '.retroone/library-layout.required', json.dumps({
        'schema_version': 1, 'manifest_sha256': manifest_digest(document),
    }).encode())


def game_id(relative):
    return hashlib.sha256(relative.encode()).hexdigest()[:24]


def test_collection_move_preserves_id_personal_history_metadata_and_config(tmp_path, monkeypatch):
    root = tmp_path / 'roms'
    monkeypatch.setenv('RETROONE_LIBRARY_DATA_ROOT', str(tmp_path / 'shared'))
    original = 'library/nes/new/FC'
    current = 'library/nes/FC'
    write(root, original + '/sample.nes')
    write(root, original + '/media/sample/boxFront.png', b'generated image reference')
    players = [Catalog(root, tmp_path / name) for name in ('player1', 'player2')]
    for player in players:
        assert player.scan()['errors'] == 0
    first = players[0]
    identifier = game_id(original + '/sample.nes')
    old = first.get(identifier)
    first.set_favorite(identifier, True)
    first.mark_played(identifier)
    first.game_configs.update_overrides(old, {'notes': 'Keep custom runtime rules', 'disabled': True})
    first.game_metadata.update_overrides(identifier, {'title': 'Edited title'})
    first.game_metadata.import_details_many([dict(id=identifier, details=dict(publisher='Fixture publisher'))])
    saves = []
    for player in players:
        saves.append(write(player.config_root, 'browser-saves/' + identifier + '.state', b'personal state'))
        saves.append(write(player.config_root, 'saves/' + identifier + '/sample.srm', b'personal battery save'))
    (root / original).rename(root / current)
    manifest(root, [dict(path=current, original_path=original)])
    for player in players:
        assert player.scan()['errors'] == 0
        game = player.get(identifier)
        assert game['rom_path'] == str(root / current / 'sample.nes')
        assert game['source'] == ''
        assert game['_save_title'] == old['_save_title'] == 'sample'
        assert game['first_added_at'] == old['first_added_at']
        assert game['title'] == 'Edited title'
        assert game['details']['publisher'] == 'Fixture publisher'
        assert game['media']['cover'] == str(root / current / 'media/sample/boxFront.png')
        assert len(list(player.discover())) == 1
        assert player.game_configs.get(game)['overrides'] == {'notes': 'Keep custom runtime rules', 'disabled': True}
        assert not player.game_configs.summary(game)['stale']
    assert first.get(identifier)['favorite'] and first.get(identifier)['play_count'] == 1
    assert not players[1].get(identifier)['favorite'] and players[1].get(identifier)['play_count'] == 0
    assert [path.read_bytes() for path in saves] == [b'personal state', b'personal battery save'] * 2


def test_mixed_declared_legacy_direct_and_new_collections_are_discovered_once(tmp_path):
    root = tmp_path / 'roms'
    paths = [
        'library/nes/direct.nes',
        'library/nes/Modern/game.nes',
        'library/nes/new/OldLayout/game.nes',
        'library/nes/Family/Declared/game.nes',
        'library/nes/Family/Extra/game.nes',
    ]
    for path in paths:
        write(root, path)
    # A platform metadata reference to the declared subtree must not re-index it.
    write(root, 'library/nes/metadata.pegasus.txt', b'game: Same declared\nfile: Family/Declared/game.nes\n')
    manifest(root, [dict(path='library/nes/Family/Declared', original_path='library/nes/old/Declared')])
    catalog = Catalog(root, tmp_path / 'profile')
    games = list(catalog.discover())
    assert len(games) == len(paths)
    assert len({game['id'] for game in games}) == len(paths)
    declared = next(game for game in games if '/Declared/' in game['rom_path'])
    assert declared['id'] == game_id('library/nes/old/Declared/game.nes')
    assert declared['source'] == ''
    assert {game['rom_path'] for game in games} == {str(root / path) for path in paths}


def test_same_named_collections_keep_distinct_original_ids_and_multidisc_order(tmp_path):
    root = tmp_path / 'roms'
    bindings = []
    for source, name in [('new', 'PS1'), ('old', 'PS1 [Group 2]')]:
        current = 'library/psx/' + name
        original = 'library/psx/' + source + '/PS1'
        for disc in ('Disc1.chd', 'Disc2.chd'):
            write(root, current + '/discs/' + disc)
        write(root, current + '/metadata.pegasus.txt', b'game: Multi\nfile: discs/Disc2.chd\nfile: discs/Disc1.chd\n')
        bindings.append(dict(path=current, original_path=original))
    manifest(root, bindings)
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 2
    assert {game['id'] for game in games} == {game_id(binding['original_path'] + '/discs/Disc2.chd') for binding in bindings}
    assert all([Path(path).name for path in game['launch_files']] == ['Disc2.chd', 'Disc1.chd'] for game in games)


def test_new_pc_folder_is_not_split_into_helper_executables(tmp_path):
    root = tmp_path / 'roms'
    collection = 'library/dos/Collection'
    write(root, collection + '/Example/game.exe')
    write(root, collection + '/Example/setup.exe')
    write(root, collection + '/Example/data/resource.bin')
    write(root, collection + '/Example/data.zip', b'resource archive, not a separate game')
    manifest(root, [dict(path=collection, original_path=collection)])
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 1
    assert games[0]['rom_path'] == str(root / collection / 'Example')
    assert games[0]['blocked_reason']


def test_arbitrary_legacy_pc_source_keeps_historical_directory_identity(tmp_path):
    root = tmp_path / 'roms'
    original = 'library/windows/custom-source/PC/Game'
    write(root, original + '/main.exe')
    write(root, original + '/data.dat')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 1
    assert games[0]['id'] == game_id(original)
    assert games[0]['rom_path'] == str(root / original)
    assert games[0]['source'] == 'custom-source'
    assert games[0]['collection'] == 'PC'


def test_unmarked_single_collection_pc_archives_are_supported_without_splitting_exes(tmp_path):
    root = tmp_path / 'roms'
    archive = write(root, 'library/dos/Collection/Game.zip', b'')
    with ZipFile(archive, 'w') as zipped:
        zipped.writestr('PLAY.BAT', b'GAME.EXE\r\n')
        zipped.writestr('GAME.EXE', b'generated executable')
    write(root, 'library/windows/One/main.exe')
    write(root, 'library/windows/One/setup.exe')
    write(root, 'library/windows/One/data.dat')
    write(root, 'library/windows/main.exe')
    write(root, 'library/windows/setup.exe')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert [game['id'] for game in games] == [game_id('library/dos/Collection/Game.zip')]
    write(root, 'library/windows/One/data/level.dat')
    write(root, 'library/windows/One/metadata.pegasus.txt', b'game: Explicit game\nfile: main.exe\n')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert {game['id'] for game in games} == {
        game_id('library/dos/Collection/Game.zip'), game_id('library/windows/One/main.exe'),
    }


def test_platform_metadata_and_child_discovery_do_not_duplicate_game(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/Family/game.nes')
    write(root, 'library/nes/metadata.pegasus.txt', b'game: Root metadata title\nfile: Family/game.nes\n')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 1
    assert games[0]['title'] == 'Root metadata title'
    assert games[0]['id'] == game_id('library/nes/Family/game.nes')


def test_platform_metadata_owns_all_secondary_disc_paths_across_child_scans(tmp_path):
    root = tmp_path / 'roms'
    for disc in ('Disc1.chd', 'Disc2.chd'):
        write(root, 'library/psx/Discs/' + disc)
    write(root, 'library/psx/metadata.pegasus.txt', b'game: Multi Disc\nfile: Discs/Disc2.chd\nfile: Discs/Disc1.chd\n')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 1
    assert games[0]['id'] == game_id('library/psx/Discs/Disc2.chd')
    assert [Path(path).name for path in games[0]['launch_files']] == ['Disc2.chd', 'Disc1.chd']


@pytest.mark.parametrize('layout', ['legacy', 'declared'])
def test_arcade_parent_remains_playable_when_an_earlier_clone_uses_it_as_dependency(tmp_path, layout):
    root = tmp_path / 'roms'
    original = 'library/fbneo/import/Arcade'
    collection = 'library/fbneo/Arcade' if layout == 'declared' else original
    for name in ('clone.zip', 'parent.zip', 'bios.zip'):
        archive = write(root, collection + '/' + name, b'')
        with ZipFile(archive, 'w') as zipped:
            zipped.writestr('generated.bin', ('generated ' + name).encode())
    write(root, collection + '/metadata.pegasus.txt',
          b'game: Clone\nfile: clone.zip\nfile: parent.zip\nfile: bios.zip\n\n'
          b'game: Parent\nfile: parent.zip\nfile: bios.zip\n')
    if layout == 'declared':
        manifest(root, [dict(path=collection, original_path=original)])
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert {game['id'] for game in games} == {
        game_id(original + '/clone.zip'), game_id(original + '/parent.zip'),
    }
    assert {game['title']: [Path(path).name for path in game['launch_files']] for game in games} == {
        'Clone': ['clone.zip', 'parent.zip', 'bios.zip'], 'Parent': ['parent.zip', 'bios.zip'],
    }
    assert {game['source'] for game in games} == {'' if layout == 'declared' else 'import'}


def test_platform_metadata_pc_directory_ownership_survives_child_scans(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/windows/Family/Game/main.exe')
    write(root, 'library/windows/Family/Game/setup.exe')
    write(root, 'library/windows/Family/Game/data/level.dat')
    write(root, 'library/windows/metadata.pegasus.txt', b'game: PC Game\nfile: Family/Game/main.exe\n')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 1
    assert games[0]['id'] == game_id('library/windows/Family/Game/main.exe')


def test_platform_metadata_pc_shared_parent_keeps_each_explicit_game(tmp_path):
    root = tmp_path / 'roms'
    for name in ('Game1', 'Game2'):
        write(root, 'library/windows/Family/' + name + '/main.exe')
        write(root, 'library/windows/Family/' + name + '/setup.exe')
        write(root, 'library/windows/Family/' + name + '/data/level.dat')
    write(root, 'library/windows/metadata.pegasus.txt',
          b'game: One\nfile: Family/Game1/main.exe\n\ngame: Two\nfile: Family/Game2/main.exe\n')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 2
    assert {game['id'] for game in games} == {
        game_id('library/windows/Family/' + name + '/main.exe') for name in ('Game1', 'Game2')
    }


def test_old_collection_reappearing_or_cross_mapping_fails_before_scanning(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/new/FC/game.nes')
    catalog = Catalog(root, tmp_path / 'profile')
    catalog.scan()
    identifier = game_id('library/nes/new/FC/game.nes')
    (root / 'library/nes/new/FC').rename(root / 'library/nes/FC')
    manifest(root, [dict(path='library/nes/FC', original_path='library/nes/new/FC')])
    assert catalog.scan()['errors'] == 0
    before = catalog.get(identifier)
    write(root, 'library/nes/new/FC/game.nes', b'different original path content')
    status = catalog.scan()
    assert '同时存在' in status['last_error']
    assert status['scanned'] == 0
    assert catalog.get(identifier)['rom_path'] == before['rom_path']
    # Swapping two live collections' identity namespaces is also ambiguous.
    manifest(root, [dict(path='library/nes/FC', original_path='library/nes/new/FC'),
                    dict(path='library/nes/new/FC', original_path='library/nes/FC')])
    with pytest.raises(ValueError, match='同时存在'):
        LibraryLayout.load(root)


@pytest.mark.parametrize('case', ['json', 'traversal', 'absolute', 'platform', 'overlap_current',
                                 'overlap_original', 'missing', 'symlink_current', 'symlink_original', 'symlink_manifest'])
def test_invalid_manifest_fails_before_import_and_preserves_existing_index(tmp_path, monkeypatch, case):
    root = tmp_path / 'roms'
    write(root, 'library/nes/new/FC/game.nes')
    write(root, 'library/nes/Second/game.nes')
    catalog = Catalog(root, tmp_path / 'profile')
    assert catalog.scan()['errors'] == 0
    with catalog.connect() as db:
        before = [(row['id'], row['payload']) for row in db.execute('SELECT id,payload FROM games ORDER BY id')]
    revision = catalog.game_metadata.revision()
    bindings = [dict(path='library/nes/new/FC', original_path='library/nes/origin/FC')]
    if case == 'traversal': bindings[0]['path'] = 'library/nes/../new/FC'
    elif case == 'absolute': bindings[0]['path'] = str(root / 'library/nes/new/FC')
    elif case == 'platform': bindings[0]['original_path'] = 'library/gb/new/FC'
    elif case == 'overlap_current':
        (root / 'library/nes/new/FC/Nested').mkdir()
        bindings.append(dict(path='library/nes/new/FC/Nested', original_path='library/nes/another/FC'))
    elif case == 'overlap_original':
        bindings.append(dict(path='library/nes/Second', original_path='library/nes/origin/FC/Nested'))
    elif case == 'missing':
        bindings.append(dict(path='library/nes/Unavailable', original_path='library/nes/old/Unavailable'))
    elif case == 'symlink_current':
        (root / 'library/nes/Linked').symlink_to(root / 'library/nes/Second', target_is_directory=True)
        bindings[0]['path'] = 'library/nes/Linked'
    elif case == 'symlink_original':
        (root / 'library/nes/origin').symlink_to(root / 'library/nes/new', target_is_directory=True)
    manifest(root, bindings)
    if case == 'json': (root / '.retroone/library-layout.json').write_text('{broken')
    elif case == 'symlink_manifest':
        (root / '.retroone/library-layout.json').rename(root / '.retroone/elsewhere.json')
        (root / '.retroone/library-layout.json').symlink_to(root / '.retroone/elsewhere.json')
    monkeypatch.setattr(catalog.game_configs, 'generate', lambda game: pytest.fail('Invalid manifest generated a launch config'))
    status = catalog.scan()
    assert status['last_error'] and status['scanned'] == 0
    assert catalog.game_metadata.revision() == revision
    with catalog.connect() as db:
        assert [(row['id'], row['payload']) for row in db.execute('SELECT id,payload FROM games ORDER BY id')] == before


def test_collection_boundary_api_and_immutable_original_namespace(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/Renamed/game.nes')
    manifest(root, [dict(path='library/nes/Renamed', original_path='library/nes/new/FC')])
    layout = LibraryLayout.load(root)
    assert layout.root == root
    assert layout.original_relative(root / 'library/nes/Renamed/game.nes') == 'library/nes/new/FC/game.nes'
    assert layout.collection_for(root / 'library/nes/Renamed/game.nes') == root / 'library/nes/Renamed'
    assert layout.collection_for(root / 'library/nes/old/Legacy/game.nes') == root / 'library/nes/old/Legacy'
    assert layout.collection_for(root / 'library/nes/NewCollection/game.nes') == root / 'library/nes/NewCollection'
    assert layout.collection_for(root / 'library/nes/loose.nes') == root / 'library/nes'
    assert layout.collection_for(tmp_path / 'outside.nes') is None
    with pytest.raises(ValueError, match='根目录'):
        LibraryLayout.load(tmp_path / 'unavailable')


def test_profile_required_setting_prevents_missing_manifest_from_changing_ids(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/FC/game.nes')
    manifest(root, [dict(path='library/nes/FC', original_path='library/nes/new/FC')])
    catalog = Catalog(root, tmp_path / 'profile')
    assert catalog.scan()['errors'] == 0
    identifier = game_id('library/nes/new/FC/game.nes')
    with catalog.connect() as db:
        assert db.execute("SELECT value FROM settings WHERE key='library_layout_required'").fetchone()[0] == '1'
    (root / '.retroone/library-layout.json').unlink()
    assert '布局文件缺失' in catalog.scan()['last_error']
    assert catalog.scan_status['scanned'] == 0
    assert catalog.get(identifier)
    assert not catalog.get(game_id('library/nes/FC/game.nes'))
    assert '布局文件缺失' in Catalog(root, tmp_path / 'profile').scan()['last_error']


def test_required_sentinel_protects_new_profiles_and_independent_helpers(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/FC/game.nes')
    write(root, '.retroone/library-layout.required', b'1\n')
    with pytest.raises(ValueError, match='布局文件缺失'):
        LibraryLayout.load(root)
    catalog = Catalog(root, tmp_path / 'new-profile')
    assert '布局文件缺失' in catalog.scan()['last_error']
    assert not catalog.has_games()
    manifest(root, [dict(path='library/nes/FC', original_path='library/nes/new/FC')])
    assert '布局校验不一致' in catalog.scan()['last_error']
    seal(root)
    assert catalog.scan()['errors'] == 0
    assert catalog.get(game_id('library/nes/new/FC/game.nes'))


def test_invalid_layout_does_not_mark_an_existing_legacy_profile_required(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/new/FC/game.nes')
    catalog = Catalog(root, tmp_path / 'profile')
    catalog.scan()
    write(root, '.retroone/library-layout.json', b'{broken')
    assert catalog.scan()['last_error']
    with catalog.connect() as db:
        assert db.execute("SELECT value FROM settings WHERE key='library_layout_required'").fetchone() is None


@pytest.mark.parametrize('change', ['empty', 'partial', 'identity'])
@pytest.mark.parametrize('sentinel', ['absent', 'unchanged', 'tampered'])
def test_changed_layout_identity_fails_before_writes_preserving_history(tmp_path, monkeypatch, change, sentinel):
    root = tmp_path / 'roms'
    bindings = [dict(path='library/nes/' + name, original_path='library/nes/new/' + name) for name in ('FC', 'FC2')]
    for binding in bindings:
        write(root, binding['path'] + '/game.nes')
    manifest(root, bindings)
    if sentinel != 'absent':
        seal(root)
    catalog = Catalog(root, tmp_path / 'profile')
    assert catalog.scan()['errors'] == 0
    identifier = game_id('library/nes/new/FC/game.nes')
    catalog.set_favorite(identifier, True)
    catalog.mark_played(identifier)
    with catalog.connect() as db:
        before = [tuple(row) for row in db.execute('SELECT id,payload FROM games ORDER BY id')]
        saved_bindings = db.execute("SELECT value FROM settings WHERE key='library_layout_bindings'").fetchone()[0]
    if change == 'empty':
        changed = []
    elif change == 'partial':
        changed = bindings[1:]
    else:
        changed = [dict(bindings[0], original_path='library/nes/other/FC'), bindings[1]]
    manifest(root, changed)
    if sentinel == 'tampered':
        # A rewritten or removed filesystem seal must not bypass profile history.
        seal(root)
    monkeypatch.setattr(catalog.game_configs, 'generate', lambda game: pytest.fail('Identity changed before validation'))
    revision = catalog.game_metadata.revision()
    status = catalog.scan()
    assert status['last_error'] and status['scanned'] == 0
    assert catalog.game_metadata.revision() == revision
    assert catalog.get(identifier)['favorite'] and catalog.get(identifier)['play_count'] == 1
    with catalog.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT id,payload FROM games ORDER BY id')] == before
        assert db.execute("SELECT value FROM settings WHERE key='library_layout_bindings'").fetchone()[0] == saved_bindings
    if sentinel != 'absent':
        (root / '.retroone/library-layout.required').unlink()
        assert catalog.scan()['scanned'] == 0
        assert catalog.scan_status['last_error']


def test_layout_seal_ignores_order_but_new_bindings_require_offline_seal_update(tmp_path):
    root = tmp_path / 'roms'
    bindings = [dict(path='library/nes/' + name, original_path='library/nes/new/' + name) for name in ('FC2', 'FC')]
    for binding in bindings:
        write(root, binding['path'] + '/game.nes')
    manifest(root, bindings)
    seal(root)
    sealed = (root / '.retroone/library-layout.required').read_bytes()
    catalog = Catalog(root, tmp_path / 'profile')
    assert catalog.scan()['errors'] == 0
    manifest(root, list(reversed(bindings)))
    assert catalog.scan()['errors'] == 0
    bindings.append(dict(path='library/gb/GB', original_path='library/gb/new/GB'))
    write(root, 'library/gb/GB/game.gb')
    manifest(root, bindings)
    with pytest.raises(ValueError, match='布局校验不一致'):
        LibraryLayout.load(root)
    assert catalog.scan()['scanned'] == 0
    assert (root / '.retroone/library-layout.required').read_bytes() == sealed
    seal(root)  # Only the approved offline tool makes this change.
    assert catalog.scan()['errors'] == 0
    assert len(list(catalog.discover())) == 3
    with catalog.connect() as db:
        remembered = json.loads(db.execute("SELECT value FROM settings WHERE key='library_layout_bindings'").fetchone()[0])
    assert remembered == canonical_manifest(dict(schema_version=1, collections=bindings))['collections']


def test_scan_records_bindings_without_writing_readonly_rom_sentinel(tmp_path):
    root = tmp_path / 'roms'
    write(root, 'library/nes/FC/game.nes')
    manifest(root, [dict(path='library/nes/FC', original_path='library/nes/new/FC')])
    assert Catalog(root, tmp_path / 'profile').scan()['errors'] == 0
    assert not (root / '.retroone/library-layout.required').exists()


def test_small_3ds_remains_cloud_by_default_even_with_new_browser_core(tmp_path, monkeypatch):
    monkeypatch.setenv('RETROONE_LIBRARY_DATA_ROOT', str(tmp_path / 'shared'))
    root = tmp_path / 'roms'
    write(root, 'library/3ds/generated.cci', b'synthetic 3DS cartridge')
    games = list(Catalog(root, tmp_path / 'profile').discover())
    assert len(games) == 1
    assert games[0]['browser_core'] == 'azahar'
    assert games[0]['browser_eligible'] is False and games[0]['mode'] == 'stream'
