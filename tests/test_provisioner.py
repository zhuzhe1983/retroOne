"""Creation/recovery policy tests: no emulated game or GPU result is mocked."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from container.provisioner import Provisioner, validate_request
from retroone.profiles import ProfileRegistry


class FakeDocker:
    def __init__(self):
        self.containers = {}
        self.calls = []

    def call(self, method, path, body=None, missing_ok=False):
        self.calls.append((method, path, body))
        if path.startswith('/containers/create?name='):
            name = path.split('=', 1)[1]
            self.containers[name] = {
                'Image': body['Image'], 'Config': {'Labels': body['Labels']},
                'State': {'Running': False},
                'Mounts': [{'Source': part.split(':')[0], 'Destination': part.split(':')[1], 'RW': part.split(':')[2] != 'ro'} for part in body['HostConfig']['Binds']],
                'NetworkSettings': {'Networks': body['NetworkingConfig']['EndpointsConfig']},
            }
            return {'Id': name}
        name = path.split('/')[2]
        if path.endswith('/json'):
            return self.containers.get(name)
        if path.endswith('/start'):
            self.containers[name]['State']['Running'] = True
            return None
        raise AssertionError((method, path, body))


class ProvisionerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'main'
        self.control = self.config / 'profile-control'
        self.profiles = self.root / 'profiles'
        self.profiles.mkdir()
        self.env = patch.dict(os.environ, {
            'RETROONE_CONTROL_ROOT': str(self.control),
            'RETROONE_REGISTRY_PATH': str(self.config / 'profiles.json'),
            'RETROONE_PROFILES_ROOT': str(self.profiles),
            'RETROONE_HOST_PROFILES_ROOT': '/fixed/profiles',
            'RETROONE_HOST_ROM_ROOT': '/fixed/roms',
            'RETROONE_HOST_ARTWORK_ROOT': '/fixed/artwork',
            'RETROONE_HOST_LIBRARY_DATA_ROOT': '/fixed/library-data',
            'RETROONE_DOCKER_NETWORK': 'family-network',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.docker = FakeDocker()
        self.worker = Provisioner(self.docker)
        self.worker.image_id = 'sha256:approved'
        self.registry = ProfileRegistry(self.config)

    def request(self):
        self.registry.create('家人', 'cat', str(uuid4()))
        path = next((self.control / 'requests').glob('*.json'))
        return path, json.loads(path.read_text())

    def test_api_outbox_matches_worker_and_result_becomes_ready(self):
        path, request = self.request()
        claimed = self.control / 'running' / path.name
        path.rename(claimed)
        with patch.object(self.worker, 'wait_ready'):
            self.worker.process(claimed)
        result = json.loads((self.control / 'results' / path.name).read_text())
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(claimed.exists())
        record = next(row for row in self.registry.snapshot()['profiles'] if row['id'] == request['profile_id'])
        self.assertEqual(record['status'], 'ready')
        self.assertEqual(record['name'], '家人')

    def test_recovery_starts_existing_owned_runtime_without_recreate(self):
        self.worker.ensure_runtime('player3')
        self.docker.containers['retroone-player3']['State']['Running'] = False
        saved = self.profiles / 'player3' / 'save.bin'
        saved.write_bytes(b'user-save')
        self.worker.ensure_runtime('player3')
        self.worker.ensure_runtime('player3')
        self.assertEqual(sum('/create?' in row[1] for row in self.docker.calls), 1)
        self.assertEqual(sum(row[1].endswith('/start') for row in self.docker.calls), 2)
        self.assertEqual(saved.read_bytes(), b'user-save')
        spec = next(row[2] for row in self.docker.calls if '/create?' in row[1])
        self.assertEqual(spec['HostConfig']['Binds'], ['/fixed/roms:/roms:ro', '/fixed/profiles/player3:/config:rw', '/fixed/artwork:/artwork:rw', '/fixed/library-data:/library-data:rw'])
        self.assertNotIn('PortBindings', spec['HostConfig'])
        self.assertFalse(any('docker.sock' in mount for mount in spec['HostConfig']['Binds']))

    def test_unowned_container_and_symlink_never_started(self):
        self.docker.containers['retroone-player3'] = {'Config': {'Labels': {}}, 'State': {'Running': False}}
        with self.assertRaises(ValueError):
            self.worker.ensure_runtime('player3')
        self.assertFalse(any(row[0] == 'POST' for row in self.docker.calls))
        (self.profiles / 'player4').symlink_to(self.root / 'outside')
        with self.assertRaises(ValueError):
            self.worker.ensure_runtime('player4')

    def test_request_cannot_choose_commands_paths_images_or_existing_players(self):
        path, request = self.request()
        validate_request(request, path.name)
        for changes in ({'image': 'attacker/image'}, {'command': ['sh']}, {'profile_id': '../other'}, {'profile_id': 'player1'}, {'profile_id': 'player03'}, {'version': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_request(dict(request, **changes), path.name)
        with self.assertRaises(ValueError):
            self.worker.authorized('player4', request['job_id'])

    def test_failed_health_retains_config_for_idempotent_retry(self):
        path, request = self.request()
        with patch.object(self.worker, 'wait_ready', side_effect=RuntimeError('gateway failed')):
            self.worker.process(path)
        result = json.loads((self.control / 'results' / (request['job_id'] + '.json')).read_text())
        self.assertEqual(result['status'], 'error')
        self.assertTrue((self.profiles / request['profile_id']).is_dir())
        self.assertEqual(len(self.docker.containers), 1)
        self.assertFalse(any(row[0] == 'DELETE' for row in self.docker.calls))


if __name__ == '__main__':
    unittest.main()
