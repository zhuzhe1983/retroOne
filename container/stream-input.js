/* Input adaptation only; Selkies owns transport, decoding and audio. */
(() => {
  let engine = '';
  let sessionRequest = 0;
  let touchEnabled = false, touchButtons = Array(17).fill(0), touchAxes = Array(4).fill(0);
  let touchSequence = -1, touchSignature = '', touchSeen = -Infinity, presentation = null, aspect = 0;
  let touchInputHost = null, touchEnableRequested = false;
  const keyCodes = {ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,Enter:13,Escape:27,' ':32,w:87,a:65,s:83,d:68,Control:17,Alt:18,Shift:16,z:90,x:88,'1':49,'5':53};
  const profilePrefix = window.location.pathname.match(/^\/p\/(player[1-9][0-9]{0,5})\//)?.[0].replace(/\/$/, '') || '';
  const refresh = () => {
    const request = ++sessionRequest;
    return fetch(profilePrefix + '/api/session', {cache:'no-store'})
      .then(r => { if (!r.ok) throw new Error('Session unavailable'); return r.json(); })
      .then(v => {
        if (request !== sessionRequest) return;
        const nextEngine = v.session?.engine || '';
        if (engine !== nextEngine) { releasePad(); keyboardHeld.clear(); }
        engine = nextEngine;
        switchActive = /\bEden\b/i.test(engine) && v.session?.platform === 'switch';
        if (!switchActive) releaseSwitchKeys();
        switchSession(switchActive || touchEnabled);
      }).catch(() => { if (request === sessionRequest) releaseSwitchKeys(); });
  };
  // The pinned Selkies client polls all browser pads into one server slot.
  // Eden therefore sees one stable, standard-mapped pad, regardless of the
  // physical source's browser index. Native connect/disconnect events must not
  // disconnect that merged slot when a physical pad is unplugged.
  const switchKeys = new Set();
  const switchButtons = {KeyJ:0,KeyK:1,KeyU:2,KeyI:3,KeyQ:4,KeyE:5,Digit1:6,Digit3:7,ShiftLeft:8,ShiftRight:8,Enter:9,KeyZ:10,KeyX:11,KeyT:12,KeyF:14,KeyG:13,KeyH:15};
  const switchAxes = [['KeyA','KeyD'],['KeyW','KeyS'],['ArrowLeft','ArrowRight'],['ArrowUp','ArrowDown']];
  const switchCodes = new Set([...Object.keys(switchButtons), ...switchAxes.flat()]);
  // Keyboard bindings are stable across old and new live RetroArch sessions.
  // Sending these touch buttons as both keys and a pad would double-trigger or
  // hit a different action in a session using the older SDL button numbering.
  const retroarchTouchKeys = {0:'KeyK',1:'KeyJ',2:'KeyU',3:'KeyI',4:'KeyQ',5:'KeyE',8:'ShiftLeft',9:'Enter',12:'KeyW',13:'KeyS',14:'KeyA',15:'KeyD'};
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
      return keyboard || touchAxes[index] || source?.axes[index] || 0;
    });
    const buttons = Array.from({length:17}, (_, index) => {
      const original = source?.buttons[index];
      const keyboard = Object.keys(switchButtons).some(code => switchButtons[code] === index && switchKeys.has(code));
      const touch = /RetroArch/.test(engine) && Object.hasOwn(retroarchTouchKeys, index) ? 0 : touchButtons[index];
      const virtual = keyboard ? 1 : touch;
      return {value:Math.max(virtual, original?.value || 0), pressed:virtual > .5 || !!original?.pressed, touched:virtual > 0 || !!original?.touched};
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
    if (!switchSupported || (switchPhase !== 'off' && enabled === bridgeEnabled) || (switchPhase === 'off' && !enabled)) return;
    bridgeEnabled = enabled;
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
  let bridgeEnabled = false;
  function menuPads(pads) {
    if (!presentation?.menu) return pads;
    // Preserve identity and sparse slots so the upstream poller sends ordinary
    // releases, without disconnecting a controller while it navigates the menu.
    return Array.from(pads, pad => pad && ({
      id:pad.id, index:pad.index, connected:pad.connected, mapping:pad.mapping,
      timestamp:pad.timestamp, vibrationActuator:pad.vibrationActuator,
      hapticActuators:pad.hapticActuators,
      axes:Array.from(pad.axes, () => 0),
      buttons:Array.from(pad.buttons, () => ({value:0, pressed:false, touched:false})),
    }));
  }
  function installSwitchBridge() {
    if (!nativeGetGamepads || navigator.getGamepads === installedRead) return;
    const upstreamRead = navigator.getGamepads.bind(navigator);
    const read = function() {
      if (switchPhase === 'off') return menuPads(upstreamRead());
      return menuPads(switchPhase === 'active' ? [mergedSwitchPad(), null, null, null] : emptyPads());
    };
    try { navigator.getGamepads = read; switchSupported = navigator.getGamepads === read; if (switchSupported) installedRead = read; } catch { /* Leave native input usable when wrapping is forbidden. */ }
  }
  installSwitchBridge();
  // Selkies's deferred module installs its touch-pad wrapper after this head
  // script. Keep the Eden view outermost; leave that wrapper intact elsewhere.
  document.addEventListener('DOMContentLoaded', () => { installSwitchBridge(); announce(); present(); }, {once:true});
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
  const keyboardHeld = new Set();
  const gamepadKeys = {ArrowUp:['ArrowUp',38],ArrowDown:['ArrowDown',40],ArrowLeft:['ArrowLeft',37],ArrowRight:['ArrowRight',39],Enter:['Enter',13],Escape:['Escape',27],Space:[' ',32],ControlLeft:['Control',17],AltLeft:['Alt',18],ShiftLeft:['Shift',16],KeyZ:['z',90],KeyX:['x',88],Digit1:['1',49],Digit5:['5',53]};
  for (const letter of 'wasdjkuieq') gamepadKeys['Key' + letter.toUpperCase()] = [letter, letter.toUpperCase().charCodeAt(0)];
  function emitPadKey(code, down) {
    if (keyboardHeld.has(code)) return;
    const [key, keyCode] = gamepadKeys[code];
    const target = document.getElementById('overlayInput') || document.getElementById('keyboard-input-assist') || document.body;
    const modifier = name => code === name ? down : held.has(name) || keyboardHeld.has(name);
    const event = new KeyboardEvent(down ? 'keydown' : 'keyup', {key, code, keyCode, which:keyCode, bubbles:true, cancelable:true,
      ctrlKey:modifier('ControlLeft'),altKey:modifier('AltLeft'),shiftKey:modifier('ShiftLeft')});
    Object.defineProperties(event, {retrooneAlias:{value:true},retroonePad:{value:true}});
    target.dispatchEvent(event);
  }
  function updateGamepad() {
    const wanted = new Set();
    if (touchEnabled && /RetroArch/.test(engine) && document.visibilityState === 'visible' && !presentation?.menu) {
      for (const [index, code] of Object.entries(retroarchTouchKeys)) if (touchButtons[index] > .5) wanted.add(code);
    }
    if ((/DOSBox/.test(engine) || touchEnabled && /SDLPAL|^MAME$/.test(engine)) && document.visibilityState === 'visible') {
      // Preserve existing physical DOS controls. MAME/SDLPAL only add the
      // parent touch source; their native joystick mapping remains independent.
      const pad = /DOSBox/.test(engine) ? [...(navigator.getGamepads?.() || [])].find(p => p?.connected)
        : {buttons:touchButtons.map(value => ({pressed:value > .5})), axes:touchAxes};
      if (pad) {
        const button = index => !!pad.buttons[index]?.pressed;
        if (button(12) || pad.axes[1] < -.45) wanted.add('ArrowUp');
        if (button(13) || pad.axes[1] > .45) wanted.add('ArrowDown');
        if (button(14) || pad.axes[0] < -.45) wanted.add('ArrowLeft');
        if (button(15) || pad.axes[0] > .45) wanted.add('ArrowRight');
        if (engine === 'MAME') {
          const keys = {0:'ControlLeft',1:'AltLeft',2:'Space',3:'ShiftLeft',4:'KeyZ',5:'KeyX',8:'Digit5',9:'Digit1'};
          for (const [index,key] of Object.entries(keys)) if (button(index)) wanted.add(key);
        } else {
          if (button(0) || button(9)) wanted.add('Enter');
          if (button(1) || button(8)) wanted.add('Escape');
          if (button(2)) wanted.add('Space');
          for (const [index,key] of [[3,'ControlLeft'],[4,'ShiftLeft'],[5,'AltLeft']])
            if (touchEnabled && touchButtons[index] > .5) wanted.add(key);
        }
      }
    }
    for (const code of held) if (!wanted.has(code)) { emitPadKey(code, false); held.delete(code); }
    for (const code of wanted) if (!held.has(code)) { emitPadKey(code, true); held.add(code); }
    requestAnimationFrame(updateGamepad);
  }
  const releasePad = () => { for (const code of held) emitPadKey(code, false); held.clear(); };
  const releaseAll = () => { releaseSwitchKeys(); keyboardHeld.clear(); releasePad(); };
  // A tap on the parent rails blurs the iframe. It must not release touch.
  window.addEventListener('blur', () => {
    releaseSwitchKeys(); keyboardHeld.clear();
    if (!touchEnabled) releasePad();
    // Selkies correctly resets keyboard on iframe blur. Reapply our separately
    // owned touch-to-keyboard state after that reset, without suppressing it.
    else setTimeout(() => { if (!document.hidden && performance.now()-touchSeen < 1200)
      for (const code of held) emitPadKey(code, true); }, 0);
  });
  window.addEventListener('pagehide', () => { releaseTouch(); releaseAll(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { releaseTouch(); releaseAll(); } });
  requestAnimationFrame(updateGamepad);
  window.addEventListener('keydown', alias, true);
  window.addEventListener('keyup', alias, true);
  for (const type of ['keydown','keyup']) window.addEventListener(type, event => {
    if (event.retroonePad || !gamepadKeys[event.code]) return;
    const host = ['overlayInput','keyboard-input-assist'].includes(event.target?.id);
    if (!host && (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement ||
        event.target instanceof HTMLSelectElement || event.target?.isContentEditable)) return;
    if (type === 'keydown') keyboardHeld.add(event.code);
    else {
      keyboardHeld.delete(event.code);
      if (held.has(event.code)) { event.preventDefault(); event.stopImmediatePropagation(); }
    }
  }, true);
  function releaseTouch() {
    touchButtons = Array(17).fill(0); touchAxes = Array(4).fill(0); touchSeen = -Infinity;
  }
  function announce() {
    if (window.parent && window.parent !== window) window.parent.postMessage({
      type:'retroone:controls-ready', version:1, supported:switchSupported,
      ...(aspect > 0 ? {aspectRatio:aspect} : {}),
    }, window.location.origin);
  }
  function present() {
    const root = document.documentElement;
    if (!presentation || !root) return;
    root.classList.toggle('retroone-embedded', true);
    root.classList.toggle('retroone-touch', presentation.touch && switchSupported);
    root.classList.toggle('retroone-immersive', switchSupported && presentation.immersive === true && presentation.menu !== true);
    // Older open parent pages have no explicit tools screen. Keep their
    // floating entries available if only the stream frame reconnects.
    root.classList.toggle('retroone-app-playing', presentation.nativeTools === true && presentation.menu !== true);
    if (!document.getElementById('retroone-stream-style')) {
      const style = document.createElement('style'); style.id = 'retroone-stream-style';
      // IDs from the pinned Selkies client. Do not change sink visibility,
      // intrinsic resolution, encoder settings, audio or transport ownership.
      style.textContent = `
        .retroone-immersive #root,.retroone-immersive #dashboard-root,.retroone-immersive #player-ui-root{display:none!important}
        /* The pinned dashboard portals these two floating entry points into
           dashboard-root. menu:true also covers the parent's native-tools
           mode: its panel is dismissed so these controls can receive a real
           user tap, preserving mobile keyboard activation and upstream state. */
        .retroone-app-playing .dashboard-overlay-container .virtual-keyboard-button,
        .retroone-app-playing .dashboard-overlay-container .toggle-handle{display:none!important}
        .retroone-touch #touch-gamepad-host,.retroone-touch #universal-touch-gamepad-controls-overlay{display:none!important}
        .retroone-embedded .video-container{overflow:hidden!important}
        .retroone-embedded .video-container #stream,.retroone-embedded .video-container #videoStream,
        .retroone-embedded .video-container #videoCanvas,.retroone-embedded .video-container #videoWorkerCanvas,
        .retroone-embedded .video-container #overlayInput{
          /* Selkies caps video elements at the viewport size. Remove those
             caps so video and canvas use the same scaled, centered rectangle. */
          max-width:none!important;max-height:none!important;
          width:var(--retroone-media-w,100%)!important;height:var(--retroone-media-h,100%)!important;
          left:var(--retroone-media-x,0px)!important;top:var(--retroone-media-y,0px)!important;object-fit:fill!important}
      `;
      (document.head || root).appendChild(style);
    }
    const sinks = ['videoStream','stream','videoWorkerCanvas','videoCanvas'].map(id=>document.getElementById(id)).filter(Boolean);
    // The pinned pointer mapper reads inline objectFit, not computed CSS.
    // Match our displayed rectangle so stretched video keeps accurate input.
    for (const media of sinks) if (media.tagName === 'VIDEO') media.style.objectFit = 'fill';
    const sink = sinks.find(s => (s.videoWidth || s.width) > 0 && (s.videoHeight || s.height) > 0);
    const box = sink?.parentElement?.getBoundingClientRect();
    const ratio = (sink?.videoWidth || sink?.width) / (sink?.videoHeight || sink?.height);
    if (!(ratio > 0 && ratio < 10) || !box?.width || !box?.height) return;
    if (ratio !== aspect) { aspect = ratio; announce(); }
    // Scale only the displayed rectangle. Keep the encoded sink resolution and
    // input overlay aligned; cover deliberately crops equally at both edges.
    const baseW = presentation.fit === 'cover'
      ? Math.max(box.width, box.height * ratio) : Math.min(box.width, box.height * ratio);
    const zoom = presentation.zoom ?? 1;
    const w = (presentation.fit === 'stretch' ? box.width : baseW) * zoom;
    const h = baseW / ratio * zoom;
    for (const [name,value] of Object.entries({w,h,x:(box.width-w)/2,y:(box.height-h)/2}))
      root.style.setProperty('--retroone-media-' + name, value + 'px');
  }
  function enableTouchGamepad() {
    if (!touchEnabled || !switchSupported) { touchEnableRequested = false; touchInputHost = null; return; }
    // Reuse the upstream dashboard command, including its normal persistence.
    // A later transport attachment/reconnection gets one retry. Desktop users
    // who have not enabled touch keep their own gamepad-toggle preference.
    const host = window.webrtcInput || null;
    if (!touchEnableRequested || (host && host !== touchInputHost)) {
      window.postMessage({type:'gamepadControl', enabled:true}, window.location.origin);
      touchEnableRequested = true; touchInputHost = host;
    }
  }
  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent || window.parent === window) return;
    const data = event.data;
    if (!data || data.version !== 1) return;
    if (data.type === 'retroone:presentation') {
      if (data.embedded !== true || !['contain','stretch','cover'].includes(data.fit) || typeof data.touch !== 'boolean' ||
          (data.zoom !== undefined && (typeof data.zoom !== 'number' || !Number.isFinite(data.zoom) || data.zoom < 1 || data.zoom > 3)) ||
          (data.menu !== undefined && typeof data.menu !== 'boolean') ||
          (data.nativeTools !== undefined && typeof data.nativeTools !== 'boolean') ||
          (data.immersive !== undefined && typeof data.immersive !== 'boolean')) return;
      presentation = data; touchEnabled = data.touch;
      if (!touchEnabled || data.menu) releaseTouch();
      switchSession(switchActive || touchEnabled); enableTouchGamepad(); present(); return;
    }
    if (data.type !== 'retroone:controls' || !Number.isSafeInteger(data.sequence) || data.sequence < 0 ||
        data.sequence < touchSequence || !Array.isArray(data.buttons) || data.buttons.length !== 17 ||
        !Array.isArray(data.axes) || data.axes.length !== 4 ||
        !Array.from(data.buttons).every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1) ||
        !Array.from(data.axes).every(v => typeof v === 'number' && Number.isFinite(v) && v >= -1 && v <= 1)) return;
    const signature = JSON.stringify([data.buttons,data.axes]);
    if (data.sequence === touchSequence && signature !== touchSignature) return;
    touchSequence = data.sequence; touchSignature = signature;
    if (document.hidden || !touchEnabled || presentation?.menu) return;
    touchButtons = data.buttons.slice(); touchAxes = data.axes.slice(); touchSeen = performance.now();
  });
  window.addEventListener('resize', present);
  setInterval(() => { if (performance.now()-touchSeen >= 1200) releaseTouch(); enableTouchGamepad(); present(); }, 100);
  announce();
})();
