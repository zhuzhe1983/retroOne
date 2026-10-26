import copy
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from retroone.game_config import GameConfigError, GameConfigStore


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'roms'; source = root / 'library/nes/new/FC/game.nes'
    source.parent.mkdir(parents=True); source.write_bytes(b'generated cartridge data')
    game = dict(id='a' * 24, title='Original title', platform='nes', rom_path=str(source),
                files=[str(source)], launch_files=[str(source)], native_core='nestopia',
                browser_core='fceumm', engine_hint=None, browser_eligible=True,
                unpacked_size=source.stat().st_size, mode='browser', blocked_reason=None)
    return GameConfigStore(tmp_path / 'library-data/runtime', root), game, source


def test_generated_config_separates_manual_and_metadata_and_never_claims_playable(setup):
    store, game, source = setup
    document = store.generate(game)
    assert document['overrides'] == {} and not document['generated']['verified_playable']
    assert 'title' not in json.dumps(document) and str(store.rom_root) not in json.dumps(document)
    summary = store.summary(game)
    assert summary['status'] == 'configured' and not summary['verified_playable']
    assert summary['engine'] == 'EmulatorJS' and summary['core'] == 'fceumm'
    original = copy.deepcopy(game)
    resolved = store.resolve(game)
    assert game == original and resolved['id'] == game['id'] and resolved['rom_path'] == str(source)
    assert resolved['_game_config']['input_profile'] == 'default'
    assert not resolved['verified_playable']


def test_rescan_preserves_overrides_and_does_not_write_for_title_or_artwork_changes(setup):
    store, game, _ = setup
    store.generate(game)
    store.update_overrides(game, {'mode': 'stream', 'notes': 'Use server for this game'})
    path = store.root / (game['id'] + '.json'); before = path.read_bytes(); stamp = path.stat().st_mtime_ns
    renamed = dict(game, title='New title', media={'cover': '/unrelated/cover.png'})
    document = store.generate(renamed)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert document['overrides']['mode'] == 'stream'
    assert store.resolve(renamed)['mode'] == 'stream'
    assert store.resolve(renamed, 'browser')['mode'] == 'browser'
    store.update_overrides(game, {'mode': None})
    assert 'mode' not in store.get(game)['overrides']
    assert store.resolve(game)['mode'] == 'browser'


def test_changed_source_blocks_until_generation_and_keeps_overrides(setup):
    store, game, source = setup
    store.generate(game); store.update_overrides(game, {'mode': 'stream'})
    old = source.stat(); source.write_bytes(b'X' * old.st_size)
    os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert store.summary(game)['stale']
    with pytest.raises(GameConfigError) as failure: store.resolve(game)
    assert failure.value.code == 'stale_config'
    new = store.generate(game)
    assert new['overrides']['mode'] == 'stream'
    assert store.resolve(game)['mode'] == 'stream'


def test_missing_resource_and_escape_block_without_overwriting_or_leaking_absolute_path(setup, tmp_path):
    store, game, source = setup
    source.unlink(); store.generate(game)
    with pytest.raises(GameConfigError) as failure: store.resolve(game)
    assert failure.value.code == 'missing_or_unreadable_source'
    summary = store.summary(game)
    assert summary['missing_dependencies'] == ['library/nes/new/FC/game.nes']
    assert str(store.rom_root) not in json.dumps(summary)
    outside = tmp_path / 'secret'; outside.write_text('not a game')
    source.symlink_to(outside)
    store.generate(game)
    with pytest.raises(GameConfigError) as failure: store.resolve(game)
    assert failure.value.code == 'outside_rom_root'


@pytest.mark.parametrize('patch', [
    {'command': 'arbitrary'}, {'rom_path': '/etc/passwd'}, {'env': {'LD_PRELOAD': 'x'}},
    {'native_core': '../../evil'}, {'native_core': 'mame'}, {'browser_core': 'mame2003'},
    {'engine_hint': 'wine'}, {'engine_hint': {}}, {'input_profile': 'unimplemented'},
    {'disabled': 'false'}, {'mode': 'shell'}, {'notes': 'x' * 2001},
])
def test_override_allowlist_rejects_unimplemented_or_unsafe_values(setup, patch):
    store, game, _ = setup
    store.generate(game); before = store.get(game)
    with pytest.raises(GameConfigError) as failure: store.update_overrides(game, patch)
    assert failure.value.status_code == 422 and store.get(game) == before


def test_arcade_core_override_is_allowed_but_no_playability_claim(setup):
    store, game, _ = setup
    game = dict(game, platform='fbneo', native_core='fbneo', browser_core='fbneo')
    store.generate(game)
    summary = store.update_overrides(game, {'mode': 'stream', 'native_core': 'fbalpha2012'})
    assert summary['status'] == 'needs_review' and summary['core'] == 'fbalpha2012'
    assert not summary['verified_playable']
    assert store.resolve(game)['native_core'] == 'fbalpha2012'


def test_historical_non_arcade_hint_is_replaced_by_the_core_runner_really_uses(setup):
    store, game, _ = setup
    game = dict(game, native_core='fceumm')
    document = store.generate(game)
    assert document['generated']['asset_identity']['native_core'] == 'fceumm'
    assert document['generated']['defaults']['native_core'] == 'nestopia'
    resolved = store.resolve(game, 'stream')
    assert resolved['native_core'] == 'nestopia'
    assert resolved['_game_config']['core'] == 'nestopia'
    assert resolved['_game_config']['status'] == 'configured'


def test_standalone_engine_summary_does_not_claim_unused_libretro_core(setup):
    store, game, _ = setup
    game = dict(game, platform='dos', mode='stream', native_core='dosbox_pure', browser_core='dosbox_pure')
    store.generate(game)
    summary = store.summary(game)
    assert summary['engine'] == 'DOSBox' and summary['core'] is None


def test_browser_mode_does_not_bypass_size_eligibility(setup):
    store, game, _ = setup
    game = dict(game, browser_eligible=False, unpacked_size=30 * 1024**2, mode='stream')
    store.generate(game)
    assert store.resolve(game)['mode'] == 'stream'
    store.update_overrides(game, {'mode': 'browser'})
    with pytest.raises(GameConfigError) as failure: store.resolve(game)
    assert failure.value.code == 'browser_not_eligible'


def test_disabled_game_cannot_launch_and_all_ids_paths_stay_original(setup):
    store, game, source = setup
    store.generate(game); store.update_overrides(game, {'disabled': True})
    with pytest.raises(GameConfigError) as failure: store.resolve(game)
    assert failure.value.code == 'manually_disabled'
    store.update_overrides(game, {'disabled': False})
    assert store.resolve(game)['rom_path'] == str(source)


@pytest.mark.parametrize('status', ['stale', 'runtime_unavailable', 'load_failed'])
def test_old_audit_environment_or_stale_fingerprint_does_not_block_launch(setup, status):
    store, game, _ = setup
    review = {'verification': {'status': status, 'mode': 'browser', 'engine': 'EmulatorJS', 'core': 'fceumm', 'runtime_build': 'old-build'}}
    store.generate(game, review)
    assert store.summary(game, review)['status'] == 'needs_review'
    assert store.resolve(game, review=review)['mode'] == 'browser'


def test_native_failure_cannot_block_browser_and_same_build_failure_is_checked(setup):
    store, game, _ = setup
    store.runtime_build = 'same-build'
    review = {'verification': {'status': 'load_failed', 'mode': 'stream', 'engine': 'RetroArch', 'core': 'nestopia', 'runtime_build': 'same-build'}}
    store.generate(game)
    assert store.resolve(game, 'browser', review)['mode'] == 'browser'
    with pytest.raises(GameConfigError) as failure: store.resolve(game, 'stream', review)
    assert failure.value.code == 'review_load_failed'
    review['verification']['runtime_build'] = 'different-build'
    assert store.resolve(game, 'stream', review)['mode'] == 'stream'


def test_dependency_failure_requires_matching_mode_engine_core(setup):
    store, game, _ = setup
    review = {'verification': {'status': 'dependency_error', 'mode': 'stream', 'engine': 'RetroArch', 'core': 'nestopia'}}
    store.generate(game)
    assert store.resolve(game, 'browser', review)['mode'] == 'browser'
    with pytest.raises(GameConfigError) as failure: store.resolve(game, 'stream', review)
    assert failure.value.code == 'review_dependency_error'
    review['verification']['core'] = 'another-core'
    assert store.resolve(game, 'stream', review)['mode'] == 'stream'


def test_corrupt_existing_configuration_never_silently_resets(setup):
    store, game, _ = setup
    store.generate(game)
    path = store.root / (game['id'] + '.json'); path.write_text('corrupt data')
    with pytest.raises(GameConfigError) as failure: store.generate(game)
    assert failure.value.code == 'corrupt_config' and path.read_text() == 'corrupt data'


def test_generated_unknown_fields_and_input_mapping_fail_closed(setup):
    store, game, _ = setup
    document = store.generate(game); document['generated']['defaults']['command'] = 'no'
    path = store.root / (game['id'] + '.json'); path.write_text(json.dumps(document))
    with pytest.raises(GameConfigError) as failure: store.resolve(game)
    assert failure.value.code == 'corrupt_config'


def test_config_path_symlink_and_traversal_rejected(setup, tmp_path):
    store, game, _ = setup
    outside = tmp_path / 'outside.json'; outside.write_text('{}')
    (store.root / (game['id'] + '.json')).symlink_to(outside)
    with pytest.raises(GameConfigError) as failure: store.generate(game)
    assert failure.value.code == 'unsafe_config_path' and outside.read_text() == '{}'
    with pytest.raises(GameConfigError): store.generate(dict(game, id='../../escape'))
    with pytest.raises(ValueError): GameConfigStore(store.rom_root / 'settings', store.rom_root)


def test_shared_workers_merge_patches_without_lost_update_and_scans_preserve_them(setup):
    store, game, _ = setup
    other = GameConfigStore(store.storage_root, store.rom_root)
    store.generate(game)
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(store.update_overrides, game, {'mode': 'stream'}),
                 pool.submit(other.update_overrides, game, {'notes': 'manual notes'}),
                 pool.submit(other.generate, game)]
        for task in tasks: task.result()
    assert store.get(game)['overrides'] == {'mode': 'stream', 'notes': 'manual notes'}


def test_lazy_generation_and_unknown_pc_engine_status(setup):
    store, game, _ = setup
    assert store.summary(game)['status'] == 'needs_review'
    assert store.resolve(game)['mode'] == 'browser'
    pc = dict(game, id='b' * 24, platform='windows', native_core=None, browser_core=None, browser_eligible=False, mode='stream')
    store.generate(pc)
    assert store.summary(pc)['status'] == 'blocked'
    with pytest.raises(GameConfigError) as failure: store.resolve(pc)
    assert failure.value.code == 'engine_not_configured'


def test_explicit_experiment_relaxes_only_size_rule_without_rewriting_identity(setup):
    store, game, source = setup
    with source.open('wb') as output:
        output.truncate(32 * 1024**2)
    game = dict(game, browser_eligible=False, unpacked_size=32 * 1024**2, mode='stream')
    store.generate(game)
    path = store.root / (game['id'] + '.json')
    before = path.read_bytes()
    resolved = store.resolve(game, 'browser', browser_limit=64 * 1024**2)
    assert resolved['mode'] == 'browser' and resolved['id'] == game['id']
    assert not resolved['browser_eligible']
    assert path.read_bytes() == before and store.resolve(game)['mode'] == 'stream'
    with pytest.raises(GameConfigError) as error:
        store.resolve(game, 'browser')
    assert error.value.code == 'browser_not_eligible'


@pytest.mark.parametrize('mode,limit', [(None, 64 * 1024**2), ('auto', 64 * 1024**2),
                                       ('stream', 64 * 1024**2), ('browser', True),
                                       ('browser', -1), ('browser', '0')])
def test_experiment_limit_is_only_valid_on_explicit_browser_requests(setup, mode, limit):
    store, game, _ = setup
    with pytest.raises(GameConfigError) as error:
        store.resolve(game, mode, browser_limit=limit)
    assert error.value.code == 'invalid_browser_limit'


@pytest.mark.parametrize('problem,code', [('disabled', 'manually_disabled'), ('stale', 'stale_config'),
                                        ('review', 'review_dependency_error'), ('multi', 'browser_not_eligible'),
                                        ('unpacked', 'browser_not_eligible')])
def test_experiments_preserve_config_dependency_and_resource_guards(setup, problem, code):
    store, game, source = setup
    review = None
    if problem == 'multi':
        second = source.with_suffix('.dat')
        second.write_bytes(b'dependency')
        game = dict(game, files=[str(source), str(second)])
    if problem == 'unpacked':
        game = dict(game, unpacked_size=1024**3)
    store.generate(game)
    if problem == 'disabled':
        store.update_overrides(game, {'disabled': True})
    if problem == 'stale':
        source.write_bytes(b'new contents')
    if problem == 'review':
        review = {'verification': {'status': 'dependency_error', 'mode': 'browser', 'engine': 'EmulatorJS', 'core': 'fceumm'}}
    with pytest.raises(GameConfigError) as error:
        store.resolve(game, 'browser', review, browser_limit=64 * 1024**2)
    assert error.value.code == code
