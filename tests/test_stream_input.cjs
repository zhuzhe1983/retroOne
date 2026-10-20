/* Run: node --test tests/test_stream_input.cjs
 * Optional actual pinned-client poller: SELKIES_BUNDLE=/path/to/selkies-core.js node --test ...
 * No browser, game, ROM, or production service is needed.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../container/stream-input.js'), 'utf8');

function harness() {
  let now = 0, id = 0, pads = [null,null,null,null], session = null;
  const timers = new Map(), frames = [], requests = [], forwarded = [], padEvents = [], messages = [], selfMessages = [], elements = new Map();
  class Event {
    constructor(type, options = {}) { this.type = type; Object.assign(this, options); this.defaultPrevented = false; this.stopped = false; }
    preventDefault() { this.defaultPrevented = true; }
    stopImmediatePropagation() { this.stopped = true; }
  }
  class Target {
    constructor() { this.listeners = []; this.id = ''; this.dataset = {}; this.classes = new Set(); this.props = {};
      this.classList = {toggle:(name,on)=>on ? this.classes.add(name) : this.classes.delete(name)};
      this.style = {setProperty:(name,value)=>{this.props[name]=value;}}; }
    appendChild(child) { elements.set(child.id,child); child.parentElement=this; }
    addEventListener(type, callback, capture = false) { this.listeners.push({type,callback,capture:!!capture}); }
    dispatchEvent(event) {
      if (!event.target) event.target = this;
      const listeners = this === window || this === document ? this.listeners : window.listeners;
      for (const listener of listeners.filter(l => l.type === event.type).sort((a,b) => Number(b.capture)-Number(a.capture))) {
        listener.callback(event); if (event.stopped) break;
      }
      return !event.defaultPrevented;
    }
  }
  class Input extends Target {} class Textarea extends Target {} class Select extends Target {}
  const window = new Target(), document = new Target(), body = new Target();
  window.location = {pathname:'/p/player3/stream/',origin:'https://retroone.test'};
  window.parent = {postMessage:(data,origin)=>messages.push({data,origin})};
  window.postMessage = (data,origin) => { selfMessages.push({data,origin}); window.dispatchEvent(new Event('message',{data,origin,source:window})); };
  document.body = body; document.head = new Target(); document.documentElement = new Target();
  document.hidden = false; document.visibilityState = 'visible'; document.getElementById = id => elements.get(id) || null;
  document.createElement = () => new Target();
  const nativeRead = () => pads, navigator = {getGamepads:nativeRead, userAgent:'Chromium'};
  const addTimer = (fn, delay, interval = false) => { const value = ++id; timers.set(value,{fn,at:now+delay,delay,interval}); return value; };
  const context = vm.createContext({window,document,navigator,Event,KeyboardEvent:Event,HTMLInputElement:Input,HTMLTextAreaElement:Textarea,HTMLSelectElement:Select,
    performance:{now:()=>now},console,Date,
    setTimeout:(fn,delay)=>addTimer(fn,delay),clearTimeout:key=>timers.delete(key),setInterval:(fn,delay)=>addTimer(fn,delay,true),clearInterval:key=>timers.delete(key),
    requestAnimationFrame:fn=>frames.push(fn),fetch:url=>{requests.push(url);return Promise.resolve({ok:true,json:async()=>({session})});}});
  vm.runInContext(source,context);
  window.addEventListener('keydown',e=>forwarded.push(e)); window.addEventListener('keyup',e=>forwarded.push(e));
  for (const type of ['gamepadconnected','gamepaddisconnected']) window.addEventListener(type,e=>padEvents.push({type,index:e.gamepad.index,id:e.gamepad.id}));
  const settle = async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
  const tick = ms => {
    const end = now+ms;
    for (;;) {
      const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0]; if(!next)break;
      const [key,timer]=next;now=timer.at;if(timer.interval)timer.at+=timer.delay;else timers.delete(key);timer.fn();
    }
    now=end;
  };
  return {context,window,document,body,Input,Textarea,Select,Event,requests,forwarded,padEvents,messages,selfMessages,elements,settle,tick,nativeRead,
    message(data,extra={}) {window.dispatchEvent(new Event('message',{data,origin:window.location.origin,source:window.parent,...extra}));},
    read:()=>navigator.getGamepads(), pads:value=>{pads=value;},
    async session(value) { session=value; [...timers.values()].find(t=>t.interval&&t.delay===2000).fn();await settle(); },
    async eden() {await this.session({engine:'Eden 0.2.1',platform:'switch'});tick(40);},
    key(type,code,options={}) {const event=new Event(type,{code,key:code.startsWith('Key')?code.slice(3).toLowerCase():code.startsWith('Digit')?code.slice(5):code.startsWith('Shift')?'Shift':code,...options});(options.target||body).dispatchEvent(event);return event;},
    padEvent(type,pad) {const event=new Event(type,{gamepad:pad});window.dispatchEvent(event);return event;},
    blur() {window.dispatchEvent(new Event('blur'));},
    hidden(value) {document.hidden=value;document.visibilityState=value?'hidden':'visible';document.dispatchEvent(new Event('visibilitychange'));},
    frame() {const pending=frames.splice(0);for(const fn of pending)fn();},
  };
}
function pad(index=0,axes=[0.123456789,-0.314159265,0.271828182,-0.618033989],mapping='standard') {
  return Object.freeze({id:'Physical controller',index,connected:true,mapping,axes:Object.freeze(axes),buttons:Object.freeze(Array.from({length:17},(_,i)=>Object.freeze({value:i===6?.37:0,pressed:i===6,touched:i===6}))),vibrationActuator:{playEffect(){}},hapticActuators:[]});
}
const plain = value => JSON.parse(JSON.stringify(value));

test('inactive sessions return the exact native result and use the current profile URL', async()=>{
  const h=harness(),native=[null,null,pad(2),null];h.pads(native);await h.settle();assert.equal(h.read(),native);assert.equal(h.requests[0],'/p/player3/api/session');
  assert.equal(h.key('keydown','KeyW').defaultPrevented,false);assert.equal(h.padEvents.length,0);
  await h.session({engine:'Eden 0.2.1',platform:'psp'});h.tick(40);assert.equal(h.read(),native);
});

test('keyboard-only Eden exposes one standard pad and consumes mapped keys before Selkies keyboard capture',async()=>{
  const h=harness();await h.eden();const virtual=h.read()[0];assert.equal(virtual.mapping,'standard');assert.equal(virtual.index,0);assert.equal(virtual.connected,true);assert.equal(virtual.buttons.length,17);assert.equal(h.read().filter(Boolean).length,1);
  const map={KeyJ:0,KeyK:1,KeyU:2,KeyI:3,KeyQ:4,KeyE:5,Digit1:6,Digit3:7,ShiftLeft:8,Enter:9,KeyZ:10,KeyX:11,KeyT:12,KeyF:14,KeyG:13,KeyH:15};
  for(const [code,index]of Object.entries(map)){assert.equal(h.key('keydown',code).defaultPrevented,true);assert.equal(h.read()[0].buttons[index].value,1);h.key('keyup',code);assert.equal(h.read()[0].buttons[index].value,0);}
  for(const [code,index,value]of [['KeyW',1,-1],['KeyS',1,1],['KeyA',0,-1],['KeyD',0,1],['ArrowUp',3,-1],['ArrowDown',3,1],['ArrowLeft',2,-1],['ArrowRight',2,1]]){h.key('keydown',code);assert.equal(h.read()[0].axes[index],value);h.key('keyup',code);}
  assert.equal(h.forwarded.length,0);assert.equal(h.padEvents.at(-1).type,'gamepadconnected');
});

test('physical analog precision, triggers and haptics survive keyboard merge and release',async()=>{
  const h=harness(),physical=pad(2);h.pads([null,null,physical,null]);await h.eden();
  assert.deepEqual(plain(h.read()[0].axes),physical.axes);assert.equal(h.read()[0].vibrationActuator,physical.vibrationActuator);assert.equal(h.read()[0].buttons[6].value,.37);
  h.key('keydown','KeyW');assert.equal(h.read()[0].axes[1],-1);assert.equal(h.read()[0].axes[0],physical.axes[0]);assert.equal(h.read()[0].axes[2],physical.axes[2]);
  h.key('keydown','KeyS');assert.equal(h.read()[0].axes[1],physical.axes[1]);h.key('keyup','KeyS');h.key('keyup','KeyW');assert.deepEqual(plain(h.read()[0].axes),physical.axes);
  h.key('keydown','Digit1');assert.equal(h.read()[0].buttons[6].value,1);h.key('keyup','Digit1');assert.equal(h.read()[0].buttons[6].value,.37);
  h.key('keydown','KeyT');h.key('keydown','KeyH');assert.equal(h.read()[0].buttons[12].value,1);assert.equal(h.read()[0].buttons[15].value,1);assert.deepEqual(plain(h.read()[0].axes),physical.axes);h.key('keyup','KeyT');h.key('keyup','KeyH');assert.equal(h.read()[0].buttons[12].value,0);assert.equal(h.read()[0].buttons[15].value,0);
});

test('native sparse indices and hotplug cannot disconnect or duplicate the stable merged pad',async()=>{
  const h=harness(),p2=pad(2),p0=pad(0,[.9,.8,.7,.6]);h.pads([null,null,p2,null]);await h.eden();const announced=h.padEvents.length;
  h.pads([p0,null,p2,null]);assert.equal(h.padEvent('gamepadconnected',p0).stopped,true);assert.equal(h.read()[0].axes[0],.9);assert.equal(h.read().filter(Boolean).length,1);
  h.pads([null,null,p2,null]);assert.equal(h.padEvent('gamepaddisconnected',p0).stopped,true);assert.deepEqual(plain(h.read()[0].axes),p2.axes);assert.equal(h.read()[0].index,0);
  h.pads([null,null,null,null]);h.padEvent('gamepaddisconnected',p2);h.key('keydown','KeyD');assert.equal(h.read()[0].axes[0],1);assert.equal(h.padEvents.length,announced);
});

test('blur, hidden page and pagehide release virtual controls without altering physical input',async()=>{
  const h=harness(),physical=pad();h.pads([physical,null,null,null]);await h.eden();
  for(const release of [()=>h.blur(),()=>h.hidden(true),()=>h.window.dispatchEvent(new h.Event('pagehide'))]){h.hidden(false);h.key('keydown','KeyW');h.key('keydown','KeyJ');release();assert.equal(h.read()[0].buttons[0].value,0);assert.deepEqual(plain(h.read()[0].axes),physical.axes);}
});

test('ordinary text fields, IME and browser shortcuts pass through; tracked keyup still releases',async()=>{
  const h=harness();await h.eden();
  for(const target of [new h.Input(),new h.Textarea(),new h.Select(),{isContentEditable:true,dispatchEvent:e=>h.body.dispatchEvent(e)}]){assert.equal(h.key('keydown','KeyW',{target}).defaultPrevented,false);assert.equal(h.read()[0].axes[1],0);}
  for(const flags of [{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true}]){assert.equal(h.key('keydown','KeyJ',flags).defaultPrevented,false);assert.equal(h.read()[0].buttons[0].value,0);}
  h.key('keydown','KeyW');assert.equal(h.key('keyup','KeyW',{ctrlKey:true}).defaultPrevented,false);assert.equal(h.read()[0].axes[1],0);
  const host=new h.Input();host.id='keyboard-input-assist';assert.equal(h.key('keydown','KeyW',{target:host}).defaultPrevented,true);h.key('keyup','KeyW',{target:new h.Input()});assert.equal(h.read()[0].axes[1],0);
});

test('both Shift keys share minus without one keyup releasing the other',async()=>{
  const h=harness();await h.eden();h.key('keydown','ShiftLeft');h.key('keydown','ShiftRight');h.key('keyup','ShiftLeft');assert.equal(h.read()[0].buttons[8].value,1);h.key('keyup','ShiftRight');assert.equal(h.read()[0].buttons[8].value,0);
});

test('leaving Eden restores original indices, events and results, including analog values',async()=>{
  const h=harness(),p2=pad(2),native=[null,null,p2,null];h.pads(native);await h.eden();h.key('keydown','KeyJ');await h.session(null);assert.equal(h.read().filter(Boolean).length,0);h.tick(40);assert.equal(h.read(),native);assert.equal(h.padEvents.at(-1).index,2);assert.equal(h.padEvent('gamepaddisconnected',p2).stopped,false);assert.equal(h.key('keydown','KeyW').defaultPrevented,false);
});

test('rapid engine changes cancel older announcements and never enable Eden for another platform',async()=>{
  const h=harness();await h.session({engine:'Eden 0.2.1',platform:'switch'});await h.session({engine:'DOSBox',platform:'dos'});h.tick(80);assert.equal(h.read().filter(Boolean).length,0);assert.equal(h.padEvents.some(e=>e.type==='gamepadconnected'),false);
});

test('an older session response cannot reactivate Eden after the current session changes',async()=>{
  const h=harness();await h.settle();const pending=[];h.context.fetch=()=>new Promise(resolve=>pending.push(resolve));
  await h.session({engine:'Eden 0.2.1',platform:'switch'});await h.session({engine:'DOSBox',platform:'dos'});
  pending[1]({ok:true,json:async()=>({session:{engine:'DOSBox',platform:'dos'}})});await h.settle();
  pending[0]({ok:true,json:async()=>({session:{engine:'Eden 0.2.1',platform:'switch'}})});await h.settle();h.tick(40);
  assert.equal(h.read().filter(Boolean).length,0);h.key('keydown','KeyW');assert.equal(h.forwarded.at(-1).key,'ArrowUp');
});

test('DOSBox, MAME and RetroArch aliases still use their existing keyboard transport',async()=>{
  const h=harness();
  for(const [engine,code,key]of [['DOSBox','KeyW','ArrowUp'],['MAME','KeyJ','Control'],['MAME','Enter','1'],['RetroArch','ArrowUp','w']]){await h.session({engine});h.forwarded.length=0;h.key('keydown',code);assert.equal(h.forwarded.length,1);assert.equal(h.forwarded[0].key,key);assert.equal(h.forwarded[0].retrooneAlias,true);}
  await h.session({engine:'DOSBox'});h.pads([pad(0,[.8,-.8,0,0]),null,null,null]);h.forwarded.length=0;h.frame();assert.ok(h.forwarded.some(e=>e.key==='ArrowUp'));assert.ok(h.forwarded.some(e=>e.key==='ArrowRight'));h.blur();assert.ok(h.forwarded.some(e=>e.type==='keyup'));
});

test('a raw nonstandard controller is never mislabeled as a standard physical mapping',async()=>{
  const h=harness(),raw=pad(1,[.9,-.8,.7,-.6],'');const native=[null,raw,null,null];h.pads(native);await h.eden();assert.deepEqual(plain(h.read()[0].axes),[0,0,0,0]);h.key('keydown','KeyW');assert.equal(h.read()[0].axes[1],-1);await h.session(null);h.tick(40);assert.equal(h.read(),native);
});

test('the later Selkies touch wrapper cannot add a competing Eden slot and is retained outside Eden',async()=>{
  const h=harness();await h.settle();const navigator=h.context.navigator,prior=navigator.getGamepads,touch=pad(1,[.9,0,0,0]);
  navigator.getGamepads=()=>{const pads=Array.from(prior());pads[1]=touch;return pads;};
  h.document.dispatchEvent(new h.Event('DOMContentLoaded'));await h.eden();assert.equal(h.read().filter(Boolean).length,1);assert.equal(h.read()[0].id,'RetroOne Switch Controller');
  h.key('keydown','KeyW');assert.equal(h.read()[0].axes[1],-1);await h.session(null);h.tick(40);assert.equal(h.read()[1],touch);
});

test('actual pinned Selkies GamepadManager reports merged input and reindexes native input on exit',{skip:!process.env.SELKIES_BUNDLE},async()=>{
  const h=harness(),bundle=fs.readFileSync(process.env.SELKIES_BUNDLE,'utf8');const start=bundle.indexOf('D=class{constructor(e,t,n,r){this.gamepad=');const end=bundle.indexOf('};function xe',start);
  assert.ok(start>=0&&end>start,'Supply the inspected pinned Selkies bundle, not a different upstream version');
  h.context.T=4;h.context.E='linux';vm.runInContext('GamepadManager = '+bundle.slice(start+2,end+1),h.context);
  const changes=[];let manager;
  h.window.addEventListener('gamepadconnected',event=>{manager ||= new h.context.GamepadManager(event.gamepad,(...args)=>changes.push(['button',...args]),(...args)=>changes.push(['axis',...args]));});
  const physical=pad(0);h.pads([physical,null,null,null]);h.padEvent('gamepadconnected',physical);h.tick(16);changes.length=0;
  await h.eden();h.tick(16);
  assert.ok(changes.some(c=>c[0]==='axis'&&c[1]===0&&c[2]===2&&c[3]===physical.axes[2]),'existing native state must be announced again even when merged values are unchanged');
  h.key('keydown','KeyW');h.key('keydown','KeyJ');h.tick(16);
  assert.ok(changes.some(c=>c[0]==='axis'&&c[1]===0&&c[2]===1&&c[3]===-1));assert.ok(changes.some(c=>c[0]==='button'&&c[1]===0&&c[2]===0&&c[3]===1));
  h.key('keyup','KeyW');h.tick(16);assert.ok(changes.some(c=>c[0]==='axis'&&c[2]===1&&c[3]===physical.axes[1]));
  h.pads([null,null,pad(2),null]);await h.session(null);h.tick(40);changes.length=0;h.tick(16);assert.ok(changes.some(c=>c[0]==='axis'&&c[1]===2&&c[2]===2&&c[3]===physical.axes[2]));manager.destroy();
});

const presentation = (extra={}) => ({type:'retroone:presentation',version:1,embedded:true,touch:true,fit:'contain',immersive:true,menu:false,nativeTools:true,...extra});
const controls = (sequence=1,buttons={},axes=[0,0,0,0]) => ({type:'retroone:controls',version:1,sequence,buttons:Array.from({length:17},(_,i)=>buttons[i]||0),axes});

test('embedded app hides only pinned floating tool entries during play and restores them through its menu',async()=>{
  const h=harness();await h.settle();const root=h.document.documentElement;
  assert.equal(root.classes.has('retroone-app-playing'),false,'standalone desktop keeps upstream UI');
  assert.equal(h.elements.has('retroone-stream-style'),false);
  h.message(presentation({touch:false,immersive:false}));
  assert.equal(root.classes.has('retroone-app-playing'),true,'normal embedded play is independent of viewport and touch setting');
  assert.equal(root.classes.has('retroone-immersive'),false);
  const css=h.elements.get('retroone-stream-style').textContent;
  for(const selector of ['.dashboard-overlay-container .virtual-keyboard-button','.dashboard-overlay-container .toggle-handle'])
    assert.ok(css.includes('.retroone-app-playing '+selector));
  assert.equal(/#[\w-]*(?:keyboard-input-assist|overlayInput)\s*\{display:none/.test(css),false);
  h.message(presentation({touch:false,immersive:false,menu:true}));
  assert.equal(root.classes.has('retroone-app-playing'),false,'native keyboard/dashboard entries return with the app menu');
  h.message(presentation({touch:false,immersive:false}));
  assert.equal(root.classes.has('retroone-app-playing'),true);
  h.message(presentation({menu:true,immersive:true}));
  assert.equal(root.classes.has('retroone-app-playing'),false);
  assert.equal(root.classes.has('retroone-immersive'),false,'menu must reveal the upstream dashboard even if immersive requested');
});

test('a reconnected stream keeps floating tools reachable for an older open parent page',async()=>{
  const h=harness();await h.settle();const root=h.document.documentElement;
  const oldParent=presentation({touch:false,immersive:false});delete oldParent.nativeTools;
  h.message(oldParent);
  assert.equal(root.classes.has('retroone-app-playing'),false);
  h.message(presentation({touch:false,immersive:false}));
  assert.equal(root.classes.has('retroone-app-playing'),true);
  h.message({...oldParent,nativeTools:'true'});
  assert.equal(root.classes.has('retroone-app-playing'),true,'malformed presentation is ignored');
  h.message(oldParent);
  assert.equal(root.classes.has('retroone-app-playing'),false,'legacy behavior returns without losing the existing frame');
});

test('native-tools mode keeps pads neutral while retaining upstream keyboard-assist input until returning to play',async()=>{
  const h=harness();await h.session({engine:'DOSBox'});h.pads([pad(0,[.7,0,0,0]),null,null,null]);
  h.message(presentation());h.tick(40);
  // The parent dismisses its own panel but keeps menu:true for native tools.
  h.message(presentation({menu:true,immersive:false}));h.message(controls(1,{0:1,15:1}));h.frame();
  assert.ok(h.read()[0].axes.every(v=>v===0));assert.ok(h.read()[0].buttons.every(b=>b.value===0));
  assert.equal(h.forwarded.length,0,'gamepad-to-keyboard translation must remain gated');
  const assist=new h.Input();assist.id='keyboard-input-assist';
  h.key('keydown','KeyB',{target:assist});h.key('keyup','KeyB',{target:assist});
  assert.deepEqual(h.forwarded.map(e=>[e.type,e.key,e.target.id]),[['keydown','b','keyboard-input-assist'],['keyup','b','keyboard-input-assist']]);
  assert.equal(h.document.documentElement.classes.has('retroone-app-playing'),false);
  h.message(presentation());h.tick(40);
  assert.equal(h.read()[0].axes[0],.7);assert.equal(h.read()[0].buttons[0].value,0);
  assert.equal(h.document.documentElement.classes.has('retroone-app-playing'),true);
});

test('menu neutralizes every native slot without reindexing, losing metadata or changing the native gamepad preference',async()=>{
  const h=harness(),p1=pad(1),p3=pad(3,[.9,-.8,.7,-.6],'');const native=[null,p1,null,p3];
  h.pads(native);await h.settle();h.message(presentation({touch:false,menu:true,immersive:false}));
  const gated=h.read();assert.equal(gated.length,4);assert.equal(gated[0],null);assert.equal(gated[2],null);
  for(const index of [1,3]){
    const source=native[index],result=gated[index];
    for(const key of ['id','index','mapping','connected','timestamp','vibrationActuator','hapticActuators'])assert.equal(result[key],source[key],key);
    assert.ok(result.axes.every(value=>value===0));assert.ok(result.buttons.every(b=>b.value===0&&!b.pressed&&!b.touched));
  }
  assert.equal(h.padEvents.length,0);assert.equal(h.selfMessages.length,0);
  h.message(presentation({touch:false}));assert.equal(h.read(),native);assert.equal(h.padEvents.length,0);
});

test('actual pinned Selkies poller emits menu releases and restores native and merged holds without controller churn',{skip:!process.env.SELKIES_BUNDLE},async()=>{
  const h=harness(),bundle=fs.readFileSync(process.env.SELKIES_BUNDLE,'utf8');
  const start=bundle.indexOf('D=class{constructor(e,t,n,r){this.gamepad='),end=bundle.indexOf('};function xe',start);
  h.context.T=4;h.context.E='linux';vm.runInContext('GamepadManager = '+bundle.slice(start+2,end+1),h.context);
  const physical=pad(2);h.pads([null,null,physical,null]);await h.settle();
  const changes=[],manager=new h.context.GamepadManager(physical,(...v)=>changes.push(['button',...v]),(...v)=>changes.push(['axis',...v]));
  try{
    h.tick(16);changes.length=0;const events=h.padEvents.length;
    h.message(presentation({touch:false,menu:true,immersive:false}));h.tick(16);
    assert.ok(changes.some(c=>c[0]==='button'&&c[1]===2&&c[2]===6&&c[3]===0));
    assert.ok(changes.some(c=>c[0]==='axis'&&c[1]===2&&c[2]===1&&c[3]===0));assert.equal(h.padEvents.length,events);
    changes.length=0;h.message(presentation({touch:false}));h.tick(16);
    assert.ok(changes.some(c=>c[0]==='button'&&c[1]===2&&c[2]===6&&c[3]===.37));
    assert.ok(changes.some(c=>c[0]==='axis'&&c[1]===2&&c[2]===1&&c[3]===physical.axes[1]));assert.equal(h.padEvents.length,events);
    h.message(presentation());h.tick(56);h.message(controls(1,{0:1}));h.tick(16);
    const mergedEvents=h.padEvents.length;changes.length=0;
    h.message(presentation({menu:true,immersive:false}));h.tick(16);
    assert.ok(changes.some(c=>c[0]==='button'&&c[1]===0&&c[2]===0&&c[3]===0));
    assert.ok(h.read()[0].axes.every(v=>v===0));assert.equal(h.padEvents.length,mergedEvents);
    changes.length=0;h.message(presentation());h.tick(16);
    assert.ok(changes.some(c=>c[0]==='button'&&c[1]===0&&c[2]===6&&c[3]===.37));
    assert.equal(h.read()[0].buttons[0].value,0,'old touch must not revive');assert.equal(h.padEvents.length,mergedEvents);
  }finally{manager.destroy();}
});

test('parent touch full snapshots merge simultaneous controls, neutral releases only touch, and iframe blur is harmless',async()=>{
  const h=harness();await h.session({engine:'RetroArch',platform:'psp'});
  const physical=pad(2);h.pads([null,null,physical,null]);h.message(presentation());h.tick(40);
  h.message(controls(1,{0:1,4:1,6:.8,12:1},[.75,-1,-.4,.2]));
  assert.deepEqual(plain(h.read()[0].axes),[.75,-1,-.4,.2]);assert.equal(h.read()[0].buttons[4].value,1);assert.equal(h.read()[0].buttons[6].value,.8);
  h.blur();assert.equal(h.read()[0].buttons[0].value,1);
  h.message(controls(2,{4:1},[0,-1,0,0]));assert.equal(h.read()[0].buttons[0].value,0);assert.equal(h.read()[0].buttons[4].value,1);
  h.message(controls(3));assert.deepEqual(plain(h.read()[0].axes),physical.axes);assert.equal(h.read()[0].buttons[6].value,.37);
  assert.equal(h.messages[0].data.supported,true);
});

test('strict message authentication, bounded values and monotonic sequence reject hostile or stale state',async()=>{
  const h=harness();await h.settle();h.message(presentation());h.tick(40);
  for(const extra of [{origin:'https://evil.test'},{source:{}}])h.message(controls(1,{0:1}),extra);
  const bad=[{buttons:Array(17)},{axes:Array(4)},{buttons:[1]},{axes:[Infinity,0,0,0]},{buttons:Array(17).fill(NaN)},{sequence:-1},{sequence:1.5},{version:2},{axes:[0,0,0,2]}];
  for(const value of bad)h.message({...controls(),...value});
  assert.equal(h.read()[0].buttons[0].value,0);
  h.message(controls(4,{0:1}));h.message(controls(3));h.message(controls(4,{1:1}));assert.equal(h.read()[0].buttons[0].value,1);
  h.tick(1000);h.message(controls(4,{0:1}));h.tick(1100);assert.equal(h.read()[0].buttons[0].value,1);
  h.tick(200);assert.equal(h.read()[0].buttons[0].value,0);
});

test('hidden, pagehide, menu and disabling touch release snapshots without losing Eden keyboard mode',async()=>{
  const h=harness();await h.eden();h.message(presentation());h.message(controls(1,{0:1}));h.hidden(true);assert.equal(h.read()[0].buttons[0].value,0);
  h.hidden(false);h.message(controls(2,{0:1}));h.message(presentation({menu:true,immersive:false}));assert.equal(h.read()[0].buttons[0].value,0);
  h.message(controls(3,{0:1}));assert.equal(h.read()[0].buttons[0].value,0);
  h.message(presentation());h.message(controls(4,{0:1}));h.window.dispatchEvent(new h.Event('pagehide'));assert.equal(h.read()[0].buttons[0].value,0);
  h.message(presentation({touch:false}));h.key('keydown','KeyW');assert.equal(h.read()[0].axes[1],-1);
});

test('touch DOS/MAME keyboard bridge preserves multi-button modifiers, blur reacquisition and physical keyboard ownership',async()=>{
  const h=harness();await h.session({engine:'DOSBox'});h.message(presentation());h.tick(40);
  h.message(controls(1,{0:1,3:1,4:1,5:1,12:1}));h.frame();
  for(const key of ['ArrowUp','Enter','Control','Shift','Alt'])assert.ok(h.forwarded.some(e=>e.key===key&&e.type==='keydown'),key);
  const alt=h.forwarded.find(e=>e.key==='Alt');assert.equal(alt.ctrlKey,true);assert.equal(alt.shiftKey,true);
  h.forwarded.length=0;h.blur();h.tick(0);assert.ok(h.forwarded.some(e=>e.key==='Enter'&&e.type==='keydown'));
  h.key('keydown','Enter');h.forwarded.length=0;h.message(controls(2));h.frame();assert.equal(h.forwarded.some(e=>e.key==='Enter'&&e.type==='keyup'),false);
  h.key('keyup','Enter');assert.equal(h.forwarded.at(-1).key,'Enter');assert.equal(h.forwarded.at(-1).type,'keyup');
  await h.session({engine:'MAME'});h.message(controls(3,{0:1,4:1,8:1,9:1}));h.forwarded.length=0;h.frame();
  for(const key of ['Control','z','5','1'])assert.ok(h.forwarded.some(e=>e.key===key&&e.type==='keydown'),key);
  h.tick(1300);h.frame();assert.ok(h.forwarded.some(e=>e.key==='5'&&e.type==='keyup'));
});

test('presentation preserves stream intrinsic dimensions, centers contain, stretches only CSS and restores chrome',async()=>{
  const h=harness();await h.settle();const sink={width:1280,height:720,parentElement:{getBoundingClientRect:()=>({width:800,height:600})}};h.elements.set('videoCanvas',sink);
  h.message(presentation());const root=h.document.documentElement;assert.equal(root.props['--retroone-media-w'],'800px');assert.equal(root.props['--retroone-media-h'],'450px');assert.equal(root.props['--retroone-media-y'],'75px');assert.ok(root.classes.has('retroone-immersive'));assert.equal(sink.width,1280);
  h.message(presentation({fit:'stretch',menu:true,immersive:false}));assert.equal(root.props['--retroone-media-h'],'450px');assert.equal(root.classes.has('retroone-immersive'),false);assert.equal(sink.height,720);
  assert.equal(h.messages.at(-1).data.aspectRatio,1280/720);
});

test('stream cover and manual zoom crop around the viewport center without changing encoded resolution',async()=>{
  const h=harness();await h.settle();
  let box={width:800,height:600};
  const sink={width:1280,height:720,parentElement:{getBoundingClientRect:()=>box}};
  h.elements.set('videoCanvas',sink);
  const rect=()=>['w','h','x','y'].map(key=>parseFloat(h.document.documentElement.props['--retroone-media-'+key]));
  function check(expected){rect().forEach((value,i)=>assert.ok(Math.abs(value-expected[i])<1e-7,`${rect()} != ${expected}`));}
  h.message(presentation({zoom:1.5}));check([1200,675,-200,-37.5]);
  h.message(presentation({fit:'cover'}));check([3200/3,600,-400/3,0]);
  h.message(presentation({fit:'cover',zoom:1.5}));check([1600,900,-400,-150]);
  box={width:1200,height:600};h.tick(100);check([1800,1012.5,-300,-206.25]);
  h.message(presentation({fit:'stretch',zoom:1.5}));check([1800,900,-300,-150]);
  h.message(presentation());check([3200/3,600,200/3,0]);
  assert.deepEqual([sink.width,sink.height],[1280,720]);
  const css=h.elements.get('retroone-stream-style').textContent;
  assert.ok(css.includes('.video-container{overflow:hidden!important}'));
  assert.ok(css.includes('#overlayInput'),'mouse input rectangle shares the visual transform');
});

test('stream invalid zoom is ignored without changing geometry or held input; old messages reset to 100%',async()=>{
  const h=harness();await h.session({engine:'RetroArch'});
  h.elements.set('videoCanvas',{width:1280,height:720,parentElement:{getBoundingClientRect:()=>({width:800,height:600})}});
  h.message(presentation({zoom:2}));h.message(controls(1,{0:1}));h.tick(40);
  const before={...h.document.documentElement.props};
  for(const zoom of [null,'2',NaN,Infinity,-Infinity,0,.99,3.01,{},[]]){
    h.message(presentation({zoom,fit:'cover',menu:true}));
    assert.deepEqual(h.document.documentElement.props,before);
    assert.equal(h.read()[0].buttons[0].value,1);
  }
  h.message(presentation({zoom:3}));assert.equal(h.document.documentElement.props['--retroone-media-w'],'2400px');
  h.message(presentation());assert.equal(h.document.documentElement.props['--retroone-media-w'],'800px');
  assert.equal(h.read()[0].buttons[0].value,1);
});

test('actual pinned Selkies poller sees touch, physical merge and timeout release',{skip:!process.env.SELKIES_BUNDLE},async()=>{
  const h=harness(),bundle=fs.readFileSync(process.env.SELKIES_BUNDLE,'utf8');const start=bundle.indexOf('D=class{constructor(e,t,n,r){this.gamepad=');const end=bundle.indexOf('};function xe',start);
  assert.ok(start>=0&&end>start);h.context.T=4;h.context.E='linux';vm.runInContext('GamepadManager = '+bundle.slice(start+2,end+1),h.context);
  const changes=[];let manager;h.window.addEventListener('gamepadconnected',event=>{manager ||= new h.context.GamepadManager(event.gamepad,(...args)=>changes.push(['button',...args]),(...args)=>changes.push(['axis',...args]));});
  await h.session({engine:'RetroArch'});const physical=pad(2);h.pads([null,null,physical,null]);h.message(presentation());h.tick(40);
  h.message(controls(1,{0:1,4:1},[-.9,.8,0,0]));h.tick(16);
  assert.ok(changes.some(c=>c[0]==='button'&&c[2]===0&&c[3]===1));assert.ok(changes.some(c=>c[0]==='axis'&&c[2]===0&&c[3]===-.9));
  h.blur();h.tick(16);assert.equal(h.read()[0].buttons[0].value,1);changes.length=0;h.tick(1250);
  assert.ok(changes.some(c=>c[0]==='button'&&c[2]===0&&c[3]===0));assert.ok(changes.some(c=>c[0]==='axis'&&c[2]===0&&c[3]===physical.axes[0]));manager.destroy();
});

test('touch uses upstream gamepadControl to re-enable a disabled pinned poller; desktop preference remains untouched',{skip:!process.env.SELKIES_BUNDLE},async()=>{
  const h=harness(),bundle=fs.readFileSync(process.env.SELKIES_BUNDLE,'utf8');const start=bundle.indexOf('D=class{constructor(e,t,n,r){this.gamepad=');const end=bundle.indexOf('};function xe',start);
  h.context.T=4;h.context.E='linux';vm.runInContext('GamepadManager = '+bundle.slice(start+2,end+1),h.context);
  const changes=[];const physical=pad(0,[0,0,0,0]);h.pads([physical,null,null,null]);const manager=new h.context.GamepadManager(physical,(...args)=>changes.push(args),()=>{});manager.disable();
  // Execute the pinned command branch, with its ordinary surrounding state
  // supplied by the fixture, then the actual GamepadManager.enable/_poll code.
  const commandStart=bundle.indexOf('case`gamepadControl`:');const commandEnd=bundle.indexOf('case`clipboardUpdateFromUI`:',commandStart);
  assert.ok(commandStart>=0&&commandEnd>commandStart);h.context.N=false;h.context.j=new Set();h.context.Gt=()=>{};h.context.er=()=>{};h.context.ir=()=>manager.enable();
  vm.runInContext('upstreamToggle = t => { switch(t.type) { '+bundle.slice(commandStart,commandEnd)+' } }',h.context);
  h.window.addEventListener('message',e=>{if(e.source===h.window)h.context.upstreamToggle(e.data);});await h.session({engine:'RetroArch'});
  h.message(presentation({touch:false}));h.tick(100);assert.equal(h.selfMessages.length,0);assert.equal(h.context.N,false);
  h.message(presentation());h.tick(40);h.message(controls(1,{0:1}));h.tick(16);assert.equal(h.context.N,true);assert.ok(changes.some(c=>c[1]===0&&c[2]===1));assert.equal(h.selfMessages.length,1);
  h.window.webrtcInput={gamepadManager:manager};h.tick(100);assert.equal(h.selfMessages.length,2,'late transport attachment replays enable once');h.tick(100);assert.equal(h.selfMessages.length,2);
  h.message(presentation({touch:false}));h.tick(100);assert.equal(h.selfMessages.length,2,'closing overlay does not disable physical input');manager.destroy();
});
