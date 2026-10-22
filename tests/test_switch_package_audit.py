"""Synthetic containers: neither real ROMs nor cryptographic keys are needed."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('switch_package_audit', Path(__file__).parents[1] / 'scripts/audit_switch_packages.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def container(files, magic=b'PFS0'):
    width = 24 if magic == b'PFS0' else 64
    names, records, data = b'', b'', b''
    for name, payload in files:
        records += struct.pack('<QQII', len(data), len(payload), len(names), 0) + bytes(width - 24)
        names += name.encode() + b'\0'
        data += payload
    return magic + struct.pack('<III', len(files), len(names), 0) + records + names + data


class PackageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_wrapper_order_names_and_auxiliary_do_not_change_full_content_identity(self):
        first = self.write('a.nsp', container([('one.nca', b'A' * 2048), ('two.nca', b'B' * 1024), ('ticket.tik', b'packaging')]))
        second = self.write('b.nsp', container([('other.nca', b'B' * 1024), ('renamed.nca', b'A' * 2048), ('info.xml', b'other packaging')]))
        a, b = audit.manifest(first, True), audit.manifest(second, True)
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertNotIn('fingerprint', audit.manifest(first))

    def test_one_byte_change_or_added_update_never_collapses(self):
        a = self.write('a.nsp', container([('same-name.nca', b'A' * 2048)]))
        b = self.write('b.nsp', container([('same-name.nca', b'A' * 2047 + b'B')]))
        c = self.write('c.nsp', container([('same-name.nca', b'A' * 2048), ('update.nca', b'C' * 1024)]))
        self.assertEqual(len({audit.manifest(p, True)['fingerprint'] for p in (a, b, c)}), 3)

    def test_xci_secure_content_matches_nsp(self):
        content = [('one.nca', b'A' * 1024)]
        header = bytearray(0x200)
        header[0x100:0x104] = b'HEAD'
        struct.pack_into('<Q', header, 0x130, len(header))
        xci = self.write('a.xci', header + container([('secure', container(content, b'HFS0'))], b'HFS0'))
        nsp = self.write('a.nsp', container(content))
        self.assertEqual(audit.manifest(xci, True)['fingerprint'], audit.manifest(nsp, True)['fingerprint'])

    def test_truncated_overlapping_and_unsafe_entries_are_rejected(self):
        payload = container([('one.nca', b'A' * 1024), ('two.nca', b'B' * 1024)])
        overlap = bytearray(payload)
        struct.pack_into('<Q', overlap, 16 + 24, 0)
        for index, data in enumerate((payload[:-1], overlap, container([('../one.nca', b'A' * 1024)]), container([('tiny.nca', b'A')]))):
            with self.subTest(index=index), self.assertRaises(ValueError):
                audit.manifest(self.write(str(index) + '.nsp', data), True)


if __name__ == '__main__':
    unittest.main()
