"""Format and filesystem-safety regression tests, not emulator runtime claims."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import create_handheld_sega_fixtures as fixtures


class FixtureTests(unittest.TestCase):
    def test_complete_deterministic_library(self):
        first = fixtures.build_all()
        self.assertEqual(set(first), {'gb', 'gbc', 'gba', 'megadrive', 'mastersystem', 'gamegear'})
        self.assertEqual(first, fixtures.build_all())
        for filename, content in first.values():
            self.assertTrue(filename.startswith('retroone-'))
            self.assertGreaterEqual(len(content), 32768)
            self.assertEqual(len(content) & (len(content)-1), 0)

    def test_gb_headers_omit_logo_and_have_valid_checksums(self):
        for color in (False, True):
            rom = fixtures.build_gb(color)
            self.assertEqual(rom[0x104:0x134], bytes(48))
            self.assertEqual(rom[0x147:0x14a], bytes(3))  # ROM-only, no SRAM.
            self.assertEqual(rom[0x143], 0xc0 if color else 0)
            self.assertEqual((sum(rom[0x134:0x14e])+25) & 255, 0)
            self.assertEqual(struct.unpack_from('>H', rom, 0x14e)[0],
                             (sum(rom[:0x14e])+sum(rom[0x150:])) & 65535)

    def test_gba_no_logo_checksum_entrypoint_and_size(self):
        rom = fixtures.build_gba()
        self.assertEqual(len(rom), 128*1024)
        self.assertEqual(rom[4:0xa0], bytes(156))
        self.assertEqual((sum(rom[0xa0:0xbe])+0x19) & 255, 0)
        # ARM entry B must land at the first program instruction, past header.
        branch = struct.unpack_from('<I', rom)[0]
        self.assertEqual(8 + (branch & 0xffffff)*4, 0xc0)
        self.assertEqual(rom[0xa0:0xac], b'RETROONE GBA')

    def test_sega_checksums_vectors_and_no_save_ram(self):
        md = fixtures.build_megadrive()
        self.assertEqual(struct.unpack_from('>II', md), (0xffff00, 0x200))
        self.assertEqual(md[0x1b0:0x1bc], bytes(12))
        expected = sum(struct.unpack('>'+'H'*((len(md)-0x200)//2), md[0x200:])) & 65535
        self.assertEqual(struct.unpack_from('>H', md, 0x18e)[0], expected)
        for gear in (False, True):
            rom = fixtures.build_sms(gear)
            self.assertEqual(rom[0x7ff0:0x7ff8], b'TMR SEGA')
            self.assertEqual(rom[0x7fff], 0x6c if gear else 0x4c)
            self.assertEqual(struct.unpack_from('<H', rom, 0x7ffa)[0], sum(rom[:0x7ff0]) & 65535)

    def test_generate_hashes_and_refuses_nonempty_without_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'roms'
            result = fixtures.generate(destination)
            self.assertEqual(len(list(destination.iterdir())), 7)
            self.assertEqual(json.loads((destination/fixtures.MANIFEST).read_text()), result)
            for record in result['fixtures']:
                content = (destination/record['filename']).read_bytes()
                self.assertEqual(len(content), record['bytes'])
                self.assertEqual(hashlib.sha256(content).hexdigest(), record['sha256'])
                self.assertEqual(record['device_validation'], 'not claimed by generator')
            before = {p.name:p.read_bytes() for p in destination.iterdir()}
            with self.assertRaises(ValueError): fixtures.generate(destination)
            self.assertEqual(before, {p.name:p.read_bytes() for p in destination.iterdir()})

    def test_replace_preserves_unrelated_files_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'roms'
            fixtures.generate(destination)
            untouched = destination/'user-owned.gb'; untouched.write_bytes(b'KEEP ORIGINAL')
            fixtures.generate(destination, replace=True)
            self.assertEqual(untouched.read_bytes(), b'KEEP ORIGINAL')
            output = destination/'retroone-gb-probe.gb'
            output.unlink(); output.symlink_to(untouched)
            # Preflight prevents overwrites even though --replace is set.
            before = (destination/'retroone-gbc-probe.gbc').read_bytes()
            with self.assertRaises(ValueError): fixtures.generate(destination, replace=True)
            self.assertEqual(untouched.read_bytes(), b'KEEP ORIGINAL')
            self.assertEqual((destination/'retroone-gbc-probe.gbc').read_bytes(), before)
            link = Path(folder)/'linked-roms'; link.symlink_to(destination, target_is_directory=True)
            with self.assertRaises(ValueError): fixtures.generate(link, replace=True)


if __name__ == '__main__':
    unittest.main()
