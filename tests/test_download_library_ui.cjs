const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function install(ctx, ...names) {
  for (const name of names) {
    const match = source.match(new RegExp(`^  (?:async )?function ${name}\\([\\s\\S]*?^  }`, 'm'));
    assert.ok(match, name); vm.runInContext(match[0], ctx);
  }
}
function harness() {
  const game = {id:'large',title:'Large ROM',mode:'unavailable'};
  const calls=[], pending=[],messages=[],added=[],order=[];
  const ctx=vm.createContext({
    state:{profile:{id:'player1',base_path:'/p/player1'},profileEpoch:1,detailOpen:true,selected:game,downloadOptions:null,downloadBusy:false,
      downloadSnapshot:{directory:{supported:true,permission:'prompt',name:'Games'}},downloadPending:null,downloadEntryBusy:new Set()},
    api:url=>{calls.push(url);return new Promise((resolve,reject)=>pending.push({resolve,reject}));},
    renderDownloadOption:()=>{},renderDownloads:()=>order.push('render'),showDownloads:()=>order.push('show'),downloadMessage:(...args)=>messages.push(args),
    receiveDownloadSnapshot:s=>ctx.state.downloadSnapshot=s,errorMessage:e=>e.message,t:s=>s,notify:(...args)=>messages.push(args),
    localURL:url=>typeof url==='string'&&url.startsWith('/p/player1/api/games/')?url:'',
    window:{RetroRomLibrary:{add:async arg=>added.push(arg),snapshot:()=>ctx.state.downloadSnapshot}},
  });
  install(ctx,'loadDownloadOptions','downloadGame','downloadDirectoryReady','chooseDownloadDirectory','flushPendingDownload');
  return {ctx,game,calls,pending,messages,added,order};
}
async function available(h) {
  const read=h.ctx.loadDownloadOptions(h.game);
  h.pending.at(-1).resolve({available:true,size_bytes:2**41,manifest_url:'/p/player1/api/games/large/download-manifest'});await read;
}
test('download availability is independent from emulator compatibility and accepts a multi-terabyte size', async()=>{
  const h=harness();await available(h);
  await h.ctx.downloadGame(h.game);
  assert.deepEqual(h.calls,['/api/games/large/download-options']);
  assert.equal(h.ctx.state.downloadOptions.data.size_bytes,2**41);
  assert.equal(h.added.length,0,'a missing folder must not trigger an in-memory download');
  assert.equal(h.ctx.state.downloadPending.gameId,'large');
  assert.deepEqual(h.order,['show']);
});
test('download information is detail-only, deduplicated, and ignores a late player response',async()=>{
  const h=harness();h.ctx.state.detailOpen=false;await h.ctx.loadDownloadOptions(h.game);assert.equal(h.calls.length,0);
  h.ctx.state.detailOpen=true;const read=h.ctx.loadDownloadOptions(h.game);await h.ctx.loadDownloadOptions(h.game);assert.equal(h.calls.length,1);
  h.ctx.state.profileEpoch=2;h.pending[0].resolve({available:true});await read;
  assert.equal(h.ctx.state.downloadOptions.data,null);
});
test('directory choice invokes the native picker before rendering or awaiting, then adds the pending download',async()=>{
  const h=harness();await available(h);await h.ctx.downloadGame(h.game);h.order.length=0;
  let resolve;h.ctx.window.RetroRomLibrary.chooseDirectory=()=>{h.order.push('picker');return new Promise(r=>resolve=r);};
  const choose=h.ctx.chooseDownloadDirectory();assert.equal(h.order[0],'picker');assert.equal(h.added.length,0);
  h.ctx.state.downloadSnapshot.directory.permission='granted';resolve();await choose;
  assert.deepEqual(plain(h.added),[{gameId:'large',title:'Large ROM',profileId:'player1',manifestUrl:'/p/player1/api/games/large/download-manifest'}]);
  assert.equal(h.ctx.state.downloadPending,null);assert.equal(h.ctx.state.downloadBusy,false);
});
test('permission reconnect is user-initiated and a cancelled prompt never starts the download',async()=>{
  const h=harness();await available(h);await h.ctx.downloadGame(h.game);
  h.ctx.window.RetroRomLibrary.reconnect=()=>{h.order.push('reconnect');return Promise.reject(Object.assign(new Error('cancelled'),{name:'AbortError'}));};
  await h.ctx.chooseDownloadDirectory(true);
  assert.ok(h.order.includes('reconnect'));assert.equal(h.added.length,0);
  assert.equal(h.messages.filter(x=>x[1]===true).length,0);
});
test('switching players while choosing a directory never enqueues the prior player request',async()=>{
  const h=harness();await available(h);await h.ctx.downloadGame(h.game);
  let resolve;h.ctx.window.RetroRomLibrary.chooseDirectory=()=>new Promise(r=>resolve=r);
  const choose=h.ctx.chooseDownloadDirectory();h.ctx.state.profileEpoch++;h.ctx.state.profile={id:'player2'};
  h.ctx.state.downloadSnapshot.directory.permission='granted';resolve();await choose;assert.equal(h.added.length,0);
});
test('authorized directories add directly without launching a game or writing play history',async()=>{
  const h=harness();await available(h);h.ctx.state.downloadSnapshot.directory.permission='granted';await h.ctx.downloadGame(h.game);
  assert.equal(h.added.length,1);assert.equal(h.ctx.state.downloadPending,null);
  assert.equal(h.calls.length,1);assert.doesNotMatch(h.calls.join(),/launch|played/);
});
test('unavailable or unsafe download endpoints never reach the manager',async()=>{
  for(const data of [{available:false,reason:'multiple files'},{available:true,manifest_url:'https://other.invalid/rom'}]) {
    const h=harness();const read=h.ctx.loadDownloadOptions(h.game);h.pending[0].resolve(data);await read;
    h.ctx.state.downloadSnapshot.directory.permission='granted';await h.ctx.downloadGame(h.game);assert.equal(h.added.length,0);
  }
});
test('the manager and advanced launch settings use one device sharing preference',async()=>{
  const saved=[],manager=[];const ctx=vm.createContext({state:{browserP2P:true},remember:(...args)=>saved.push(args),
    window:{RetroRomLibrary:{setSharing:value=>manager.push(value)}},renderDownloads:()=>{},downloadMessage:()=>{},errorMessage:e=>e.message});
  install(ctx,'setBrowserP2PPreference');ctx.setBrowserP2PPreference(false);
  assert.deepEqual(manager,[false]);assert.deepEqual(saved,[['retroone:browser-p2p','false']]);
  const rendered=[];ctx.state.detailOpen=true;ctx.state.selected={id:'g'};ctx.renderLaunchChoices=g=>rendered.push(g.id);ctx.updateDeviceCacheBadges=()=>{};
  install(ctx,'receiveDownloadSnapshot');ctx.receiveDownloadSnapshot({sharing:true,directory:{supported:true,permission:'granted'},entries:[]});
  assert.equal(ctx.state.browserP2P,true);assert.deepEqual(rendered,['g']);assert.equal(manager.length,1,'snapshot updates do not loop back into the manager');
});
test('paused downloads, paused completed files, active sharing and completion have distinct labels',()=>{
  const ctx=vm.createContext({t:s=>s});install(ctx,'downloadEntryStatus');
  assert.equal(ctx.downloadEntryStatus({status:'paused',complete:false}),'下载已暂停');
  assert.equal(ctx.downloadEntryStatus({status:'paused',complete:true}),'分享已暂停');
  assert.equal(ctx.downloadEntryStatus({status:'sharing',complete:true}),'正在分享');
  assert.equal(ctx.downloadEntryStatus({status:'complete',complete:true}),'已下载');
});

test('device cache badges require current verification and permission, retaining only an honest historical marker',()=>{
  const ctx=vm.createContext({});install(ctx,'deviceCacheStates');
  const entries=[
    {gameId:'complete',profileId:'player2',status:'sharing',complete:true,cachedComplete:true},
    {gameId:'unchecked',status:'ready',complete:false,cachedComplete:true},
    {gameId:'verifying',status:'verifying',complete:false,cachedComplete:true},
    {gameId:'partial',status:'downloading',complete:false},
    {gameId:'queued',status:'queued',complete:false},
    {gameId:'file-permission',status:'permission',complete:false,cachedComplete:true},
  ];
  const active=ctx.deviceCacheStates({directory:{permission:'granted'},entries});
  assert.equal(active.get('complete'),'ready');assert.equal(active.has('unchecked'),false);
  assert.equal(active.get('verifying'),'checking');assert.equal(active.get('partial'),'downloading');assert.equal(active.get('queued'),'downloading');
  assert.equal(active.get('file-permission'),'permission');
  const restored=ctx.deviceCacheStates({directory:{permission:'prompt'},entries:entries.map(e=>({...e,status:'permission',complete:false}))});
  assert.equal(restored.get('complete'),'permission');assert.equal(restored.get('verifying'),'permission');assert.equal(restored.has('partial'),false);
  assert.equal(ctx.deviceCacheStates({directory:{permission:'granted'},entries:[]}).size,0);
});

test('game files are aggregated across household profiles without downgrading a usable cache',()=>{
  const ctx=vm.createContext({});install(ctx,'deviceCacheStates');
  const entries=[{gameId:'same',profileId:'player2',status:'ready',complete:true},{gameId:'same',profileId:'player1',status:'downloading',complete:false}];
  const result=ctx.deviceCacheStates({directory:{permission:'granted'},entries});
  assert.equal(result.size,1);assert.equal(result.get('same'),'ready');
});

test('progress-only subscription updates do not touch DOM or replace game buttons; status changes only alter affected badges',()=>{
  let scans=0,writes=0;const badge=id=>({dataset:{deviceCacheGame:id,cacheLayout:'card'},setAttribute:()=>{},set innerHTML(value){writes++;this.markup=value;}});
  const a=badge('a'),b=badge('b'),ctx=vm.createContext({state:{deviceCacheBadges:new Map()},
    document:{querySelectorAll:selector=>{assert.equal(selector,'[data-device-cache-game]');scans++;return [a,b];}},
    deviceCacheLabel:kind=>kind||'',deviceCacheContent:kind=>kind||''});
  install(ctx,'deviceCacheStates','updateDeviceCacheBadges');
  const snapshot={directory:{permission:'granted'},entries:[{gameId:'a',status:'downloading',received:10},{gameId:'b',status:'ready',complete:true}]};
  ctx.updateDeviceCacheBadges(snapshot);assert.equal(writes,2);assert.equal(scans,1);
  snapshot.entries[0].received=20;ctx.updateDeviceCacheBadges(snapshot);assert.equal(writes,2);assert.equal(scans,1);
  snapshot.entries[0].status='verifying';ctx.updateDeviceCacheBadges(snapshot);assert.equal(writes,3);assert.equal(a.markup,'checking');assert.equal(b.markup,'ready');
  snapshot.entries=[];ctx.updateDeviceCacheBadges(snapshot);assert.equal(a.hidden,true);assert.equal(b.hidden,true);
});

test('paused complete files remain downloaded while restored unchecked files wait for verification',()=>{
  const ctx=vm.createContext({t:value=>value});install(ctx,'deviceCacheStates','deviceCacheLabel');
  const states=ctx.deviceCacheStates({directory:{permission:'granted'},entries:[
    {gameId:'verified',status:'paused',complete:true,cachedComplete:true},
    {gameId:'restored',status:'paused',complete:false,cachedComplete:true},
  ]});
  assert.equal(ctx.deviceCacheLabel(states.get('verified'),true),'已下载');
  assert.equal(ctx.deviceCacheLabel(states.get('verified')),'已下载到此设备 · 分享已暂停');
  assert.equal(ctx.deviceCacheLabel(states.get('restored')),'缓存待校验');
});
