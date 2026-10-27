const test=require('node:test'),assert=require('node:assert/strict'),{EventEmitter}=require('node:events');
const {LibraryManager,manifestValue}=require('../web/rom-library.js');
const {DirectoryStore}=require('../web/rom-directory-store.js');
const {Directory}=require('./test_rom_directory_store.cjs');
const fixture=()=>({name:'demo.gba',size:10,revision:'r1',info_hash:'a'.repeat(40),torrent_base64:'ZA==',download_url:'/rom/demo.gba',tracker_path:'/rom-tracker?token=test',ice_servers:[]});
let uuidSerial=0;
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(fn){for(let i=0;i<100;i++){if(fn())return;await tick();}throw new Error('Condition not reached');}
function setup({directory=new Directory(),persistence,locks=new Map(),sharing=false,fetch,managerOptions={}}={}){
 let saved=null,serial=0;const clients=[],timers=new Map(),listeners={};
 persistence=persistence||{async load(){return saved;},async save(value){saved={...value,entries:value.entries.map(x=>({...x}))};}};
 const env={isSecureContext:true,indexedDB:{},location:{href:'https://retro.example/',origin:'https://retro.example'},crypto:{randomUUID:()=>`unique-${++uuidSerial}`},AbortController,RTCPeerConnection:class{},
  localStorage:{getItem:()=>String(sharing),setItem(){}},showDirectoryPicker:async()=>directory,
  navigator:{locks:{async request(name,options,callback){const shared=options.mode==='shared',blocked=()=>locks.has(name)&&!(shared&&locks.get(name).shared);if(options.ifAvailable&&blocked())return callback(null);while(blocked())await locks.get(name);let held=locks.get(name);if(!held){let release;held=new Promise(resolve=>release=resolve);Object.assign(held,{shared,count:0,release});locks.set(name,held);}held.count++;try{return await callback({name});}finally{if(--held.count===0){locks.delete(name);held.release();}}}}},
  addEventListener:(name,callback)=>{listeners[name]=callback;},setTimeout,clearTimeout,
  setInterval:fn=>{const id=++serial;timers.set(id,fn);return id;},clearInterval:id=>timers.delete(id),atob:value=>Buffer.from(value,'base64').toString('binary'),
  fetch:fetch||(async()=>({ok:true,status:200,json:async()=>fixture()})),RetroRomDirectoryStore:DirectoryStore};
 class TorrentClient extends EventEmitter{
  constructor(options){super();this.options=options;this.destroyed=false;this.downloaded=0;clients.push(this);}
  add(bytes,options,ready){this.optionsAdd=options;this.store=new options.store(4);const torrent=new EventEmitter();this.torrent=torrent;
   Object.assign(torrent,{infoHash:fixture().info_hash,length:10,files:[{name:'demo.gba'}],downloaded:0,uploaded:0,wires:[],done:false});
   queueMicrotask(async()=>{try{
    const data=Uint8Array.from({length:10},(_,i)=>i+1);
    for(let i=0;i<3;i++){
     if(this.destroyed)return;
     const wanted=data.slice(i*4,Math.min(10,(i+1)*4));let actual;
     try{actual=await new Promise((resolve,reject)=>this.store.get(i,(error,value)=>error?reject(error):resolve(value)));}catch{}
     if(!actual||!Buffer.from(actual).equals(Buffer.from(wanted))){this.downloaded+=wanted.length;await new Promise((resolve,reject)=>this.store.put(i,wanted,error=>error?reject(error):resolve()));}
     torrent.downloaded+=wanted.length;
    if(managerOptions.afterPiece)await managerOptions.afterPiece(i,this);
    }
    torrent.done=true;ready(torrent);torrent.emit('done');
   }catch(error){torrent.emit('error',error);}});return torrent;
  }
  destroy(callback=()=>{}){this.destroyed=true;this.store?this.store.close(callback):callback();}
 }
 env.WebTorrent=TorrentClient;
 return {env,directory,persistence,locks,clients,timers,listeners,manager:new LibraryManager(env,{persistence,...managerOptions})};
}
const add=m=>m.add({gameId:'game1',title:'Generated demo',profileId:'player1',manifestUrl:'/p/player1/api/games/game1/download-manifest'});

test('selected directory is owned, download is disk backed, completed file is reused',async()=>{
 const h=setup();try{
  await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0]?.complete);
  const entry=h.manager.snapshot().entries[0];assert.equal(entry.size,10);assert.equal(entry.status,'ready');
  const file=await h.manager.getFile({gameId:'game1',revision:'r1'});assert.equal(file.size,10);assert.equal(h.clients[0].options.tracker,false);
  assert.equal(h.clients[0].downloaded,10);assert.equal(h.directory.requests,0);assert.equal(h.manager.snapshot().sharing,false);
  const owned=h.directory.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash);assert.ok(owned.files.has('demo.gba'));assert.ok(owned.maxRead<=4);
 }finally{await h.manager.pauseAll();}
});

test('completed ROM is shared across player URL prefixes without another manifest, writer, or download',async()=>{
 const requests=[],h=setup({fetch:async url=>{requests.push(url);return {ok:true,status:200,json:async()=>fixture()};}});
 try{
  await h.manager.chooseDirectory();
  const first=await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/p/player1/api/games/game1/download-manifest'});
  await until(()=>h.manager.jobs.size===0);
  const writes=h.directory.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash).writes.length;
  const next=await h.manager.ensureFile({...fixture(),download_url:'/p/player2/api/games/game1/download-rom/demo.gba'},{gameId:'game1',profileId:'player2',manifestUrl:'/p/player2/api/games/game1/download-manifest'});
  assert.equal(next.size,first.size);assert.equal(next.lastModified,first.lastModified);
  assert.equal(h.clients.length,1);assert.equal(h.clients[0].downloaded,10);assert.equal(requests.length,1);
  assert.equal(h.manager.snapshot().entries.length,1);
  assert.equal(h.directory.dirs.get('RetroOne').dirs.size,1);
  assert.equal(h.directory.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash).writes.length,writes);
 }finally{await h.manager.pauseAll();}
});

test('reopening re-verifies completed bytes and does not download or rewrite intact cache',async()=>{
 const first=setup();await first.manager.chooseDirectory();await add(first.manager);await until(()=>first.manager.snapshot().entries[0]?.complete);await until(()=>first.manager.jobs.size===0);
 const owned=first.directory.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash),writes=owned.writes.length;
 const next=setup({directory:first.directory,persistence:first.persistence,locks:first.locks});try{
  await next.manager.init();assert.equal(next.manager.snapshot().entries[0].complete,false);
  await until(()=>next.manager.snapshot().entries[0].complete);assert.equal(next.clients[0].downloaded,0);assert.equal(owned.writes.length,writes);assert.equal(first.directory.requests,0);
  owned.files.get('demo.gba').mtime++;assert.equal(await next.manager.getFile({gameId:'game1',revision:'r1'}),null);assert.equal(next.manager.snapshot().entries[0].complete,false);
 }finally{await next.manager.pauseAll();await first.manager.pauseAll();}
});

test('permission restoration never prompts until explicit reconnect',async()=>{
 const first=setup();await first.manager.chooseDirectory();await add(first.manager);await until(()=>first.manager.snapshot().entries[0]?.complete);await until(()=>first.manager.jobs.size===0);
 first.directory.permission='prompt';const next=setup({directory:first.directory,persistence:first.persistence});try{
  await next.manager.init();assert.equal(first.directory.requests,0);assert.equal(next.clients.length,0);assert.equal(next.manager.snapshot().directory.permission,'prompt');assert.equal(next.manager.snapshot().entries[0].cachedComplete,true);assert.equal(next.manager.snapshot().entries[0].complete,false);
  await next.manager.reconnect();await until(()=>next.manager.snapshot().entries[0].complete);assert.equal(first.directory.requests,1);
 }finally{await next.manager.pauseAll();}
});

test('existing unrelated RetroOne folder and contents are not overwritten',async()=>{
 const h=setup();const unrelated=await h.directory.getDirectoryHandle('RetroOne',{create:true});unrelated.files.set('notes.txt',{bytes:new Uint8Array([9]),mtime:1});
 await h.manager.chooseDirectory();assert.match(h.manager.state.subdirectory,/^RetroOne-unique-/);assert.equal(unrelated.files.size,1);assert.ok(unrelated.files.has('notes.txt'));
});

test('two tabs cannot write or seed the same cache simultaneously and pause releases its lock',async()=>{
 const first=setup({sharing:true});await first.manager.chooseDirectory();await add(first.manager);await until(()=>first.manager.snapshot().entries[0]?.complete);
 const second=setup({sharing:true,directory:first.directory,persistence:first.persistence,locks:first.locks});try{
  await second.manager.init();await until(()=>second.manager.snapshot().entries[0].status==='busy');assert.equal(second.clients.length,0);
  await first.manager.pause(first.manager.snapshot().entries[0].id);assert.equal(first.locks.size,0);
  await until(()=>second.manager.jobs.size===0);await second.manager.resume(second.manager.snapshot().entries[0].id);await until(()=>second.manager.snapshot().entries[0].complete);
  assert.equal(second.clients[0].downloaded,0);assert.equal(second.locks.size,1);
 }finally{await second.manager.pauseAll();await first.manager.pauseAll();}
});

test('download size is not constrained by WASM memory limits',()=>{
 const h=setup();assert.equal(manifestValue({...fixture(),size:8*1024**3},h.env).size,8*1024**3);
 assert.throws(()=>manifestValue({...fixture(),size:Number.MAX_SAFE_INTEGER+1},h.env));
});

test('202 manifest polling is cancellable and never starts discovery after pause',async()=>{
 let requests=0;const h=setup({fetch:async()=>{requests++;return {status:202,headers:{get:()=>1}};}});
 await h.manager.chooseDirectory();const entry=await add(h.manager);await until(()=>requests===1);await h.manager.pause(entry.id);
 assert.equal(h.clients.length,0);assert.equal(h.manager.snapshot().entries[0].status,'paused');assert.equal(h.locks.size,0);
});

test('ensureFile rejects a changed revision instead of supplying a different ROM',async()=>{
 const h=setup();try{await h.manager.chooseDirectory();await assert.rejects(h.manager.ensureFile({...fixture(),revision:'old'},{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'}),{code:'mismatch'});}
 finally{await h.manager.pauseAll();}
});

test('external modification stops seeding and resume verifies and repairs the affected piece',async()=>{
 const h=setup({sharing:true});try{
  await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0]?.complete);
  const dir=h.directory.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash),value=dir.files.get('demo.gba');value.bytes[0]=99;value.mtime++;
  assert.equal(await h.manager.getFile({gameId:'game1',revision:'r1'}),null);await until(()=>h.clients[0].destroyed);
  await h.manager.resume(h.manager.snapshot().entries[0].id);await until(()=>h.manager.snapshot().entries[0].complete);
  assert.equal(dir.files.get('demo.gba').bytes[0],1);assert.equal(h.clients[1].downloaded,4);
 }finally{await h.manager.pauseAll();}
});

test('metadata commits preserve other tabs entries and cannot revert a directory change',async()=>{
 const first=setup();await first.manager.chooseDirectory();
 const second=setup({directory:first.directory,persistence:first.persistence,locks:first.locks});await second.manager.init();
 first.manager.entries.set('one',{id:'one',status:'paused',paused:true,cachedComplete:true});await first.manager.persist();
 second.manager.entries.set('two',{id:'two',status:'paused',paused:true,cachedComplete:true});await second.manager.persist();
 assert.deepEqual((await first.persistence.load()).entries.map(x=>x.id).sort(),['one','two']);
 const replacement=new Directory('different');second.env.showDirectoryPicker=async()=>replacement;await second.manager.chooseDirectory();
 await assert.rejects(first.manager.persist(),{code:'directory-changed'});
 assert.equal((await first.persistence.load()).directory,replacement);
});

test('cancelling ensureFile does not cancel an existing background download',async()=>{
 let response;const h=setup({sharing:true,fetch:()=>new Promise(resolve=>response=resolve)});
 await h.manager.chooseDirectory();const entry=await add(h.manager);await until(()=>Boolean(response));
 const abort=new AbortController();const waiting=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',signal:abort.signal});
 await tick();abort.abort();await assert.rejects(waiting,{name:'AbortError'});
 assert.equal(h.manager.jobs.get(entry.id).abort.signal.aborted,false);
 response({ok:true,status:200,json:async()=>fixture()});await until(()=>h.manager.snapshot().entries[0].complete);await h.manager.pauseAll();
});

test('ensureFile HTTP-only policy does not change the library sharing preference',async()=>{
 const h=setup({sharing:true});try{
  await h.manager.chooseDirectory();const file=await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',allowP2P:false});
  assert.equal(file.size,10);assert.equal(h.clients[0].options.tracker,false);assert.equal(h.manager.snapshot().sharing,true);assert.equal(h.manager.snapshot().entries[0].status,'ready');
 }finally{await h.manager.pauseAll();}
});

test('operator-disabled P2P still requests verified HTTP metadata and downloads without a tracker',async()=>{
 let requested;const h=setup({sharing:true,fetch:async url=>{requested=url;return {ok:true,status:200,json:async()=>({...fixture(),p2p_available:false,tracker_path:null,ice_servers:[]})};}});
 try{await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0].complete);assert.equal(new URL(requested).searchParams.get('transport'),'http');assert.equal(h.clients[0].options.tracker,false);assert.equal(h.manager.snapshot().entries[0].status,'ready');}
 finally{await h.manager.pauseAll();}
});

test('profiles share one pending game download and a later launch cannot cancel its owner',async()=>{
 let response,requests=0;const h=setup({sharing:true,fetch:()=>{requests++;return new Promise(resolve=>response=resolve);}});
 await h.manager.chooseDirectory();const first=await add(h.manager);
 const second=await h.manager.add({gameId:'game1',title:'Same game',profileId:'player2',manifestUrl:'/p/player2/manifest'});
 assert.equal(second.id,first.id);assert.equal(h.manager.entries.size,1);assert.equal(requests,1);
 const signal=new AbortController();const waiting=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player2',manifestUrl:'/p/player2/manifest',signal:signal.signal});
 await tick();signal.abort();await assert.rejects(waiting,{name:'AbortError'});assert.equal(h.manager.jobs.get(first.id).abort.signal.aborted,false);
 response({ok:true,status:200,json:async()=>fixture()});await until(()=>h.manager.snapshot().entries[0].complete);
 assert.equal(h.clients.length,1);await h.manager.pauseAll();
});

test('concurrent profile launches reserve exactly one transfer owner',async()=>{
 let response;const h=setup({sharing:true,fetch:()=>new Promise(resolve=>response=resolve)});await h.manager.chooseDirectory();
 const secondSignal=new AbortController();
 const first=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});
 const second=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player2',manifestUrl:'/manifest',signal:secondSignal.signal});
 await until(()=>Boolean(response));secondSignal.abort();await assert.rejects(second,{name:'AbortError'});
 assert.equal(h.manager.jobs.size,1);assert.equal([...h.manager.jobs.values()][0].abort.signal.aborted,false);
 response({ok:true,status:200,json:async()=>fixture()});assert.equal((await first).size,10);assert.equal(h.clients.length,1);await h.manager.pauseAll();
});

test('completed sharing refreshes tickets with one client and verified cache, without redownloading',async()=>{
 let now=1000000,requests=0;
 const h=setup({sharing:true,managerOptions:{now:()=>now},fetch:async()=>({ok:true,status:200,json:async()=>({...fixture(),tracker_path:'/rom-tracker?token=refresh-'+(++requests)})})});
 try{
  await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0].complete);
  for(let round=1;round<=3;round++){
   now+=6*60*60*1000+1;for(const callback of [...h.timers.values()])await callback();
   await until(()=>h.clients.length===round+1 && h.manager.snapshot().entries[0].complete);
   assert.equal(h.clients.filter(client=>!client.destroyed).length,1);assert.equal(h.clients[round].downloaded,0);
   assert.equal(h.clients[round].optionsAdd.announce[0],`wss://retro.example/rom-tracker?token=refresh-${round+1}`);
   assert.equal(h.manager.jobs.size,1);assert.equal(h.timers.size,1);
  }
 }finally{await h.manager.pauseAll();}
 assert.equal(h.timers.size,0);assert.equal(h.locks.size,0);
});

test('pausing while a ticket refresh is pending cannot restart sharing after its late response',async()=>{
 let now=1000000,requests=0,fresh;
 const h=setup({sharing:true,managerOptions:{now:()=>now},fetch:async()=>++requests===1?{ok:true,status:200,json:async()=>fixture()}:new Promise(resolve=>fresh=resolve)});
 await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0].complete);
 now+=6*60*60*1000+1;const callback=[...h.timers.values()][0]();await until(()=>Boolean(fresh));
 await h.manager.pause(h.manager.snapshot().entries[0].id);fresh({ok:true,status:200,json:async()=>fixture()});await callback;await tick();
 assert.equal(h.clients.length,1);assert.equal(h.manager.jobs.size,0);assert.equal(h.timers.size,0);assert.equal(h.manager.snapshot().entries[0].status,'paused');
 assert.equal((await h.manager.getFile({gameId:'game1',revision:'r1'})).size,10);
});

test('changed revision during ticket refresh stops the old session and requests attention',async()=>{
 let now=1000000,requests=0;const h=setup({sharing:true,managerOptions:{now:()=>now},fetch:async()=>({ok:true,status:200,json:async()=>({...fixture(),revision:++requests===1?'r1':'r2'})})});
 await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0].complete);
 now+=6*60*60*1000+1;for(const callback of [...h.timers.values()])await callback();await until(()=>h.manager.jobs.size===0);
 assert.equal(h.clients.length,1);assert.equal(h.clients[0].destroyed,true);assert.equal(h.manager.snapshot().entries[0].error,'revision-changed');assert.equal(h.manager.snapshot().entries[0].complete,false);
});

test('manifest metadata, ICE structure, credential lengths and URL origin are bounded',()=>{
 const h=setup();
 for(const patch of [
  {torrent_base64:'A'.repeat(1024*1024+1)}, {ice_servers:null}, {ice_servers:Array(9).fill({urls:'stun:example.com'})},
  {ice_servers:[{urls:'https://external.example/path'}]}, {ice_servers:[{urls:'turn://external.example'}]},
  {ice_servers:[{urls:'turn:external.example:99999'}]}, {ice_servers:[{urls:'turn:external.example',username:'x'.repeat(1025)}]},
  {ice_servers:[{urls:'turn:external.example',credential:{secret:true}}]}, {ice_servers:[{urls:'stun:example.com',unexpected:true}]},
  {download_url:'//external.example/rom'}, {tracker_path:'https://external.example/tracker'},
 ]) assert.throws(()=>manifestValue({...fixture(),...patch},h.env),{code:'invalid'});
 assert.doesNotThrow(()=>manifestValue({...fixture(),ice_servers:[{urls:['stun:example.com:3478','turns:[::1]:5349?transport=tcp'],username:'user',credential:'secret'}]},h.env));
});

test('temporary ticket refresh failure keeps the existing seed and retries once due again',async()=>{
 let now=1000000,requests=0;
 const h=setup({sharing:true,managerOptions:{now:()=>now},fetch:async()=>{requests++;if(requests===2)throw new TypeError('Network unavailable');return {ok:true,status:200,json:async()=>fixture()};}});
 try{
  await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0].complete);
  now+=6*60*60*1000+1;for(const callback of [...h.timers.values()])await callback();
  assert.equal(h.clients.length,1);assert.equal(h.clients[0].destroyed,false);assert.equal(h.manager.snapshot().entries[0].complete,true);
  for(const callback of [...h.timers.values()])await callback();assert.equal(requests,2);
  now+=60001;for(const callback of [...h.timers.values()])await callback();await until(()=>h.clients.length===2&&h.manager.snapshot().entries[0].complete);
  assert.equal(requests,3);assert.equal(h.clients[1].downloaded,0);
 }finally{await h.manager.pauseAll();}
});

test('a shorter tracker expiry refreshes before expiration rather than waiting six hours',async()=>{
 let now=1000000;const expiry=Math.floor(now/1000)+3600;
 const h=setup({sharing:true,managerOptions:{now:()=>now},fetch:async()=>({ok:true,status:200,json:async()=>({...fixture(),tracker_path:`/rom-tracker?token=${expiry}.signature`})})});
 try{
  await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0].complete);
  assert.equal([...h.manager.jobs.values()][0].refreshAt,expiry*1000-5*60*1000);
 }finally{await h.manager.pauseAll();}
});


function enableOPFS(h, directory=new Directory('browser-storage'), estimate=async()=>({quota:1e9,usage:0})) {
 h.env.navigator.storage={getDirectory:async()=>directory,estimate};return directory;
}
test('first launch creates one persistent OPFS owner without a picker or changing sharing preference',async()=>{
 const h=setup({sharing:false}),opfs=enableOPFS(h);delete h.env.showDirectoryPicker;
 try{
  await h.manager.init();const state=h.manager.snapshot();
  assert.equal(state.directory.storage_kind,'opfs');assert.equal(state.directory.permission,'granted');assert.equal(state.directory.can_choose_directory,false);assert.equal(state.directory.supported,true);assert.equal(state.sharing,false);
  const first=await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',allowP2P:false});
  const next=await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player2',manifestUrl:'/manifest',allowP2P:false});
  assert.equal(first.size,next.size);assert.equal(h.clients.length,1);assert.equal(h.clients[0].downloaded,10);assert.equal(opfs.dirs.size,1);assert.equal(opfs.requests,0);
 }finally{await h.manager.pauseAll();}
});
test('two simultaneous first tabs create and adopt exactly one OPFS registry and owner',async()=>{
 let saved=null,writes=0;const persistence={async load(){await tick();return saved;},async save(value){await tick();saved=value;writes++;}},locks=new Map(),opfs=new Directory('browser-storage');
 const a=setup({persistence,locks}),b=setup({persistence,locks});enableOPFS(a,opfs);enableOPFS(b,opfs);
 await Promise.all([a.manager.init(),b.manager.init()]);
 assert.equal(writes,1);assert.equal(opfs.dirs.size,1);assert.equal(a.manager.state.owner,b.manager.state.owner);assert.equal(locks.size,0);
});
test('legacy external records remain authoritative when permission is denied, without OPFS replacement',async()=>{
 const old=setup();await old.manager.chooseDirectory();await add(old.manager);await until(()=>old.manager.snapshot().entries[0]?.complete);await until(()=>old.manager.jobs.size===0);
 const record=await old.persistence.load();delete record.storage_kind;await old.persistence.save(record);old.directory.permission='denied';
 const h=setup({directory:old.directory,persistence:old.persistence}),opfs=enableOPFS(h);
 await h.manager.init();assert.equal(h.manager.snapshot().directory.storage_kind,'external');assert.equal(h.manager.snapshot().directory.error,'permission');assert.equal(h.manager.snapshot().directory.permission,'denied');assert.equal(opfs.dirs.size,0);assert.equal(h.clients.length,0);assert.equal(old.directory.requests,0);
 await assert.rejects(add(h.manager),{code:'permission'});await h.manager.reconnect();await until(()=>h.manager.snapshot().entries[0]?.complete);assert.equal(h.clients[0].downloaded,0);await h.manager.pauseAll();
});
test('HTTP-only opt out remains in the OPFS registry after a full manager reload',async()=>{
 const a=setup({sharing:true}),opfs=enableOPFS(a);
 await a.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',allowP2P:false});await until(()=>a.manager.jobs.size===0);
 assert.equal((await a.persistence.load()).entries[0].allowP2P,false);
 const b=setup({sharing:true,persistence:a.persistence});enableOPFS(b,opfs);await b.manager.init();await until(()=>b.manager.snapshot().entries[0]?.complete);
 assert.equal(b.clients[0].downloaded,0);assert.equal(b.clients[0].options.tracker,false);assert.equal(b.manager.snapshot().sharing,true);await b.manager.pauseAll();
});
test('OPFS quota failure is specific and occurs before torrent starts; a complete file remains reusable',async()=>{
 const h=setup();enableOPFS(h,new Directory('browser-storage'),async()=>({quota:5,usage:0}));
 try{await assert.rejects(h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'}),{code:'quota'});assert.equal(h.clients.length,0);}
 finally{await h.manager.pauseAll();}
 const a=setup();const opfs=enableOPFS(a);await a.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>a.manager.jobs.size===0);
 const b=setup({persistence:a.persistence});enableOPFS(b,opfs,async()=>({quota:10,usage:10}));
 try{await b.manager.init();await until(()=>b.manager.snapshot().entries[0]?.complete);assert.equal(b.clients[0].downloaded,0);}
 finally{await b.manager.pauseAll();}
});
test('unsupported storage and OPFS initialization failure never create an empty replacement registry',async()=>{
 const h=setup();delete h.env.showDirectoryPicker;await h.manager.init();assert.equal(h.manager.snapshot().directory.error,'unsupported');await assert.rejects(add(h.manager),{code:'unsupported'});assert.equal(await h.persistence.load(),null);
 const denied=setup();denied.env.navigator.storage={getDirectory:async()=>{throw Object.assign(new Error(),{name:'QuotaExceededError'});}};await denied.manager.init();assert.equal(denied.manager.snapshot().directory.error,'quota');assert.equal(await denied.persistence.load(),null);
});
test('switching directories preserves old files and metadata and selecting the original reuses its owner',async()=>{
 const h=setup(),externalA=h.directory,externalB=new Directory('second');enableOPFS(h);await h.manager.chooseDirectory();
 await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>h.manager.jobs.size===0);const owner=h.manager.state.owner;
 const final=externalA.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash).files.get('demo.gba');
 h.env.showDirectoryPicker=async()=>externalB;await h.manager.chooseDirectory();assert.equal(h.manager.state.storage_kind,'external');assert.equal(final.bytes.length,10);assert.equal(h.manager.entries.size,0);
 h.env.showDirectoryPicker=async()=>externalA;await h.manager.chooseDirectory();assert.equal(h.manager.state.owner,owner);assert.equal(h.manager.entries.size,1);assert.equal(externalA.dirs.size,1);
 try{await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player2',manifestUrl:'/manifest'});assert.equal(h.clients.at(-1).downloaded,0);assert.equal(externalA.removed.length,0);assert.equal(externalB.removed.length,0);}
 finally{await h.manager.pauseAll();}
});
test('deleted owned OPFS root can be restored without touching unrelated files; bad existing marker is blocked',async()=>{
 const a=setup(),opfs=enableOPFS(a);await a.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>a.manager.jobs.size===0);
 const name=a.manager.state.subdirectory;opfs.dirs.delete(name);opfs.files.set('untouched.txt',{bytes:new Uint8Array([4]),mtime:1});
 const b=setup({persistence:a.persistence});enableOPFS(b,opfs);await b.manager.init();await until(()=>b.manager.snapshot().entries[0]?.complete);assert.equal(b.clients[0].downloaded,10);assert.ok(opfs.files.has('untouched.txt'));await until(()=>b.manager.jobs.size===0);
 const marker=opfs.dirs.get(name).files.get('.retroone-owner.json');marker.bytes=new TextEncoder().encode(JSON.stringify({version:1,owner:'someone-else'}));
 const c=setup({persistence:a.persistence});enableOPFS(c,opfs);await c.manager.init();assert.equal(c.manager.snapshot().directory.error,'ownership');assert.equal(c.clients.length,0);assert.equal(opfs.dirs.size,1);assert.equal(JSON.parse(new TextDecoder().decode(marker.bytes)).owner,'someone-else');
});


test('a stale tab returning to the same owner cannot erase intervening location archives',async()=>{
 const a=setup();await a.manager.chooseDirectory();
 const b=setup({directory:a.directory,persistence:a.persistence,locks:a.locks});await b.manager.init();
 const other=new Directory('other');b.env.showDirectoryPicker=async()=>other;await b.manager.chooseDirectory();
 b.env.showDirectoryPicker=async()=>a.directory;await b.manager.chooseDirectory();assert.equal(b.manager.state.owner,a.manager.state.owner);
 a.manager.entries.set('kept',{id:'kept',paused:true});await a.manager.persist();
 assert.ok((await a.persistence.load()).previous_locations.some(location=>location.directory===other));
});


test('selecting the current directory again leaves its active sharing task and verified file intact',async()=>{
 const h=setup({sharing:true});try{
  await h.manager.chooseDirectory();await add(h.manager);await until(()=>h.manager.snapshot().entries[0]?.complete);
  const job=[...h.manager.jobs.values()][0];await h.manager.chooseDirectory();assert.equal([...h.manager.jobs.values()][0],job);assert.equal(job.abort.signal.aborted,false);assert.equal(h.clients[0].destroyed,false);assert.equal(h.clients.length,1);
 }finally{await h.manager.pauseAll();}
});


test('useBrowserStorage restores archived OPFS and returning to external restores its exact file without copying',async()=>{
 const h=setup(),external=h.directory,opfs=enableOPFS(h);
 const args={gameId:'game1',profileId:'player1',manifestUrl:'/manifest',allowP2P:false};
 try{
  await h.manager.ensureFile(fixture(),args);await until(()=>h.manager.jobs.size===0);const opfsOwner=h.manager.state.owner;
  const opfsFile=opfs.dirs.get(h.manager.state.subdirectory).dirs.get('rom-'+fixture().info_hash).files.get('demo.gba');
  await h.manager.chooseDirectory();await h.manager.ensureFile(fixture(),args);await until(()=>h.manager.jobs.size===0);const externalOwner=h.manager.state.owner;
  const externalFile=external.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash).files.get('demo.gba');
  const result=await h.manager.useBrowserStorage();assert.equal(result.directory.storage_kind,'opfs');assert.equal(result.directory.can_use_browser_storage,true);assert.equal(h.manager.state.owner,opfsOwner);
  await h.manager.ensureFile(fixture(),args);await until(()=>h.manager.jobs.size===0);assert.equal(h.clients.at(-1).downloaded,0);
  await h.manager.chooseDirectory();assert.equal(h.manager.state.owner,externalOwner);await h.manager.ensureFile(fixture(),args);await until(()=>h.manager.jobs.size===0);assert.equal(h.clients.at(-1).downloaded,0);
  assert.equal(opfsFile,opfs.dirs.get('retroone-rom-library').dirs.get('rom-'+fixture().info_hash).files.get('demo.gba'));
  assert.equal(externalFile,external.dirs.get('RetroOne').dirs.get('rom-'+fixture().info_hash).files.get('demo.gba'));assert.equal(opfs.removed.length,0);assert.equal(external.removed.length,0);
 }finally{await h.manager.pauseAll();}
});
test('useBrowserStorage creates a location for a legacy external-only registry and keeps that registry archived',async()=>{
 const h=setup();await h.manager.chooseDirectory();const oldOwner=h.manager.state.owner;const opfs=enableOPFS(h);
 await h.manager.useBrowserStorage();assert.equal(h.manager.state.storage_kind,'opfs');assert.equal(opfs.dirs.size,1);assert.ok(h.manager.state.previous_locations.some(value=>value.owner===oldOwner));
 const owner=h.manager.state.owner;await h.manager.useBrowserStorage();assert.equal(h.manager.state.owner,owner);assert.equal(opfs.dirs.size,1);
});
test('useBrowserStorage fails with existing codes and does not change the external registry when unsupported or denied',async()=>{
 const h=setup();await h.manager.chooseDirectory();const owner=h.manager.state.owner;
 await assert.rejects(h.manager.useBrowserStorage(),{code:'unsupported'});assert.equal((await h.persistence.load()).owner,owner);
 h.env.navigator.storage={getDirectory:async()=>{throw Object.assign(new Error(),{name:'QuotaExceededError'});}};
 await assert.rejects(h.manager.useBrowserStorage(),{code:'quota'});assert.equal((await h.persistence.load()).owner,owner);assert.equal(h.manager.state.storage_kind,'external');
});


test('real DOMException numeric legacy codes map to stable quota and permission errors',async()=>{
 for(const [name,code] of [['QuotaExceededError','quota'],['SecurityError','permission']]){
  const h=setup();h.env.navigator.storage={getDirectory:async()=>{throw new DOMException('browser storage unavailable',name);}};
  await h.manager.init();assert.equal(h.manager.snapshot().directory.error,code);assert.equal(h.manager.snapshot().directory.temporary_fallback,false);
 }
});
test('switching location rejects without cancelling a launch awaiting metadata',async()=>{
 let response;const h=setup({fetch:()=>new Promise(resolve=>response=resolve)});
 await h.manager.chooseDirectory();const pending=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});
 await until(()=>Boolean(response));const owner=h.manager.state.owner,epoch=h.manager.locationEpoch,job=[...h.manager.jobs.values()][0];
 h.env.showDirectoryPicker=async()=>new Directory('replacement');await assert.rejects(h.manager.chooseDirectory(),{code:'location-active'});
 assert.equal(h.manager.state.owner,owner);assert.equal(h.manager.locationEpoch,epoch);assert.equal(job.abort.signal.aborted,false);assert.equal(h.manager.jobs.size,1);
 response({ok:true,status:200,json:async()=>fixture()});assert.equal((await pending).size,10);await h.manager.pauseAll();assert.equal(h.manager.listeners.size,0);
});
test('an explicit location switch can adopt another tabs location after stopping the stale owner job',async()=>{
 const a=setup({sharing:true});await a.manager.chooseDirectory();await a.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>!a.manager.snapshot().directory.location_change_blocked);
 const oldRoot=a.manager.root,b=setup({directory:a.directory,persistence:a.persistence,locks:a.locks});await b.manager.init();await until(()=>b.manager.snapshot().entries[0]?.status==='busy'&&b.manager.jobs.size===0);const second=new Directory('second');b.env.showDirectoryPicker=async()=>second;await b.manager.chooseDirectory();
 a.env.showDirectoryPicker=async()=>second;await a.manager.chooseDirectory();assert.equal(a.manager.state.owner,b.manager.state.owner);assert.equal(a.manager.state.directory,second);assert.equal(a.clients[0].destroyed,true);assert.ok(oldRoot.dirs.get('rom-'+fixture().info_hash).files.has('demo.gba'));assert.equal(a.manager.jobs.size,0);
});
test('reselecting the same directory does not cancel a launch awaiting metadata',async()=>{
 let response;const h=setup({fetch:()=>new Promise(resolve=>response=resolve)});await h.manager.chooseDirectory();
 const pending=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>Boolean(response));await h.manager.chooseDirectory();
 response({ok:true,status:200,json:async()=>fixture()});assert.equal((await pending).size,10);await h.manager.pauseAll();
});
test('missing persistence capability permits temporary small-ROM loading only without an existing external record',async()=>{
 const empty=setup();delete empty.options;delete empty.env.showDirectoryPicker;empty.manager.options={};delete empty.env.indexedDB;
 await empty.manager.init();assert.equal(empty.manager.snapshot().directory.temporary_fallback,true);assert.equal(empty.manager.snapshot().directory.error,'unsupported');
 const old=setup();await old.manager.chooseDirectory();old.directory.permission='prompt';
 const locked=setup({persistence:old.persistence});locked.manager.options={};locked.manager.database=async()=>old.persistence;delete locked.env.navigator.locks;
 await locked.manager.init();assert.equal(locked.manager.snapshot().directory.storage_kind,'external');assert.equal(locked.manager.snapshot().directory.temporary_fallback,false);
 const unreadable=setup();unreadable.manager.options={};delete unreadable.env.navigator.locks;unreadable.manager.database=async()=>({load:async()=>{throw new DOMException('blocked','SecurityError');}});
 await unreadable.manager.init();assert.equal(unreadable.manager.snapshot().directory.temporary_fallback,false);assert.equal(unreadable.manager.snapshot().directory.error,'permission');
});


test('returning to an authorized cached location re-verifies automatically instead of waiting for permission',async()=>{
 const h=setup({sharing:false});enableOPFS(h);await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',allowP2P:false});await until(()=>h.manager.jobs.size===0);
 await h.manager.chooseDirectory();const returned=await h.manager.useBrowserStorage();assert.equal(returned.directory.permission,'granted');assert.notEqual(returned.entries[0].status,'permission');
 await until(()=>h.manager.snapshot().entries[0].complete);assert.equal(h.clients.at(-1).downloaded,0);assert.equal(h.manager.snapshot().entries[0].status,'ready');await h.manager.pauseAll();
});
test('switching storage pauses its current job without turning automatic resume intent into a manual pause',async()=>{
 const h=setup({sharing:true});enableOPFS(h);await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',allowP2P:true});
 assert.equal(h.manager.jobs.size,1);await h.manager.chooseDirectory();await h.manager.useBrowserStorage();await until(()=>h.manager.snapshot().entries[0].complete);
 assert.equal(h.manager.snapshot().entries[0].paused,false);assert.equal(h.clients.at(-1).downloaded,0);assert.equal(h.manager.snapshot().entries[0].status,'sharing');await h.manager.pauseAll();
});
test('returning to a location preserves a users manual pause and does not start verification or sharing',async()=>{
 const h=setup({sharing:true});enableOPFS(h);await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});
 await h.manager.pause(h.manager.snapshot().entries[0].id);const clientCount=h.clients.length;await h.manager.chooseDirectory();await h.manager.useBrowserStorage();await tick();
 assert.equal(h.manager.snapshot().entries[0].paused,true);assert.equal(h.manager.snapshot().entries[0].status,'paused');assert.equal(h.manager.snapshot().entries[0].cachedComplete,true);assert.equal(h.manager.jobs.size,0);assert.equal(h.clients.length,clientCount);
});

test('automatic OPFS asks for a choice, and explicitly reselecting it confirms persistently',async()=>{
 const h=setup();enableOPFS(h);await h.manager.init();
 assert.equal(h.manager.snapshot().directory.selection_required,true);assert.equal((await h.persistence.load()).user_selected,false);
 const owner=h.manager.state.owner;await h.manager.useBrowserStorage();
 assert.equal(h.manager.state.owner,owner);assert.equal(h.manager.snapshot().directory.selection_required,false);assert.equal((await h.persistence.load()).user_selected,true);
 const next=setup({persistence:h.persistence,locks:h.locks});enableOPFS(next,h.env.navigator.storage.getDirectory&&await h.env.navigator.storage.getDirectory());await next.manager.init();assert.equal(next.manager.snapshot().directory.selection_required,false);
});
test('legacy external is confirmed but legacy automatic OPFS still requests an explicit choice',async()=>{
 for(const kind of ['external','opfs']){
  const first=setup();if(kind==='opfs')enableOPFS(first);else await first.manager.chooseDirectory();await first.manager.init();
  const saved=await first.persistence.load();delete saved.user_selected;await first.persistence.save(saved);
  const next=setup({persistence:first.persistence,locks:first.locks});if(kind==='opfs')enableOPFS(next,await first.env.navigator.storage.getDirectory());await next.manager.init();
  assert.equal(next.manager.snapshot().directory.selection_required,kind==='opfs');
 }
});
test('before any download a newly auto-created OPFS can be replaced by a fixed folder',async()=>{
 const h=setup();enableOPFS(h);await h.manager.init();const owner=h.manager.state.owner;
 assert.equal(h.manager.snapshot().directory.location_change_blocked,false);await h.manager.chooseDirectory();
 assert.equal(h.manager.state.storage_kind,'external');assert.equal(h.manager.state.user_selected,true);assert.equal(h.manager.entries.size,0);
 assert.equal(h.manager.state.previous_locations.find(value=>value.owner===owner).user_selected,false);
 await h.manager.useBrowserStorage();assert.equal(h.manager.state.owner,owner);assert.equal(h.manager.state.user_selected,true);
 await h.manager.chooseDirectory();assert.equal(h.manager.state.previous_locations.find(value=>value.owner===owner).user_selected,true);
});
test('a stale tab metadata save cannot undo another tabs explicit location confirmation',async()=>{
 const a=setup();const opfs=enableOPFS(a);await a.manager.init();const b=setup({persistence:a.persistence,locks:a.locks});enableOPFS(b,opfs);await b.manager.init();
 await b.manager.useBrowserStorage();assert.equal(a.manager.state.user_selected,false);await a.manager.persist();
 assert.equal((await a.persistence.load()).user_selected,true);assert.equal(a.manager.snapshot().directory.selection_required,false);
});
test('invalid confirmation metadata fails closed instead of silently changing a legacy choice',async()=>{
 const h=setup();await h.manager.chooseDirectory();const saved=await h.persistence.load();saved.user_selected='yes';await h.persistence.save(saved);
 const next=setup({persistence:h.persistence});await next.manager.init();assert.equal(next.manager.snapshot().directory.error,'ownership');
});
test('paused partial pieces block a different folder and resume only missing bytes in the original',async()=>{
 let unblock;const gate=new Promise(resolve=>unblock=resolve);let held=false;
 const h=setup({managerOptions:{afterPiece:async(index,client)=>{if(index===0&&!held){held=true;await gate;}}}});
 await h.manager.chooseDirectory();const entry=await add(h.manager);await until(()=>held);const owner=h.manager.state.owner;
 const status=h.manager.snapshot().entries[0].status;assert.equal(status,'verifying');await h.manager.chooseDirectory();assert.equal(h.manager.snapshot().entries[0].status,status);
 await h.manager.pause(entry.id);const root=h.manager.root,part=root.dirs.get('rom-'+fixture().info_hash);const before=[...part.files.keys()];
 assert.equal(h.manager.snapshot().directory.location_change_blocked,true);const other=new Directory('other');h.env.showDirectoryPicker=async()=>other;
 await assert.rejects(h.manager.chooseDirectory(),{code:'location-active'});assert.equal(h.manager.state.owner,owner);assert.equal(other.dirs.size,0);assert.deepEqual([...part.files.keys()],before);assert.equal(h.manager.snapshot().entries[0].paused,true);
 h.env.showDirectoryPicker=async()=>h.directory;await h.manager.chooseDirectory();assert.equal(h.manager.snapshot().entries[0].paused,true);
 unblock();await h.manager.resume(entry.id);await until(()=>h.manager.snapshot().entries[0].complete);await until(()=>h.manager.jobs.size===0);
 assert.equal(h.clients[0].downloaded,4);assert.equal(h.clients[1].downloaded,6);assert.equal((await h.manager.getFile({gameId:'game1',revision:'r1'})).size,10);
 h.env.showDirectoryPicker=async()=>other;await h.manager.chooseDirectory();assert.equal(h.manager.state.directory,other);
});
test('another tab cannot switch away during verification even when its index status is stale',async()=>{
 let release;const gate=new Promise(resolve=>release=resolve);let waiting=false;
 const a=setup({managerOptions:{afterPiece:async index=>{if(index===0&&!waiting){waiting=true;await gate;}}}});await a.manager.chooseDirectory();
 const b=setup({directory:a.directory,persistence:a.persistence,locks:a.locks});await b.manager.init();const task=await add(a.manager);await until(()=>waiting);
 // Other tabs may persist a cache/busy snapshot while one verifier is active.
 const stale=await a.persistence.load();stale.entries=stale.entries.map(entry=>({...entry,status:'busy',cachedComplete:true,complete:false}));await a.persistence.save(stale);
 const owner=a.manager.state.owner;b.env.showDirectoryPicker=async()=>new Directory('different');await assert.rejects(b.manager.chooseDirectory(),{code:'location-active'});
 assert.equal((await a.persistence.load()).owner,owner);assert.equal(a.manager.jobs.get(task.id).abort.signal.aborted,false);release();await until(()=>a.manager.snapshot().entries[0].complete);await until(()=>a.manager.jobs.size===0);
 await b.manager.chooseDirectory();assert.notEqual(b.manager.state.owner,owner);
});
test('a queued reservation and simultaneous cross-tab switch cannot discard the waiting launch',async()=>{
 const a=setup();await a.manager.chooseDirectory();const b=setup({directory:a.directory,persistence:a.persistence,locks:a.locks});await b.manager.init();
 let unlock;const lock=new Promise(resolve=>unlock=resolve);a.locks.set('retroone-rom-library-state',lock);
 const pending=a.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await tick();
 b.env.showDirectoryPicker=async()=>new Directory('new');const switching=b.manager.chooseDirectory();await tick();a.locks.delete('retroone-rom-library-state');unlock();
 await assert.rejects(switching,{code:'location-active'});assert.equal((await pending).size,10);await a.manager.pauseAll();
});
test('confirming current OPFS during manifest preparation does not cancel or replace the download',async()=>{
 let response;const h=setup({fetch:()=>new Promise(resolve=>response=resolve)});enableOPFS(h);await h.manager.init();
 const pending=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>Boolean(response));const job=[...h.manager.jobs.values()][0],owner=h.manager.state.owner;
 await h.manager.useBrowserStorage();assert.equal(h.manager.state.owner,owner);assert.equal(h.manager.snapshot().directory.selection_required,false);assert.equal(job.abort.signal.aborted,false);
 response({ok:true,status:200,json:async()=>fixture()});assert.equal((await pending).size,10);await h.manager.pauseAll();
});


test('a refused final location check restores completed sharing without resuming pauses or partial tasks',async()=>{
 const h=setup({sharing:true});await h.manager.chooseDirectory();await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest'});await until(()=>!h.manager.snapshot().directory.location_change_blocked);
 const owner=h.manager.state.owner,original=h.manager.snapshot().entries[0].id;
 const pauseAll=h.manager.pauseAll.bind(h.manager);let injected=false;
 h.manager.pauseAll=async options=>{
  await pauseAll(options);
  if(injected)return;injected=true;
  // Another tab reserves unfinished work between the two switch checks.
  const saved=await h.persistence.load();saved.entries.push({id:'remote-queued',gameId:'other',status:'queued',complete:false,cachedComplete:false,paused:false});await h.persistence.save(saved);
  h.manager.entries.set('manual-complete',{id:'manual-complete',gameId:'manual',cachedComplete:true,complete:true,status:'paused',paused:true});
  h.manager.entries.set('manual-partial',{id:'manual-partial',gameId:'partial',cachedComplete:false,complete:false,status:'paused',paused:true});
 };
 h.env.showDirectoryPicker=async()=>new Directory('different');await assert.rejects(h.manager.chooseDirectory(),{code:'location-active'});
 await until(()=>h.clients.length===2&&h.manager.entries.get(original).status==='sharing'&&!h.manager.snapshot().entries.find(x=>x.id===original).error);
 assert.equal(h.manager.state.owner,owner);assert.equal(h.clients[1].downloaded,0);assert.equal(h.manager.entries.get('manual-complete').paused,true);assert.equal(h.manager.entries.get('manual-partial').paused,true);assert.equal(h.manager.jobs.size,1);
 assert.ok((await h.persistence.load()).entries.some(entry=>entry.id==='remote-queued'));await pauseAll();
});


test('detaching the launch waiter keeps its own partial transfer alive and reusable across players',async()=>{
 let release;const gate=new Promise(resolve=>release=resolve);let held=false;
 const h=setup({managerOptions:{afterPiece:async index=>{if(index===0&&!held){held=true;await gate;}}}});await h.manager.chooseDirectory();
 const signal=new AbortController();const waiting=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',signal:signal.signal});
 await until(()=>held);const entry=h.manager.snapshot().entries[0],job=h.manager.jobs.get(entry.id);assert.equal(h.clients[0].downloaded,4);
 signal.abort('retroone:detach');await assert.rejects(waiting,{name:'AbortError'});
 assert.equal(h.manager.listeners.size,0);assert.equal(h.manager.jobs.get(entry.id),job);assert.equal(job.abort.signal.aborted,false);assert.equal(h.manager.entries.get(entry.id).paused,false);assert.equal(h.clients[0].destroyed,false);
 release();await until(()=>h.manager.snapshot().entries[0].complete);await until(()=>h.manager.jobs.size===0);
 const file=await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player2',manifestUrl:'/p/player2/manifest'});
 assert.equal(file.size,10);assert.equal(h.clients.length,1);assert.equal(h.clients[0].downloaded,10);assert.deepEqual([...new Uint8Array(await file.slice(0,4).arrayBuffer()),...new Uint8Array(await file.slice(4,8).arrayBuffer()),...new Uint8Array(await file.slice(8,10).arrayBuffer())],[1,2,3,4,5,6,7,8,9,10]);
});
test('explicit cancellation still pauses the launch-owned partial download and preserves resumable pieces',async()=>{
 let release;const gate=new Promise(resolve=>release=resolve);let held=false;
 const h=setup({managerOptions:{afterPiece:async index=>{if(index===0&&!held){held=true;await gate;}}}});await h.manager.chooseDirectory();
 const signal=new AbortController();const waiting=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',signal:signal.signal});
 await until(()=>held);const id=h.manager.snapshot().entries[0].id;signal.abort();await assert.rejects(waiting,{name:'AbortError'});await until(()=>h.manager.jobs.size===0);
 assert.equal(h.manager.listeners.size,0);assert.equal(h.manager.entries.get(id).paused,true);assert.equal(h.clients[0].destroyed,true);assert.equal(h.clients[0].downloaded,4);
 release();await h.manager.resume(id);await until(()=>h.manager.snapshot().entries[0].complete);await until(()=>h.manager.jobs.size===0);
 assert.equal(h.clients[1].downloaded,6);assert.equal((await h.manager.getFile({gameId:'game1',revision:'r1'})).size,10);
});
test('another player cannot cancel the detached owners ongoing background download',async()=>{
 let response;const h=setup({fetch:()=>new Promise(resolve=>response=resolve)});await h.manager.chooseDirectory();
 const ownerSignal=new AbortController(),otherSignal=new AbortController();
 const owner=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',signal:ownerSignal.signal});await until(()=>Boolean(response));
 ownerSignal.abort('retroone:detach');await assert.rejects(owner,{name:'AbortError'});const job=[...h.manager.jobs.values()][0];
 const other=h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player2',manifestUrl:'/p/player2/manifest',signal:otherSignal.signal});await tick();otherSignal.abort();await assert.rejects(other,{name:'AbortError'});
 assert.equal(job.abort.signal.aborted,false);assert.equal(h.manager.listeners.size,0);response({ok:true,status:200,json:async()=>fixture()});await until(()=>h.manager.snapshot().entries[0].complete);await until(()=>h.manager.jobs.size===0);assert.equal(h.clients[0].downloaded,10);
});
test('aborting an already completed launch never stops its background seeding',async()=>{
 const h=setup({sharing:true});await h.manager.chooseDirectory();const signal=new AbortController();
 await h.manager.ensureFile(fixture(),{gameId:'game1',profileId:'player1',manifestUrl:'/manifest',signal:signal.signal});await until(()=>!h.manager.snapshot().directory.location_change_blocked);
 const job=[...h.manager.jobs.values()][0];signal.abort();await tick();assert.equal(job.abort.signal.aborted,false);assert.equal(h.clients[0].destroyed,false);assert.equal(h.manager.snapshot().entries[0].status,'sharing');assert.equal(h.manager.listeners.size,0);await h.manager.pauseAll();
});
