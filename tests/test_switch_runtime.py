"""Synthetic bytes only: readiness is never proof of decryption/game compatibility."""
import configparser
import json
from pathlib import Path
import stat
import zipfile

import pytest

from retroone.catalog import Catalog
from retroone.runner import Runner
from retroone.switch_runtime import SwitchRuntime, SELKIES_GUID


@pytest.fixture
def runtime(tmp_path):
    adapter = SwitchRuntime(tmp_path / 'player1', tmp_path / 'roms', lambda _: '/usr/local/bin/retroone-eden')
    adapter.source.mkdir(parents=True)
    return adapter


def assets(adapter):
    # Deliberately non-secret synthetic fixtures; no valid console material.
    (adapter.source / 'prod.keys').write_text('header_key = ' + '0' * 64 + '\n')
    with zipfile.ZipFile(adapter.source / 'firmware.zip', 'w') as zipped:
        zipped.writestr('firmware/' + '0' * 32 + '.nca', b'synthetic firmware test fixture')
    game = adapter.rom_root / 'test.nsp'
    game.write_bytes(b'synthetic non-game fixture')
    return game


def test_readiness_distinguishes_dependencies_and_never_claims_playable(runtime):
    assert not runtime.readiness()['available']
    game = assets(runtime)
    ready = runtime.readiness()
    assert ready['available'] and not ready['verified_playable'] and ready['experimental']
    assert ready['checks']['firmware_files'] == 1
    assert '0' * 64 not in json.dumps(ready)
    runtime.program = lambda _: None
    ready = runtime.readiness()
    assert not ready['available'] and ready['checks']['keys_structure']
    assert not ready['checks']['emulator_installed']


def test_key_filename_is_not_enough(runtime):
    assets(runtime)
    (runtime.source / 'prod.keys').write_text('invalid private-looking value')
    result = runtime.readiness()
    assert not result['available'] and not result['checks']['keys_structure']
    assert 'private-looking' not in json.dumps(result)


def test_private_portable_data_preserves_saves_and_preferences(runtime, tmp_path):
    game = assets(runtime)
    source_before = {p.name: p.read_bytes() for p in runtime.source.iterdir()}
    command = runtime.prepare(game)
    assert command == ['/usr/local/bin/retroone-eden', '-f', '-u', '0', '-g', str(game)]
    saved = runtime.user / 'nand/user/save/fixture'
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b'keep this member save')
    ini = runtime.user / 'config/qt-config.ini'
    contents = ini.read_text().replace('resolution_setup=3', 'resolution_setup=4')
    ini.write_text(contents + '\n[Other]\npreference=keep\n')
    runtime.prepare(game)
    assert saved.read_bytes() == b'keep this member save'
    assert 'resolution_setup=4' in ini.read_text() and 'preference=keep' in ini.read_text()
    assert (runtime.user / 'keys/prod.keys').stat().st_mode & 0o777 == 0o600
    second = SwitchRuntime(tmp_path / 'player2', runtime.rom_root, runtime.program)
    second.prepare(game)
    assert not (second.user / 'nand/user/save/fixture').exists()
    assert (second.user / 'nand/system/Contents/registered').resolve() != (runtime.user / 'nand/system/Contents/registered').resolve()
    assert source_before == {p.name: p.read_bytes() for p in runtime.source.iterdir()}


def test_settings_match_normalized_sdl_and_respect_real_analog_axes(runtime):
    runtime.prepare(assets(runtime))
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(runtime.user / 'config/qt-config.ini')
    assert parser['Renderer']['backend'] == '1'
    assert parser['Renderer']['backend\\default'] == 'false'
    controls = parser['Controls']
    assert SELKIES_GUID in controls['player_0_lstick']
    assert 'axis_x:3,axis_y:4' in controls['player_0_rstick']
    assert 'axis:5' in controls['player_0_button_zr']
    assert 'button:1' in controls['player_0_button_a']
    assert controls['player_1_connected'] == 'false'


@pytest.mark.parametrize('name,link', [('../'+'0'*32+'.nca', False), ('/absolute.nca', False), ('bad.nca', False), ('0'*32+'.nca', True)])
def test_firmware_archive_rejects_unsafe_members(runtime, name, link):
    assets(runtime)
    with zipfile.ZipFile(runtime.source / 'firmware.zip', 'w') as zipped:
        entry = zipfile.ZipInfo(name)
        if link:
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(entry, b'fixture')
    assert not runtime.readiness()['checks']['firmware_structure']


def test_duplicate_and_bounded_firmware(runtime, monkeypatch):
    assets(runtime)
    with zipfile.ZipFile(runtime.source / 'firmware.zip', 'w') as zipped:
        zipped.writestr('a/'+'0'*32+'.nca', b'fixture')
        zipped.writestr('b/'+'0'*32+'.nca', b'fixture')
    assert not runtime.readiness()['available']
    assets(runtime)
    monkeypatch.setattr('retroone.switch_runtime.FIRMWARE_LIMIT', 8)
    assert not runtime.readiness()['available']


def test_firmware_change_retains_old_generation_and_saves(runtime):
    game = assets(runtime)
    runtime.prepare(game)
    registered = runtime.user / 'nand/system/Contents/registered'
    before = registered.resolve()
    with zipfile.ZipFile(runtime.source / 'firmware.zip', 'w') as zipped:
        zipped.writestr('1'*32+'.nca', b'different synthetic fixture')
    runtime.prepare(game)
    assert before.exists() and before != registered.resolve()


def test_external_config_and_existing_nand_are_preserved(runtime, tmp_path):
    game = assets(runtime)
    outside = tmp_path / 'outside'
    outside.mkdir()
    runtime.user.mkdir(parents=True)
    (runtime.user / 'config').symlink_to(outside)
    with pytest.raises(ValueError, match='超出'):
        runtime.prepare(game)
    assert not list(outside.iterdir())
    (runtime.user / 'config').unlink()
    registered = runtime.user / 'nand/system/Contents/registered'
    registered.unlink()
    registered.mkdir()
    (registered / 'preserve').write_text('already installed by user')
    with pytest.raises(RuntimeError, match='已有独立'):
        runtime.prepare(game)
    assert (registered / 'preserve').read_text() == 'already installed by user'


def test_rom_source_link_cannot_escape(runtime, tmp_path):
    assets(runtime)
    keys = runtime.source / 'prod.keys'
    other = tmp_path / 'other.keys'
    other.write_bytes(keys.read_bytes())
    keys.unlink()
    keys.symlink_to(other)
    assert not runtime.readiness()['checks']['keys_structure']


def test_small_switch_always_cloud_and_compressed_formats_stay_blocked(tmp_path):
    root = tmp_path / 'roms'
    collection = root / 'library/switch/local/collection'
    collection.mkdir(parents=True)
    for extension in ('nsp', 'nro', 'nsz', 'xcz'):
        (collection / ('fixture.' + extension)).write_bytes(b'not a game')
    catalog = Catalog(root, tmp_path / 'catalog')
    catalog.scan()
    catalog.apply_runtime({'platforms': {'switch': {'available': True, 'experimental': True}}}, True)
    games = catalog.list()['games']
    assert len(games) == 4
    for game in games:
        assert not game['browser_eligible']
        if Path(game['rom_path']).suffix in {'.nsz', '.xcz'}:
            assert game['mode'] == 'unavailable' and game['blocked_reason']
        else:
            assert game['mode'] == 'stream'


def test_switch_assets_never_copied_as_generic_bios(runtime):
    assets(runtime)
    (runtime.rom_root / 'firmware/disksys.rom').write_bytes(b'synthetic bios fixture')
    runner = Runner(runtime.config_root, runtime.rom_root)
    system = runner._firmware()
    assert (system / 'disksys.rom').exists()
    assert not (system / 'switch').exists() and not (system / 'prod.keys').exists()


def test_unsupported_format_fails_before_preparing_data(runtime):
    game = assets(runtime).with_suffix('.nsz')
    with pytest.raises(ValueError, match='NSZ'):
        runtime.prepare(game)
    assert not runtime.user.exists()
