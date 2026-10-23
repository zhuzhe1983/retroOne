"""Matching must preserve game identity, release-stage and sequel boundaries."""
from retroone.artwork_match import NameIndex, collection_path, normalized_title
from scripts.prepare_artwork import Directory, valid_png
from scripts.match_artwork_hashes import archive_member, dat_entries
import zipfile
import pytest
import hashlib
import json
from scripts.publish_artwork import publish


def test_known_tags_match_same_title_and_prefer_same_region():
    index = NameIndex(['Super Mario Bros. (Japan)', 'Super Mario Bros. (USA)', 'Super Mario Bros. 2 (USA)'])
    result = index.match(['Super Mario Bros. (USA) (Rev 1)'])
    assert result['method'] == 'same-title-known-tags'
    assert result['matched_name'] == 'Super Mario Bros. (USA)'
    assert result['same_region']
    assert index.match(['Super Mario Bros. (USA) (Rev A)'])['matched_name'] == 'Super Mario Bros. (USA)'
    assert index.match(['Super Mario Bros. 3 (USA)']) is None
    assert index.match(['Super Mario Bro (USA)']) is None


def test_unknown_tags_and_disc_numbers_are_never_stripped():
    index = NameIndex(['Game (USA)', 'RPG (Disc 1) (Japan)', 'Game (Proto) (Japan)'])
    assert index.match(['Game (Beta) (USA)']) is None
    assert index.match(['Game (Hack) (USA)']) is None
    assert index.match(['RPG (Disc 2) (Japan)']) is None
    assert index.match(['Game (Proto) (USA)'])['matched_name'] == 'Game (Proto) (Japan)'
    assert normalized_title('Game (En,Fr) (Rev 2)')['title'] == 'game'


def test_directory_only_accepts_leaf_png_names():
    parser = Directory()
    parser.feed('<a href="Mario%20(USA).png">cover</a><a href="../escape.png">bad</a><a href="https://evil/x.png">bad</a><a href="/other.png">bad</a>')
    assert parser.names == {'Mario (USA)'}
    assert collection_path('/roms/library/nes/new/FC/media/a/boxFront.png', ['/roms/library/nes/new/FC']) == '/roms/library/nes/new/FC'
    assert not valid_png(b'\x89PNG\r\n\x1a\nFAKE')


def test_official_dat_identity_uses_size_crc_and_keeps_sha1():
    records = dat_entries('''game (
        name "Mario (USA)"
        rom ( name "Mario.gba" size 10 crc 1234ABCD md5 00000000000000000000000000000000 sha1 1234567890123456789012345678901234567890 )
)
''')
    assert records[(10, 0x1234ABCD)][0] == {'title': 'Mario (USA)', 'sha1': '1234567890123456789012345678901234567890'}
    assert not records[(11, 0x1234ABCD)]


def test_hash_lookup_rejects_multigame_archive_and_bounded_directory(tmp_path):
    path = tmp_path / 'rom.zip'
    with zipfile.ZipFile(path, 'w') as stream:
        stream.writestr('one.gba', b'first')
        stream.writestr('two.gba', b'second')
    with pytest.raises(ValueError, match='exactly one'):
        archive_member(path, {'.gba'})
    with zipfile.ZipFile(path, 'w') as stream:
        for number in range(129): stream.writestr(str(number) + '.txt', b'')
    with pytest.raises(ValueError, match='directory'):
        archive_member(path, {'.gba'})


def test_publish_only_complete_verified_images_and_preserve_other_batch(tmp_path):
    source, destination = tmp_path / 'staging', tmp_path / 'shared'
    source.mkdir(); destination.mkdir()
    data = b'\x89PNG\r\n\x1a\nfixture'
    key = 'a' * 64
    (source / (key + '.png')).write_bytes(data)
    (source / (key + '.json')).write_text(json.dumps({'source': 'https://thumbnails.libretro.com/Nintendo/a.png', 'sha256': 'wrong'}))
    assert publish(source, destination)['invalid'] == 1
    assert not (destination / (key + '.png')).exists()
    (source / (key + '.json')).write_text(json.dumps({'source': 'https://thumbnails.libretro.com/Nintendo/a.png', 'sha256': hashlib.sha256(data).hexdigest()}))
    (destination / 'local-overrides.json').write_text(json.dumps({'version': 1, 'games': {'older': {'source_path': '/roms/library/nes/old.png'}}}))
    local = tmp_path / 'local.json'
    local.write_text(json.dumps({'version': 1, 'games': {'newer': {'local_validation': {'valid': True}, 'source_path': '/roms/library/nes/new.png'}}}))
    result = publish(source, destination, local)
    assert result['published'] == 1 and result['local_added'] == 1
    assert (destination / (key + '.png')).read_bytes() == data
    assert set(json.loads((destination / 'local-overrides.json').read_text())['games']) == {'older', 'newer'}
    assert publish(source, destination, local)['existing'] == 1


def test_collection_matching_uses_recorded_boundaries_in_flat_and_legacy_layouts():
    boundaries = ['/roms/library/nes', '/roms/library/nes/Cart set', '/roms/library/nes/new/Legacy']
    assert collection_path('/roms/library/nes/Game.nes', boundaries) == '/roms/library/nes'
    assert collection_path('/roms/library/nes/Cart set/media/001/boxFront.png', boundaries) == '/roms/library/nes/Cart set'
    assert collection_path('/roms/library/nes/new/Legacy/Game.nes', boundaries) == '/roms/library/nes/new/Legacy'
    assert collection_path('/roms/library/gba/001.gba', boundaries) is None
    assert collection_path('/roms/library/nes/Game.nes') is None
    assert collection_path('/roms/library/nes/../gba/Game.gba', boundaries) is None
    assert collection_path('/roms/library/nes-other/Game.nes', boundaries) is None


def artwork_database(tmp_path, games):
    import sqlite3
    path = tmp_path / 'catalog.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE games (payload TEXT)')
        db.executemany('INSERT INTO games VALUES (?)', [(json.dumps(game),) for game in games])
    return path


@pytest.mark.parametrize('collection', ['library/nes', 'library/nes/FC', 'library/nes/new/FC'])
def test_artwork_inventory_preserves_actual_collection_boundary(tmp_path, collection):
    from scripts.audit_artwork import audit
    root = tmp_path / 'one'
    folder = root / collection
    (folder / 'media/Game').mkdir(parents=True)
    (folder / 'Game.nes').write_bytes(b'generated cartridge')
    (folder / 'media/Game/boxFront.png').write_bytes(b'generated artwork placeholder')
    db = artwork_database(tmp_path, [{'id':'game','platform':'nes','title':'Game','rom_path':'/roms/' + collection + '/Game.nes'}])
    result = audit(db, root, {})
    assert result['layout_error'] is None
    assert result['games'][0]['collection_path'] == '/roms/' + collection
    assert result['local_media'][0]['collection_path'] == '/roms/' + collection
    assert result['recognized_local_covers'] == 1


def test_artwork_inventory_uses_manifest_for_collection_named_like_old_source(tmp_path):
    from scripts.audit_artwork import audit
    root = tmp_path / 'one'
    folder = root / 'library/nes/new'
    (folder / 'media/Game').mkdir(parents=True)
    (folder / 'roms').mkdir()
    (folder / 'roms/Game.nes').write_bytes(b'generated cartridge')
    (folder / 'media/Game/boxFront.png').write_bytes(b'generated artwork placeholder')
    (root / '.retroone').mkdir()
    (root / '.retroone/library-layout.json').write_text(json.dumps({'schema_version':1,'collections':[
        {'path':'library/nes/new','original_path':'library/nes/old/Original'}]}))
    db = artwork_database(tmp_path, [{'id':'game','platform':'nes','title':'Game','rom_path':'/roms/library/nes/new/roms/Game.nes'}])
    result = audit(db, root, {})
    assert result['games'][0]['collection_path'] == '/roms/library/nes/new'
    assert result['recognized_local_covers'] == 1


def test_artwork_inventory_invalid_mapping_does_not_guess_or_scan_media(tmp_path):
    from scripts.audit_artwork import audit
    root = tmp_path / 'one'
    folder = root / 'library/nes/FC'
    (folder / 'media/Game').mkdir(parents=True)
    (folder / 'Game.nes').write_bytes(b'generated cartridge')
    (folder / 'media/Game/boxFront.png').write_bytes(b'generated artwork placeholder')
    (root / '.retroone').mkdir()
    (root / '.retroone/library-layout.json').write_text('{invalid json')
    db = artwork_database(tmp_path, [{'id':'game','platform':'nes','title':'Game','rom_path':'/roms/library/nes/FC/Game.nes'}])
    result = audit(db, root, {})
    assert result['layout_error']
    assert result['games'][0]['collection_path'] is None
    assert result['local_media'] == []


def prepare_fixture(tmp_path, monkeypatch, boundaries):
    import scripts.prepare_artwork as module
    monkeypatch.setattr(module, 'load_index', lambda platform, *_: (platform, {'status':'unavailable','error':'offline fixture'}))
    game = {'id':'game','platform':'nes','title':'001','rom_path':'/roms/library/nes/Family/001.nes',
            'local_state':'no_local_reference','cache_key':'a' * 64,'cached_in':[]}
    media = [{'platform':'nes','match_name':'001','path':'/roms/library/nes/' + collection + '/media/001/boxFront.png'}
             for collection in ['Another', 'Family']]
    inventory = {'finished_at':0,'rom_root':str(tmp_path / 'not-mounted'), 'games':[game],'local_media':media,
                 'collections':[{'path':value} for value in boundaries]}
    return module, inventory


def test_numbered_artwork_matches_only_same_recorded_collection_off_host(tmp_path, monkeypatch):
    module, inventory = prepare_fixture(tmp_path, monkeypatch, ['/roms/library/nes/Family','/roms/library/nes/Another'])
    result = module.prepare(inventory, tmp_path / 'output', None, 1)
    entry = result['games']['game']
    assert entry['status'] == 'local'
    assert entry['source_path'] == '/roms/library/nes/Family/media/001/boxFront.png'
    assert entry['source_candidates'] == 1


def test_numbered_artwork_without_boundary_is_not_a_match(tmp_path, monkeypatch):
    module, inventory = prepare_fixture(tmp_path, monkeypatch, [])
    result = module.prepare(inventory, tmp_path / 'output', None, 1)
    assert result['games']['game']['status'] != 'local'


def test_legacy_artwork_inventory_resolves_collection_on_host(tmp_path):
    from scripts.prepare_artwork import inventory_collections
    root = tmp_path / 'one'
    (root / 'library/nes/old/FC').mkdir(parents=True)
    result = inventory_collections({'rom_root':str(root),'games':[{'rom_path':'/roms/library/nes/old/FC/Game.nes'}],'local_media':[]})
    assert result == {'/roms/library/nes/old/FC'}


def test_arbitrary_legacy_source_artwork_uses_catalog_boundary(tmp_path):
    from scripts.audit_artwork import audit
    from scripts.prepare_artwork import inventory_collections
    root = tmp_path / 'one'
    folder = root / 'library/nes/custom-source/FC'
    (folder / 'media/001').mkdir(parents=True)
    (folder / '001.nes').write_bytes(b'generated cartridge')
    (folder / 'media/001/boxFront.png').write_bytes(b'generated artwork')
    game = dict(id='game', platform='nes', source='custom-source', collection='FC',
                title='001', rom_path='/roms/library/nes/custom-source/FC/001.nes')
    result = audit(artwork_database(tmp_path, [game]), root, {})
    assert result['recognized_local_covers'] == 1
    assert result['games'][0]['collection_path'] == '/roms/library/nes/custom-source/FC'
    assert len(result['games'][0]['exact_local_candidates']) == 1
    assert inventory_collections({'rom_root': str(root), 'games': [game]}) == {
        '/roms/library/nes/custom-source/FC'}


def test_numbered_artwork_audit_does_not_count_another_collection(tmp_path):
    from scripts.audit_artwork import audit
    root = tmp_path / 'one'
    for name in ['Family', 'Another']:
        (root / ('library/nes/' + name)).mkdir(parents=True)
        (root / ('library/nes/' + name + '/001.nes')).write_bytes(b'generated cartridge')
    cover = root / 'library/nes/Another/media/001/boxFront.png'
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b'generated artwork placeholder')
    games = [dict(id=name,platform='nes',title='001',rom_path='/roms/library/nes/' + name + '/001.nes')
             for name in ['Family', 'Another']]
    result = audit(artwork_database(tmp_path,games), root, {})
    records = {game['id']:game for game in result['games']}
    assert records['Family']['exact_local_candidates'] == []
    assert records['Another']['exact_local_candidates'] == ['/roms/library/nes/Another/media/001/boxFront.png']
