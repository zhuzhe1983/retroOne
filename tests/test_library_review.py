"""Synthetic files only: proof-driven grouping must preserve original save IDs."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from retroone.catalog import Catalog
from retroone.library_management import LibraryManager
from retroone.library_review import ReviewIndex, filename_tags, publish_report


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.roms = self.root / 'roms'
        self.directory = self.roms / 'library' / 'nes' / 'manual' / 'Synthetic'
        self.directory.mkdir(parents=True)
        for name, data in [('Sample (USA).nes', b'synthetic payload'), ('Copy.nes', b'synthetic payload'), ('Sample (Hack).nes', b'different synthetic payload')]:
            (self.directory / name).write_bytes(data)
        self.catalog = Catalog(self.roms, self.root / 'config')
        self.catalog.scan()
        self.games = {g['title']: g for g in self.catalog.list()['games']}

    def tearDown(self):
        self.tmp.cleanup()

    def index(self):
        entries = {}
        for title in ['Sample (USA)', 'Copy']:
            game = self.games[title]
            source = Path(game['rom_path'])
            info = source.stat()
            entries[game['id']] = dict(platform='nes', native_core='fceumm', browser_core='nes', engine_hint=None,
                sources=[dict(relative_path=source.relative_to(self.roms).as_posix(), size=info.st_size,
                              mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns, ino=info.st_ino)],
                fingerprint=dict(sha256=hashlib.sha256(source.read_bytes()).hexdigest(), scope='single_cartridge_payload', complete=True),
                verification=dict(status='fingerprinted', checked_at='test'))
        return dict(schema_version=1, games=entries, groups=[dict(id='a'*24, scope='single_cartridge_payload', auto_merge_safe=True, game_ids=list(entries))])

    def publish(self, data):
        path = self.catalog._reviews.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_safe_group_does_not_change_ids_titles_favorites_or_history(self):
        original = self.games['Copy']
        self.catalog.mark_played(original['id'])
        self.catalog.set_favorite(original['id'], True)
        self.publish(self.index())
        result = self.catalog.list()
        self.assertEqual(result['total'], 2)
        representative = next(g for g in result['games'] if g['id'] == original['id'])
        self.assertEqual(representative['title'], 'Copy')
        self.assertTrue(representative['favorite'])
        self.assertEqual(representative['review']['duplicates']['total_count'], 2)
        self.assertEqual(self.catalog.count(), 2)
        self.assertEqual(self.catalog.platforms()[0]['count'], 2)
        other = self.games['Sample (USA)']
        self.assertEqual(self.catalog.get(other['id'])['title'], other['title'])
        self.assertEqual(self.catalog.list(q='USA')['games'][0]['id'], other['id'])
        self.assertEqual(self.catalog.list(favorite=True)['games'][0]['id'], original['id'])
        self.assertEqual(self.catalog.list(recent=True)['games'][0]['id'], original['id'])

    def test_modified_hidden_alias_reappears_and_fingerprint_expires(self):
        self.publish(self.index())
        self.assertEqual(self.catalog.list()['total'], 2)
        game = self.games['Copy']
        Path(game['rom_path']).write_bytes(b'different version')
        self.assertEqual(self.catalog.list()['total'], 3)
        view = self.catalog.get(game['id'])['review']
        self.assertEqual(view['verification']['status'], 'stale')
        self.assertNotIn('fingerprint', view)

    def test_partial_proof_or_different_context_never_collapses(self):
        data = self.index()
        first = next(iter(data['games']))
        data['games'][first]['browser_core'] = 'different'
        self.publish(data)
        self.assertEqual(self.catalog.list()['total'], 3)
        data = self.index()
        data['games'][first]['fingerprint']['complete'] = False
        self.publish(data)
        self.assertEqual(self.catalog.list()['total'], 3)

    def test_new_patch_unhides_alias_even_after_catalog_rescan(self):
        self.publish(self.index())
        self.assertEqual(self.catalog.list()['total'], 2)
        (self.directory / 'Copy.ips').write_bytes(b'synthetic patch')
        self.assertEqual(self.catalog.list()['total'], 3)
        self.catalog.scan()
        self.assertEqual(self.catalog.list()['total'], 3)
        self.assertEqual(self.catalog.get(self.games['Copy']['id'])['review']['verification']['status'], 'stale')

    def test_filters_and_missing_index_are_conservative(self):
        data = self.index()
        self.publish(data)
        self.assertEqual(self.catalog.list(review_filter='observed')['total'], 0)
        self.assertEqual(self.catalog.list(review_filter='pending')['total'], 2)
        self.catalog._reviews.path.unlink()
        self.assertEqual(self.catalog.list()['total'], 3)
        self.assertEqual(self.catalog.get(self.games['Copy']['id'])['review']['verification']['status'], 'unverified')

    def test_real_readonly_job_exports_separate_metadata_and_preserves_sources(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()}
        manager = LibraryManager(self.catalog)
        job = manager.start()
        self.assertEqual(job['status'], 'running')
        manager.thread.join(timeout=15)
        self.assertFalse(manager.thread.is_alive())
        self.assertEqual(manager.job()['status'], 'complete')
        self.assertEqual(manager.summary()['display_entries'], 2)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.directory.iterdir()})
        self.assertFalse((self.roms / '.retroone').exists())
        self.assertTrue((self.catalog.library_data_root / 'metadata' / 'library-review.json').exists())
        self.assertFalse(manager.view()['capabilities']['observe'])


class SwitchReviewCase(unittest.TestCase):
    def test_full_nca_proof_is_restricted_and_invalidates_after_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = {}
            for index in range(2):
                path = root / (str(index) + '.nsp')
                path.write_bytes(b'synthetic package')
                info = path.stat()
                records[str(index) * 24] = dict(platform='switch', native_core=None, browser_core=None,
                    sources=[dict(relative_path=path.name, **{k: getattr(info, 'st_' + k) for k in ('size', 'mtime_ns', 'ctime_ns', 'ino')})],
                    fingerprint=dict(sha256='a' * 64, scope='switch_nca_set', complete=True, algorithm='sha256_full_nca_set_v1'),
                    variant_tags=[dict(kind='revision', label='本体 1.0.0', source='package_manifest')],
                    verification=dict(status='fingerprinted'))
            data = dict(schema_version=1, games=records, known_sources=['0.nsp', '1.nsp'], groups=[dict(
                id='b' * 24, scope='switch_nca_set', auto_merge_safe=True, game_ids=list(records))])
            index_path = root / 'index.json'
            index_path.write_text(json.dumps(data))
            review = ReviewIndex(root, index_path)
            self.assertTrue(all(row[1] == 'b' * 24 for row in review.rows()))
            first = records['0' * 24]
            self.assertEqual(review.public(dict(rom_path='0.nsp'), first)['variant_tags'][0]['label'], '本体 1.0.0')
            first['fingerprint'].pop('algorithm')
            index_path.write_text(json.dumps(data))
            self.assertTrue(all(row[1] is None for row in review.rows()))
            first['fingerprint']['algorithm'] = 'sha256_full_nca_set_v1'
            first['platform'] = 'nes'
            records['1' * 24]['platform'] = 'nes'
            index_path.write_text(json.dumps(data))
            self.assertTrue(all(row[1] is None for row in review.rows()))
            first['platform'] = records['1' * 24]['platform'] = 'switch'
            index_path.write_text(json.dumps(data))
            self.assertTrue(all(row[1] for row in review.rows()))
            (root / '1.nsp').write_bytes(b'changed package')
            self.assertEqual(review.changed_groups(), {'b' * 24})

    def test_mod_labels_are_filename_evidence_not_verified_mod_claims(self):
        tags = filename_tags('Example (USA) [MOD widescreen].nsp')
        self.assertEqual([t['kind'] for t in tags], ['region', 'variant'])
        self.assertTrue(all(t['source'] == 'filename' for t in tags))

    def test_general_scan_preserves_switch_proof_but_not_changed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'review.json'
            source = dict(relative_path='example.nsp', size=2048, mtime_ns=1, ctime_ns=2, ino=3)
            entry = dict(platform='switch', native_core=None, browser_core=None, engine_hint=None,
                         sources=[source], fingerprint=dict(scope='switch_nca_set', sha256='a' * 64, complete=True))
            old = dict(schema_version=1, games={'a' * 24: entry, 'b' * 24: entry}, groups=[dict(
                id='c' * 24, scope='switch_nca_set', auto_merge_safe=True, game_ids=['a' * 24, 'b' * 24])])
            game = dict(id='a' * 24, platform='switch', relative_rom_path='example.nsp', source_manifest=[source])
            report = dict(schema_version=1, read_only=True, games=[game])
            path.write_text(json.dumps(old))
            result = publish_report(report, path)
            self.assertEqual(len(result['groups']), 1)
            self.assertEqual(result['games']['a' * 24], entry)
            game['source_manifest'] = [dict(source, mtime_ns=4)]
            result = publish_report(report, path)
            self.assertEqual(result['groups'], [])
            self.assertNotIn('a' * 24, result['games'])


if __name__ == '__main__':
    unittest.main()
