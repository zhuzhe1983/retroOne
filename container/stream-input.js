/* Input adaptation only; Selkies owns transport, decoding and audio. */
(() => {
  let engine = '';
  let sessionRequest = 0;
  const keyCodes = {ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,Enter:13,Escape:27,' ':32,w:87,a:65,s:83,d:68,Control:17,Alt:18,Shift:16,z:90,x:88,'1':49,'5':53};
  const profilePrefix = window.location.pathname.match(/^\/p\/(player[1-9][0-9]{0,5})\//)?.[0].replace(/\/$/, '') || '';
  const refresh = () => {
    const request = ++sessionRequest;
    return fetch(profilePrefix + '/api/session', {cache:'no-store'})
      .then(r => { if (!r.ok) throw new Error('Session unavailable'); return r.json(); })
      .then(v => {
        if (request !== sessionRequest) return;
        engine = v.session?.engine || '';
        switchSession(/\bEden\b/i.test(engine) && v.session?.platform === 'switch');
      }).catch(() => { if (request === sessionRequest) releaseSwitchKeys(); });
  };
  // The pinned Selkies client polls all browser pads into one server slot.
  // Eden therefore sees one stable, standard-mapped pad, regardless of the
  // physical source's browser index. Native connect/disconnect events must not
  // disconnect that merged slot when a physical pad is unplugged.
  const switchKeys = new Set();
  const switchButtons = {KeyJ:0,KeyK:1,KeyU:2,KeyI:3,KeyQ:4,KeyE:5,Digit1:6,Digit3:7,ShiftLeft:8,ShiftRight:8,Enter:9,KeyZ:10,KeyX:11};
  const switchAxes = [['KeyA','KeyD'],['KeyW','KeyS'],['ArrowLeft','ArrowRight'],['ArrowUp','ArrowDown']];
  const switchCodes = new Set([...Object.keys(switchButtons), ...switchAxes.flat()]);
  const nativeGetGamepads = navigator.getGamepads?.bind(navigator);
  const switchId = 'RetroOne Switch Controller';
  let switchActive = false, switchSupported = false, switchPhase = 'off', switchTransition = 0, switchTimer, installedRead;
  const emptyPads = () => [null, null, null, null];
  const physicalPads = () => {
    try { return nativeGetGamepads ? nativeGetGamepads() : []; }
    catch { return []; } // An inactive frame may temporarily deny native sampling.
  };
  function releaseSwitchKeys() { switchKeys.clear(); }
  function mergedSwitchPad(pads = physicalPads()) {
    const source = Array.from(pads).find(pad => pad?.connected && pad.mapping === 'standard');
    const axes = switchAxes.map(([negative, positive], index) => {
      const keyboard = Number(switchKeys.has(positive)) - Number(switchKeys.has(negative));
      return keyboard || source?.axes[index] || 0;
    });
    const buttons = Array.from({length:17}, (_, index) => {
      const original = source?.buttons[index];
      const keyboard = Object.keys(switchButtons).some(code => switchButtons[code] === index && switchKeys.has(code));
      return {value:keyboard ? 1 : original?.value || 0, pressed:keyboard || !!original?.pressed, touched:keyboard || !!original?.touched};
    });
    return {id:switchId, index:0, connected:true, mapping:'standard', axes, buttons,
      timestamp:performance.now(), vibrationActuator:source?.vibrationActuator || null, hapticActuators:source?.hapticActuators || []};
  }
  function padEvent(type, pad) {
    const event = new Event(type);
    Object.defineProperties(event, {gamepad:{value:pad}, retrooneSwitchPad:{value:true}});
    window.dispatchEvent(event);
  }
  function switchSession(enabled) {
    if (!switchSupported || switchActive === enabled) return;
    switchActive = enabled; releaseSwitchKeys();
    const transition = ++switchTransition;
    clearTimeout(switchTimer);
    switchPhase = 'transition';
    padEvent('gamepaddisconnected', {...mergedSwitchPad([]), connected:false});
    // Let the existing 16 ms Selkies poll forget the former browser indices
    // before announcing a different pad. Otherwise held controls with the same
    // value can be omitted from its first changed-state report after reconnect.
    switchTimer = setTimeout(() => {
      if (transition !== switchTransition) return;
      switchPhase = enabled ? 'active' : 'off';
      if (enabled) padEvent('gamepadconnected', mergedSwitchPad());
      else {
        const source = Array.from(physicalPads()).find(pad => pad?.connected);
        if (source) padEvent('gamepadconnected', source);
      }
    }, 40);
  }
  function installSwitchBridge() {
    if (!nativeGetGamepads || navigator.getGamepads === installedRead) return;
    const upstreamRead = navigator.getGamepads.bind(navigator);
    const read = function() {
      if (switchPhase === 'off') return upstreamRead();
      return switchPhase === 'active' ? [mergedSwitchPad(), null, null, null] : emptyPads();
    };
    try { navigator.getGamepads = read; switchSupported = navigator.getGamepads === read; if (switchSupported) installedRead = read; } catch { /* Leave native input usable when wrapping is forbidden. */ }
  }
  installSwitchBridge();
  // Selkies's deferred module installs its touch-pad wrapper after this head
  // script. Keep the Eden view outermost; leave that wrapper intact elsewhere.
  document.addEventListener('DOMContentLoaded', installSwitchBridge, {once:true});
  for (const type of ['gamepadconnected','gamepaddisconnected']) window.addEventListener(type, event => {
    if (switchPhase !== 'off' && !event.retrooneSwitchPad) event.stopImmediatePropagation();
  }, true);
  function switchKey(event, editable) {
    if (!switchActive || !switchSupported || event.retrooneAlias) return false;
    const code = event.code || (event.key === 'Shift' ? 'ShiftLeft' : event.key === 'Enter' ? 'Enter' : /^Arrow/.test(event.key) ? event.key : /^[13]$/.test(event.key) ? 'Digit' + event.key : 'Key' + event.key?.toUpperCase());
    if (!switchCodes.has(code)) return false;
    const owned = switchKeys.has(code);
    if (event.type === 'keyup') switchKeys.delete(code);
    if (editable || document.hidden || event.isComposing || event.ctrlKey || event.altKey || event.metaKey) return false;
    if (event.type === 'keydown') switchKeys.add(code);
    else if (!owned) return false;
    event.preventDefault(); event.stopImmediatePropagation();
    return true;
  }
  refresh(); setInterval(refresh, 2000);
  function alias(event) {
    const streamInput = ['overlayInput', 'keyboard-input-assist'].includes(event.target?.id);
    const editable = !streamInput && (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement || event.target?.isContentEditable);
    if (switchKey(event, editable)) return;
    if (event.retrooneAlias || event.ctrlKey || event.altKey || event.metaKey || editable) return;
    let map = {};
    if (/DOSBox|SDLPAL/.test(engine)) {
      map = {w:'ArrowUp',a:'ArrowLeft',s:'ArrowDown',d:'ArrowRight',j:'Enter',k:'Escape',u:' '};
    } else if (engine === 'MAME') {
      map = {w:'ArrowUp',a:'ArrowLeft',s:'ArrowDown',d:'ArrowRight',j:'Control',k:'Alt',u:' ',i:'Shift',q:'z',e:'x',Enter:'1',Shift:'5'};
    } else if (/RetroArch/.test(engine)) {
      map = {ArrowUp:'w',ArrowLeft:'a',ArrowDown:'s',ArrowRight:'d'};
    }
    const key = map[event.key] || map[event.key?.toLowerCase()];
    if (!key) return;
    event.preventDefault(); event.stopImmediatePropagation();
    const translated = new KeyboardEvent(event.type, {
      key, code:key.length === 1 ? (key === ' ' ? 'Space' : (/^[0-9]$/.test(key) ? 'Digit' + key : 'Key' + key.toUpperCase())) : ({Control:'ControlLeft',Alt:'AltLeft',Shift:'ShiftLeft'}[key] || key),
      keyCode:keyCodes[key], which:keyCodes[key], charCode:0,
      bubbles:true, cancelable:true, repeat:event.repeat, shiftKey:event.shiftKey,
    });
    Object.defineProperty(translated, 'retrooneAlias', {value:true});
    (event.target || document).dispatchEvent(translated);
  }
  // DOS titles often use keyboard controls even when a joystick is present.
  // Translate the standard browser Gamepad layout into those native keys.
  const held = new Set();
  const gamepadKeys = {ArrowUp:['ArrowUp',38],ArrowDown:['ArrowDown',40],ArrowLeft:['ArrowLeft',37],ArrowRight:['ArrowRight',39],Enter:['Enter',13],Escape:['Escape',27],Space:[' ',32]};
  function emitPadKey(code, down) {
    const [key, keyCode] = gamepadKeys[code];
    const target = document.getElementById('overlayInput') || document.getElementById('keyboard-input-assist') || document.body;
    const event = new KeyboardEvent(down ? 'keydown' : 'keyup', {key, code, keyCode, which:keyCode, bubbles:true, cancelable:true});
    Object.defineProperty(event, 'retrooneAlias', {value:true});
    target.dispatchEvent(event);
  }
  function updateGamepad() {
    const wanted = new Set();
    if (/DOSBox/.test(engine) && document.visibilityState === 'visible') {
      const pad = [...(navigator.getGamepads?.() || [])].find(p => p?.connected);
      if (pad) {
        const button = index => !!pad.buttons[index]?.pressed;
        if (button(12) || pad.axes[1] < -.45) wanted.add('ArrowUp');
        if (button(13) || pad.axes[1] > .45) wanted.add('ArrowDown');
        if (button(14) || pad.axes[0] < -.45) wanted.add('ArrowLeft');
        if (button(15) || pad.axes[0] > .45) wanted.add('ArrowRight');
        if (button(0) || button(9)) wanted.add('Enter');
        if (button(1) || button(8)) wanted.add('Escape');
        if (button(2)) wanted.add('Space');
      }
    }
    for (const code of held) if (!wanted.has(code)) { emitPadKey(code, false); held.delete(code); }
    for (const code of wanted) if (!held.has(code)) { emitPadKey(code, true); held.add(code); }
    requestAnimationFrame(updateGamepad);
  }
  const releasePad = () => { for (const code of held) emitPadKey(code, false); held.clear(); };
  const releaseAll = () => { releaseSwitchKeys(); releasePad(); };
  window.addEventListener('blur', releaseAll);
  window.addEventListener('pagehide', releaseAll);
  document.addEventListener('visibilitychange', () => { if (document.hidden) releaseAll(); });
  requestAnimationFrame(updateGamepad);
  window.addEventListener('keydown', alias, true);
  window.addEventListener('keyup', alias, true);
})();
