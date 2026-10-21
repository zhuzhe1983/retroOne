const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
function harness() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {id, open: false, hidden: false, children: [], textContent: '', innerHTML: '',
      focus() {}, querySelectorAll() {return [];}, replaceChildren(...children) {this.children=children;},
      showModal() {this.open=true;}, close() {this.open=false;}});
    return elements.get(id);
  };
  const requests = [], pending = [];
  const state = {profiles:[{id:'player2',name:'Me'},{id:'player1',name:'Family',presence:{online:false}}],profile:{id:'player2',name:'Me'},profileEpoch:0,
    session:{id:'own-session'},playMode:'stream',familySelectedId:'player1',familySessions:new Map([['player1',{id:'family-session',title:'PSP Game'}]]),
    familySessionsLoading:false,familySessionsAvailable:true,watchBusy:false,watchRequest:0};
  element('family-dialog').open=true;
  const context=vm.createContext({state,Map,URL,location:{origin:'https://retro.test'},document:{createElement:()=>({})},$:element,
    t:(s,p={})=>s.replace(/\{(\w+)\}/g,(_,k)=>p[k]??''),interfaceError:s=>new Error(s),errorMessage:e=>e.message,
    renderFamilyDetails(){},renderFamilyPresence(){},syncModalBody(){},
    api:(url,opts)=>{requests.push({url,opts});return new Promise((resolve,reject)=>pending.push({resolve,reject}));}});
  const names=['closeFamily','refreshFamilySessions','watchFamily','closeWatch'];
  for(const name of names){const start=source.indexOf(`  ${name==='watchFamily'||name==='refreshFamilySessions'?'async ':''}function ${name}(`);assert.ok(start>=0);const end=source.indexOf('\n  }',start)+4;vm.runInContext(source.slice(start,end),context);}
  return {context,state,element,requests,pending};
}
const grant='/spectate/abcdefghijklmnopqrstuvwxyz012345/';
test('watching binds an exact family session and retains the current player and game',async()=>{
  const h=harness();const own=h.state.session,profile=h.state.profile;
  const promise=h.context.watchFamily();
  assert.equal(h.requests[0].url,'/spectate/api/watch');
  assert.deepEqual(JSON.parse(h.requests[0].opts.body),{viewer_profile_id:'player2',target_profile_id:'player1',expected_session_id:'family-session'});
  h.pending[0].resolve({watch_url:grant});await promise;
  assert.equal(h.element('watch-dialog').open,true);assert.equal(h.element('family-dialog').open,false);
  assert.equal(h.element('watch-frame-wrap').children[0].src,'https://retro.test'+grant);
  assert.equal(h.state.profile,profile);assert.equal(h.state.session,own);assert.equal(h.state.playMode,'stream');
  h.context.closeWatch();assert.equal(h.element('watch-frame-wrap').children.length,0);assert.equal(h.element('watch-dialog').open,false);
  assert.equal(h.requests.length,1);assert.equal(h.state.session,own);assert.equal(h.state.profile,profile);
});
test('late grant cannot open a watch after leaving the family dialog or changing player',async()=>{
  for(const change of [h=>h.element('family-dialog').close(),h=>h.state.profileEpoch++,h=>h.state.familySelectedId='player3']){
    const h=harness(),promise=h.context.watchFamily();change(h);h.pending[0].resolve({watch_url:grant});await promise;
    assert.equal(h.element('watch-dialog').open,false);assert.equal(h.element('watch-frame-wrap').children.length,0);
  }
});
test('family sessions reflect cloud activity even when browser presence is offline',async()=>{
  const h=harness();const promise=h.context.refreshFamilySessions();
  h.pending[0].resolve({sessions:[{profile_id:'player1',session:{id:'new-session',title:'Cloud game'}},{profile_id:'player3',session:null}]});await promise;
  assert.equal(h.state.familySessions.get('player1').id,'new-session');assert.equal(h.state.familySessions.has('player3'),false);
  assert.equal(h.state.familySessionsAvailable,true);assert.equal(h.state.profiles[1].presence.online,false);
  const retry=h.context.refreshFamilySessions();h.pending[1].reject(new Error('optional sidecar unavailable'));await retry;
  assert.equal(h.state.familySessionsAvailable,false);assert.equal(h.state.familySessions.size,0);assert.equal(h.state.profile.id,'player2');
});
test('foreign or controller URLs cannot be embedded by a malformed grant response',async()=>{
  for(const watch_url of ['https://evil.test'+grant,'/p/player1/stream/','/spectate/abcdefghijklmnopqrstuvwxyz012345/?role=controller']){
    const h=harness(),promise=h.context.watchFamily();h.pending[0].resolve({watch_url});await promise;
    assert.equal(h.element('watch-dialog').open,false);assert.equal(h.element('watch-frame-wrap').children.length,0);
    assert.equal(h.element('family-invite-message').hidden,false);
  }
});
