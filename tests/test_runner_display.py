"""Policy/failure checks; real encoded dimensions are validated on NAS separately."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from retroone.runner import Runner


class DisplayRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = Runner(Path(self.temp.name), Path(self.temp.name) / 'roms')
        self.env = patch.dict(os.environ, {
            'DISPLAY': ':1', 'RETROONE_GPU_BACKEND': 'intel', 'SELKIES_MANUAL_WIDTH': '0', 'SELKIES_MANUAL_HEIGHT': '0',
            'SELKIES_MANUAL_RESOLUTION': 'false|locked', 'SELKIES_ENABLE_RESIZE': 'false|locked',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.program = patch.object(self.runner, '_program', side_effect=lambda name: '/usr/bin/' + name)
        self.program.start()
        self.addCleanup(self.program.stop)
        self.ready = patch('retroone.display_runtime.urlopen')
        self.ready.start().return_value.__enter__.return_value.status = 200
        self.addCleanup(self.ready.stop)

    def test_old_unlocked_runtime_does_not_advertise_resolution_control(self):
        self.assertTrue(self.runner.display_capabilities()['available'])
        with patch.dict(os.environ, {'SELKIES_MANUAL_RESOLUTION': 'false'}):
            self.assertFalse(self.runner.display_capabilities()['available'])

    def test_nvidia_and_unknown_backend_never_fall_back_to_intel(self):
        for name in ('nvidia', 'unknown'):
            with self.subTest(backend=name), patch.dict(os.environ, {'RETROONE_GPU_BACKEND': name}), patch('retroone.display_runtime.subprocess.run') as invoke:
                runner = Runner(Path(self.temp.name), Path(self.temp.name) / 'roms')
                self.assertFalse(runner.display_capabilities()['available'])
                with self.assertRaisesRegex(RuntimeError, 'NVIDIA'):
                    runner._apply_display({'stream_resolution': '1080p'})
                with self.assertRaisesRegex(RuntimeError, 'NVIDIA'):
                    runner.start({'_display': {'stream_resolution': '1080p'}})
                capabilities = runner.capabilities()
                self.assertFalse(capabilities['available'])
                self.assertFalse(capabilities['engines']['wine']['available'])
                invoke.assert_not_called()

    def test_only_matching_cli_and_randr_readback_confirm_application(self):
        results = [subprocess.CompletedProcess([], 0, '(1920, 1080)\n', ''),
                   subprocess.CompletedProcess([], 0, 'Screen 0: minimum 1 x 1, current 1920 x 1080, maximum 16384 x 16384\n', '')]
        with patch('retroone.display_runtime.subprocess.run', side_effect=results):
            self.assertEqual(self.runner._apply_display({'stream_resolution': '1080p'}),
                             {'settings': {'stream_resolution': '1080p'}, 'width': 1920, 'height': 1080})

    def test_api_ready_before_selkies_does_not_offer_unstable_geometry(self):
        with patch('retroone.display_runtime.urlopen', side_effect=OSError('connection refused')):
            self.assertFalse(self.runner.display_capabilities()['available'])

    def test_upstream_exit_zero_none_and_readback_mismatch_are_failures(self):
        for outputs in [('None\n',), ('(1920, 1080)\n', 'Screen 0: minimum 1 x 1, current 1280 x 720, maximum 16384 x 16384')]:
            with self.subTest(outputs=outputs), patch('retroone.display_runtime.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0, output, '') for output in outputs]), self.assertRaises(RuntimeError):
                self.runner._apply_display({'stream_resolution': '1080p'})

    def test_active_session_cannot_apply_another_snapshot(self):
        with patch.object(self.runner, 'status', return_value={'game_id': 'existing'}), patch.object(self.runner, '_apply_display') as resize:
            with self.assertRaisesRegex(RuntimeError, '已有游戏'):
                self.runner.start({'_display': {'stream_resolution': '1080p'}})
        resize.assert_not_called()

    def test_untrusted_dimensions_and_unsupported_resolution_are_rejected(self):
        with patch('retroone.display_runtime.subprocess.run') as invoke:
            for snapshot in ({'stream_resolution': '4k'}, {'stream_resolution': '720p', 'width': 9999}):
                with self.assertRaises(ValueError):
                    self.runner._apply_display(snapshot)
        invoke.assert_not_called()
