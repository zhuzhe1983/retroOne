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
    assert collection_path('/roms/library/nes/new/FC/media/a/boxFront.png') == '/roms/library/nes/new/FC'
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
