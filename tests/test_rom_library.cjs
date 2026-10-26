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
  navigator:{locks:{async request(name,options,callback){if(locks.has(name))return callback(null);locks.set(name,true);try{return await callback({name});}finally{locks.delete(name);}}}},
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
 first.manager.entries.set('one',{id:'one',status:'paused',paused:true});await first.manager.persist();
 second.manager.entries.set('two',{id:'two',status:'paused',paused:true});await second.manager.persist();
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
