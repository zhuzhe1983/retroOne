import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

SCRIPT = Path(__file__).resolve().parents[1] / 'build_fbneo_index.py'
spec = importlib.util.spec_from_file_location('fbneo_index', SCRIPT)
indexer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(indexer)


class FbneoIndexTest(unittest.TestCase):
    def tree(self):
        root = ET.Element('datafile')
        header = ET.SubElement(root, 'header')
        ET.SubElement(header, 'version').text = 'original-fixture'
        bios = [(f'bios{i}.bin', f'{i + 1:08x}', 64 + i) for i in range(6)]
        self.add(root, 'neogeo', bios, isbios='yes')
        parent = [('base.bin', '12345678', 1024), ('gfx.bin', '23456789', 2048)]
        clone = [('clone.bin', '34567890', 1024), parent[1]]
        self.add(root, 'parent', parent)
        self.add(root, 'clone', clone, cloneof='parent', romof='parent')
        self.add(root, 'neo', parent + bios, romof='neogeo')
        self.add(root, 'neoclone', clone + bios, romof='neo', cloneof='neo')
        return root, bios, parent, clone

    @staticmethod
    def add(root, name, members, **attrs):
        game = ET.SubElement(root, 'game', name=name, **attrs)
        for n, crc, size in members:
            ET.SubElement(game, 'rom', name=n, crc=crc, size=str(size))
        return game

    def test_complete_clone_and_neo_game_signatures_keep_inherited_game_bytes(self):
        root, bios, parent, clone = self.tree()
        result = indexer.derive(root, set(bios[:4]))
        self.assertEqual(['clone', 'neoclone'], result['signatures'][indexer.signature(clone)])
        self.assertEqual('', result['catalog']['clone'][1])
        self.assertEqual('neogeo', result['catalog']['neoclone'][1])
        self.assertEqual('neo', result['catalog']['neoclone'][0])
        members = [result['members'][i] for i in result['catalog']['neoclone'][3]]
        self.assertEqual(clone, members)
        self.assertEqual(4, sum(row[3] for row in result['neogeo_bios']))
        self.assertNotIn(indexer.signature([clone[0]]), result['signatures'])
        self.assertEqual(1, result['catalog']['neogeo'][2])

    def test_neogeo_extra_game_file_is_not_removed_as_generic_bios(self):
        root, bios, parent, _ = self.tree()
        special = ('special.key', '87654321', 4096)
        self.add(root, 'specialneo', parent + bios + [special], romof='neogeo')
        result = indexer.derive(root, set(bios[:4]))
        self.assertIn('specialneo', result['signatures'][indexer.signature(parent + [special])])
        self.assertNotIn('specialneo', result['signatures'][indexer.signature(parent)])

    def test_missing_cycle_and_unknown_dependency_are_not_inferred(self):
        for mode in ('missing', 'cycle'):
            root, bios, _, _ = self.tree()
            node = root.find("game[@name='parent']")
            node.set('romof', 'missing' if mode == 'missing' else 'clone')
            with self.assertRaisesRegex(ValueError, 'cyclic'):
                indexer.derive(root, set(bios[:4]))

    def test_duplicate_names_and_unknown_crc_sets_have_no_admission_signature(self):
        root, bios, parent, _ = self.tree()
        bad = self.add(root, 'bad', [parent[0], parent[0]], cloneof='parent', romof='parent')
        unknown = self.add(root, 'unknown', [('dump.bin', '', 30)])
        result = indexer.derive(root, set(bios[:4]))
        self.assertEqual(2, result['catalog']['bad'][2])
        self.assertEqual(2, result['catalog']['unknown'][2])
        all_names = {n for names in result['signatures'].values() for n in names}
        self.assertNotIn('bad', all_names)
        self.assertNotIn('unknown', all_names)

    def test_pinned_inputs_fail_before_derivation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.dat'
            path.write_text('<datafile/>')
            with self.assertRaisesRegex(ValueError, 'Unreviewed DAT'):
                indexer.build(path)
        with self.assertRaisesRegex(ValueError, 'Unreviewed NeoGeo'):
            indexer.source_bios_flags(b'original fixture, not upstream source')

    def test_signature_preserves_multiplicity_and_ignores_names(self):
        first = [('one', '01234567', 32), ('two', '12345678', 64)]
        renamed = [('new', '12345678', 64), ('another', '01234567', 32)]
        self.assertEqual(indexer.signature(first), indexer.signature(renamed))
        self.assertNotEqual(indexer.signature(first), indexer.signature(first + [first[0]]))


if __name__ == '__main__':
    unittest.main()
