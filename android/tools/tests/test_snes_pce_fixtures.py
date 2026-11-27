"""Structure/output safety checks. These do not substitute for emulator runs."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import create_snes_pce_fixtures as fixtures


class CartridgeFixturesTest(unittest.TestCase):
    def test_snes_header_checksum_and_reset(self):
        rom = fixtures.build_snes()
        self.assertEqual(len(rom), 32768)
        self.assertEqual(rom[0x7fd5:0x7fda], bytes([0x20, 0, 5, 0, 1]))
        inverse, checksum = struct.unpack_from('<HH', rom, 0x7fdc)
        self.assertEqual(inverse ^ checksum, 65535)
        self.assertEqual(sum(rom) & 65535, checksum)
        reset, = struct.unpack_from('<H', rom, 0x7ffc)
        self.assertEqual(reset, 0x8000)
        self.assertEqual(rom[reset - 0x8000], 0x78)  # Starts with SEI, not zero/open bus.

    def test_pce_one_bank_vectors(self):
        rom = fixtures.build_pce()
        self.assertEqual(len(rom), 8192)
        vectors = struct.unpack_from('<5H', rom, 0x1ff6)
        self.assertEqual(vectors[-1], 0xe000)
        self.assertTrue(all(0xe000 <= vector < 0xe800 for vector in vectors))
        self.assertEqual(rom[0], 0x78)
        self.assertTrue(all(rom[vector - 0xe000] == 0x40 for vector in vectors[:-1]))

    def test_build_all_identity_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            first = fixtures.build_all()
            self.assertEqual(first, fixtures.build_all())
            self.assertEqual(set(first), {'snes', 'pce'})
            fixtures.generate(directory)
            manifest = json.loads((Path(directory) / 'snes-pce-fixtures-manifest.json').read_text())
            self.assertEqual(manifest['license'], 'CC0-1.0')
            for artifact in manifest['artifacts']:
                filename, expected = first[artifact['platform']]
                actual = (Path(directory) / filename).read_bytes()
                self.assertEqual(actual, expected)
                self.assertEqual(artifact['bytes'], len(actual))
                self.assertEqual(artifact['sha256'], hashlib.sha256(actual).hexdigest())
                self.assertFalse(artifact['bios_required'])
                self.assertFalse(artifact['cd_content'])

    def test_existing_output_preflight_preserves_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / 'retroone-pce-probe.pce'
            blocked.write_bytes(b'existing user content')
            with self.assertRaises(FileExistsError):
                fixtures.generate(root)
            self.assertEqual(blocked.read_bytes(), b'existing user content')
            self.assertFalse((root / 'retroone-snes-probe.sfc').exists())

    def test_replace_is_limited_to_fixed_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            untouched = root / 'other-game.sfc'
            untouched.write_bytes(b'preserve this')
            fixtures.generate(root)
            fixtures.generate(root, replace=True)
            self.assertEqual(untouched.read_bytes(), b'preserve this')
            self.assertEqual(len(list(root.iterdir())), 4)

    def test_replace_does_not_follow_output_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'user-content.bin'
            target.write_bytes(b'preserve this')
            (root / 'retroone-pce-probe.pce').symlink_to(target)
            with self.assertRaises(FileExistsError):
                fixtures.generate(root, replace=True)
            self.assertEqual(target.read_bytes(), b'preserve this')
            self.assertFalse((root / 'retroone-snes-probe.sfc').exists())


if __name__ == '__main__':
    unittest.main()
