const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function harness() {
  const state = {profile:null, familySessions:new Map(), presenceStale:false, profileRetrying:new Set()};
  const context = vm.createContext({state, remembered:()=> 'player1',
    t:(s,p={})=>s.replace(/\{(\w+)\}/g,(_,key)=>p[key]??''),
    escapeHTML:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    avatarHTML:()=>'<span class="profile-avatar"></span>', icon:()=>'<svg></svg>'});
  vm.runInContext(source.slice(source.indexOf('  function profileStatus('),source.indexOf('  function validProfile(')),context);
  for(const name of ['profileCardActivity','profileCardMarkup']) {
    const start=source.indexOf(`  function ${name}(`), end=source.indexOf('\n  }',start)+4;
    vm.runInContext(source.slice(start,end),context);
  }
  return {state, context};
}
const player={id:'player1',name:'Test player',avatar:'star',configured:true,status:'ready',presence:{online:false}};
test('player chooser separates retained cloud sessions from online browser presence',()=>{
  const {state,context}=harness();
  assert.equal(context.profileCardActivity(player).kind,'idle');
  state.familySessions.set('player1',{id:'session',game_title:'Retained game'});
  assert.equal(context.profileCardActivity(player).kind,'playing');
  assert.equal(context.profileCardActivity(player).detail,'Retained game');
  state.presenceStale=true;
  assert.equal(context.profileCardActivity(player).kind,'playing');
  state.familySessions.clear();
  assert.equal(context.profileCardActivity(player).kind,'unknown');
});
test('unready profiles never appear ready because stale presence says they are online',()=>{
  const {state,context}=harness();
  state.familySessions.set('player1',{id:'old-session',title:'Old game'});
  for(const [status,kind] of [['provisioning','preparing'],['error','error']]){
    const candidate={...player,status,presence:{online:true,activity:'library'}};
    assert.equal(context.profileCardActivity(candidate).kind,kind);
    assert.match(context.profileCardMarkup(candidate),/aria-disabled="true"/);
  }
});
test('profile markup escapes user names, game titles, and recovery diagnostics',()=>{
  const {state,context}=harness();
  const attack='<img src=x onerror="alert(1)">';
  state.familySessions.set('player1',{id:'session',game_title:attack});
  const html=context.profileCardMarkup({...player,name:attack});
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
  const error=context.profileCardMarkup({...player,status:'error',error:attack});
  assert.ok(!error.includes('<img'));
  assert.ok(error.includes('data-profile-action="reason:player1"'));
});
test('current player takes precedence over last used and unavailable profiles are not suggested',()=>{
  const {state,context}=harness();
  assert.match(context.profileCardMarkup(player),/上次使用/);
  state.profile=player;
  assert.match(context.profileCardMarkup(player),/aria-current="true"/);
  assert.match(context.profileCardMarkup(player),/当前玩家/);
  assert.ok(!context.profileCardMarkup(player).includes('上次使用'));
  state.profile=null;
  assert.ok(!context.profileCardMarkup({...player,status:'provisioning'}).includes('上次使用'));
});
