const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {sameOriginURL, validateManifest, clientEligibility, webtorrentOptions, createStats, DownloadController} = require('../web/rom-transfer.js');
const MiB=1024*1024;
const location={href:'https://retro.example/p/player2/play/game'};
const fixture=()=>({name:'abc123.nds',size:64,revision:'a'.repeat(64),download_url:'/p/player2/api/games/abc/browser-rom/abc123.nds?revision='+ 'a'.repeat(64),requires_threads:false,requires_webgl2:false,
  torrent_base64:Buffer.from('d4:infod4:name10:abc123.ndsee').toString('base64'),info_hash:'b'.repeat(40),tracker_path:'/rom-tracker?token=private',ice_servers:[]});
const capabilities={disk:true,memory:8,secure:true,threads:true,webgl2:true,rtc:true,crypto:true};

test('download and tracker URLs stay same-origin and use the correct WebSocket scheme',()=>{
  assert.equal(sameOriginURL('/p/player2/api/x?revision=a',location),'https://retro.example/p/player2/api/x?revision=a');
  assert.equal(sameOriginURL('/rom-tracker?token=private',location,true),'wss://retro.example/rom-tracker?token=private');
  assert.equal(sameOriginURL('/rom-tracker',{href:'http://localhost:8000/'},true),'ws://localhost:8000/rom-tracker');
  for(const url of ['https://other.example/file','//other.example/file','/\\other.example/file','javascript:alert(1)','/file#fragment','/a\nb'])assert.throws(()=>sameOriginURL(url,location));
});

test('manifest validation accepts large files but rejects unsafe metadata and unrelated tracker URLs',()=>{
  const valid=validateManifest(fixture(),'p2p',location);
  assert.equal(valid.size,64); assert.deepEqual(valid.ice_servers,[]);
  for(const patch of [{size:0},{size:Number.MAX_SAFE_INTEGER+1},{size:1.2},{name:'../save.bin'},{name:'a/b.iso'},{revision:'x\n'},{tracker_path:'//public.example/announce'},{info_hash:'not-a-hash'},{torrent_base64:'bad%'},{ice_servers:[{urls:['https://example.com']}]},{ice_servers:[{urls:'turn:example.com',credential:{secret:true}}]}]) {
    assert.throws(()=>validateManifest({...fixture(),...patch},'p2p',location));
  }
  assert.doesNotThrow(()=>validateManifest({...fixture(),size:8*1024*MiB},'http',location));
});

test('client memory, disk and emulator requirements are checked independently of transport',()=>{
  assert.equal(clientEligibility(fixture(),'p2p',capabilities),null);
  assert.equal(clientEligibility({...fixture(),size:129*MiB},'p2p',{...capabilities,disk:false}),'storage');
  assert.equal(clientEligibility({...fixture(),size:65*MiB},'http',{...capabilities,memory:2}),'memory');
  assert.equal(clientEligibility({...fixture(),size:257*MiB},'p2p',{...capabilities,memory:4}),'memory');
  assert.equal(clientEligibility({...fixture(),size:32*MiB,unpacked_bytes:65*MiB},'http',{...capabilities,memory:2}),'memory');
  assert.equal(clientEligibility({...fixture(),requires_threads:true},'http',{...capabilities,threads:false}),'threads');
  assert.equal(clientEligibility({...fixture(),requires_webgl2:true},'http',{...capabilities,webgl2:false}),'graphics');
  assert.equal(clientEligibility(fixture(),'p2p',{...capabilities,rtc:false}),'unsupported');
  assert.equal(clientEligibility(fixture(),'http',{...capabilities,rtc:false,secure:false}),null);
});

test('WebTorrent uses only the selected tracker, explicit ICE and HTTP seed without public discovery',()=>{
  const manifest=validateManifest(fixture(),'p2p',location), store=function Store(){};
  const options=webtorrentOptions(manifest,store);
  for(const flag of ['dht','lsd','utPex','natUpnp','natPmp','utp'])assert.equal(options.client[flag],false);
  assert.deepEqual(options.client.tracker,{announce:[],rtcConfig:{iceServers:[]}});
  assert.equal(options.client.seedOutgoingConnections,true,'a completed browser must connect to peers joining later');
  assert.deepEqual(options.torrent.announce,['wss://retro.example/rom-tracker?token=private']);
  assert.deepEqual(options.torrent.urlList,[manifest.downloadURL]);
  assert.equal(options.torrent.private,true); assert.equal(options.torrent.destroyStoreOnDestroy,true);
  assert.equal(options.torrent.storeCacheSlots,2); assert.equal(options.torrent.store,store);
});

test('transfer statistics distinguish HTTP and peer payloads, exclude web seeds from peers, and attach once',()=>{
  const stats=createStats(), seed=new EventEmitter(), peer=new EventEmitter(); seed.type='webSeed'; peer.type='webrtc';
  stats.attach(seed);stats.attach(peer);stats.attach(peer);
  seed.emit('download',2048);peer.emit('download',1024);peer.emit('upload',512);seed.emit('upload',999);peer.emit('download',-5);
  assert.deepEqual(stats.snapshot(),{http_bytes:2048,peer_bytes:1024,uploaded_bytes:512,peer_count:1});
  peer.emit('close'); assert.equal(stats.snapshot().peer_count,0);
});

class Element {
  constructor(tag,doc){this.tagName=tag;this.doc=doc;this.style={};this.children=[];this.listeners={};this.hidden=false;this.isConnected=false;this.textContent='';}
  setAttribute(name,value){this[name]=value;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  append(...elements){for(const element of elements){element.parent=this;element.isConnected=this.isConnected;this.children.push(element);if(element.id)this.doc.ids.set(element.id,element);if(element.tagName==='script')this.doc.script(element);}}
  replaceChildren(...elements){for(const child of this.children)child.isConnected=false;this.children=[];this.append(...elements);}
  remove(){this.isConnected=false;if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  after(element){this.parent?.append(element);}
  getContext(){return {getExtension(){return {loseContext(){}};}};}
}

function harness({transport='http',fetch,autoDone=true,disk=false,blobPending=false,loadBundles=false}={}) {
  const pending=[],requests=[],scripts=[],clients=[],listeners={},timeouts=new Map(),intervals=new Map(); let serial=0;
  const document={ids:new Map(),createElement(tag){return new Element(tag,this);},getElementById(id){return this.ids.get(id)||null;},script(element){scripts.push(element.src);queueMicrotask(()=>{if(element.src.endsWith('/webtorrent.min.js'))env.WebTorrent=FakeWebTorrent;if(element.src.endsWith('/rom-chunk-store.js')){env.RetroRomChunkStore=FakeStore;env.RetroRomMemoryStore=FakeMemoryStore;}element.onload?.();});}};
  document.body=document.createElement('body');document.body.isConnected=true;
  for(const id of ['game','status']){const element=document.createElement('div');element.id=id;document.body.append(element);}
  class FakeStore{constructor(length,opts){this.length=length;this.opts=opts;}}
  class FakeMemoryStore{}
  class FakeWebTorrent extends EventEmitter {
    constructor(options){super();this.options=options;this.destroyed=false;this.destroyCalls=0;clients.push(this);}
    add(bytes,options,ready){
      this.addOptions=options;this.torrentBytes=bytes;
      const torrent=new EventEmitter(); Object.assign(torrent,{infoHash:fixture().info_hash,length:64,done:autoDone,wires:[],downloaded:64,
        files:[{name:fixture().name,length:64,blob:()=>blobPending?new Promise((resolve,reject)=>pending.push({resolve,reject})):Promise.resolve(new Blob([new Uint8Array(64)]))}]});
      this.torrent=torrent;queueMicrotask(()=>ready(torrent));return torrent;
    }
    destroy(callback){this.destroyCalls++;this.destroyed=true;callback?.();}
  }
  const env={document,location,Blob,File,AbortController,crypto:{subtle:{},randomUUID:()=>`unique-${++serial}`},navigator:{deviceMemory:8},isSecureContext:true,crossOriginIsolated:true,SharedArrayBuffer,RTCPeerConnection:function(){},
    atob:value=>Buffer.from(value,'base64').toString('binary'),
    setTimeout:fn=>{const id=++serial;timeouts.set(id,fn);return id;},clearTimeout:id=>timeouts.delete(id),
    setInterval:fn=>{const id=++serial;intervals.set(id,fn);return id;},clearInterval:id=>intervals.delete(id),
    addEventListener:(name,fn)=>{listeners[name]=fn;},
    fetch:async(url,options)=>{requests.push({url,options});if(fetch)return fetch(url,options,requests.length);if(url.includes('manifest'))return {ok:true,json:async()=>fixture()};return new Response(new Uint8Array(64),{headers:{'content-length':'64'}});},
  };
  if(disk){env.navigator.storage={getDirectory:async()=>({}),estimate:async()=>({quota:1024*MiB,usage:0})};env.FileSystemFileHandle=function(){};env.FileSystemFileHandle.prototype.createWritable=function(){};}
  if(!loadBundles){env.WebTorrent=FakeWebTorrent;env.RetroRomChunkStore=FakeStore;env.RetroRomMemoryStore=FakeMemoryStore;}
  const config={transport,manifestUrl:'/p/player2/api/games/abc/browser-manifest?transport=p2p',loaderUrl:'/emulatorjs/data/loader.js',webtorrentUrl:'/vendor/webtorrent.min.js',chunkStoreUrl:'/vendor/rom-chunk-store.js',locale:'en'};
  const controller=new DownloadController(config,env);
  return {env,controller,requests,scripts,clients,pending,listeners,timeouts,intervals,config,FakeStore,FakeMemoryStore};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('HTTP downloads a complete file before loading EmulatorJS and never starts torrent discovery',async()=>{
  const h=harness();try{
    h.env.EJS_gameName='original-save-identity';h.env.EJS_gameID=77;
    await h.controller.start();
    assert.equal(h.controller.phase,'running');assert.equal(h.clients.length,0);
    assert.equal(h.env.EJS_gameUrl.name,'abc123.nds');assert.equal(h.env.EJS_gameUrl.size,64);
    assert.equal(h.env.EJS_CacheLimit,0);assert.equal(h.env.EJS_gameName,'original-save-identity');assert.equal(h.env.EJS_gameID,77);
    assert.deepEqual(h.scripts,['https://retro.example/emulatorjs/data/loader.js']);
    assert.equal(new URL(h.requests[0].url).searchParams.get('transport'),'http');
    assert.equal(h.controller.snapshot().http_bytes,64);assert.equal(h.intervals.size,0);
  }finally{h.controller.cancel(false);}
});

test('cancel during manifest fetch cannot download a ROM or start the emulator after a late response',async()=>{
  let resolve;
  const h=harness({fetch:()=>new Promise(done=>resolve=done)});
  const run=h.controller.start();await flush();h.controller.cancel();
  resolve({ok:true,json:async()=>fixture()});await run;
  assert.equal(h.requests.length,1);assert.equal(h.scripts.length,0);assert.equal(h.controller.phase,'cancelled');
  assert.equal(h.timeouts.size,0);assert.equal(h.intervals.size,0);
});

test('HTTP size mismatch fails before emulator startup and exposes a retry',async()=>{
  const h=harness({fetch:async(url)=>url.includes('manifest')?{ok:true,json:async()=>fixture()}:new Response(new Uint8Array(65),{headers:{'content-length':'65'}})});
  try{await h.controller.start();assert.equal(h.controller.phase,'error');assert.equal(h.scripts.length,0);assert.equal(h.controller.ui.retry.hidden,false);assert.equal(h.intervals.size,0);}finally{h.controller.cancel(false);}
});

test('WebTorrent loads separate bundles, uses isolated upstream storage, and keeps seeding after launch',async()=>{
  const h=harness({transport:'p2p',disk:true,loadBundles:true});try{
    await h.controller.start();const client=h.clients[0];
    assert.equal(h.controller.phase,'running');assert.equal(client.destroyed,false);
    assert.deepEqual(h.scripts,['https://retro.example/vendor/webtorrent.min.js','https://retro.example/vendor/rom-chunk-store.js','https://retro.example/emulatorjs/data/loader.js']);
    const Store=client.addOptions.store, instance=new Store(1024,{name:'shared-torrent-name',rootDir:{shared:true},files:[]});
    assert.ok(instance instanceof h.FakeStore);assert.match(instance.opts.name,/^retroone-rom-unique-/);assert.equal(instance.opts.rootDir,undefined);
    assert.equal(client.addOptions.destroyStoreOnDestroy,true);
    h.controller.stopSharing();assert.equal(client.destroyCalls,1);assert.equal(h.controller.phase,'running');assert.equal(h.env.EJS_gameUrl.size,64);
    assert.equal(h.intervals.size,0);h.controller.stopSharing();assert.equal(client.destroyCalls,1);
  }finally{h.controller.cancel(false);}
});

test('memory fallback is explicit and never falls back to a shared default FSA directory',async()=>{
  const h=harness({transport:'p2p',disk:false});try{
    await h.controller.start();assert.equal(h.clients[0].addOptions.store,h.FakeMemoryStore);
  }finally{h.controller.cancel(false);}
});

test('WebTorrent without other peers can complete through HTTP seed and reports the source accurately',async()=>{
  const h=harness({transport:'p2p',autoDone:false});try{
    const run=h.controller.start();await flush();const client=h.clients[0],torrent=client.torrent;
    const seed=new EventEmitter();seed.type='webSeed';torrent.emit('wire',seed);seed.emit('download',64);torrent.emit('download',64);
    torrent.done=true;torrent.emit('done');await run;
    assert.equal(h.controller.phase,'running');assert.equal(h.controller.snapshot().http_bytes,64);assert.equal(h.controller.snapshot().peer_bytes,0);assert.equal(h.controller.snapshot().peer_count,0);
  }finally{h.controller.cancel(false);}
});

test('cancel during blob assembly destroys only this client and never starts the emulator',async()=>{
  const h=harness({transport:'p2p',blobPending:true});const run=h.controller.start();await flush();
  assert.equal(h.pending.length,1);h.controller.cancel();h.pending[0].resolve(new Blob([new Uint8Array(64)]));await run;
  assert.equal(h.clients[0].destroyCalls,1);assert.equal(h.controller.phase,'cancelled');assert.equal(h.scripts.length,0);
  assert.equal(h.intervals.size,0);
});

test('page exit closes the owned torrent and releases timers without touching game settings',async()=>{
  const h=harness({transport:'p2p'});await h.controller.start();
  h.listeners.pagehide();assert.equal(h.clients[0].destroyCalls,1);assert.equal(h.controller.phase,'closed');assert.equal(h.intervals.size,0);
  await h.controller.start();assert.equal(h.clients.length,1,'an unloaded frame must not restart');
});

test('failed P2P downloads automatically retry HTTP once and preserve the browser game identity',async()=>{
  const h=harness({transport:'p2p',autoDone:false});try{
    h.env.EJS_gameName='player2-original-save';h.env.EJS_gameID=42;
    const first=h.controller.start();await flush();h.clients[0].emit('error',new Error('private diagnostic'));await first;
    assert.equal(h.controller.phase,'running');assert.equal(h.clients.length,1);assert.equal(h.clients[0].destroyCalls,1);
    assert.equal(new URL(h.requests[1].url).searchParams.get('transport'),'http');
    assert.equal(h.requests.length,3);assert.equal(h.controller.snapshot().transport,'http');
    assert.equal(h.env.EJS_gameName,'player2-original-save');assert.equal(h.env.EJS_gameID,42);
    assert.equal(h.scripts.filter(url=>url.endsWith('/loader.js')).length,1);
  }finally{h.controller.cancel(false);}
});

test('missing WebRTC falls back to HTTP without attempting peer discovery',async()=>{
  const h=harness({transport:'p2p'});delete h.env.RTCPeerConnection;
  try{await h.controller.start();assert.equal(h.controller.phase,'running');assert.equal(h.clients.length,0);assert.equal(h.controller.snapshot().transport,'http');}
  finally{h.controller.cancel(false);}
});

test('a failed HTTP fallback is shown once instead of retrying forever',async()=>{
  const h=harness({transport:'p2p',autoDone:false,fetch:async url=>url.includes('manifest')?{ok:true,json:async()=>fixture()}:{ok:false}});
  try{
    const run=h.controller.start();await flush();h.clients[0].emit('error',new Error('network'));await run;
    assert.equal(h.controller.phase,'error');assert.equal(h.requests.length,3);assert.equal(h.controller.ui.retry.hidden,false);
    assert.equal(h.controller.ui.http.hidden,true);assert.equal(h.intervals.size,0);assert.equal(h.timeouts.size,0);
  }finally{h.controller.cancel(false);}
});

test('P2P stalls fall back to HTTP and discard timers from the old transfer',async()=>{
  const h=harness({transport:'p2p',autoDone:false});
  try{
    const run=h.controller.start();await flush();h.controller.lastProgress=0;
    for(const tick of [...h.intervals.values()])tick();await run;
    assert.equal(h.controller.phase,'running');assert.equal(h.controller.snapshot().transport,'http');
    assert.equal(h.clients[0].destroyCalls,1);assert.equal(h.intervals.size,0);assert.equal(h.timeouts.size,0);
  }finally{h.controller.cancel(false);}
});

test('P2P integrity and memory failures do not bypass validation through HTTP',async()=>{
  const corrupt=harness({transport:'p2p',fetch:async()=>({ok:true,json:async()=>({...fixture(),info_hash:'bad'})})});
  try{await corrupt.controller.start();assert.equal(corrupt.controller.phase,'error');assert.equal(corrupt.requests.length,1);assert.equal(corrupt.scripts.length,0);}
  finally{corrupt.controller.cancel(false);}
  const lowMemory=harness({transport:'p2p',fetch:async()=>({ok:true,json:async()=>({...fixture(),size:100*MiB})})});lowMemory.env.navigator.deviceMemory=2;
  try{await lowMemory.controller.start();assert.equal(lowMemory.controller.phase,'error');assert.equal(lowMemory.requests.length,1);assert.equal(lowMemory.scripts.length,0);}
  finally{lowMemory.controller.cancel(false);}
});

test('runtime torrent storage, allocation and integrity errors never retry through HTTP',async()=>{
  for (const [problem, expected] of [
    [Object.assign(new Error('disk full'),{name:'QuotaExceededError'}),'insufficient free storage'],
    [new RangeError('Array buffer allocation failed'),'memory allowance'],
    [Object.assign(new Error('content changed'),{transferCode:'mismatch'}),'content identity changed'],
    [Object.assign(new Error('bad metadata'),{transferCode:'invalid'}),'Invalid download information'],
    [new Error('Invalid torrent identifier'),'Invalid download information'],
    [new Error('Torrent is missing required field'),'Invalid download information'],
    [Object.assign(new Error('cancelled'),{name:'AbortError'}),'download connection failed'],
  ]) {
    const h=harness({transport:'p2p',autoDone:false});
    try {
      const run=h.controller.start();await flush();h.clients[0].torrent.emit('error',problem);await run;
      assert.equal(h.controller.phase,'error');assert.equal(h.requests.length,1);assert.equal(h.scripts.length,0);
      assert.ok(h.controller.ui.detail.textContent.toLowerCase().includes(expected.toLowerCase()));
      assert.equal(h.controller.ui.http.hidden,true);assert.equal(h.clients[0].destroyCalls,1);
      assert.equal(h.intervals.size,0);assert.equal(h.timeouts.size,0);
    }finally{h.controller.cancel(false);}
  }
});

test('malformed manifest JSON does not retry but a manifest network rejection can retry HTTP once',async()=>{
  for(const malformed of [true,false]){
    const h=harness({transport:'p2p',fetch:async(url,options,count)=>{
      if(count===1){if(malformed)return {ok:true,json:async()=>{throw new SyntaxError('invalid JSON');}};throw new TypeError('Failed to fetch');}
      return url.includes('manifest')?{ok:true,json:async()=>fixture()}:new Response(new Uint8Array(64));
    }});
    try{await h.controller.start();assert.equal(h.controller.phase,malformed?'error':'running');assert.equal(h.requests.length,malformed?1:3);assert.equal(h.clients.length,0);}
    finally{h.controller.cancel(false);}
  }
});

test('failed piece verification stops before HTTP fallback but tracker warnings leave the web seed usable',async()=>{
  const h=harness({transport:'p2p',autoDone:false});
  try {
    const run=h.controller.start();await flush();
    h.clients[0].torrent.emit('warning',new Error('Tracker connection failed'));await flush();
    assert.equal(h.controller.phase,'downloading');assert.equal(h.requests.length,1);
    h.clients[0].torrent.emit('warning',new Error('Piece 0 failed verification'));await run;
    assert.equal(h.controller.phase,'error');assert.equal(h.requests.length,1);assert.equal(h.scripts.length,0);
    assert.match(h.controller.ui.detail.textContent,/content identity changed/);
    assert.equal(h.intervals.size,0);assert.equal(h.clients[0].destroyCalls,1);
  }finally{h.controller.cancel(false);}
});

test('cancelling while HTTP fallback is preparing cannot restart emulation',async()=>{
  let resolveFallback;
  const h=harness({transport:'p2p',autoDone:false,fetch:async(url,options,count)=>count===1?{ok:true,json:async()=>fixture()}:new Promise(resolve=>resolveFallback=resolve)});
  const run=h.controller.start();await flush();h.clients[0].emit('error',new Error('network'));await flush();
  assert.equal(h.requests.length,2);h.controller.cancel();resolveFallback({ok:true,json:async()=>fixture()});await run;
  assert.equal(h.controller.phase,'cancelled');assert.equal(h.requests.length,2);assert.equal(h.scripts.length,0);
  assert.equal(h.timeouts.size,0);assert.equal(h.intervals.size,0);
});

test('late events from a cancelled torrent cannot stop the replacement client or pollute its statistics',async()=>{
  const h=harness({transport:'p2p'});try{
    await h.controller.start();const previous=h.clients[0];
    await h.controller.start();const current=h.clients[1];
    assert.equal(h.controller.phase,'running');assert.equal(previous.destroyCalls,1);assert.equal(current.destroyed,false);
    const snapshot=h.controller.snapshot();
    previous.emit('error',new Error('late client error'));
    previous.torrent.emit('error',new Error('late torrent error'));
    previous.torrent.downloaded=1;previous.torrent.emit('download',1);
    const peer=new EventEmitter();peer.type='webrtc';previous.torrent.emit('wire',peer);peer.emit('download',123456);
    assert.equal(current.destroyed,false);assert.equal(current.destroyCalls,0);
    assert.deepEqual(h.controller.snapshot(),snapshot);
  }finally{h.controller.cancel(false);}
});

function attachLibrary(h, {known=true,permission='granted',ensure}={}) {
  const file=new File([new Uint8Array(64)],fixture().name),calls=[];
  const snapshot=()=>({directory:{permission},entries:known?[{gameId:'abc',size:64,received:64,status:'ready'}]:[]});
  const library={init:async()=>{},snapshot,subscribe(callback){callback(snapshot());return ()=>calls.push('unsubscribe');},async ensureFile(manifest,options){calls.push(options);return ensure?ensure(manifest,options):file;}};
  h.env.parent={RetroRomLibrary:library};
  Object.assign(h.controller.config,{gameId:'abc',profileId:'player2',title:'Generated demo',downloadManifestUrl:'/p/player2/api/games/abc/download-manifest'});
  return {file,calls,library};
}

test('browser launch uses the parent-owned verified disk File without re-downloading or another peer client',async()=>{
  const h=harness({transport:'p2p'}),disk=attachLibrary(h);
  h.env.RetroRomLibrary={init:()=>{throw Error('must use parent library');}};
  h.env.EJS_gameName='player2-save-identity';h.env.EJS_gameID=123;
  try {
    await h.controller.start();assert.equal(h.controller.phase,'running');assert.equal(h.env.EJS_gameUrl,disk.file);
    assert.equal(h.clients.length,0);assert.equal(h.requests.length,1);assert.equal(new URL(h.requests[0].url).searchParams.get('transport'),'http');
    assert.equal(disk.calls[0].profileId,'player2');assert.equal(disk.calls[0].allowP2P,true);assert.equal(disk.calls[1],'unsubscribe');
    assert.equal(h.env.EJS_gameName,'player2-save-identity');assert.equal(h.env.EJS_gameID,123);
    h.listeners.pagehide();assert.equal(h.controller.phase,'closed');assert.equal(disk.calls.length,2,'leaving the game does not pause the parent library');
  }finally{h.controller.cancel(false);}
});

test('automatic browser launch reuses an existing disk cache but never downloads an uncached game through the library',async()=>{
  for(const known of [true,false]){
    const h=harness(),disk=attachLibrary(h,{known});h.controller.config.cachedOnly=true;h.env.EJS_gameUrl='/original/rom';
    try {
      await h.controller.start();assert.equal(h.controller.phase,'running');assert.equal(h.requests.length,known?1:0);
      assert.equal(h.env.EJS_gameUrl,known?disk.file:'/original/rom');assert.equal(disk.calls.length,known?2:0);assert.equal(h.clients.length,0);
      assert.deepEqual(h.scripts,['https://retro.example/emulatorjs/data/loader.js']);
    }finally{h.controller.cancel(false);}
  }
});

test('cancelling while the parent library verifies a file prevents a late emulator start',async()=>{
  let finish;const h=harness(),disk=attachLibrary(h,{ensure:()=>new Promise(resolve=>finish=resolve)});
  const run=h.controller.start();await flush();assert.equal(typeof finish,'function');h.controller.cancel();finish(disk.file);await run;
  assert.equal(h.controller.phase,'cancelled');assert.equal(h.scripts.length,0);assert.equal(disk.calls.at(-1),'unsubscribe');
});

test('202 piece preparation polls without downloading early and cancellation clears the polling timer',async()=>{
  for(const cancel of [false,true]){
    const h=harness({transport:'p2p',fetch:async(url,options,count)=>count===1?{ok:true,status:202,headers:{get:()=>1}}:{ok:true,status:200,json:async()=>fixture()}});
    const run=h.controller.start();await flush();assert.equal(h.requests.length,1);assert.equal(h.clients.length,0);assert.equal(h.timeouts.size,1);
    if(cancel)h.controller.cancel();else for(const tick of [...h.timeouts.values()])tick();
    await run;assert.equal(h.controller.phase,cancel?'cancelled':'running');assert.equal(h.requests.length,cancel?1:2);
    assert.equal(h.timeouts.size,0);h.controller.cancel(false);
  }
});
