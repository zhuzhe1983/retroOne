#!/usr/bin/env python3
"""Run the real RetroOne API against isolated, generated test content only."""
import argparse
import os
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from create_psp_fixture import build, build_iso, build_cso
from create_nes_fixture import build as build_nes
from create_handheld_sega_fixtures import build_all as build_handheld_sega
from create_snes_pce_fixtures import build_all as build_snes_pce

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18780)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--throttle-kib',type=int,default=0)
    args=parser.parse_args();root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
    marker=root/'.retroone-android-fixture'
    if any(root.iterdir()) and not marker.exists():raise SystemExit('Refusing non-empty, unmarked fixture directory')
    marker.touch()
    roms=root/'roms';psp=roms/'library/psp';psp.mkdir(parents=True,exist_ok=True)
    if not (psp/'RetroOne-Self-Test.pbp').exists():(psp/'RetroOne-Self-Test.pbp').write_bytes(build())
    for name, factory in [('RetroOne-ISO-Self-Test.iso', build_iso), ('RetroOne-CSO-Self-Test.cso', build_cso)]:
        if not (psp/name).exists():(psp/name).write_bytes(factory())
    nes=roms/'library/nes';nes.mkdir(parents=True,exist_ok=True)
    for name, alternate in [('RetroOne-NES-Self-Test.nes', False), ('RetroOne-NES-Alternate.nes', True)]:
        (nes/name).write_bytes(build_nes(alternate))
    with zipfile.ZipFile(nes/'RetroOne-NES-Zip-Test.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('原创测试/RetroOne.nes', build_nes(False))
    for platform, (name, payload) in {**build_handheld_sega(), **build_snes_pce()}.items():
        cartridge = roms/'library'/platform
        cartridge.mkdir(parents=True, exist_ok=True)
        (cartridge/('RetroOne-Cart-' + platform + '-Self-Test' + Path(name).suffix)).write_bytes(payload)
        with zipfile.ZipFile(cartridge/('RetroOne-Cart-' + platform + '-Zip-Test.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('原创测试/' + name, payload)
    # Large deterministic non-playable bytes exercise multiple Range requests and pauses.
    transfer=psp/'Transfer-Only-Not-A-Game.iso'
    if not transfer.exists():
        with transfer.open('xb') as f:
            for _ in range(32):f.write(bytes(range(256))*4096)
    multi=roms/'library/psx/Multifile-Rejection';multi.mkdir(parents=True,exist_ok=True)
    (multi/'test.cue').write_text('FILE "test.bin" BINARY\n TRACK 01 MODE1/2352\n INDEX 01 00:00:00\n')
    (multi/'test.bin').write_bytes(b'Original synthetic track, not a game'*100)
    os.environ.update(ROM_ROOT=str(roms), CONFIG_ROOT=str(root/'unused-default'),
                      EMULATORJS_DATA=str(root/'no-browser-core'),
                      RETROONE_LIBRARY_DATA_ROOT=str(root/'library-data'),
                      RETROONE_FETCH_ARTWORK='0',RETROONE_P2P_ENABLED='0')
    from retroone.api import create_app, MissingRunner
    import uvicorn
    app=create_app(roms,root/'config',runner=MissingRunner(),browser_data=root/'no-browser-core',auto_scan=False,base_path='/p/player1')
    app.state.catalog.scan()
    async def gateway(scope, receive, send):
        # Match production nginx: strip the selected worker prefix before FastAPI.
        if scope['type'] == 'http' and scope['path'].startswith('/p/player1/'):
            scope = dict(scope, path=scope['path'][len('/p/player1'):],
                         raw_path=scope['raw_path'][len(b'/p/player1'):])
        async def throttled(message):
            if args.throttle_kib and scope['type'] == 'http' and '/download-rom/' in scope['path'] and message['type'] == 'http.response.body':
                import asyncio
                await asyncio.sleep(len(message.get('body', b''))/(args.throttle_kib*1024))
            await send(message)
        await app(scope, receive, throttled)
    uvicorn.run(gateway,host=args.host,port=args.port,access_log=False)

if __name__=='__main__':main()
