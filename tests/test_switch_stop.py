"""Regression coverage for Eden's actual sequential-modal shutdown behavior."""
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from retroone.runner import Runner


class FakeEdenProcess:
    pid = 1234

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("eden", timeout)
        return self.returncode


class SwitchStopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.runner = Runner(root / "config", root / "roms")
        self.process = FakeEdenProcess()
        self.runner._process = self.process
        self.runner._session = {
            "session_id": "own-switch-session", "game_id": "own-switch-game",
            "title": "Eden smoke", "platform": "switch", "log": "own.log",
        }
        self.runner._started = 0

    def test_sequential_modals_close_without_forced_signal(self):
        # Real smoke: dismissing the missing-keys dialog reveals a second
        # Vulkan dialog. A main-window close while a modal exists does nothing.
        modal = ["0x000002"]
        closed = []
        scans = []

        def wmctrl(argv, **kwargs):
            if argv[1] == "-lp":
                scans.append(True)
                lines = ["0x000001 0 1234 host Eden", "0x000099 0 9999 host Another app"]
                if modal[0]:
                    lines.append(f"{modal[0]} 0 1234 host Startup dialog")
                return subprocess.CompletedProcess(argv, 0, "\n".join(lines))
            window = argv[2]
            closed.append(window)
            if window == modal[0]:
                modal[0] = "0x000003" if window == "0x000002" else None
            elif window == "0x000001" and modal[0] is None:
                self.process.returncode = 0
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(self.runner, "_program", return_value="wmctrl"), \
             patch("retroone.runner.subprocess.run", side_effect=wmctrl), \
             patch("retroone.runner.os.getpgid", side_effect=lambda pid: pid), \
             patch("retroone.runner.os.killpg") as kill:
            result = self.runner.stop()

        self.assertTrue(result["graceful"])
        self.assertFalse(result["forced"])
        self.assertGreaterEqual(len(scans), 2)
        self.assertIn("0x000003", closed)
        self.assertNotIn("0x000099", closed)
        kill.assert_not_called()

    def test_only_owned_process_group_windows_receive_close(self):
        def wmctrl(argv, **kwargs):
            if argv[1] == "-lp":
                return subprocess.CompletedProcess(argv, 0,
                    "0x000099 0 9999 host Other\n"
                    "0x000098 0 invalid host Malformed\n"
                    "not-a-window 0 1234 host Invalid\n"
                    "0x000001 0 1235 host Eden child\n")
            self.assertEqual(argv[2], "0x000001")
            self.process.returncode = 0
            return subprocess.CompletedProcess(argv, 0)

        def group(pid):
            return 1234 if pid == 1235 else 9999

        with patch.object(self.runner, "_program", return_value="wmctrl"), \
             patch("retroone.runner.subprocess.run", side_effect=wmctrl), \
             patch("retroone.runner.os.getpgid", side_effect=group), \
             patch("retroone.runner.os.killpg") as kill:
            result = self.runner.stop()

        self.assertTrue(result["stopped"])
        self.assertTrue(result["graceful"])
        self.assertEqual(result["session_id"], "own-switch-session")
        kill.assert_not_called()

    def test_unavailable_window_manager_uses_only_owned_process_group(self):
        def terminate(pid, signum):
            self.assertEqual(pid, self.process.pid)
            self.assertEqual(signum, signal.SIGTERM)
            self.process.returncode = -signal.SIGTERM

        with patch.object(self.runner, "_program", return_value=None), \
             patch("retroone.runner.os.killpg", side_effect=terminate) as kill:
            result = self.runner.stop()

        self.assertFalse(result["graceful"])
        self.assertTrue(result["forced"])
        kill.assert_called_once_with(self.process.pid, signal.SIGTERM)

    def test_window_retry_uses_one_total_deadline(self):
        clock = [100.0]
        scans = []

        def wmctrl(argv, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 2)
            clock[0] += min(0.01, kwargs["timeout"])
            if argv[1] == "-lp":
                scans.append(True)
                return subprocess.CompletedProcess(argv, 0, "0x000001 0 1234 host Eden")
            return subprocess.CompletedProcess(argv, 0)

        def wait(timeout=None):
            clock[0] += timeout
            raise subprocess.TimeoutExpired("eden", timeout)

        with patch.object(self.runner, "_program", return_value="wmctrl"), \
             patch("retroone.runner.subprocess.run", side_effect=wmctrl), \
             patch("retroone.runner.os.getpgid", return_value=1234), \
             patch("retroone.runner.time.monotonic", side_effect=lambda: clock[0]), \
             patch.object(self.process, "wait", side_effect=wait):
            result = self.runner._close_switch_window()

        self.assertFalse(result)
        self.assertGreater(len(scans), 1)
        self.assertLessEqual(clock[0] - 100, 10.02)

    def test_nonzero_normal_exit_is_not_reported_as_a_forced_signal(self):
        def close():
            self.process.returncode = 1
            return False

        with patch.object(self.runner, "_close_switch_window", side_effect=close), \
             patch("retroone.runner.os.killpg") as kill:
            result = self.runner.stop()

        self.assertFalse(result["graceful"])
        self.assertFalse(result["forced"])
        self.assertEqual(self.runner.last_exit["exit_code"], 1)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
