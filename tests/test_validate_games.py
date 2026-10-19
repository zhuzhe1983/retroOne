"""Synthetic fixtures only; no ROM, firmware, vendor binary or game image."""
import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

SPEC = importlib.util.spec_from_file_location("validate_games", Path(__file__).resolve().parents[1] / "scripts/validate_games.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class ValidateGamesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.roms = self.root / "roms"
        self.roms.mkdir()
        self.system = self.root / "system"
        self.system.mkdir()

    def archive(self, name, entries):
        path = self.roms / name
        path.parent.mkdir(exist_ok=True, parents=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
            for member, payload in entries.items():
                output.writestr(member, payload)
        return path

    def dat(self, body):
        path = self.root / "set.dat"
        path.write_text("<datafile>" + body + "</datafile>")
        return audit.Dat(path)

    @staticmethod
    def member(name, data, extra=""):
        return f'<rom name="{name}" size="{len(data)}" crc="{zlib.crc32(data):08x}" {extra}/>'

    def test_parent_and_bios_can_be_satisfied_by_nonmerged_child(self):
        a, b, c = b"program", b"graphics", b"firmware fixture"
        dat = self.dat('<game name="bios" isbios="yes">' + self.member("bios", c) + '</game>'
                       '<game name="parent" romof="bios">' + self.member("shared", b) + '</game>'
                       '<game name="child" cloneof="parent" romof="bios">' +
                       self.member("main", a) + self.member("shared", b, 'merge="shared"') +
                       self.member("bios", c, 'merge="bios"') + '</game>')
        path = self.archive("child.zip", {"main": a, "shared": b, "bios": c})
        result = audit.compare_dat(dat, "child", [audit.zip_inventory(path, True)])
        self.assertEqual(result["status"], "dat_members_match")
        self.assertTrue(result["all_matched_bytes_verified"])
        self.assertEqual(set(result["dependencies"]), {"parent", "bios"})
        self.assertIsNone(result["playable"])

    def test_split_parent_members_match_by_crc_even_when_names_differ(self):
        data = b"original sample"
        dat = self.dat('<game name="parent"/><game name="child" cloneof="parent">' +
                       self.member("renamed", data, 'merge="original"') + '</game>')
        parent = self.archive("parent.zip", {"original": data})
        result = audit.compare_dat(dat, "child", [audit.zip_inventory(parent, True)])
        self.assertEqual(result["matched"][0]["member"], "original")
        self.assertEqual(result["status"], "dat_members_match")

    def test_directory_crc_does_not_certify_payload_integrity(self):
        data = b"sample"
        dat = self.dat('<game name="game">' + self.member("x", data) + '</game>')
        inventory = audit.zip_inventory(self.archive("game.zip", {"x": data}), False)
        result = audit.compare_dat(dat, "game", [inventory])
        self.assertFalse(result["all_matched_bytes_verified"])
        self.assertIsNone(inventory["sha256"])

    def test_crc_header_match_does_not_hide_corrupted_payload(self):
        path = self.archive("game.zip", {"x": b"UNIQUE PAYLOAD"})
        path.write_bytes(path.read_bytes().replace(b"UNIQUE PAYLOAD", b"UNIQUE XAYLOAD", 1))
        self.assertEqual(audit.zip_inventory(path, False)["status"], "directory_only")
        with self.assertRaises(zipfile.BadZipFile):
            audit.zip_inventory(path, True)

    def test_sha1_dat_is_unknown_until_payload_is_read(self):
        import hashlib
        data = b"sample"
        dat = self.dat('<game name="game"><rom name="x" size="6" sha1="' +
                       hashlib.sha1(data).hexdigest() + '"/></game>')
        archive = self.archive("game.zip", {"x": data})
        self.assertEqual(audit.compare_dat(dat, "game", [audit.zip_inventory(archive, False)])["status"], "dat_incomplete_evidence")
        self.assertEqual(audit.compare_dat(dat, "game", [audit.zip_inventory(archive, True)])["status"], "dat_members_match")

    def test_missing_bios_alternative_is_not_labeled_unplayable(self):
        dat = self.dat('<game name="game" romof="bios">' + self.member("variant", b"x") +
                       '<rom name="undumped" size="1" status="nodump"/>' + '</game><game name="bios" isbios="yes"/>')
        result = audit.compare_dat(dat, "game", [])
        self.assertEqual(result["status"], "dat_differences")
        self.assertEqual(len(result["missing_dat_members"]), 1)
        self.assertEqual(len(result["optional_or_nodump"]), 1)
        self.assertIsNone(result["playable"])

    def test_does_not_search_unrelated_collection_for_parent(self):
        dat = self.dat('<game name="child" cloneof="parent"/><game name="parent"/>')
        child = self.archive("collection1/child.zip", {})
        other = self.archive("collection2/parent.zip", {})
        candidates = audit.candidate_archives(child, dat, self.system)
        self.assertNotIn(other, candidates)
        self.assertIn(self.system / "fbneo/parent.zip", candidates)

    def test_dat_cycles_entities_and_path_names_are_rejected(self):
        with self.assertRaises(ValueError):
            self.dat('<game name="a" cloneof="b"/><game name="b" cloneof="a"/>').dependencies("a")
        with self.assertRaises(ValueError):
            self.dat('<game name="../escape"/>')
        path = self.root / "entity.dat"
        path.write_text('<!DOCTYPE x [<!ENTITY a "x">]><datafile/>')
        with self.assertRaises(ValueError):
            audit.Dat(path)

    def test_symlink_outside_readonly_root_is_rejected(self):
        elsewhere = self.root / "outside.zip"
        elsewhere.write_bytes(b"private")
        (self.roms / "link.zip").symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            audit.contained(self.roms / "link.zip", [self.roms])

    def test_unsafe_member_and_unpacked_budget_are_rejected_without_extraction(self):
        with self.assertRaises(ValueError):
            audit.zip_inventory(self.archive("unsafe.zip", {"../x": b"x"}), True)
        with patch.object(audit, "MAX_UNPACKED", 2):
            with self.assertRaises(ValueError):
                audit.zip_inventory(self.archive("large.zip", {"x": b"123"}), True)
        self.assertFalse((self.root / "x").exists())

    def test_resume_and_source_change_invalidate_previous_result(self):
        path = self.archive("game.zip", {"x": b"sample"})
        self.dat('<game name="game">' + self.member("x", b"sample") + '</game>')
        core = self.root / "fixture-core.txt"
        core.write_text("not an executable; metadata audit never dlopens")
        manifest = {"cores": {"fbneo": {"path": str(core), "sha256": audit.sha256(core),
                     "dat": str(self.root / "set.dat"), "dat_sha256": audit.sha256(self.root / "set.dat"),
                     "version": "fixture", "dat_provenance": {"kind": "synthetic"}}},
                    "games": [{"id": "test", "native_core": "fbneo", "rom_path": str(path)}]}
        input_path = self.root / "input.json"
        input_path.write_text(json.dumps(manifest))
        args = argparse.Namespace(manifest=input_path, rom_root=self.roms, system_root=self.system,
                                  output=self.root / "results", level="metadata", limit=3,
                                  frames=1800, timeout=10, game_id=None, retry=False)
        self.assertEqual(audit.run_batch(args)["executed"], 1)
        self.assertEqual(audit.run_batch(args)["resumed_skipped"], 1)
        self.archive("game.zip", {"x": b"changed"})
        self.assertEqual(audit.run_batch(args)["executed"], 1)
        with sqlite3.connect(args.output / "progress.sqlite3") as database:
            self.assertEqual(database.execute("SELECT count(*) FROM results").fetchone()[0], 2)
        original = json.loads(input_path.read_text())
        original["cores"]["fbneo"]["sha256"] = "0" * 64
        input_path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            audit.run_batch(args)

    def test_unknown_set_and_unsupported_core_do_not_inherit_platform_claims(self):
        dat = self.dat('<game name="known"/>')
        self.assertEqual(audit.compare_dat(dat, "unknown", [])["status"], "unknown_set")
        self.assertIsNone(audit.compare_dat(dat, "unknown", [])["playable"])

    def test_child_wall_timeout_and_diagnostic_output_are_bounded(self):
        fake = self.root / "noisy_worker.py"
        fake.write_text("import sys,time\nsys.stdout.write('x'*3000000)\nsys.stdout.flush()\ntime.sleep(30)\n")
        with patch.object(audit, "__file__", str(fake)):
            result = audit.run_child({}, self.root / "attempt", timeout=0.3)
        self.assertEqual(result["worker_state"], "timeout")
        self.assertEqual(result["diagnostic_retained_bytes"], audit.CHUNK)
        self.assertEqual(result["diagnostic_total_bytes"], 3000000)
        self.assertIsNone(result["playable"])

    def test_controller_termination_cleans_only_owned_child_group(self):
        class Process:
            pid = 43210
            stdout = None
            returncode = None
            waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise SystemExit(143)
                self.returncode = -9

        import io
        process = Process()
        process.stdout = io.BytesIO(b"")
        with patch.object(audit.subprocess, "Popen", return_value=process), \
             patch.object(audit.os, "killpg") as kill:
            with self.assertRaises(SystemExit):
                audit.run_child({}, self.root / "terminated-attempt", timeout=10)
        kill.assert_called_once_with(process.pid, audit.signal.SIGKILL)
        self.assertEqual(process.waits, 2)


if __name__ == "__main__":
    unittest.main()
