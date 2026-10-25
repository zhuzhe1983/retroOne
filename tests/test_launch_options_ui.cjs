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
    assert.match(result.find(o=>o.id==='browser-p2p').reason,/WebTorrent/);
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

test('network invitations force cloud even when a local mode is selected', async () => {
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

test('a launch response after a player change cannot open the prior player game', async () => {
  const h=launchHarness(); const promise=h.ctx.launchGame(h.game);
  h.ctx.state.profileEpoch++; h.ctx.state.launching=true;
  h.pending[0].resolve({mode:'browser',play_url:'/p/player1/play/g'}); await promise;
  assert.equal(h.opened.length,0); assert.equal(h.ctx.state.launching,true);
});

test('controller selection skips disabled modes and wraps through available modes', () => {
  const dispatched=[];
  const active={disabled:false,options:[{value:'auto',selected:true},{value:'stream',disabled:true},{value:'browser-http'}],
    matches:s=>s==='select',dispatchEvent:event=>dispatched.push(event.type),set value(value){this.chosen=value;for(const o of this.options)o.selected=o.value===value;}};
  const ctx=vm.createContext({document:{activeElement:active},Event:class{constructor(type){this.type=type;}}}); install(ctx,'activateFocused');
  ctx.activateFocused(); assert.equal(active.chosen,'browser-http');
  ctx.activateFocused(); assert.equal(active.chosen,'auto');
  assert.deepEqual(dispatched,['change','change']);
});
