#!/usr/bin/env python3
"""Start only a task-owned, empty-library container and verify the public image.

No GPU, ROM mount, Docker socket, host save directory, or production service.
The container is always removed, including failures and interruptions.
"""
import argparse
import json
import subprocess
import time
from urllib.request import urlopen
from uuid import uuid4


def check(image):
    name = 'retroone-ci-' + uuid4().hex[:12]
    try:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--shm-size', '512m', '-p', '127.0.0.1::3000', '-e', 'PUID=1000', '-e', 'PGID=1000', '-e', 'RETROONE_FETCH_ARTWORK=0', '-e', 'SELKIES_USE_CPU=true', image], check=True)
        port = subprocess.check_output(['docker', 'port', name, '3000/tcp'], text=True).strip().split(':')[-1]
        base = 'http://127.0.0.1:' + port
        health = None
        for _ in range(90):
            try:
                with urlopen(base + '/api/health', timeout=2) as response: health = json.load(response)
                break
            except (OSError, ValueError): time.sleep(2)
        if health is None: raise RuntimeError('Public image API did not start within 180 seconds')
        if health.get('status') != 'ok' or health.get('browser', {}).get('available'):
            raise RuntimeError('Public image health/browser capability is inconsistent')
        for platform in ('nes', 'snes', 'psp', 'ps2', '3ds'):
            if health['runtime']['platforms'][platform]['available']:
                raise RuntimeError('Uninstalled private runtime incorrectly available: ' + platform)
        with urlopen(base + '/api/profiles', timeout=10) as response: profiles = json.load(response)
        with urlopen(base + '/p/player1/api/games?limit=1', timeout=10) as response: games = json.load(response)
        with urlopen(base + '/', timeout=10) as response:
            if b'RetroOne' not in response.read(): raise RuntimeError('Missing public web entry')
        if games['total'] != 0: raise RuntimeError('Public image unexpectedly contains indexed games')
        probe = "from pathlib import Path; import shutil; assert all(not any(p.is_file() for p in Path(root).rglob('*')) for root in ('/opt/retroone/libretro','/opt/retroone/vendor','/opt/retroone/system','/roms','/artwork')); assert not shutil.which('pcsx2'); assert not shutil.which('sdlpal'); print('no private runtime, ROM, firmware or artwork payloads')"
        subprocess.run(['docker', 'exec', name, '/lsiopy/bin/python', '-c', probe], check=True)
        report = {'health': health, 'empty_library': True, 'profiles_endpoint': bool(profiles), 'private_vendor_payloads': False}
        print(json.dumps(report, ensure_ascii=False))
    finally:
        subprocess.run(['docker', 'logs', '--tail', '80', name], check=False)
        subprocess.run(['docker', 'rm', '-f', '-v', name], check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    check(parser.parse_args().image)
