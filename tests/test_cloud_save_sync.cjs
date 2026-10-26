const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const source = fs.readFileSync('web/cloud-save-sync.js', 'utf8');
const ROOT='/data/saves/PPSSPP/PSP/SAVEDATA', OLD_ROOT='/data/saves/PSP/SAVEDATA', META='/data/saves/.retroone-cloud.json';
const CORE_CONFIG='/home/web_user/.config/retroarch/retroarch.cfg';
const file = text => [{path:'TEST00001SAVE00/DATA.BIN',data:Buffer.from(text).toString('base64')}];
const hash = files => createHash('sha256').update(JSON.stringify(files)).digest('hex');
function memoryFS() {
    const entries = new Map([['/',null],['/data',null],['/data/saves',null],
        [CORE_CONFIG,new TextEncoder().encode('savefile_directory = "/data/saves"\nvideo_vsync = true\n')]]);
    const filesystem = {
        entries, persisted: null, streams: [],
        analyzePath: path => ({exists:entries.has(path)}),
        mkdir: path => { entries.set(path,null); },
        readdir: path => ['.','..',...Array.from(entries.keys()).filter(p=>p.startsWith(path+'/')&&!p.slice(path.length+1).includes('/')).map(p=>p.slice(path.length+1))],
        lstat: path => { if(!entries.has(path)) throw Error('ENOENT'); const item=entries.get(path); return {mode:item===null?0x4000:0x8000,size:item?.length||0}; },
        isDir: mode=>(mode&0xf000)===0x4000, isFile: mode=>(mode&0xf000)===0x8000,
        writeFile: (path,value) => entries.set(path,typeof value==='string'?new TextEncoder().encode(value):new Uint8Array(value)),
        readFile: (path,opts) => { if(!entries.has(path))throw Error('ENOENT'); const value=entries.get(path); return opts?.encoding?new TextDecoder().decode(value):value; },
        unlink:path=>entries.delete(path),rmdir:path=>entries.delete(path),
        syncfs: (_,callback)=>{ filesystem.persisted=new Map(entries); callback(null); }
    };
    return filesystem;
}
function setup() {
    const files=file('native progress'), requests=[], timers=new Map();
    const server={revision:0,files,digest:hash(files),failUpload:false,lease:'lease-test'};
    const context={console,TextEncoder,Uint8Array,ArrayBuffer,crypto:webcrypto,btoa:s=>Buffer.from(s,'binary').toString('base64'),atob:s=>Buffer.from(s,'base64').toString('binary'),
        AbortController,Date,JSON,Promise,Map,Error,module:{exports:{}},
        RetroCloudSaveConfig:{gameId:'a'.repeat(24),profileId:'player1',locale:'en',url:'/save',legacyDatabase:'/old'},
        indexedDB:{databases:async()=>[]},navigator:{},sessionStorage:{getItem:()=>null,setItem:()=>{}},addEventListener:()=>{},
        setTimeout:()=>1,clearTimeout:()=>{},setInterval:(callback,ms)=>{timers.set(ms,callback);return ms;},clearInterval:ms=>timers.delete(ms),
        fetch:async(url,options)=>{const body=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method,body});
            if(options.method==='PUT') {
                if(server.failUpload)throw Error('offline');
                if(body.base_revision!==server.revision)return {ok:false,json:async()=>({detail:{code:'revision_conflict'}})};
                server.files=body.files;server.digest=hash(server.files);server.revision++;
            }
            return {ok:true,json:async()=>({lease_id:server.lease,revision:server.revision,digest:server.digest,files:server.files})};
        }};
    context.window=context;context.parent=context;
    vm.createContext(context);vm.runInContext(source,context);
    return {api:context.RetroCloudSaveSync,context,server,requests,timers,FS:memoryFS()};
}
test('mount is awaited, native files arrive before caller can start the core',async()=>{
    const x=setup();let originalMounted=false;
    class Manager {constructor(){this.FS=x.FS;}async mountFileSystems(){await Promise.resolve();originalMounted=true;return 42;}}
    x.api._test.install(Manager);
    assert.equal(await new Manager().mountFileSystems(),42);
    assert.ok(originalMounted);
    assert.equal(new TextDecoder().decode(x.FS.readFile(ROOT+'/TEST00001SAVE00/DATA.BIN')),'native progress');
    assert.equal(x.api.status().status,'synced');
    assert.ok(x.FS.persisted.has(META));
    const config=x.FS.readFile(CORE_CONFIG,{encoding:'utf8'});
    assert.match(config,/sort_savefiles_enable = true/);
    assert.match(config,/sort_savefiles_by_content_enable = false/);
    assert.match(config,/savefiles_in_content_dir = false/);
    assert.match(config,/video_vsync = true/);
});
test('flush persists dirty outbox before upload and successful exit releases lease',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);
    x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','browser progress');
    assert.equal((await x.api.flush()).synced,true);
    assert.equal(x.server.revision,1);
    assert.equal(JSON.parse(x.FS.readFile(META,{encoding:'utf8'})).dirty,false);
    const result=await x.api.prepareExit();
    assert.equal(result.safeToExit,true);
    assert.equal(x.requests.at(-1).url,'/save/release');
    assert.equal(x.timers.size,0);
});
test('failed upload retains persistent data and does not falsely release',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);x.server.failUpload=true;
    x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','offline progress');
    const result=await x.api.prepareExit();
    assert.equal(result.safeToExit,false);
    assert.equal(x.api.status().dirty,true);
    assert.equal(JSON.parse(new TextDecoder().decode(x.FS.persisted.get(META))).dirty,true);
    assert.equal(new TextDecoder().decode(x.FS.persisted.get(ROOT+'/TEST00001SAVE00/DATA.BIN')),'offline progress');
    assert.ok(!x.requests.some(r=>r.url.endsWith('/release')));
    x.server.failUpload=false;assert.equal((await x.api.flush()).synced,true);
});
test('revision conflicts preserve local and remote progress',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);
    x.server.revision++;x.server.files=file('other device');x.server.digest=hash(x.server.files);
    x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','local pending');
    assert.equal((await x.api.flush()).synced,false);
    assert.equal(x.api.status().status,'conflict');
    assert.equal(new TextDecoder().decode(x.FS.readFile(ROOT+'/TEST00001SAVE00/DATA.BIN')),'local pending');
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'other device');
});
test('open writable save streams postpone upload and exit',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);
    x.FS.streams.push({path:ROOT+'/TEST00001SAVE00/DATA.BIN',flags:2});
    assert.equal((await x.api.prepareExit()).safeToExit,false);
    assert.ok(!x.requests.some(r=>r.method==='PUT'));
});
test('snapshot excludes metadata and cannot walk links or private siblings',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);
    x.FS.writeFile('/data/saves/firmware.keys','PRIVATE');
    assert.equal(x.api._test.snapshot(x.FS).length,1);
    x.FS.entries.set(ROOT+'/link',new Uint8Array());
    const native=x.FS.lstat;x.FS.lstat=path=>path.endsWith('/link')?{mode:0xa000}:native(path);
    assert.throws(()=>x.api._test.snapshot(x.FS),/links/);
});
test('canonical snapshot hash matches Python JSON contract',async()=>{
    const x=setup();assert.equal(await x.api._test.digest(file('bytes')),hash(file('bytes')));
});
test('unuploaded local deletion is a change, not a virgin save space',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);
    x.FS.unlink(ROOT+'/TEST00001SAVE00/DATA.BIN');
    // Simulate reload before the change detector ran: the baseline persists,
    // although the prior dirty flag is still false.
    const y=setup();y.FS.entries.clear();for(const [key,value] of x.FS.entries)y.FS.entries.set(key,value);
    await y.api._test.prepare(y.FS);
    assert.equal(y.server.files.length,0);
    assert.equal(y.server.revision,1);
});

async function settleUntil(predicate) {
    for(let i=0;i<100;i++){if(predicate())return;await new Promise(resolve=>setImmediate(resolve));}
    assert.fail('condition did not settle');
}
test('unstarted native-busy page can cancel and preserve dirty local outbox',async()=>{
    const x=setup();x.api._test.replaceFiles(x.FS,file('pending local'));
    x.FS.writeFile(META,JSON.stringify({baseRevision:0,baseDigest:hash(file('old')),dirty:true}));
    x.context.fetch=async()=>({ok:false,json:async()=>({detail:{code:'native_active'}})});
    const preparing=x.api._test.prepare(x.FS).catch(error=>error.code);
    await settleUntil(()=>x.api.status().status==='busy');
    const exit=await x.api.prepareExit();
    assert.equal(exit.safeToExit,true);assert.equal(exit.synced,false);assert.equal(exit.cancelled,true);
    assert.equal(await preparing,'startup_cancelled');
    assert.equal(new TextDecoder().decode(x.FS.persisted.get(ROOT+'/TEST00001SAVE00/DATA.BIN')),'pending local');
    assert.equal(JSON.parse(new TextDecoder().decode(x.FS.persisted.get(META))).dirty,true);
    assert.equal(x.timers.size,0);
});
test('legacy confirmation can be cancelled without accepting or changing old data',async()=>{
    const x=setup();x.context.indexedDB.databases=undefined;
    const preparing=x.api._test.prepare(x.FS).catch(error=>error.code);
    await settleUntil(()=>x.api.status().status==='legacy');
    const exit=await x.api.prepareExit();
    assert.equal(exit.safeToExit,true);assert.equal(exit.synced,false);
    assert.equal(await preparing,'startup_cancelled');
    assert.equal(x.requests.length,0);assert.equal(x.FS.entries.has(META),false);
});
test('a busy Web Lock can be cancelled before taking ownership of the filesystem',async()=>{
    const x=setup();x.context.navigator.locks={request:async(name,options,callback)=>callback(null)};
    const preparing=x.api._test.prepare(x.FS).catch(error=>error.code);
    await settleUntil(()=>x.api.status().status==='busy');
    assert.equal((await x.api.prepareExit()).safeToExit,true);
    assert.equal(await preparing,'startup_cancelled');
    assert.equal(x.requests.length,0);assert.equal(x.FS.persisted,null);
});
test('late lease reply after cancellation is released and cannot import data or start core',async()=>{
    const x=setup();let reply;const nativeFetch=x.context.fetch;
    x.context.fetch=(url,options)=>url.endsWith('/lease')?new Promise(resolve=>reply=resolve):nativeFetch(url,options);
    let mounted=false;
    class Manager {constructor(){this.FS=x.FS;}async mountFileSystems(){}}
    x.api._test.install(Manager);new Manager().mountFileSystems().then(()=>mounted=true);
    await settleUntil(()=>!!reply);
    assert.equal((await x.api.prepareExit()).safeToExit,true);
    reply({ok:true,json:async()=>({lease_id:'late-lease',revision:0,digest:x.server.digest,files:x.server.files})});
    await settleUntil(()=>x.requests.some(r=>r.url.endsWith('/release')));
    assert.equal(mounted,false);assert.equal(x.FS.entries.has(ROOT+'/TEST00001SAVE00/DATA.BIN'),false);
    assert.equal(x.timers.size,0);
});
test('normal exit pauses the core before release and leaves it paused on success',async()=>{
    const x=setup();let running=true;
    class Manager {constructor(){this.FS=x.FS;this.Module={pauseMainLoop:()=>running=false,resumeMainLoop:()=>running=true};}async mountFileSystems(){}}
    x.api._test.install(Manager);await new Manager().mountFileSystems();
    const nativeFetch=x.context.fetch;
    x.context.fetch=(url,options)=>{if(url.endsWith('/release')&&running)x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','late unpaused write');return nativeFetch(url,options);};
    assert.equal((await x.api.prepareExit()).safeToExit,true);assert.equal(running,false);
    assert.equal(new TextDecoder().decode(x.FS.persisted.get(ROOT+'/TEST00001SAVE00/DATA.BIN')),'native progress');
});
test('write arriving despite pause during release is persisted and blocks exit',async()=>{
    const x=setup();let running=true;
    class Manager {constructor(){this.FS=x.FS;this.Module={pauseMainLoop:()=>running=false,resumeMainLoop:()=>running=true};}async mountFileSystems(){}}
    x.api._test.install(Manager);await new Manager().mountFileSystems();
    const nativeFetch=x.context.fetch;
    x.context.fetch=(url,options)=>{if(url.endsWith('/release'))x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','delayed worker write');return nativeFetch(url,options);};
    const exit=await x.api.prepareExit();
    assert.equal(exit.safeToExit,false);assert.equal(exit.synced,false);assert.equal(running,true);
    assert.equal(new TextDecoder().decode(x.FS.persisted.get(ROOT+'/TEST00001SAVE00/DATA.BIN')),'delayed worker write');
    assert.equal(JSON.parse(new TextDecoder().decode(x.FS.persisted.get(META))).dirty,true);
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'native progress');
    x.context.fetch=nativeFetch;await x.api.retry();assert.equal(x.api.status().dirty,false);
});
test('an open save writer causes failed exit to resume the core instead of deadlocking',async()=>{
    const x=setup();let running=true;
    class Manager {constructor(){this.FS=x.FS;this.Module={pauseMainLoop:()=>running=false,resumeMainLoop:()=>running=true};}async mountFileSystems(){}}
    x.api._test.install(Manager);await new Manager().mountFileSystems();
    x.FS.streams.push({path:ROOT+'/TEST00001SAVE00/DATA.BIN',flags:2});
    const exit=await x.api.prepareExit();assert.equal(exit.safeToExit,false);assert.equal(running,true);
    assert.ok(!x.requests.some(r=>r.url.endsWith('/release')));
});

test('unexpected unmount is not interpreted as deleting every native save',async()=>{
    const x=setup();await x.api._test.prepare(x.FS);
    for(const path of Array.from(x.FS.entries.keys()))if(path.startsWith('/data/saves/'))x.FS.entries.delete(path);
    assert.equal((await x.api.flush()).synced,false);
    assert.ok(!x.requests.some(r=>r.method==='PUT'));
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'native progress');
});
test('core-owned exit awaits sync and never invokes the upstream unmount callback',async()=>{
    const x=setup();let upstreamExit=0,navigated=0,running=true;
    const emulator={callEvent:name=>{if(name==='exit'){upstreamExit++;for(const path of Array.from(x.FS.entries.keys()))if(path.startsWith('/data/saves/'))x.FS.entries.delete(path);}return 7;}};
    class Manager {constructor(){this.FS=x.FS;this.EJS=emulator;this.Module={pauseMainLoop:()=>running=false,resumeMainLoop:()=>running=true};}async mountFileSystems(){}}
    x.context.EJS_onExit=()=>navigated++;
    x.api._test.install(Manager);await new Manager().mountFileSystems();
    x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','final in-game save');
    assert.equal(emulator.callEvent('exit'),0);
    await settleUntil(()=>navigated===1);
    assert.equal(upstreamExit,0);assert.equal(running,false);
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'final in-game save');
    assert.equal(x.FS.entries.has(META),true);
    assert.equal((await x.api.prepareExit()).safeToExit,true); // Parent gate is idempotent.
    assert.equal(x.requests.filter(r=>r.url.endsWith('/release')).length,1);
    assert.equal(emulator.callEvent('non-exit'),7);
});
test('failed core-owned exit keeps filesystem mounted and resumes gameplay',async()=>{
    const x=setup();let original=0,navigated=0,running=true;
    const emulator={callEvent:()=>original++};
    class Manager {constructor(){this.FS=x.FS;this.EJS=emulator;this.Module={pauseMainLoop:()=>running=false,resumeMainLoop:()=>running=true};}async mountFileSystems(){}}
    x.context.EJS_onExit=()=>navigated++;
    x.api._test.install(Manager);await new Manager().mountFileSystems();
    x.server.failUpload=true;x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','unsent');
    emulator.callEvent('exit');await settleUntil(()=>x.api.status().status==='offline');
    assert.equal(original,0);assert.equal(navigated,0);assert.equal(running,true);
    assert.equal(x.FS.entries.has(META),true);
});
test('4.3 ESM manager initialization awaits restore before caller can load the ROM',async()=>{
    const x=setup();let initialized=false;
    const emulator={Module:{},callEvent:()=>0,async initializeGameManager(){await Promise.resolve();this.gameManager={FS:x.FS,EJS:this,Module:this.Module};initialized=true;return 'original';}};
    x.api._test.guardEmulator(emulator);
    assert.equal(await emulator.initializeGameManager(),'original');assert.equal(initialized,true);
    assert.equal(new TextDecoder().decode(x.FS.readFile(ROOT+'/TEST00001SAVE00/DATA.BIN')),'native progress');
    assert.equal(x.requests.filter(r=>r.url.endsWith('/lease')).length,1);
});
test('cancelled 4.3 initialization cannot later let the caller start a core',async()=>{
    const x=setup();let initialize,returned=false;
    const emulator={callEvent:()=>0,async initializeGameManager(){await new Promise(resolve=>initialize=resolve);this.gameManager={FS:x.FS,EJS:this,Module:{}};}};
    x.api._test.guardEmulator(emulator);emulator.initializeGameManager().then(()=>returned=true);
    await settleUntil(()=>!!initialize);assert.equal((await x.api.prepareExit()).safeToExit,true);
    initialize();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(returned,false);assert.equal(x.requests.length,0);
});
test('conflict backup pauses before persistence and resumes if a delayed write changes it',async()=>{
    const x=setup();let running=true,reloaded=false;
    class Manager {constructor(){this.FS=x.FS;this.Module={pauseMainLoop:()=>running=false,resumeMainLoop:()=>running=true};}async mountFileSystems(){}}
    x.context.location={reload:()=>reloaded=true};
    x.api._test.install(Manager);await new Manager().mountFileSystems();
    const sync=x.FS.syncfs;let first=true;
    x.FS.syncfs=(mode,callback)=>{assert.equal(running,false);if(first){first=false;x.FS.writeFile(ROOT+'/TEST00001SAVE00/DATA.BIN','late conflict write');}return sync(mode,callback);};
    await x.api._test.useCloudVersion();
    assert.equal(reloaded,false);assert.equal(running,true);
    assert.equal(JSON.parse(x.FS.readFile(META,{encoding:'utf8'})).allowCloud,undefined);
    assert.equal(JSON.parse(x.FS.readFile(META,{encoding:'utf8'})).dirty,true);
});

function writeTree(FS, root, files) {
    for (const item of files) {
        let path='';
        for(const part of (root+'/'+item.path).split('/').filter(Boolean).slice(0,-1)) {
            path+='/'+part;if(!FS.analyzePath(path).exists)FS.mkdir(path);
        }
        FS.writeFile(root+'/'+item.path,Buffer.from(item.data,'base64'));
    }
}
test('the configured PPSSPP core directory receives imports and its writes are uploaded',async()=>{
    const x=setup();
    x.FS.writeFile(CORE_CONFIG,'savefile_directory = "/wrong"\nsort_savefiles_enable = false\nsort_savefiles_enable = false\nsort_savefiles_by_content_enable = true\nsavefiles_in_content_dir = true\nvideo_vsync = true\n');
    const emulator={Module:{},callEvent:()=>0,async initializeGameManager(){this.gameManager={FS:x.FS,EJS:this,Module:this.Module};}};
    x.api._test.guardEmulator(emulator);await emulator.initializeGameManager();
    const config=x.FS.readFile(CORE_CONFIG,{encoding:'utf8'});
    const options=Object.fromEntries(config.split('\n').filter(line=>line.includes('=')).map(line=>line.split('=').map(piece=>piece.trim().replace(/^"|"$/g,''))));
    assert.equal(config.match(/sort_savefiles_enable\s*=/g).length,1);
    assert.equal(options.sort_savefiles_by_content_enable,'false');assert.equal(options.savefiles_in_content_dir,'false');
    // PPSSPP uses RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY and appends PSP/SAVEDATA.
    // Actual 4.3 core acceptance found the core-name directory with sorting on.
    const coreRoot=options.savefile_directory+(options.sort_savefiles_enable==='true'?'/PPSSPP':'')+'/PSP/SAVEDATA';
    assert.equal(coreRoot,ROOT);
    assert.equal(new TextDecoder().decode(x.FS.readFile(coreRoot+'/TEST00001SAVE00/DATA.BIN')),'native progress');
    writeTree(x.FS,coreRoot,file('actual core path save'));
    assert.equal((await x.api.flush()).synced,true);
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'actual core path save');
});
test('old per-game flat folder migrates without discarding dirty progress or its source',async()=>{
    const x=setup();writeTree(x.FS,OLD_ROOT,file('pending old-folder progress'));
    x.FS.writeFile(META,JSON.stringify({baseRevision:0,baseDigest:x.server.digest,dirty:true}));
    await x.api._test.prepare(x.FS);
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'pending old-folder progress');
    assert.equal(x.server.revision,1);
    assert.equal(new TextDecoder().decode(x.FS.readFile(OLD_ROOT+'/TEST00001SAVE00/DATA.BIN')),'pending old-folder progress');
    const metadata=JSON.parse(x.FS.readFile(META,{encoding:'utf8'}));
    assert.equal(metadata.saveRoot,ROOT);assert.equal(metadata.dirty,false);
    // The retained migration source must not resurrect a later in-game deletion.
    x.FS.unlink(ROOT+'/TEST00001SAVE00/DATA.BIN');
    const y=setup();Object.assign(y.server,x.server);y.FS.entries.clear();
    for(const [path,value] of x.FS.entries)y.FS.entries.set(path,value);
    await y.api._test.prepare(y.FS);
    assert.equal(y.server.files.length,0);assert.equal(y.server.revision,2);
});
test('old-folder migration preserves a stale dirty baseline instead of overwriting either side',async()=>{
    const x=setup();writeTree(x.FS,OLD_ROOT,file('offline old-folder progress'));
    const baseline=hash(file('previous cloud'));
    x.FS.writeFile(META,JSON.stringify({baseRevision:0,baseDigest:baseline,dirty:true}));
    x.server.revision=1;
    const preparing=x.api._test.prepare(x.FS).catch(error=>error.code);
    await settleUntil(()=>x.api.status().status==='conflict');
    const metadata=JSON.parse(x.FS.readFile(META,{encoding:'utf8'}));
    assert.equal(metadata.baseRevision,0);assert.equal(metadata.baseDigest,baseline);assert.equal(metadata.dirty,true);
    assert.equal(new TextDecoder().decode(x.FS.readFile(ROOT+'/TEST00001SAVE00/DATA.BIN')),'offline old-folder progress');
    assert.equal(Buffer.from(x.server.files[0].data,'base64').toString(),'native progress');
    assert.ok(!x.requests.some(item=>item.method==='PUT'));
    assert.equal((await x.api.prepareExit()).safeToExit,true);assert.equal(await preparing,'startup_cancelled');
});
test('different old and current folders require a choice and cloud choice backs up both',async()=>{
    const x=setup();writeTree(x.FS,OLD_ROOT,file('old-folder pending'));writeTree(x.FS,ROOT,file('core-folder pending'));
    x.FS.writeFile(META,JSON.stringify({baseRevision:0,baseDigest:x.server.digest,dirty:true}));
    const preparing=x.api._test.prepare(x.FS);
    await settleUntil(()=>x.api.status().status==='conflict');
    assert.equal(x.requests.length,0);
    assert.equal(new TextDecoder().decode(x.FS.readFile(ROOT+'/TEST00001SAVE00/DATA.BIN')),'core-folder pending');
    await x.api._test.useCloudVersion();await preparing;
    const backupPath=Array.from(x.FS.entries.keys()).find(path=>path.startsWith('/data/saves/.retroone-conflict-backup-'));
    const backup=JSON.parse(x.FS.readFile(backupPath,{encoding:'utf8'}));
    assert.equal(Buffer.from(backup.files[0].data,'base64').toString(),'core-folder pending');
    assert.equal(Buffer.from(backup.legacyFiles[0].data,'base64').toString(),'old-folder pending');
    assert.equal(new TextDecoder().decode(x.FS.readFile(ROOT+'/TEST00001SAVE00/DATA.BIN')),'native progress');
    assert.equal(new TextDecoder().decode(x.FS.readFile(OLD_ROOT+'/TEST00001SAVE00/DATA.BIN')),'old-folder pending');
    assert.ok(!x.requests.some(item=>item.method==='PUT'));
});
for(const prefix of [ROOT,OLD_ROOT])test('old shared IDB detects existing PSP saves under '+prefix,async()=>{
    const x=setup();
    class Factory {
        open(){
            const request={};
            setImmediate(()=>{const transaction={objectStore:()=>({openKeyCursor:()=>{
                const cursor={};setImmediate(()=>{cursor.result={key:prefix+'/TEST00001SAVE00/DATA.BIN',continue:()=>{throw Error('known prefix missed');}};cursor.onsuccess();setImmediate(()=>transaction.oncomplete());});return cursor;
            }})};request.result={objectStoreNames:{contains:()=>true},transaction:()=>transaction,close:()=>{}};request.onsuccess();});
            return request;
        }
        async databases(){return [{name:'/old'}];}
    }
    x.context.IDBFactory=Factory;x.context.indexedDB=new Factory();
    assert.equal(await x.api._test.legacyExists(),true);
});
