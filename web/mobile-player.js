/* Same-origin touch controller and presentation shell. No game/save/session mutations. */
((global) => {
  'use strict';
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const presets = {
    classic: {label:'经典双键', face:['B','A'], shoulders:false, sticks:0},
    gba: {label:'掌机双键', face:['B','A'], shoulders:true, sticks:0},
    nintendo: {label:'任天堂四键', face:['B','A','Y','X'], shoulders:true, sticks:0},
    nintendo3ds: {label:'Nintendo 3DS / 双摇杆', face:['B','A','Y','X'], shoulders:true, sticks:2, triggers:true},
    switch: {label:'Switch / 双摇杆', face:['B','A','Y','X'], shoulders:true, sticks:2, triggers:true},
    psp: {label:'PSP / 单摇杆', face:['×','○','□','△'], shoulders:true, sticks:1},
    playstation: {label:'PlayStation / 双摇杆', face:['×','○','□','△'], shoulders:true, sticks:2, triggers:true},
    standard: {label:'标准手柄 / 双摇杆', face:['A','B','X','Y'], shoulders:true, sticks:2, triggers:true},
    dos: {label:'DOS / 键盘', face:['Enter','Esc','Space','Ctrl'], shoulders:true, sticks:0},
    arcade: {label:'街机 / 六键', face:['1','2','3','4','5','6'], shoulders:false, sticks:0},
    sega: {label:'SEGA MD / 六键', face:['B','C','A','Y','X','Z'], shoulders:false, sticks:0},
  };
  function automaticPreset(platform='') {
    if (['nes','fds','gb','gbc','gamegear','mastersystem','atari2600','atari7800','colecovision','ngp','ngpc','wonderswan','wonderswancolor'].includes(platform)) return 'classic';
    if (platform==='gba') return 'gba';
    if (['snes','nds'].includes(platform)) return 'nintendo';
    if (platform==='3ds') return 'nintendo3ds';
    if (platform==='switch') return 'switch';
    if (platform==='psp') return 'psp';
    if (['psx','ps2','ps3','psvita'].includes(platform)) return 'playstation';
    if (['dos','windows','pc98','scummvm'].includes(platform)) return 'dos';
    if (['arcade','mame','fbneo','fba','neogeo','hbmame'].includes(platform)) return 'arcade';
    if (['megadrive','genesis','md','segacd','sega32x','32x'].includes(platform)) return 'sega';
    return 'standard';
  }
  function preferences(raw, touch=false) {
    raw = raw && typeof raw==='object' ? raw : {};
    return {fit:raw.fit==='stretch'?'stretch':'contain',size:Number.isFinite(raw.size)?clamp(raw.size,.75,1.3):1,opacity:Number.isFinite(raw.opacity)?clamp(raw.opacity,.35,1):.78,touch:typeof raw.touch==='boolean'?raw.touch:touch,preset:Object.hasOwn(presets,raw.preset)?raw.preset:'auto'};
  }
  class TouchState {
    constructor(){this.sources=new Map();}
    set(id,value){this.sources.delete(id);this.sources.set(id,value);return this.snapshot();}
    remove(id){this.sources.delete(id);return this.snapshot();}
    clear(){this.sources.clear();return this.snapshot();}
    snapshot(){
      const buttons=Array(17).fill(0),axes=Array(4).fill(0);
      for(const source of this.sources.values()){
        for(const [index,value]of Object.entries(source.buttons||{}))if(Number.isInteger(+index)&&+index>=0&&+index<17&&Number.isFinite(value))buttons[index]=Math.max(buttons[index],clamp(value,0,1));
        for(const [index,value]of Object.entries(source.axes||{}))if(Number.isInteger(+index)&&+index>=0&&+index<4&&Number.isFinite(value))axes[index]=clamp(value,-1,1);
      }
      return {buttons,axes};
    }
  }
  function coordinates(kind,x,y){
    x=clamp(x,-1,1);y=clamp(y,-1,1);
    if(kind==='dpad')return {buttons:{12:y<-.28?1:0,13:y>.28?1:0,14:x<-.28?1:0,15:x>.28?1:0}};
    const length=Math.hypot(x,y);if(length>1){x/=length;y/=length;}
    const offset=kind==='right'?2:0;return {axes:{[offset]:Math.abs(x)<.08?0:x,[offset+1]:Math.abs(y)<.08?0:y}};
  }
  const pure={TouchState,coordinates,preferences,automaticPreset,presets};
  if(typeof module!=='undefined'&&module.exports)module.exports=pure;
  if(!global.document)return;
  const document=global.document,$=id=>document.getElementById(id),t=(source,params={})=>global.RetroI18n?.t(source,params)||source;
  const escape=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const dialog=$('play-dialog'),frameWrap=$('play-frame-wrap'),toolbar=dialog.querySelector('.play-toolbar');
  const body=document.createElement('div');body.className='play-body';frameWrap.before(body);body.append(frameWrap);
  const left=document.createElement('div'),right=document.createElement('div'),system=document.createElement('div');
  left.className='touch-rail touch-left';right.className='touch-rail touch-right';system.className='touch-system';body.prepend(left);body.append(right,system);
  dialog.insertAdjacentHTML('beforeend',`<button id="mobile-menu-toggle" class="mobile-menu-toggle" type="button" aria-expanded="false" aria-controls="mobile-player-menu">☰</button><section id="mobile-player-menu" class="mobile-player-menu" hidden aria-label="${escape(t('游玩菜单'))}"><div class="mobile-menu-heading"><strong data-mobile-text="游玩菜单"></strong><button id="mobile-menu-close" class="button button-secondary" data-mobile-text="返回游戏"></button></div><div class="mobile-player-settings"><label><span data-mobile-text="触控手柄"></span><input id="touch-enabled" type="checkbox"></label><label><span data-mobile-text="按键布局"></span><select id="touch-preset"></select></label><label><span data-mobile-text="画面比例"></span><select id="touch-fit"><option value="contain" data-mobile-text="原比例居中"></option><option value="stretch" data-mobile-text="横向铺满"></option></select></label><label><span data-mobile-text="按键大小"></span><input id="touch-size" type="range" min="0.75" max="1.3" step="0.05"></label><label><span data-mobile-text="按键透明度"></span><input id="touch-opacity" type="range" min="0.35" max="1" step="0.01"></label></div><p class="mobile-menu-note" data-mobile-text="设置只保存在这台设备的当前玩家名下，不改变模拟器分辨率。"></p><p class="mobile-menu-note" data-mobile-text="模拟器与存档工具已展开；可在游戏画面内操作。"></p><p id="touch-status" class="mobile-menu-note" role="status"></p></section>`);
  const menu=$('mobile-player-menu'),input=new TouchState(),pointers=new Map();
  let context=null,prefs=preferences(null),ready=false,supported=true,menuOpen=false,sequence=0,timer=null,waitingTimer=null,immersive=false,fullscreenPending=false;
  function storageKey(){return `retroone:touch:${context.profileId}`;}
  function save(){try{localStorage.setItem(storageKey(),JSON.stringify(prefs));}catch{}}
  function post(payload){if(context?.iframe?.contentWindow)context.iframe.contentWindow.postMessage(payload,location.origin);}
  function interactive(){return !!context&&dialog.open&&prefs.touch&&ready&&supported&&!menuOpen&&!document.hidden&&!document.querySelector('dialog[open]:not(#play-dialog)');}
  function send(){if(context)post({type:'retroone:controls',version:1,...input.snapshot(),sequence:++sequence});}
  function presentation(){if(context)post({type:'retroone:presentation',version:1,fit:prefs.fit,embedded:true,touch:prefs.touch,menu:menuOpen,immersive:immersive&&!menuOpen});}
  function release(){input.clear();pointers.clear();for(const node of dialog.querySelectorAll('[data-touch-index], [data-touch-zone]')){node.classList.remove('is-pressed');node.style.removeProperty('--stick-x');node.style.removeProperty('--stick-y');}send();}
  function status(){
    $('touch-status').textContent=!ready?t('正在连接触控输入；也可使用原模拟器控件。'):!supported?t('此运行环境尚不支持这套触控，请使用原模拟器控件或实体手柄。'):effectivePreset()==='dos'?t('DOS 默认键盘布局；不同游戏可在模拟器内调整按键。'):t('实体手柄与键盘仍可使用。');
  }
  function effectivePreset(){return prefs.preset==='auto'?automaticPreset(context?.platform):prefs.preset;}
  function button(index,label,cls=''){return `<button type="button" class="touch-button ${cls}" data-touch-index="${index}" aria-label="${escape(label)}">${escape(label)}</button>`;}
  function stick(side){return `<div class="touch-stick" data-touch-zone="${side}" role="group" aria-label="${escape(t(side==='left'?'左摇杆':'右摇杆'))}"><span class="touch-stick-knob"></span><small>${side==='left'?'L':'R'}</small></div>`;}
  function renderControls(){
    const layout=presets[effectivePreset()],dos=effectivePreset()==='dos',dual=layout.sticks===2,is3ds=effectivePreset()==='nintendo3ds';
    const shoulders=side=>layout.shoulders?`<div class="touch-shoulders">${button(side==='left'?4:5,dos?(side==='left'?'Shift':'Alt'):effectivePreset()==='arcade'?(side==='left'?'E':'F'):(side==='left'?'L':'R'))}${layout.triggers?button(side==='left'?6:7,is3ds?(side==='left'?'ZL':'ZR'):(side==='left'?'L2':'R2')):''}</div>`:'';
    left.innerHTML=`${shoulders('left')}<div class="touch-dpad" data-touch-zone="dpad" role="group" aria-label="${escape(t('方向键'))}">${button(12,'↑','dpad-up')}${button(13,'↓','dpad-down')}${button(14,'←','dpad-left')}${button(15,'→','dpad-right')}</div>${layout.sticks?stick('left'):''}`;
    right.innerHTML=`${shoulders('right')}<div class="touch-face">${layout.face.map((label,index)=>button(index,label,`face-${index}`)).join('')}</div>${dual?stick('right'):''}`;
    system.innerHTML=`${dual&&!is3ds?button(10,'L3'):''}${button(8,dos?'Esc':effectivePreset()==='arcade'?t('投币'):effectivePreset()==='switch'?'−':'SELECT')}${button(9,dos?'Enter':effectivePreset()==='switch'?'+':'START')}${dual&&!is3ds?button(11,'R3'):''}`;
    dialog.dataset.touchLayout=effectivePreset();update();
  }
  function update(){
    dialog.classList.toggle('touch-active',!!context&&prefs.touch);dialog.classList.toggle('mobile-immersive',!!context&&immersive);dialog.classList.toggle('player-menu-open',menuOpen);
    dialog.style.setProperty('--touch-size',prefs.size);dialog.style.setProperty('--touch-opacity',prefs.opacity);
    for(const rail of [left,right,system])rail.inert=!interactive();
    $('touch-enabled').checked=prefs.touch;$('touch-preset').value=prefs.preset;$('touch-fit').value=prefs.fit;$('touch-size').value=prefs.size;$('touch-opacity').value=prefs.opacity;
    $('mobile-menu-toggle').setAttribute('aria-expanded',String(menuOpen));menu.hidden=!menuOpen;status();presentation();
  }
  function localize(){
    for(const element of dialog.querySelectorAll('[data-mobile-text]'))element.textContent=t(element.dataset.mobileText);
    $('mobile-menu-toggle').setAttribute('aria-label',t(menuOpen?'返回游戏':'游玩菜单'));menu.setAttribute('aria-label',t('游玩菜单'));
    $('touch-preset').innerHTML=`<option value="auto">${escape(t('按平台自动选择'))}</option>${Object.entries(presets).map(([id,preset])=>`<option value="${id}">${escape(t(preset.label))}</option>`).join('')}`;
    $('touch-preset').value=prefs.preset;
    for(const zone of dialog.querySelectorAll('[data-touch-zone]'))zone.setAttribute('aria-label',t(zone.dataset.touchZone==='dpad'?'方向键':zone.dataset.touchZone==='left'?'左摇杆':'右摇杆'));
    if(effectivePreset()==='arcade'&&system.querySelector('[data-touch-index="8"]')){const coin=system.querySelector('[data-touch-index="8"]');coin.textContent=t('投币');coin.setAttribute('aria-label',t('投币'));}
    status();
  }
  function setMenu(open){
    if(!context)return;
    release();menuOpen=!!open;
    if(menuOpen)menu.append(toolbar);else dialog.insertBefore(toolbar,body);
    update();localize();
    if(menuOpen){menu.scrollTop=0;$('mobile-menu-close').focus({preventScroll:true});}
    else context.iframe.focus({preventScroll:true});
  }
  function navigate(action){
    if(!menuOpen)return;
    if(action==='back'){setMenu(false);return;}
    const active=document.activeElement,delta=action==='left'?-1:1;
    if(active?.matches('select')&&['left','right','accept'].includes(action)){
      active.selectedIndex=(active.selectedIndex+delta+active.options.length)%active.options.length;active.dispatchEvent(new Event('input',{bubbles:true}));return;
    }
    if(active?.matches('input[type=range]')&&['left','right'].includes(action)){
      if(delta<0)active.stepDown();else active.stepUp();active.dispatchEvent(new Event('input',{bubbles:true}));return;
    }
    if(action==='accept'){active?.click();return;}
    if(['left','right','up','down'].includes(action)){
      const elements=[...menu.querySelectorAll('button:not(:disabled),select,input')].filter(node=>!node.hidden&&node.getClientRects().length);
      const index=elements.indexOf(active),step=['left','up'].includes(action)?-1:1;elements[(index+step+elements.length)%elements.length]?.focus();
    }
  }
  function open(options){
    close();context=options;ready=false;supported=true;
    let saved;try{saved=JSON.parse(localStorage.getItem(storageKey()));}catch{}
    const touch=global.matchMedia('(pointer: coarse)').matches||navigator.maxTouchPoints>0;
    prefs=preferences(saved,touch);immersive=touch&&Math.min(innerWidth,innerHeight)<700;menuOpen=false;
    localize();renderControls();presentation();send();
    // A child can announce ready before its load event (for example, while media loads).
    const onLoad=()=>{release();presentation();status();};context.onLoad=onLoad;context.iframe.addEventListener('load',onLoad);
    timer=setInterval(()=>{if(interactive())send();else if(input.sources.size)release();},250);
    waitingTimer=setTimeout(()=>{if(context&&!ready){status();$('mobile-menu-toggle').classList.add('needs-attention');}},5000);
  }
  function close(){
    if(context){release();context.iframe.removeEventListener('load',context.onLoad);}
    clearInterval(timer);clearTimeout(waitingTimer);context=null;ready=false;menuOpen=false;immersive=false;
    dialog.insertBefore(toolbar,body);
    menu.hidden=true;dialog.classList.remove('touch-active','mobile-immersive','player-menu-open');$('mobile-menu-toggle').classList.remove('needs-attention');
  }
  function pointerValue(event,target){
    const zone=target.dataset.touchZone;
    if(!zone)return {buttons:{[target.dataset.touchIndex]:1}};
    const rect=target.getBoundingClientRect(),x=(event.clientX-rect.left-rect.width/2)/(rect.width*.38),y=(event.clientY-rect.top-rect.height/2)/(rect.height*.38);
    const value=coordinates(zone,x,y);
    if(zone!=='dpad'){const offset=zone==='left'?0:2;target.style.setProperty('--stick-x',`${value.axes[offset]*26}px`);target.style.setProperty('--stick-y',`${value.axes[offset+1]*26}px`);}
    return value;
  }
  body.addEventListener('pointerdown',event=>{
    const target=event.target.closest('[data-touch-zone]')||event.target.closest('[data-touch-index]');
    if(!target||!interactive()||(event.pointerType==='mouse'&&event.button!==0))return;
    event.preventDefault();pointers.set(event.pointerId,target);try{target.setPointerCapture(event.pointerId);}catch{}
    target.classList.add('is-pressed');input.set(event.pointerId,pointerValue(event,target));send();
  });
  body.addEventListener('pointermove',event=>{const target=pointers.get(event.pointerId);if(!target)return;event.preventDefault();input.set(event.pointerId,pointerValue(event,target));send();});
  const endPointer=event=>{const target=pointers.get(event.pointerId);if(!target)return;pointers.delete(event.pointerId);input.remove(event.pointerId);if(![...pointers.values()].includes(target)){target.classList.remove('is-pressed');target.style.removeProperty('--stick-x');target.style.removeProperty('--stick-y');}send();};
  for(const name of ['pointerup','pointercancel','lostpointercapture'])body.addEventListener(name,endPointer);
  body.addEventListener('contextmenu',event=>{if(event.target.closest('.touch-rail,.touch-system'))event.preventDefault();});
  body.addEventListener('keydown',event=>{const target=event.target.closest('[data-touch-index]');if(!target||!interactive()||!['Space','Enter'].includes(event.code))return;event.preventDefault();input.set(`key:${target.dataset.touchIndex}`,{buttons:{[target.dataset.touchIndex]:1}});send();});
  body.addEventListener('keyup',event=>{const target=event.target.closest('[data-touch-index]');if(!target||!['Space','Enter'].includes(event.code))return;event.preventDefault();input.remove(`key:${target.dataset.touchIndex}`);send();});
  global.addEventListener('message',event=>{
    if(!context||event.origin!==location.origin||event.source!==context.iframe.contentWindow||event.data?.type!=='retroone:controls-ready'||event.data.version!==1)return;
    ready=true;supported=event.data.supported!==false;clearTimeout(waitingTimer);$('mobile-menu-toggle').classList.remove('needs-attention');update();send();
  });
  // Focusing an iframe blurs the parent window but does not leave its document.
  global.addEventListener('blur',()=>setTimeout(()=>{if(!document.hasFocus())release();},0));
  document.addEventListener('visibilitychange',()=>{if(document.hidden)release();});
  global.addEventListener('pagehide',release);
  global.addEventListener('resize',()=>{if(context){release();update();}});
  new MutationObserver(()=>{if(context&&!interactive()&&input.sources.size)release();}).observe(document.body,{subtree:true,attributes:true,attributeFilter:['open']});
  $('mobile-menu-toggle').addEventListener('click',()=>setMenu(!menuOpen));$('mobile-menu-close').addEventListener('click',()=>setMenu(false));
  document.addEventListener('keydown',event=>{
    if(!menuOpen||document.querySelector('dialog[open]:not(#play-dialog)')||event.ctrlKey||event.metaKey||event.altKey)return;
    const action={ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down',KeyA:'left',KeyD:'right',KeyW:'up',KeyS:'down',Enter:'accept',Escape:'back'}[event.code];
    if(action){event.preventDefault();event.stopPropagation();navigate(action);}
  },true);
  for(const [id,key]of [['touch-enabled','touch'],['touch-preset','preset'],['touch-fit','fit'],['touch-size','size'],['touch-opacity','opacity']]){
    $(id).addEventListener('input',event=>{release();prefs[key]=key==='touch'?event.target.checked:['size','opacity'].includes(key)?Number(event.target.value):event.target.value;prefs=preferences(prefs);save();if(key==='preset')renderControls();else update();});
  }
  async function toggleFullscreen(){
    if(!context||!dialog.open||fullscreenPending)return false;
    const playing=context;
    fullscreenPending=true;
    try{
      if(document.fullscreenElement){await document.exitFullscreen();return true;}
      await document.documentElement.requestFullscreen({navigationUI:'hide'});
      // The user may have returned to the library while the browser was asking.
      if(context!==playing||!dialog.open){
        if(document.fullscreenElement===document.documentElement)await document.exitFullscreen();
        return false;
      }
      const focused=document.activeElement;
      const overlays=[...document.querySelectorAll('dialog[open]')].filter(node=>node!==dialog&&node.matches(':modal'));
      setMenu(false);
      // Fullscreen adds the page above existing modal dialogs in some browsers.
      // Re-open the same modal nodes after it, without moving/reloading the iframe.
      dialog.close();dialog.showModal();
      for(const overlay of overlays){overlay.close();overlay.showModal();}
      if(overlays.length&&focused?.isConnected)focused.focus({preventScroll:true});
      else playing.iframe.focus({preventScroll:true});
      return true;
    }
    catch{
      if(context===playing&&dialog.open){
        immersive=true;update();setMenu(true);
        $('touch-status').textContent=t('浏览器未允许系统全屏，已使用沉浸布局；仍可通过菜单返回。');
      }
      return false;
    }
    finally{fullscreenPending=false;}
  }
  localize();
  global.RetroMobilePlayer=Object.freeze({open,close,release,localize,toggleFullscreen,setMenu,navigate,isMenuOpen:()=>menuOpen});
})(typeof window==='undefined'?globalThis:window);
