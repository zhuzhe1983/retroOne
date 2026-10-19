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
  const timers = new Map(), frames = [], requests = [], forwarded = [], padEvents = [];
  class Event {
    constructor(type, options = {}) { this.type = type; Object.assign(this, options); this.defaultPrevented = false; this.stopped = false; }
    preventDefault() { this.defaultPrevented = true; }
    stopImmediatePropagation() { this.stopped = true; }
  }
  class Target {
    constructor() { this.listeners = []; this.id = ''; }
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
  window.location = {pathname:'/p/player3/stream/'};
  document.body = body; document.hidden = false; document.visibilityState = 'visible'; document.getElementById = () => null;
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
  return {context,window,document,body,Input,Textarea,Select,Event,requests,forwarded,padEvents,settle,tick,nativeRead,
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
  const map={KeyJ:0,KeyK:1,KeyU:2,KeyI:3,KeyQ:4,KeyE:5,Digit1:6,Digit3:7,ShiftLeft:8,Enter:9,KeyZ:10,KeyX:11};
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
