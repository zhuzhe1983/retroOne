"""Regression: recognized SDL2 controllers use controller enums, not joydev IDs.

The deployed RetroArch 1.20 sdl2 driver calls SDL_GameControllerGetButton/Axis
and advertises zero hats for Selkies' Xbox 360 device. SDL2 defines Start=6,
D-pad=11..14, right stick axes=2/3, and trigger axes=4/5.
"""
import json
from unittest.mock import patch

import pytest

from retroone.runner import Runner


@pytest.fixture
def settings(tmp_path):
    runner = Runner(tmp_path / 'config', tmp_path / 'roms')
    workspace = tmp_path / 'work'
    workspace.mkdir()
    with patch.object(runner, '_program', return_value='/usr/bin/retroarch'), \
            patch.object(runner, '_firmware', return_value=tmp_path / 'system'):
        command = runner._retroarch({'id': 'fixture', 'platform': 'nes'}, tmp_path / 'fixture.nes', workspace)
    assert command[:3] == ['/usr/bin/retroarch', '--verbose', '--config']
    return {key: json.loads(value) for key, value in
            (line.split(' = ', 1) for line in (workspace / 'retroarch.cfg').read_text().splitlines())}


@pytest.mark.parametrize('player', [1, 2, 3, 4])
def test_standard_controller_buttons_and_dpad_work_in_each_slot(settings, player):
    prefix = f'input_player{player}_'
    assert settings['input_joypad_driver'] == 'sdl2'
    assert settings[prefix + 'joypad_index'] == str(player - 1)
    # SDL_GameControllerButton enumeration, independently of the Linux pad map.
    expected = {'b': '0', 'a': '1', 'y': '2', 'x': '3', 'select': '4', 'start': '6',
                'l3': '7', 'r3': '8', 'l': '9', 'r': '10',
                'up': '11', 'down': '12', 'left': '13', 'right': '14'}
    for control, index in expected.items():
        assert settings[prefix + control + '_btn'] == index
    # Guide=5 must not accidentally become a gameplay shoulder button.
    assert '5' not in [settings[prefix + name + '_btn'] for name in expected]


@pytest.mark.parametrize('player', [1, 2, 3, 4])
def test_sticks_and_triggers_follow_sdl_controller_axes(settings, player):
    prefix = f'input_player{player}_'
    for stick, axis in (('l_x', 0), ('l_y', 1), ('r_x', 2), ('r_y', 3)):
        assert settings[prefix + stick + '_minus_axis'] == f'-{axis}'
        assert settings[prefix + stick + '_plus_axis'] == f'+{axis}'
    assert settings[prefix + 'l2_axis'] == '+4'
    assert settings[prefix + 'r2_axis'] == '+5'


def test_keyboard_gameplay_and_hotkey_separation_survive_gamepad_fix(settings):
    assert settings['input_player1_start'] == 'enter'
    assert settings['input_player1_up'] == 'w'
    assert settings['input_player1_a'] == 'j'
    assert settings['input_player1_b'] == 'k'
    assert settings['input_frame_advance'] == 'nul'
    assert settings['input_hold_fast_forward'] == 'nul'
