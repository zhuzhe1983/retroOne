"""Experiment eligibility is independent of automatic mode and trusts live files."""
import copy
import json
import re
from pathlib import Path
import stat
import zipfile

import pytest

from retroone.browser_runtime import BrowserRuntime, BrowserRuntimeError
from retroone.platforms import PLATFORMS

MIB = 1024**2


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / 'roms'
    library = root / 'library'
    library.mkdir(parents=True)
    data = tmp_path / 'emulatorjs/data'
    (data / 'cores').mkdir(parents=True)
    (data / 'loader.js').write_text('// fixture')

    def game(platform='n64', suffix='.z64', size=32 * MIB, **values):
        core = PLATFORMS[platform]['browser_core']
        if core:
            (data / 'cores' / f'{core}{"-thread" if core == "ppsspp" else ""}-wasm.data').touch()
        if platform == 'psp':
            (data / 'cores/ppsspp-assets.zip').touch()
        path = library / ('game' + suffix)
        with path.open('wb') as file:
            file.truncate(size)
        record = dict(platform=platform, browser_core=core, rom_path=str(path),
                      files=[str(path)], launch_files=[str(path)], browser_eligible=False,
                      size=size, unpacked_size=size, mode='stream', **values)
        return record, path

    return BrowserRuntime(root, data), game


def test_actual_large_cartridge_is_explicitly_supported_without_changing_auto_or_record(fixture):
    runtime, make = fixture
    game, path = make()
    original = copy.deepcopy(game)
    view = runtime.inspect(game)
    assert view['available'] and view['size_bytes'] == 32 * MIB
    assert runtime.require(game) == path
    assert game == original and game['mode'] == 'stream'
    assert str(runtime.rom_root) not in json.dumps(view)
    assert 'verified_playable' not in view


def test_thread_only_psp_requires_assets_and_reports_device_requirements(fixture):
    runtime, make = fixture
    game, _ = make('psp', '.iso')
    assert not (runtime.browser_data / 'cores/ppsspp-wasm.data').exists()
    view = runtime.inspect(game)
    assert view['available'] and view['requires_threads'] and view['requires_webgl2']
    (runtime.browser_data / 'cores/ppsspp-assets.zip').unlink()
    assert not runtime.inspect(game)['available']


@pytest.mark.parametrize('platform,suffix', [('psp', '.chd'), ('psx', '.chd'), ('psx', '.iso'), ('psx', '.bin'),
                                           ('n64', '.7z'), ('nds', '.exe'), ('psp', '.zip')])
def test_reject_formats_unadvertised_or_not_self_contained_in_pinned_core(fixture, platform, suffix):
    runtime, make = fixture
    game, _ = make(platform, suffix)
    assert not runtime.inspect(game)['available']
    with pytest.raises(BrowserRuntimeError):
        runtime.require(game)


@pytest.mark.parametrize('platform,suffix', [('dos', '.zip'), ('windows', '.exe'), ('fbneo', '.zip'),
                                           ('ps2', '.iso'), ('switch', '.nsp'), ('3ds', '.3ds')])
def test_unreviewed_platforms_stay_cloud_even_for_tiny_payloads(fixture, platform, suffix):
    runtime, make = fixture
    game, _ = make(platform, suffix, 512)
    assert not runtime.inspect(game)['available']


def test_small_cartridge_platform_is_allowed_but_not_large_nes(fixture):
    runtime, make = fixture
    small, _ = make('nes', '.nes', 64 * 1024)
    assert runtime.inspect(small)['available']
    large, _ = make('nes', '.nes', 21 * MIB)
    assert not runtime.inspect(large)['available']


def test_metadata_cannot_hide_actual_limit_or_changed_dependency(fixture):
    runtime, make = fixture
    runtime.max_bytes = 8 * MIB
    game, path = make(size=10 * MIB)
    game['size'] = game['unpacked_size'] = 1
    assert not runtime.inspect(game)['available']
    with path.open('wb') as output:
        output.truncate(MIB)
    dependency = path.with_suffix('.dat')
    dependency.write_bytes(b'required')
    game['files'].append(str(dependency))
    assert not runtime.inspect(game)['available']


@pytest.mark.parametrize('field', ['files', 'launch_files'])
def test_secondary_file_launches_and_empty_resource_lists_are_rejected(fixture, field):
    runtime, make = fixture
    game, path = make()
    game[field] = []
    assert not runtime.inspect(game)['available']
    game[field] = [str(path.with_name('absent.z64'))]
    assert not runtime.inspect(game)['available']


def test_escape_symlinks_directories_and_untrusted_core_cannot_be_downloaded(fixture, tmp_path):
    runtime, make = fixture
    game, path = make()
    path.unlink()
    outside = tmp_path / 'private.z64'
    outside.write_bytes(b'not public')
    path.symlink_to(outside)
    result = runtime.inspect(game)
    assert not result['available'] and str(tmp_path) not in json.dumps(result)
    path.unlink()
    path.mkdir()
    assert not runtime.inspect(game)['available']
    path.rmdir()
    path.write_bytes(b'cartridge')
    game['browser_core'] = '../../private'
    assert not runtime.inspect(game)['available']


def test_zip_single_cartridge_counts_live_unpacked_bytes_without_extraction(fixture):
    runtime, make = fixture
    game, path = make('gba', '.zip', 1)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('game.gba', b'x' * (21 * MIB))
    assert path.stat().st_size < MIB
    view = runtime.inspect(game)
    assert view['available'] and view['unpacked_bytes'] == 21 * MIB
    assert list(path.parent.iterdir()) == [path]
    runtime.max_bytes = 20 * MIB
    assert not runtime.inspect(game)['available']


@pytest.mark.parametrize('names', [['one.gba', 'two.gba'], ['game.gba', 'readme.txt'],
                                  ['../game.gba'], ['x\\game.gba'], ['/game.gba'], ['nested.zip'],
                                  ['game.exe'], ['drive:game.gba']])
def test_zip_requires_one_safe_cartridge_without_hidden_dependencies(fixture, names):
    runtime, make = fixture
    game, path = make('gba', '.zip', 1)
    with zipfile.ZipFile(path, 'w') as archive:
        for name in names:
            archive.writestr(name, b'fixture')
    assert not runtime.inspect(game)['available']


def test_zip_links_and_excessive_directory_are_rejected(fixture):
    runtime, make = fixture
    game, path = make('gba', '.zip', 1)
    entry = zipfile.ZipInfo('game.gba')
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(entry, '../private')
    assert not runtime.inspect(game)['available']
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('game.gba', b'fixture')
        for index in range(129):
            archive.writestr(f'{index}/', '')
    assert not runtime.inspect(game)['available']


def test_default_is_unlimited_and_positive_admin_limits_are_not_clamped(fixture, monkeypatch):
    runtime, make = fixture
    monkeypatch.delenv('RETROONE_WASM_MAX_BYTES', raising=False)
    assert BrowserRuntime(runtime.rom_root, runtime.browser_data).max_bytes == 0
    monkeypatch.setenv('RETROONE_WASM_MAX_BYTES', str(4 * 1024**3))
    assert BrowserRuntime(runtime.rom_root, runtime.browser_data).max_bytes == 4 * 1024**3
    for invalid in (-1, True, 'invalid', '1.5'):
        with pytest.raises(ValueError):
            BrowserRuntime(runtime.rom_root, runtime.browser_data, max_bytes=invalid)
    game, path = make('psp', '.iso', size=3 * 1024**3 + 7)
    # Eligibility must stat a sparse large file, never read it into memory.
    def no_read(*args, **kwargs):
        raise AssertionError('raw ROM contents must not be read for eligibility')
    monkeypatch.setattr(Path, 'open', no_read)
    assert runtime.require(game) == path and runtime.inspect(game)['max_bytes'] == 0
    limited = BrowserRuntime(runtime.rom_root, runtime.browser_data, max_bytes=1024**3)
    assert not limited.inspect(game)['available']


def test_3ds_experiment_uses_complete_threaded_runtime_and_old_catalog_rows(fixture):
    runtime, make = fixture
    game, path = make('3ds', '.cci', 917 * MIB)
    game['browser_core'] = None  # Stored before Azahar WASM was introduced.
    before = copy.deepcopy(game)
    assert not runtime.inspect(game)['available']
    data = runtime.browser_3ds_data
    (data / 'cores').mkdir(parents=True, exist_ok=True)
    (data / 'loader.js').write_text('// official 4.3.0-pre fixture')
    (data / 'cores/azahar-thread-wasm.data').touch()
    view = runtime.inspect(game)
    assert view['available'] and view['core'] == 'azahar'
    assert view['requires_threads'] and view['requires_webgl2']
    assert runtime.require(game) == path
    assert runtime.runtime_data(game) == data
    assert re.fullmatch('/emulatorjs/[0-9a-f]{20}/data/', runtime.runtime_url(game))
    assert runtime.runtime_url(game) == runtime.runtime_url({'platform': 'nes'})
    assert game == before and game['mode'] == 'stream'
    assert PLATFORMS['3ds']['browser_experimental_only']
    assert str(data) not in json.dumps(view)
    game['browser_core'] = 'ppsspp'
    assert not runtime.inspect(game)['available']


@pytest.mark.parametrize('suffix', ['.cia', '.7z', '.iso', '.elf'])
def test_3ds_rejects_formats_outside_supported_cartridges(fixture, suffix):
    runtime, make = fixture
    game, _ = make('3ds', suffix, 10 * MIB)
    data = runtime.browser_3ds_data
    (data / 'cores').mkdir(parents=True, exist_ok=True)
    (data / 'loader.js').touch()
    (data / 'cores/azahar-thread-wasm.data').touch()
    assert not runtime.inspect(game)['available']


def test_3ds_runtime_can_be_explicitly_selected_without_changing_stable_platforms(fixture, tmp_path, monkeypatch):
    runtime, make = fixture
    selected = tmp_path / 'explicit-pre/data'
    monkeypatch.setenv('EMULATORJS_3DS_DATA', str(selected))
    other = BrowserRuntime(runtime.rom_root, runtime.browser_data)
    assert other.runtime_data({'platform': '3ds'}) == selected.resolve()
    assert other.runtime_data({'platform': 'gba'}) == runtime.browser_data
    assert other.runtime_url({'platform': 'gba'}) == runtime.runtime_url({'platform': 'gba'})
    direct = tmp_path / 'explicit-argument/data'
    assert BrowserRuntime(runtime.rom_root, runtime.browser_data, browser_3ds_data=direct).browser_3ds_data == direct.resolve()


def test_runtime_upgrade_changes_asset_namespace_for_entire_bundle(fixture):
    runtime, _ = fixture
    previous = runtime.runtime_url({'platform': 'nes'})
    (runtime.browser_data / '.retroone-runtime.json').write_text('{"lock_sha256":"changed"}')
    upgraded = BrowserRuntime(runtime.rom_root, runtime.browser_data)
    assert upgraded.runtime_url({'platform': 'nes'}) != previous
    assert upgraded.runtime_url({'platform': 'nes'}) == upgraded.runtime_url({'platform': '3ds'})
