const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {TouchState,coordinates,preferences,automaticPreset,presets}=require('../web/mobile-player.js');

test('independent fingers combine directions and actions; lifting one keeps the other',()=>{
  const state=new TouchState();
  state.set(10,coordinates('dpad',-1,-1));
  let value=state.set(20,{buttons:{0:1}});
  assert.equal(value.buttons[12],1);assert.equal(value.buttons[14],1);assert.equal(value.buttons[0],1);
  value=state.remove(20);assert.equal(value.buttons[0],0);assert.equal(value.buttons[12],1);
  value=state.remove(10);assert.deepEqual(value,{buttons:Array(17).fill(0),axes:Array(4).fill(0)});
});
test('overlapping sources on the same action remain pressed until both release',()=>{
  const state=new TouchState();state.set(1,{buttons:{9:1}});state.set(2,{buttons:{9:.7}});
  assert.equal(state.remove(1).buttons[9],.7);assert.equal(state.remove(2).buttons[9],0);
});
test('dual analog sticks retain continuous values independently and clamp radial motion',()=>{
  const state=new TouchState();state.set(1,coordinates('left',.32,-.65));state.set(2,coordinates('right',-.8,.3));
  assert.deepEqual(state.snapshot().axes,[.32,-.65,-.8,.3]);
  const diagonal=coordinates('left',1,1);assert.ok(Math.abs(Math.hypot(...Object.values(diagonal.axes))-1)<1e-10);
  assert.deepEqual(state.remove(1).axes,[0,0,-.8,.3]);
  assert.deepEqual(state.clear().axes,[0,0,0,0]);
});
test('invalid state values never reach the standard wire payload',()=>{
  const state=new TouchState();const value=state.set(1,{buttons:{0:Infinity,1:2,2:-1,99:1,'1.5':1},axes:{0:NaN,1:Infinity,2:4,3:-4,4:1}});
  assert.deepEqual(value.buttons,[0,1,...Array(15).fill(0)]);assert.deepEqual(value.axes,[0,0,1,-1]);
});
test('device preferences are bounded, opt-in on desktop, and contain by default',()=>{
  assert.deepEqual(preferences(null),{fit:'contain',size:1,opacity:.78,touch:false,preset:'auto'});
  assert.equal(preferences(null,true).touch,true);
  assert.deepEqual(preferences({fit:'cover',size:20,opacity:-5,preset:'evil',touch:'true'}),{fit:'contain',size:1.3,opacity:.35,touch:false,preset:'auto'});
  assert.equal(preferences({size:NaN}).size,1);
});
test('automatic controls distinguish small consoles, arcade, MD, DOS and modern pads',()=>{
  for(const [platform,preset]of Object.entries({nes:'classic',gba:'gba',snes:'nintendo',nds:'nintendo','3ds':'nintendo3ds',psp:'psp',switch:'switch',ps2:'playstation',dos:'dos',mame:'arcade',fbneo:'arcade',neogeo:'arcade',megadrive:'sega'}))assert.equal(automaticPreset(platform),preset);
  assert.deepEqual(presets.dos.face,['Enter','Esc','Space','Ctrl']);
  assert.deepEqual(presets.sega.face,['B','C','A','Y','X','Z']);
  assert.equal(presets.arcade.face.length,6);assert.equal(presets.arcade.sticks,0);
  assert.deepEqual(presets.nintendo3ds.face,['B','A','Y','X']);
  assert.equal(presets.nintendo3ds.sticks,2);assert.equal(presets.nintendo3ds.triggers,true);
  assert.equal(presets.nintendo.sticks,0);
});
test('mobile UI and preset labels have English translations',()=>{
  const context=vm.createContext({});context.window=context;
  vm.runInContext(fs.readFileSync(require.resolve('../web/i18n.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(require.resolve('../web/i18n-en.js'),'utf8'),context);
  context.RetroI18n.setLocale('en');
  const source=fs.readFileSync(require.resolve('../web/mobile-player.js'),'utf8');
  const strings=[...source.matchAll(/\bt\('([^']+)'\)|data-mobile-text="([^"]+)"/g)].map(match=>match[1]||match[2]);
  strings.push(...Object.values(presets).map(preset=>preset.label));
  for(const text of strings.filter(text=>/\p{Script=Han}/u.test(text)))assert.notEqual(context.RetroI18n.t(text),text,text);
});
