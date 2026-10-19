import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from retroone.runner import Runner


class NetworkRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config' / 'retroone'
        self.config.mkdir(parents=True)
        self.workspace = self.config / 'work' / 'ridge'
        self.workspace.mkdir(parents=True)
        self.runner = Runner(self.config, self.root / 'roms')
        self.game = {'id': 'f9eb2a58978f3b367e18a092', 'platform': 'psp', 'network': {
            'room_id': str(uuid4()), 'host_profile_id': 'player1', 'role': 'host', 'nickname': '家人',
        }}
        self.profile = patch.dict(os.environ, {'RETROONE_PROFILE_ID': 'player1'})
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def test_network_resolves_only_fixed_docker_name_and_builds_official_ip_digits(self):
        with patch('retroone.runner.socket.gethostbyname', return_value='172.20.0.2') as resolver:
            self.game['_network'] = self.runner._validated_network(self.game)
        resolver.assert_called_once_with('retroone')
        options = self.runner._ppsspp_options(self.game, self.workspace).read_text()
        self.assertIn('ppsspp_enable_wlan = "enabled"', options)
        self.assertIn('ppsspp_enable_builtin_pro_ad_hoc_server = "enabled"', options)
        values = dict(line.split(' = ', 1) for line in options.splitlines())
        digits = ''.join(values[f'ppsspp_pro_ad_hoc_server_address{i:02}'].strip('"') for i in range(1, 13))
        self.assertEqual(digits, '172020000002')
        self.assertEqual(values['ppsspp_port_offset'], '"10000"')

    def test_solo_disables_previous_network_without_overwriting_legacy_config(self):
        legacy = self.config.parent / '.config/retroarch/config/PPSSPP/PPSSPP.opt'
        legacy.parent.mkdir(parents=True)
        original = 'ppsspp_enable_wlan = "enabled"\nppsspp_enable_builtin_pro_ad_hoc_server = "enabled"\nppsspp_internal_resolution = "960x544"\n'
        legacy.write_text(original)
        options = self.runner._ppsspp_options({'platform': 'psp'}, self.workspace).read_text()
        self.assertIn('ppsspp_enable_wlan = "disabled"', options)
        self.assertIn('ppsspp_enable_builtin_pro_ad_hoc_server = "disabled"', options)
        self.assertIn('ppsspp_internal_resolution = "960x544"', options)
        self.assertEqual(legacy.read_text(), original)
        self.assertEqual(self.runner._psp_identity(), Runner(self.config, self.root / 'roms')._psp_identity())

    def test_guest_identity_is_distinct_and_does_not_start_builtin_server(self):
        first_mac = self.runner._psp_identity()
        second = Runner(self.root / 'second' / 'retroone', self.root / 'roms')
        second_workspace = second.config_root / 'work'
        second_workspace.mkdir(parents=True)
        self.game['network']['role'] = 'guest'
        with patch.dict(os.environ, {'RETROONE_PROFILE_ID': 'player3'}), patch('retroone.runner.socket.gethostbyname', return_value='172.20.0.2'):
            self.game['_network'] = second._validated_network(self.game)
            options = second._ppsspp_options(self.game, second_workspace).read_text()
            second_mac = second._psp_identity()
        self.assertNotEqual(first_mac, second_mac)
        self.assertIn('ppsspp_enable_builtin_pro_ad_hoc_server = "disabled"', options)

    def test_unverified_games_hostnames_and_roles_are_rejected(self):
        for changes in ({'id': 'another-game'}, {'platform': 'nes'}, {'network': dict(self.game['network'], host_ip='8.8.8.8')}, {'network': dict(self.game['network'], host_profile_id='attacker.example')}, {'network': dict(self.game['network'], role='guest')}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.runner._validated_network(dict(self.game, **changes))
        with patch('retroone.runner.socket.gethostbyname', return_value='8.8.8.8'), self.assertRaises(RuntimeError):
            self.runner._validated_network(self.game)

    def test_network_cannot_replace_an_active_game(self):
        with patch.object(self.runner, 'status', return_value={'game_id': 'existing'}), patch.object(self.runner, '_validated_network') as validator:
            with self.assertRaisesRegex(RuntimeError, '已有游戏'):
                self.runner.start(self.game)
        validator.assert_not_called()


if __name__ == '__main__':
    unittest.main()
