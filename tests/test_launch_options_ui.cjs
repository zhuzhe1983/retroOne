const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const install = (ctx, name) => {
  const match = source.match(new RegExp(`^  (?:async )?function ${name}\\([\\s\\S]*?^  }`, 'm'));
  assert.ok(match, name); vm.runInContext(match[0], ctx);
};
const options = () => ({options: [
  {id:'auto', available:true}, {id:'stream', available:true},
  {id:'browser-http', available:true, requires_threads:true, size_bytes:134217728},
  {id:'browser-p2p', available:true, requires_threads:true, size_bytes:134217728},
]});
const fullClient = {secure:true, rtc:true, crypto:true, threads:true, webgl2:true};

test('WASM choices require the actual client capabilities and retain server restrictions', () => {
  const ctx=vm.createContext({t:s=>s}); install(ctx,'usableLaunchOptions');
  const data=options();
  assert.equal(ctx.usableLaunchOptions(data,fullClient).filter(o=>o.available).length,4);
  for(const capability of ['secure','rtc','crypto']) {
    const result=ctx.usableLaunchOptions(data,{...fullClient,[capability]:false});
    assert.equal(result.find(o=>o.id==='browser-p2p').available,false);
    assert.match(result.find(o=>o.id==='browser-p2p').reason,/P2P/);
  }
  for(const capability of ['threads','webgl2']) {
    const result=ctx.usableLaunchOptions(data,{...fullClient,[capability]:false});
    assert.equal(result.find(o=>o.id==='browser-http').available,false);
    assert.equal(result.find(o=>o.id==='browser-p2p').available,false);
    assert.equal(result.find(o=>o.id==='stream').available,true);
  }
  data.options[2].available=false; data.options[2].reason='Missing BIOS';
  const denied=ctx.usableLaunchOptions(data,fullClient).find(o=>o.id==='browser-http');
  assert.equal(denied.available,false); assert.equal(denied.reason,'Missing BIOS');
  assert.equal(data.options[3].available,true,'input is never mutated');
});

test('single-thread HTTP remains available without P2P or thread support; unknown modes never become controls', () => {
  const ctx=vm.createContext({t:s=>s}); install(ctx,'usableLaunchOptions');
  const data=options(); data.options[2].requires_threads=false;
  data.options.push({id:'untrusted-mode',available:true});
  const result=ctx.usableLaunchOptions(data,{});
  assert.equal(result.find(o=>o.id==='browser-http').available,true);
  assert.equal(result.some(o=>o.id==='untrusted-mode'),false);
});

function mergedOptions(data=options(), capabilities=fullClient, allowP2P=true) {
  const ctx=vm.createContext({t:s=>s});
  install(ctx,'usableLaunchOptions'); install(ctx,'mergeBrowserLaunchOptions');
  return ctx.mergeBrowserLaunchOptions(ctx.usableLaunchOptions(data,capabilities),allowP2P);
}

test('the three launch choices merge transports without changing automatic or cloud availability', () => {
  const data=options(); data.options[0].available=false; data.options[0].reason='Missing native runtime';
  const merged=mergedOptions(data);
  assert.deepEqual(plain(merged.map(o=>o.id)),['auto','stream','browser']);
  assert.equal(merged[0].available,false); assert.equal(merged[0].reason,'Missing native runtime');
  assert.equal(merged[1].available,true);
  assert.equal(merged[2].available,true); assert.equal(merged[2].launchMode,'browser-p2p');
  assert.equal(merged[2].size_bytes,134217728);
});

test('browser gameplay automatically uses HTTP when P2P capabilities are absent', () => {
  for(const capability of ['rtc','crypto','secure']) {
    const merged=mergedOptions(options(),{...fullClient,[capability]:false});
    assert.equal(merged[2].available,true,capability);
    assert.equal(merged[2].launchMode,'browser-http',capability);
    assert.equal(merged[2].reason,'');
  }
});

test('disabling P2P selects HTTP and never silently uses a peer-only option', () => {
  assert.equal(mergedOptions(options(),fullClient,false)[2].launchMode,'browser-http');
  const data=options(); data.options[2].available=false;
  const merged=mergedOptions(data,fullClient,false)[2];
  assert.equal(merged.available,false); assert.equal(merged.launchMode,null);
  assert.match(merged.reason,/直接下载/);
  assert.equal(mergedOptions(data,fullClient,true)[2].launchMode,'browser-p2p');
});

test('unavailable server P2P falls back to HTTP, and unavailable ROMs stay unavailable', () => {
  const data=options(); data.options[3].available=false; data.options[3].reason='Tracker unavailable';
  assert.equal(mergedOptions(data)[2].launchMode,'browser-http');
  for(const option of data.options) {option.available=false;option.reason='Unsupported game';}
  const merged=mergedOptions(data);
  assert.ok(merged.every(o=>!o.available));
  assert.equal(merged[2].launchMode,null); assert.equal(merged[2].reason,'Unsupported game');
  assert.equal(mergedOptions({options:[]})[2].available,false);
});

test('browser choice resolves a real transport at launch while other choices and profile epochs remain isolated', () => {
  const game={id:'g'}, state={profileEpoch:1,launchOptions:{gameId:'g',epoch:1},launchChoice:'browser',browserP2P:true};
  const ctx=vm.createContext({state,t:s=>s,currentLaunchOptions:()=>options().options});
  install(ctx,'mergeBrowserLaunchOptions'); install(ctx,'selectedLaunchMode');
  assert.equal(ctx.selectedLaunchMode(game),'browser-p2p');
  state.browserP2P=false; assert.equal(ctx.selectedLaunchMode(game),'browser-http');
  for(const choice of ['auto','stream']) {state.launchChoice=choice;assert.equal(ctx.selectedLaunchMode(game),choice);}
  state.launchChoice='browser'; state.profileEpoch=2;
  assert.equal(ctx.selectedLaunchMode(game),'auto');
  state.profileEpoch=1; assert.equal(ctx.selectedLaunchMode({id:'other'}),'auto');
});

test('P2P preference is device-scoped, defaults on, and survives a later page initialization', () => {
  const storage=new Map(); const state={browserP2P:true};
  const ctx=vm.createContext({state,remember:(k,v)=>storage.set(k,v)}); install(ctx,'setBrowserP2PPreference');
  const initializer=source.match(/browserP2P: (remembered\('retroone:browser-p2p'\) !== 'false')/)[1];
  const load=()=>vm.runInNewContext(initializer,{remembered:k=>storage.get(k)??null});
  assert.equal(load(),true);
  ctx.setBrowserP2PPreference(false);
  assert.equal(state.browserP2P,false); assert.equal(load(),false);
  assert.deepEqual([...storage.keys()],['retroone:browser-p2p']);
  ctx.setBrowserP2PPreference(true); assert.equal(load(),true);
});

test('the advanced switch remains reachable when sharing was disabled and only P2P is available', () => {
  const data=options(); data.options[2].available=false;
  const game={id:'g'}, handlers={};
  const nodes=Object.fromEntries(['launch-mode','launch-allow-p2p'].map(id=>[id,{addEventListener:(name,fn)=>handlers[id+':'+name]=fn,focus:()=>{}}]));
  const holder={innerHTML:'',contains:()=>false,querySelector:()=>null};
  nodes['launch-choices']=holder; nodes['game-detail']={querySelector:()=>null};
  const ctx=vm.createContext({state:{selected:game,profileEpoch:1,launchOptions:{gameId:'g',epoch:1,data},launchChoice:'auto',browserP2P:false},
    document:{activeElement:null},$:id=>nodes[id],t:s=>s,escapeHTML:s=>String(s??''),diagnostic:s=>s,formatSize:String,
    remember:()=>{},updateLaunchButton:()=>{},currentLaunchOptions:()=>data.options});
  for(const name of ['mergeBrowserLaunchOptions','setBrowserP2PPreference','launchOptionLabel','selectedLaunchMode','renderLaunchChoices'])install(ctx,name);
  ctx.renderLaunchChoices(game);
  assert.equal((holder.innerHTML.match(/<option /g)||[]).length,3);
  assert.match(holder.innerHTML,/role="switch" aria-checked="false"/);
  assert.doesNotMatch(holder.innerHTML,/WebTorrent|本机|本地|HTTP/);
  handlers['launch-allow-p2p:click']();
  assert.equal(ctx.state.browserP2P,true);
  handlers['launch-mode:change']({target:{value:'browser'}});
  assert.equal(ctx.selectedLaunchMode(game),'browser-p2p');
  assert.match(holder.innerHTML,/role="switch" aria-checked="true"/);
});

function optionsHarness() {
  const game={id:'g',mode:'stream'};
  const pending=[],calls=[],renders=[];
  const ctx=vm.createContext({
    state:{profile:{id:'player1'},profileEpoch:1,detailOpen:true,selected:game,launchOptions:null,launchChoice:'auto'},
    api:(url)=>{calls.push(url);return new Promise((resolve,reject)=>pending.push({resolve,reject}));},
    renderLaunchChoices:game=>renders.push(game.id),
  });
  install(ctx,'loadLaunchOptions');
  return {ctx,game,pending,calls,renders};
}

test('options load only for the detail page and coalesce renders while in flight', async () => {
  const h=optionsHarness(); h.ctx.state.detailOpen=false;
  await h.ctx.loadLaunchOptions(h.game); assert.equal(h.calls.length,0);
  h.ctx.state.detailOpen=true;
  const first=h.ctx.loadLaunchOptions(h.game);
  await h.ctx.loadLaunchOptions(h.game); assert.equal(h.calls.length,1);
  h.pending[0].resolve(options()); await first;
  assert.equal(h.ctx.state.launchOptions.loading,false);
  assert.equal(h.ctx.state.launchOptions.data.options.length,4);
  await h.ctx.loadLaunchOptions(h.game); assert.equal(h.calls.length,1);
});

test('late responses cannot replace another game or player launch options', async () => {
  for(const change of ['game','player']) {
    const h=optionsHarness(); const first=h.ctx.loadLaunchOptions(h.game);
    if(change==='player') h.ctx.state.profileEpoch++;
    const other={id:'second',mode:'stream'}; h.ctx.state.selected=other;
    const second=h.ctx.loadLaunchOptions(other);
    h.pending[1].resolve(options()); await second;
    h.ctx.state.launchChoice='stream';
    h.pending[0].resolve({options:[{id:'auto',available:false}]}); await first;
    assert.equal(h.ctx.state.launchOptions.gameId,'second');
    assert.equal(h.ctx.state.launchOptions.data.options.length,4);
    assert.equal(h.ctx.state.launchChoice,'stream');
  }
});

test('failed capability reads leave automatic mode and permit an explicit retry', async () => {
  const h=optionsHarness(); const first=h.ctx.loadLaunchOptions(h.game);
  h.pending[0].reject(new Error('offline')); await first;
  assert.equal(h.ctx.state.launchOptions.error,true);
  assert.equal(h.ctx.state.launchOptions.loading,false);
  const retry=h.ctx.loadLaunchOptions(h.game,true);
  h.pending[1].resolve(options()); await retry;
  assert.equal(h.ctx.state.launchOptions.error,false);
});

function launchHarness() {
  const calls=[],opened=[],conflicts=[],pending=[];
  const game={id:'g',mode:'unavailable',title:'Fixture'};
  const ctx=vm.createContext({
    state:{profile:{id:'player1',base_path:'/p/player1'},profileEpoch:1,selected:null,playMode:null,launching:false},
    $:()=>({open:false}), t:s=>s, displayDeviceId:'device',
    selectedLaunchMode:()=> 'browser-p2p', currentLaunchOptions:()=>options().options,
    requestGameLaunch:(id,body)=>{calls.push({id,body});return new Promise((resolve,reject)=>pending.push({resolve,reject}));},
    localURL:url=>url, sessionNetworkRoomId:()=>null, renderFamilyPresence:()=>{},closeNetwork:()=>{},
    openPlayer:(...args)=>opened.push(args),refreshSession:()=>{},notify:()=>{},interfaceError:message=>new Error(message),
    showLaunchConflict:(...args)=>conflicts.push(args), errorMessage:e=>e.message,
  });
  install(ctx,'launchGame');
  return {ctx,game,calls,opened,conflicts,pending};
}

test('explicit browser launch is allowed when automatic catalog mode is unavailable', async () => {
  const h=launchHarness(); const promise=h.ctx.launchGame(h.game,{launchMode:'browser-http'});
  assert.equal(h.calls[0].body.mode,'browser-http');
  h.pending[0].resolve({mode:'browser',play_url:'/p/player1/play/g?experimental=true&transport=http'}); await promise;
  assert.equal(h.opened[0][2],'browser');
  assert.equal(h.ctx.state.launching,false);
});

test('network invitations force cloud even when a browser mode is selected', async () => {
  const h=launchHarness(); const promise=h.ctx.launchGame(h.game,{networkRoomId:'room',launchMode:'browser-p2p'});
  assert.deepEqual(plain(h.calls[0].body),{mode:'stream',device_id:'device',network_room_id:'room'});
  h.pending[0].resolve({mode:'stream',play_url:'/p/player1/stream/'}); await promise;
  assert.equal(h.opened[0][2],'stream');
});

test('conflict stores the selected launch mode and retries preserve the confirmed session identity', async () => {
  const h=launchHarness();
  const first=h.ctx.launchGame(h.game,{launchMode:'stream'});
  h.pending[0].reject(Object.assign(new Error('conflict'),{status:409,detail:{code:'session_conflict',session:{id:'old'}}})); await first;
  assert.equal(h.conflicts[0][2].launchMode,'stream');
  const second=h.ctx.launchGame(h.game,{launchMode:h.conflicts[0][2].launchMode,replaceSessionId:'old'});
  assert.deepEqual(plain(h.calls[1].body),{mode:'stream',device_id:'device',replace_session_id:'old'});
  h.pending[1].resolve({mode:'stream',play_url:'/p/player1/stream/'}); await second;
});

test('browser conflict confirmation freezes its resolved transport even after the preference changes', async () => {
  const h=launchHarness();
  const first=h.ctx.launchGame(h.game,{launchMode:'browser-p2p'});
  h.pending[0].reject(Object.assign(new Error('conflict'),{status:409,detail:{code:'session_conflict',session:{id:'old'}}})); await first;
  h.ctx.selectedLaunchMode=()=> 'browser-http';
  const second=h.ctx.launchGame(h.game,{launchMode:h.conflicts[0][2].launchMode,replaceSessionId:'old'});
  assert.equal(h.calls[1].body.mode,'browser-p2p'); assert.equal(h.calls[1].body.replace_session_id,'old');
  h.pending[1].resolve({mode:'browser',play_url:'/p/player1/play/g?transport=p2p'}); await second;
});

test('a launch response after a player change cannot open the prior player game', async () => {
  const h=launchHarness(); const promise=h.ctx.launchGame(h.game);
  h.ctx.state.profileEpoch++; h.ctx.state.launching=true;
  h.pending[0].resolve({mode:'browser',play_url:'/p/player1/play/g'}); await promise;
  assert.equal(h.opened.length,0); assert.equal(h.ctx.state.launching,true);
});

test('controller selection skips disabled modes and wraps through available modes', () => {
  const dispatched=[];
  const active={disabled:false,options:[{value:'auto',selected:true},{value:'stream',disabled:true},{value:'browser'}],
    matches:s=>s==='select',dispatchEvent:event=>dispatched.push(event.type),set value(value){this.chosen=value;for(const o of this.options)o.selected=o.value===value;}};
  const ctx=vm.createContext({document:{activeElement:active},Event:class{constructor(type){this.type=type;}}}); install(ctx,'activateFocused');
  ctx.activateFocused(); assert.equal(active.chosen,'browser');
  ctx.activateFocused(); assert.equal(active.chosen,'auto');
  assert.deepEqual(dispatched,['change','change']);
});

test('detail controller navigation includes the advanced switch and activates it as a native button', () => {
  let clicked=0;
  const select={disabled:false,getClientRects:()=>[{}],focus(){document.activeElement=this;}};
  const hidden={disabled:false,getClientRects:()=>[],focus(){throw new Error('hidden control focused');}};
  const toggle={disabled:false,getClientRects:()=>[{}],focus(){document.activeElement=this;},matches:s=>s==='button, a, summary',click:()=>clicked++};
  const document={activeElement:select};
  const ctx=vm.createContext({document,$:()=>({querySelectorAll:selector=>{assert.match(selector,/button/);assert.match(selector,/select/);return [select,hidden,toggle];}})});
  install(ctx,'focusDetailNeighbor'); install(ctx,'activateFocused');
  ctx.focusDetailNeighbor('down'); assert.equal(document.activeElement,toggle);
  ctx.activateFocused(); assert.equal(clicked,1);
  ctx.focusDetailNeighbor('down'); assert.equal(document.activeElement,select);
});
