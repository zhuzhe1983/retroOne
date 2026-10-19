from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from retroone.profiles import ProfileError, ProfileRegistry, atomic_json


def allocate_in_process(root, request_id):
    return ProfileRegistry(Path(root), limit=32).create('同名成员', 'cat', request_id)['id']


class ProfileRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = ProfileRegistry(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return json.loads(self.registry.path.read_text())

    def create(self):
        return self.registry.create('家人', 'cat', str(uuid4()))

    def result(self, identifier, status, **extra):
        record = self.state()['profiles'][identifier]
        value = {'version': 1, 'profile_id': identifier, 'job_id': record['job_id'], 'status': status, **extra}
        atomic_json(self.registry.control / 'results' / (record['job_id'] + '.json'), value)
        return value

    def test_legacy_names_and_saves_survive_addition(self):
        self.registry.path.write_text(json.dumps({'player1': {'id': 'player1', 'name': '爸爸', 'avatar': 'rocket', 'configured': True}}))
        saved = self.root / 'browser-saves/old.state'
        saved.parent.mkdir(); saved.write_bytes(b'original progress')
        added = self.create()
        self.assertEqual((added['id'], added['status']), ('player3', 'provisioning'))
        first = self.registry.snapshot()['profiles'][0]
        self.assertEqual((first['name'], first['avatar'], first['status']), ('爸爸', 'rocket', 'ready'))
        self.assertEqual(saved.read_bytes(), b'original progress')
        job = next((self.registry.control / 'requests').glob('*.json'))
        self.assertEqual(set(json.loads(job.read_text())), {'version', 'operation', 'profile_id', 'job_id', 'attempt', 'created_at'})
        self.assertNotIn('家人', job.read_text())

    def test_duplicate_submission_is_idempotent_across_instances(self):
        token = str(uuid4())
        with ThreadPoolExecutor(max_workers=8) as pool:
            identifiers = list(pool.map(lambda _: ProfileRegistry(self.root).create('同名', 'leaf', token)['id'], range(20)))
        self.assertEqual(set(identifiers), {'player3'})
        self.assertEqual(len(list((self.registry.control / 'requests').glob('*.json'))), 1)
        with self.assertRaises(ProfileError) as error:
            self.registry.create('改了名字', 'leaf', token)
        self.assertEqual(error.exception.status_code, 409)

    def test_process_concurrency_does_not_reuse_member_numbers(self):
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as pool:
            ids = list(pool.map(allocate_in_process, [str(self.root)] * 8, [str(uuid4()) for _ in range(8)]))
        self.assertEqual(set(ids), {'player' + str(i) for i in range(3, 11)})
        self.assertEqual(len(self.registry.snapshot()['profiles']), 10)

    def test_worker_results_control_ready_and_retry_ignores_stale_results(self):
        added = self.create(); self.assertEqual(self.registry.snapshot()['profiles'][-1]['status'], 'provisioning')
        old = self.result(added['id'], 'error', error='磁盘空间不足')
        self.assertEqual(self.registry.snapshot()['profiles'][-1]['error'], '磁盘空间不足')
        retried = self.registry.retry(added['id'])
        self.assertEqual((retried['id'], retried['status']), ('player3', 'provisioning'))
        record = self.state()['profiles']['player3']
        self.assertNotEqual(record['job_id'], old['job_id']); self.assertEqual(record['attempt'], 2)
        atomic_json(self.registry.control / 'results' / (old['job_id'] + '.json'), dict(old, status='ready'))
        self.assertEqual(self.registry.snapshot()['profiles'][-1]['status'], 'provisioning')
        self.registry.retry('player3')
        self.assertEqual(self.state()['profiles']['player3']['job_id'], record['job_id'])
        self.result('player3', 'ready')
        self.assertEqual(self.registry.snapshot()['profiles'][-1]['status'], 'ready')

    def test_interrupted_outbox_write_is_repaired(self):
        with patch.object(self.registry, 'ensure_request', side_effect=OSError('disk temporarily unavailable')):
            with self.assertRaises(ProfileError):
                self.create()
        self.assertEqual(len(list((self.registry.control / 'requests').glob('*.json'))), 0)
        self.registry.snapshot()
        self.assertEqual(len(list((self.registry.control / 'requests').glob('*.json'))), 1)

    def test_failed_registry_commit_never_reuses_reserved_id(self):
        def failing_write(path, value):
            if path == self.registry.path and 'player3' in value.get('profiles', {}):
                raise OSError('simulated interrupted commit')
            atomic_json(path, value)
        with patch('retroone.profiles.atomic_json', side_effect=failing_write):
            with self.assertRaises(ProfileError):
                self.create()
        self.assertEqual(self.create()['id'], 'player4')

    def test_corrupt_registry_or_foreign_result_never_resets_or_becomes_ready(self):
        self.registry.path.write_text('{broken')
        with self.assertRaises(ProfileError): self.create()
        self.assertEqual(self.registry.path.read_text(), '{broken')
        self.registry.path.unlink(); self.create()
        self.result('player3', 'ready', profile_id='player999')
        with self.assertRaises(ProfileError): self.registry.snapshot()
        self.assertEqual(self.state()['profiles']['player3']['status'], 'provisioning')

    def test_capacity_and_missing_registry_fail_safely(self):
        limited = ProfileRegistry(self.root, limit=3)
        limited.create('甲', 'cat', str(uuid4()))
        self.assertEqual(limited.snapshot()['capacity'], {'limit': 3, 'total': 3, 'remaining': 0})
        with self.assertRaises(ProfileError) as error: limited.create('乙', 'cat', str(uuid4()))
        self.assertEqual(error.exception.status_code, 409)
        limited.path.unlink()
        with self.assertRaises(ProfileError): limited.snapshot()


if __name__ == '__main__':
    unittest.main()
