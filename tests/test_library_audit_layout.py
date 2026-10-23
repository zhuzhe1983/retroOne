"""Resource closure safety after removing provenance directories."""
from contextlib import closing
import json
from pathlib import Path

import pytest

from scripts.library_audit import Auditor, Cache


def write(root, relative, data=b'generated fixture'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def record(relative, identifier='a', **extra):
    return dict(id=identifier, title='Generated fixture', platform='windows',
                rom_path='/roms/' + relative, native_core='test-core', **extra)


def audit(tmp_path, records):
    with closing(Cache(tmp_path / 'cache.sqlite')) as cache:
        return Auditor(tmp_path / 'one', cache, hash_all=True, rate_mib=0).run(records)


@pytest.mark.parametrize('layout', ['manifest', 'legacy'])
def test_known_collection_audits_entire_game_not_launcher_subfolder(tmp_path, layout):
    root = tmp_path / 'one'
    collection = 'library/windows/new/PC' if layout == 'legacy' else 'library/windows/PC'
    records = []
    for identifier, payload in [('a', b'one'), ('b', b'two'), ('c', b'one')]:
        launch = collection + '/' + identifier + '/bin/main.exe'
        write(root, launch, b'identical launcher')
        write(root, collection + '/' + identifier + '/data/level.bin', payload)
        write(root, collection + '/' + identifier + '/settings.ini')
        records.append(record(launch, identifier))
    if layout == 'manifest':
        write(root, '.retroone/library-layout.json', json.dumps({'schema_version':1,'collections':[
            {'path':collection,'original_path':'library/windows/new/PC'}]}).encode())
    result = audit(tmp_path, records)
    assert result['duplicates']['content_exact'][0]['game_ids'] == ['a', 'c']
    assert all(len(item['source_manifest']) == 3 for item in result['games'])
    assert result['games'][0]['resource_root'] == collection + '/a'
    assert result['games'][1]['content']['sha256'] != result['games'][0]['content']['sha256']


def test_pegasus_launcher_does_not_prove_bin_is_complete_game_tree(tmp_path):
    root = tmp_path / 'one'
    records = []
    for name, content in [('One', b'first level'), ('Two', b'different level')]:
        folder = 'library/windows/' + name
        write(root, folder + '/bin/main.exe', b'identical launcher')
        write(root, folder + '/data/levels.bin', content)
        write(root, folder + '/metadata.pegasus.txt', ('game: ' + name + '\nfile: bin/main.exe\n').encode())
        records.append(record(folder + '/bin/main.exe', name))
    result = audit(tmp_path, records)
    assert all(item['unverified'][0]['code'] == 'unknown_directory_boundary' for item in result['games'])
    assert result['progress']['bytes_hashed'] == 0
    assert not result['duplicates']['content_exact']
    for item in records:
        item['resource_root'] = item['rom_path'].rsplit('/bin/', 1)[0]
    complete = audit(tmp_path, records)
    assert all(len(item['source_manifest']) == 3 for item in complete['games'])
    assert not complete['duplicates']['content_exact']
    assert complete['games'][0]['content']['sha256'] != complete['games'][1]['content']['sha256']


def test_unmarked_flat_pc_path_never_audits_only_bin_subfolder(tmp_path):
    root = tmp_path / 'one'
    launch = 'library/windows/Game/bin/main.exe'
    write(root, launch)
    write(root, 'library/windows/Game/data/level.bin', b'essential resources')
    result = audit(tmp_path, [record(launch)])
    assert result['games'][0]['unverified'][0]['code'] == 'unknown_directory_boundary'
    assert result['progress']['bytes_hashed'] == 0
    explicit = audit(tmp_path, [record(launch, resource_root='/roms/library/windows/Game')])
    assert len(explicit['games'][0]['source_manifest']) == 2
    whole_directory = audit(tmp_path, [record('library/windows/Game')])
    assert len(whole_directory['games'][0]['source_manifest']) == 2


def test_launcher_at_collection_root_needs_explicit_resource_root(tmp_path):
    root = tmp_path / 'one'
    write(root, 'library/windows/PC/main.exe')
    write(root, 'library/windows/PC/metadata.pegasus.txt', b'collection: PC\n')
    result = audit(tmp_path, [record('library/windows/PC/main.exe')])
    assert result['games'][0]['unverified'][0]['code'] == 'unknown_directory_boundary'
    assert result['progress']['bytes_hashed'] == 0


def test_invalid_layout_does_not_fall_back_to_legacy_depth(tmp_path):
    root = tmp_path / 'one'
    write(root, 'library/windows/new/PC/Game/main.exe')
    write(root, '.retroone/library-layout.json', b'{broken')
    result = audit(tmp_path, [record('library/windows/new/PC/Game/main.exe')])
    assert result['games'][0]['unverified'][0]['code'] == 'unknown_directory_boundary'
    assert result['progress']['bytes_hashed'] == 0
