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
  const game = {id:'large',title:'Large ROM',mode:'stream'};
  const messages=[],added=[],order=[],launches=[];
  const ctx=vm.createContext({
    state:{profile:{id:'player1',base_path:'/p/player1'},profileEpoch:1,detailOpen:true,selected:game,downloadBusy:false,
      downloadSnapshot:{directory:{supported:true,permission:'prompt',name:'Games'}},downloadPending:null,downloadEntryBusy:new Set()},
    resumeBrowserDownload:()=>{},closeDownloads:()=>order.push('close'),launchGame:async(...args)=>launches.push(args),
    renderDownloads:()=>order.push('render'),showDownloads:()=>order.push('show'),downloadMessage:(...args)=>messages.push(args),
    receiveDownloadSnapshot:s=>ctx.state.downloadSnapshot=s,errorMessage:e=>e.message,t:s=>s,notify:(...args)=>messages.push(args),
    downloadProblemMessage:e=>e.code,window:{RetroRomLibrary:{init:async()=>{},add:async arg=>added.push(arg),snapshot:()=>ctx.state.downloadSnapshot}},
  });
  install(ctx,'prepareDownloadStorage','downloadDirectoryReady','chooseDownloadDirectory','flushPendingDownload');
  return {ctx,game,messages,added,order,launches};
}
async function prepare(h) {
  return h.ctx.prepareDownloadStorage(h.game,{kind:'launch',game:h.game,launchOptions:{launchMode:'browser-p2p'}});
}
test('directory choice invokes the native picker in the click turn, then resumes the pending browser launch',async()=>{
  const h=harness();assert.equal(await prepare(h),false);h.order.length=0;
  let resolve;h.ctx.window.RetroRomLibrary.chooseDirectory=()=>{h.order.push('picker');return new Promise(r=>resolve=r);};
  const choose=h.ctx.chooseDownloadDirectory();assert.equal(h.order[0],'picker');assert.equal(h.launches.length,0);
  h.ctx.state.downloadSnapshot.directory.permission='granted';resolve();await choose;
  assert.deepEqual(plain(h.launches),[[h.game,{launchMode:'browser-p2p'}]]);
  assert.equal(h.added.length,0);assert.equal(h.ctx.state.downloadPending,null);assert.equal(h.ctx.state.downloadBusy,false);
});
test('a cancelled permission prompt never starts the game or download',async()=>{
  const h=harness();await prepare(h);
  h.ctx.window.RetroRomLibrary.reconnect=()=>{h.order.push('reconnect');return Promise.reject(Object.assign(new Error('cancelled'),{name:'AbortError'}));};
  await h.ctx.chooseDownloadDirectory(true);
  assert.ok(h.order.includes('reconnect'));assert.equal(h.launches.length,0);assert.equal(h.added.length,0);
  assert.equal(h.messages.filter(x=>x[1]===true).length,0);
});
test('switching players while choosing a directory never launches the prior player request',async()=>{
  const h=harness();await prepare(h);
  let resolve;h.ctx.window.RetroRomLibrary.chooseDirectory=()=>new Promise(r=>resolve=r);
  const choose=h.ctx.chooseDownloadDirectory();h.ctx.state.profileEpoch++;h.ctx.state.profile={id:'player2'};
  h.ctx.state.downloadSnapshot.directory.permission='granted';resolve();await choose;assert.equal(h.launches.length,0);
});
test('the manager and advanced launch settings use one device sharing preference',async()=>{
  const saved=[],manager=[];const ctx=vm.createContext({state:{browserP2P:true},remember:(...args)=>saved.push(args),
    window:{RetroRomLibrary:{setSharing:value=>manager.push(value)}},renderDownloads:()=>{},downloadMessage:()=>{},errorMessage:e=>e.message});
  install(ctx,'setBrowserP2PPreference');ctx.setBrowserP2PPreference(false);
  assert.deepEqual(manager,[false]);assert.deepEqual(saved,[['retroone:browser-p2p','false']]);
  const rendered=[];ctx.state.detailOpen=true;ctx.state.selected={id:'g'};ctx.renderLaunchChoices=g=>rendered.push(g.id);ctx.updateDeviceCacheBadges=()=>{};ctx.renderDownloadOverview=()=>{};
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


test('download overview counts only active incomplete transfers and keeps completed seeding out of progress',()=>{
  const ctx=vm.createContext({});install(ctx,'downloadOverview');
  assert.deepEqual(plain(ctx.downloadOverview({entries:[
    {size:100,received:40,status:'downloading'}, {size:300,received:60,status:'verifying'},
    {size:500,received:500,status:'sharing',complete:true}, {size:50,status:'paused'}, {status:'permission'},
  ]})),{active:2,total:5,attention:1,percent:25});
  assert.deepEqual(plain(ctx.downloadOverview(null)),{active:0,total:0,attention:0,percent:0});
});

test('closing the download manager leaves the job owner intact and returns focus to the game control',()=>{
  const calls=[],button={isConnected:true,focus:()=>calls.push('focus')};
  const ctx=vm.createContext({state:{downloadReturn:button,downloadFromSelf:false,downloadPending:null},
    $:()=>({close:()=>calls.push('close')}),syncModalBody:()=>{},window:{RetroRomLibrary:{pauseAll:()=>assert.fail('closing UI must not cancel downloads')}}});
  install(ctx,'closeDownloads');ctx.closeDownloads();assert.deepEqual(calls,['close','focus']);
});


test('download completion never implies WASM compatibility',()=>{
  const ctx=vm.createContext({state:{profileEpoch:3,downloadSupport:new Map()},t:s=>s});install(ctx,'downloadSupportLabel');
  const entry={gameId:'3ds',complete:true,status:'sharing'};
  assert.match(ctx.downloadSupportLabel(entry),/不代表/);
  ctx.state.downloadSupport.set('3:3ds',{data:{browser_launchable:false}});assert.match(ctx.downloadSupportLabel(entry),/暂不支持/);
  ctx.state.downloadSupport.set('3:3ds',{data:{browser_launchable:true}});assert.match(ctx.downloadSupportLabel(entry),/^支持浏览器/);
});


test('storage presentation distinguishes automatic browser cache, external permission, and quota',()=>{
  const ctx=vm.createContext({t:s=>s,downloadProblemMessage:s=>s});install(ctx,'downloadStoragePresentation');
  const cached=ctx.downloadStoragePresentation({storage_kind:'opfs',permission:'granted'});
  assert.equal(cached.title,'浏览器缓存'); assert.match(cached.note,/清除网站数据/); assert.doesNotMatch(cached.note,/重新授权/);
  assert.match(ctx.downloadStoragePresentation({storage_kind:'external',name:'Games',permission:'prompt',error:'permission'}).note,/重新授权/);
  assert.equal(ctx.downloadStoragePresentation({storage_kind:'opfs',permission:'error',error:'quota'}).note,'quota');
});
test('only the matching blocked browser launch opens cache authorization',()=>{
  const shown=[],messages=[];
  const ctx=vm.createContext({state:{playMode:'browser',profile:{id:'p1'},playGameId:'g'},showDownloads:()=>shown.push(1),downloadMessage:(...args)=>messages.push(args),downloadProblemMessage:s=>s});
  install(ctx,'handleRomTransferError');
  ctx.handleRomTransferError({profileId:'p2',gameId:'g',code:'folder'});
  ctx.handleRomTransferError({profileId:'p1',gameId:'old',code:'folder'});
  ctx.handleRomTransferError({profileId:'p1',gameId:'g',code:'network'});
  assert.equal(shown.length,0);
  ctx.handleRomTransferError({profileId:'p1',gameId:'g',code:'folder'});
  assert.equal(shown.length,1); assert.deepEqual(messages,[['permission',true]]);
});
test('restoring storage resumes only the same blocked launch without reloading the iframe',()=>{
  const calls=[],config={profileId:'p1',gameId:'g'};
  const transfer={phase:'error',error:'folder'};
  const frame={RetroRomTransferConfig:config,RetroRomTransfer:{snapshot:()=>transfer,start:c=>calls.push(c)}};
  const ctx=vm.createContext({state:{playMode:'browser',profile:{id:'p1'},playGameId:'g'},$:()=>({contentWindow:frame}),closeDownloads:()=>calls.push('close')});
  install(ctx,'resumeBrowserDownload'); ctx.resumeBrowserDownload();
  assert.deepEqual(calls,['close',config]); calls.length=0;
  transfer.phase='running'; ctx.resumeBrowserDownload(); assert.equal(calls.length,0);
  transfer.phase='error';ctx.state.profile.id='p2';ctx.resumeBrowserDownload();assert.equal(calls.length,0);
});


test('first automatic browser cache requires a choice; confirmed storage does not ask again',async()=>{
  const h=harness();
  h.ctx.state.downloadSnapshot.directory={supported:true,permission:'granted',storage_kind:'opfs',selection_required:true};
  assert.equal(await prepare(h),false);
  assert.equal(h.launches.length,0);assert.equal(h.ctx.state.downloadPending.gameId,'large');
  h.ctx.window.RetroRomLibrary.useBrowserStorage=async()=>{h.order.push('confirm-cache');h.ctx.state.downloadSnapshot.directory.selection_required=false;};
  await h.ctx.chooseDownloadDirectory(false,true);
  assert.equal(h.launches.length,1);assert.ok(h.order.includes('confirm-cache'));
  assert.equal(await prepare(h),true);assert.equal(h.added.length,0);
});
test('profile change during storage initialization cannot resume a stale launch',async()=>{
  const h=harness();let resume;
  h.ctx.window.RetroRomLibrary.init=()=>new Promise(resolve=>resume=resolve);
  const start=prepare(h);h.ctx.state.profileEpoch++;resume();await start;
  assert.equal(h.launches.length,0);assert.equal(h.ctx.state.downloadPending,null);assert.equal(h.order.length,0);
});
test('storage choice for a browser launch preserves its requested mode and does not add a separate download',async()=>{
  const h=harness(), launches=[];h.ctx.closeDownloads=()=>h.order.push('close');h.ctx.launchGame=async(...args)=>launches.push(args);
  h.ctx.state.downloadSnapshot.directory={supported:true,permission:'granted',selection_required:true};
  const options={launchMode:'browser-http'};
  assert.equal(await h.ctx.prepareDownloadStorage(h.game,{kind:'launch',game:h.game,launchOptions:options}),false);
  assert.equal(h.added.length,0);h.ctx.state.downloadSnapshot.directory.selection_required=false;
  await h.ctx.flushPendingDownload();
  assert.equal(h.added.length,0);assert.deepEqual(plain(launches),[[h.game,options]]);
});

test('a late folder picker never starts a replacement request for the same or another player',async()=>{
  for(const switchPlayer of [false,true]) {
    const h=harness();await prepare(h);let resolve;
    h.ctx.window.RetroRomLibrary.chooseDirectory=()=>new Promise(r=>resolve=r);
    const choosing=h.ctx.chooseDownloadDirectory();
    if(switchPlayer){h.ctx.state.profileEpoch++;h.ctx.state.profile={id:'player2'};}
    h.ctx.state.downloadPending={gameId:'second',title:'Second',epoch:h.ctx.state.profileEpoch,profileId:h.ctx.state.profile.id,manifestUrl:'/p/player1/api/games/second/download-manifest'};
    h.ctx.state.downloadSnapshot.directory.permission='granted';resolve();await choosing;
    assert.equal(h.added.length,0);assert.equal(h.launches.length,0);assert.equal(h.ctx.state.downloadPending.gameId,'second');
  }
});
