"""Apply reviewed binary metadata fixes, then audit every core's load ABI.

The upstream cache stays unchanged and SHA-pinned. A separate output SHA pins
each runtime transform; this never changes emulation instructions or game data.
"""
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path('/opt/retroone')
lock = json.loads((root / 'runtime-lock.json').read_text())
for change in lock.get('runtime_transforms', []):
    if change['operation'] != 'clear-execstack' or '/' in change['binary']:
        raise SystemExit('Unrecognized reviewed runtime transform')
    path = root / 'libretro' / change['binary']
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest == change['output_sha256']:
        continue
    if digest != change['input_sha256']:
        raise SystemExit(f'Core transform input changed: {path.name}')
    subprocess.run(['patchelf', '--clear-execstack', str(path)], check=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != change['output_sha256']:
        raise SystemExit(f'Unexpected core transform output: {path.name}')

results = []
for path in sorted((root / 'libretro').glob('*.so')):
    try:
        probe = subprocess.run([sys.executable, '-c',
            'import ctypes,sys; core=ctypes.CDLL(sys.argv[1]); print(core.retro_api_version())',
            str(path)], capture_output=True, text=True, timeout=15)
        result = {'core': path.name, 'ok': probe.returncode == 0 and probe.stdout.strip() == '1',
                  'returncode': probe.returncode, 'stderr': probe.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        result = {'core': path.name, 'ok': False, 'error': 'dlopen timed out'}
    result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    results.append(result)
report = {'count': len(results), 'passed': sum(row['ok'] for row in results), 'cores': results}
(root / 'core-load-audit.json').write_text(json.dumps(report, indent=2) + '\n')
print(f"Core shared-library audit: {report['passed']}/{report['count']} loaded; gameplay still requires separate verification")
for row in results:
    if not row['ok']:
        print(row, file=sys.stderr)
if report['passed'] != report['count']:
    raise SystemExit(1)
