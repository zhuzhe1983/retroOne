/* Keyboard aliases only; Selkies owns transport, gamepads, decoding and audio. */
(() => {
  let engine = '';
  const keyCodes = {ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,Enter:13,Escape:27,' ':32,w:87,a:65,s:83,d:68,Control:17,Alt:18,Shift:16,z:90,x:88,'1':49,'5':53};
  const profilePrefix = window.location.pathname.match(/^\/p\/(player[1-9][0-9]{0,5})\//)?.[0].replace(/\/$/, '') || '';
  const refresh = () => fetch(profilePrefix + '/api/session', {cache:'no-store'})
    .then(r => r.json()).then(v => { engine = v.session?.engine || ''; }).catch(() => {});
  refresh(); setInterval(refresh, 2000);
  function alias(event) {
    if (event.retrooneAlias || event.ctrlKey || event.altKey || event.metaKey) return;
    const streamInput = ['overlayInput', 'keyboard-input-assist'].includes(event.target?.id);
    if (!streamInput && (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target?.isContentEditable)) return;
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
  window.addEventListener('blur', releasePad);
  document.addEventListener('visibilitychange', () => { if (document.hidden) releasePad(); });
  requestAnimationFrame(updateGamepad);
  window.addEventListener('keydown', alias, true);
  window.addEventListener('keyup', alias, true);
})();
