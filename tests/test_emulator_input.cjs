/* No ROM/BIOS or WebAssembly required. Uses the pinned GameManager when present. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../web/emulator-input.js'),'utf8');
const managerPath = process.env.EJS_GAME_MANAGER || path.join(__dirname,'../vendor/emulatorjs/data/src/GameManager.js');
const hasPinned = fs.existsSync(managerPath);
const plain = value => JSON.parse(JSON.stringify(value));
const presentation = (extra={}) => ({type:'retroone:presentation',version:1,embedded:true,touch:true,fit:'contain',immersive:true,menu:false,...extra});
const controls = (sequence=1,buttons={},axes=[0,0,0,0]) => ({type:'retroone:controls',version:1,sequence,buttons:Array.from({length:17},(_,i)=>buttons[i]||0),axes});
function harness(initialManager=true) {
  let now=0;
  const timers=[],messages=[],calls=[],elements=new Map();
  class Target {
    constructor(){this.events={};this.classes=new Set();this.props={};this.dataset={};this.classList={toggle:(key,value)=>value?this.classes.add(key):this.classes.delete(key)};this.style={setProperty:(key,value)=>{this.props[key]=value;}};}
    addEventListener(type,fn){(this.events[type]||=[]).push(fn);}
    emit(type,event={}){for(const fn of this.events[type]||[])fn(event);}
    appendChild(child){elements.set(child.id,child);}
  }
  const window=new Target(),document=new Target();
  window.location={origin:'https://retroone.test'};window.parent={postMessage:(data,origin)=>messages.push({data,origin})};
  document.documentElement=new Target();document.head=new Target();document.hidden=false;document.createElement=()=>new Target();document.getElementById=id=>elements.get(id)||null;
  const context=vm.createContext({window,document,console,performance:{now:()=>now},setInterval:(fn,ms)=>timers.push({fn,ms,next:now+ms})});
  if(hasPinned)vm.runInContext(fs.readFileSync(managerPath,'utf8'),context);
  function makeManager(){
    const gm=hasPinned?Object.create(window.EJS_GameManager.prototype):{simulateInput(player,index,value){this.functions.simulateInput(player,index,value);},getVideoDimensions(){return 4/3;}};
    gm.EJS={isNetplay:false,settings:{},displayMessage(){},localization:value=>value};
    gm.functions={simulateInput:(...args)=>calls.push(args),getVideoDimensions:()=>4/3};
    gm.quickSave=()=>{calls.push(['save']);return true;};return gm;
  }
  const canvas={width:256,height:240,parentElement:{getBoundingClientRect:()=>({width:900,height:400})}};
  window.EJS_emulator={gameManager:initialManager?makeManager():null,started:initialManager,canvas};
  vm.runInContext(source,context);
  return {window,document,context,calls,messages,canvas,makeManager,elements,
    tick(ms){const end=now+ms;for(;;){const t=timers.filter(t=>t.next<=end).sort((a,b)=>a.next-b.next)[0];if(!t)break;now=t.next;t.next+=t.ms;t.fn();}now=end;},
    message(data,extra={}){window.emit('message',{data,origin:window.location.origin,source:window.parent,...extra});},
    hidden(value){document.hidden=value;document.emit('visibilitychange');},
    manager:()=>window.EJS_emulator.gameManager,
  };
}

test('canonical touch maps all standard buttons, both analog sticks and simultaneous state without touching hotkeys',()=>{
  const h=harness();h.message(presentation());const map=[0,8,1,9,10,11,12,13,2,3,14,15,4,5,6,7];let seq=0;
  for(let button=0;button<16;button++){h.calls.length=0;h.message(controls(++seq,{[button]:1}));assert.ok(h.calls.some(([p,i,v])=>p===0&&i===map[button]&&v===1),`button ${button}`);h.message(controls(++seq));}
  h.calls.length=0;h.message(controls(++seq,{0:1,8:1,12:1,16:1},[.75,-.5,-1,.25]));
  for(const [i,v]of [[0,1],[2,1],[4,1],[16,24575],[19,16383],[21,32767],[22,8191]])assert.ok(h.calls.some(([p,index,value])=>index===i&&value===v),`input ${i}`);
  assert.equal(h.calls.some(([,i])=>i>=24),false);h.message(controls(++seq));assert.ok(h.calls.some(([,i,v])=>i===21&&v===0));
});

test('physical/keyboard controls and other players retain ownership when touch is released or times out',()=>{
  const h=harness();h.message(presentation());const gm=h.manager();gm.simulateInput(0,0,1);gm.simulateInput(0,16,9876);
  h.message(controls(1,{0:1,1:1},[.8,0,0,0]));h.calls.length=0;h.message(controls(2));
  assert.ok(h.calls.some(([p,i,v])=>i===0&&v===1));assert.ok(h.calls.some(([p,i,v])=>i===16&&v===9876));
  gm.simulateInput(1,0,1);assert.deepEqual(h.calls.at(-1),[1,0,1]);
  h.message(controls(3,{0:1}));gm.simulateInput(0,0,0);assert.deepEqual(h.calls.at(-1),[0,0,1]);h.tick(1300);assert.deepEqual(h.calls.at(-1),[0,0,0]);
});

test('menu gates all native players, keeps their latest held state and restores it without reviving released touch',()=>{
  const h=harness();h.message(presentation());const gm=h.manager();
  gm.simulateInput(0,0,1);gm.simulateInput(1,16,12345);gm.simulateInput(3,7,1);
  h.message(controls(1,{1:1}));h.calls.length=0;
  h.message(presentation({menu:true,immersive:false}));
  for(const [player,index]of [[0,0],[0,8],[1,16],[3,7]])
    assert.ok(h.calls.some(([p,i,v])=>p===player&&i===index&&v===0),`release ${player}/${index}`);
  h.calls.length=0;
  gm.simulateInput(0,0,0);gm.simulateInput(1,16,22222);gm.simulateInput(2,5,1);
  assert.deepEqual(h.calls,[[0,0,0],[1,16,0],[2,5,0]]);
  h.message(controls(2,{1:1}));assert.equal(h.calls.length,3);
  h.message(presentation());
  for(const [player,index,value]of [[0,0,0],[1,16,22222],[2,5,1],[3,7,1]])
    assert.ok(h.calls.some(([p,i,v])=>p===player&&i===index&&v===value),`restore ${player}/${index}`);
  assert.equal(h.calls.some(([p,i,v])=>p===0&&i===8&&v===1),false);
  h.calls.length=0;h.tick(1300);assert.equal(h.calls.length,0,'touch watchdog must not clear restored native state');
});

test('menu does not disable upstream quick-save hotkeys or custom calls outside ordinary game inputs',{skip:!hasPinned},()=>{
  const h=harness();h.message(presentation({menu:true,immersive:false}));
  h.manager().simulateInput(0,24,1);assert.deepEqual(h.calls.at(-1),['save']);
  h.manager().simulateInput(1,0,1);assert.deepEqual(h.calls.at(-1),[1,0,0]);
  h.message(presentation({touch:false}));assert.deepEqual(h.calls.at(-1),[1,0,1]);
});

test('rejects wrong source/origin, invalid values, reordered snapshots and mutated duplicate sequences',()=>{
  const h=harness();h.message(presentation());
  for(const extra of [{origin:'https://evil.test'},{source:h.window}, {source:{}}])h.message(controls(1,{0:1}),extra);
  for(const data of [{buttons:Array(17)},{axes:Array(4)},{buttons:Array(17).fill(NaN)},{axes:[0,0,0,Infinity]},{axes:[0,0]},{buttons:Array(17).fill('1')},{sequence:1.5},{sequence:-1},{sequence:Number.MAX_SAFE_INTEGER+1},{version:9}])h.message({...controls(1,{0:1}),...data});
  assert.equal(h.calls.length,0);h.message(controls(9,{0:1}));h.message(controls(8));h.message(controls(9,{1:1}));assert.deepEqual(h.calls.at(-1),[0,0,1]);
  h.tick(1000);h.message(controls(9,{0:1}));h.tick(1100);assert.deepEqual(h.calls.at(-1),[0,0,1]);h.tick(200);assert.deepEqual(h.calls.at(-1),[0,0,0]);
});

test('frame blur preserves parent-held controls; hidden, pagehide, menu and disabled overlay release them',()=>{
  const h=harness();h.message(presentation());h.message(controls(1,{0:1}));h.window.emit('blur');assert.deepEqual(h.calls.at(-1),[0,0,1]);
  h.hidden(true);assert.deepEqual(h.calls.at(-1),[0,0,0]);h.message(controls(2,{0:1}));assert.deepEqual(h.calls.at(-1),[0,0,0]);
  h.hidden(false);h.message(controls(3,{0:1}));h.window.emit('pagehide');assert.deepEqual(h.calls.at(-1),[0,0,0]);
  h.message(controls(4,{0:1}));h.message(presentation({menu:true,immersive:false}));assert.deepEqual(h.calls.at(-1),[0,0,0]);h.message(controls(5,{0:1}));assert.deepEqual(h.calls.at(-1),[0,0,0]);
  h.message(presentation());h.message(controls(6,{0:1}));h.message(presentation({touch:false}));assert.deepEqual(h.calls.at(-1),[0,0,0]);
});

test('late core binding and core replacement announce readiness, preserve native methods and require fresh snapshots',()=>{
  const h=harness(false);assert.equal(h.messages.at(-1).data.supported,false);h.message(presentation());h.message(controls(1,{0:1}));assert.equal(h.calls.length,0);assert.equal(h.document.documentElement.classes.has('retroone-touch'),false);assert.equal(h.document.documentElement.classes.has('retroone-immersive'),false);
  h.window.EJS_emulator.gameManager=h.makeManager();h.window.EJS_emulator.started=true;h.tick(100);assert.equal(h.messages.at(-1).data.supported,true);assert.equal(h.document.documentElement.classes.has('retroone-touch'),true);
  h.message(controls(1,{0:1}));assert.deepEqual(h.calls.at(-1),[0,0,1]);const old=h.manager();
  h.window.EJS_emulator.gameManager=h.makeManager();h.tick(100);assert.deepEqual(h.calls.at(-1),[0,0,0]);assert.equal(old.simulateInput,hasPinned?h.window.EJS_GameManager.prototype.simulateInput:old.simulateInput);
  h.message(controls(2,{1:1}));assert.deepEqual(h.calls.at(-1),[0,8,1]);
});

test('presentation centers core-native aspect, stretches horizontally only and reveals existing tools',()=>{
  const h=harness();h.message(presentation());const root=h.document.documentElement;
  assert.ok(Math.abs(parseFloat(root.props['--retroone-media-w'])-400*4/3)<1e-9);assert.ok(Math.abs(parseFloat(root.props['--retroone-media-h'])-400)<1e-9);assert.equal(root.classes.has('retroone-immersive'),true);
  h.message(presentation({fit:'stretch',menu:true,immersive:false}));assert.equal(root.props['--retroone-media-w'],'900px');assert.ok(Math.abs(parseFloat(root.props['--retroone-media-h'])-400)<1e-9);assert.equal(root.classes.has('retroone-menu'),true);assert.equal(root.classes.has('retroone-immersive'),false);
  h.canvas.parentElement.getBoundingClientRect=()=>({width:300,height:500});h.tick(100);assert.equal(root.props['--retroone-media-h'],'225px');assert.equal(root.props['--retroone-media-y'],'137.5px');assert.equal(h.canvas.width,256);assert.equal(h.canvas.height,240);
  const css=h.elements.get('retroone-emulator-style').textContent;assert.ok(css.includes('body>header'));assert.ok(css.includes('.ejs_virtualGamepad_parent'));assert.ok(css.includes('.retroone-immersive .ejs_virtualGamepad_open'));assert.equal(h.messages.at(-1).data.aspectRatio,4/3);
});

test('actual pinned GameManager preserves quick-save hotkey and netplay routing outside the touch map',{skip:!hasPinned},()=>{
  const h=harness();h.manager().simulateInput(0,24,1);assert.deepEqual(h.calls.at(-1),['save']);
  const network=[];h.manager().EJS.isNetplay=true;h.manager().EJS.netplay={simulateInput:(...args)=>network.push(args)};
  h.message(presentation());h.message(controls(1,{0:1}));assert.deepEqual(network.at(-1),[0,0,1]);h.message(controls(2));assert.deepEqual(network.at(-1),[0,0,0]);
});
