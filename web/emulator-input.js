/* Same-origin parent controls for pinned EmulatorJS 4.2.3. No ROM/save/config changes. */
(() => {
  const origin = window.location.origin;
  // Canonical libretro/SDL pad positions (B/A/Y/X). Upstream physical/keyboard
  // remappings still go through the original GameManager unchanged.
  const buttonMap = [0, 8, 1, 9, 10, 11, 12, 13, 2, 3, 14, 15, 4, 5, 6, 7];
  let sequence = -1, lastInput = -Infinity, snapshot = '', manager, original;
  let native = new Map(), touch = Array(24).fill(0), supported = false, aspect = 0;
  let presentation = null;
  function ready() {
    if (window.parent !== window) window.parent.postMessage({
      type:'retroone:controls-ready', version:1, supported,
      ...(aspect > 0 ? {aspectRatio:aspect} : {}),
    }, origin);
  }
  function output(index, value, player=0) {
    if (!manager || !original) return;
    const merged = presentation?.menu ? 0 : Math.max(native.get(player)?.get(index) || 0, value);
    try { original.call(manager, player, index, merged); }
    catch { /* A core may be tearing down; never interfere with its exit/save path. */ }
  }
  function apply(values) {
    for (let i=0; i<24; i++) if (values[i] !== touch[i]) output(i, values[i]);
    touch = values;
  }
  function release() { apply(Array(24).fill(0)); lastInput = -Infinity; }
  function syncMenu() {
    // Track every upstream player: the parent menu can be navigated by any
    // attached controller. Retain live native state while its game is gated.
    for (const [player, values] of native)
      for (const index of values.keys()) output(index, player === 0 ? touch[index] : 0, player);
  }
  function bind() {
    const next = window.EJS_emulator?.gameManager;
    if (next === manager || typeof next?.simulateInput !== 'function') return;
    release();
    if (manager && original) manager.simulateInput = original;
    manager = next; original = manager.simulateInput; native = new Map();
    const forward = original;
    manager.simulateInput = function(player, index, value) {
      // Preserve physical-controller remapping, keyboard, netplay and hotkeys.
      // Only the parent touch source is fixed to standard player-one controls.
      if (this === manager && Number.isInteger(player) && player >= 0 &&
          Number.isInteger(index) && index >= 0 && index < 24) {
        if (!native.has(player)) native.set(player, new Map());
        native.get(player).set(index, value);
        return forward.call(this, player, index, presentation?.menu ? 0 : Math.max(value, player === 0 ? touch[index] : 0));
      }
      return forward.call(this, player, index, value);
    };
    supported = true; ready();
  }
  function validControls(data) {
    return Number.isSafeInteger(data.sequence) && data.sequence >= 0 &&
      Array.isArray(data.buttons) && data.buttons.length === 17 &&
      Array.isArray(data.axes) && data.axes.length === 4 &&
      Array.from(data.buttons).every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1) &&
      Array.from(data.axes).every(v => typeof v === 'number' && Number.isFinite(v) && v >= -1 && v <= 1);
  }
  function present() {
    if (!presentation || !document.documentElement) return;
    const root = document.documentElement;
    root.classList.toggle('retroone-embedded', true);
    root.classList.toggle('retroone-touch', presentation.touch && supported);
    root.classList.toggle('retroone-immersive', supported && presentation.immersive === true && presentation.menu !== true);
    root.classList.toggle('retroone-menu', presentation.menu === true);
    root.dataset.retrooneFit = presentation.fit;
    if (!document.getElementById('retroone-emulator-style')) {
      const style = document.createElement('style'); style.id = 'retroone-emulator-style';
      style.textContent = `
        .retroone-immersive body>header,.retroone-immersive body>footer,
        .retroone-immersive .ejs_menu_bar,.retroone-immersive .ejs_virtualGamepad_open{display:none!important}
        .retroone-menu body>header{display:flex!important}
        .retroone-menu .ejs_menu_bar{display:flex!important;transform:none!important;opacity:1!important;visibility:visible!important}
        .retroone-touch .ejs_virtualGamepad_parent{display:none!important}
        .retroone-embedded .ejs_canvas{position:absolute!important;
          width:var(--retroone-media-w,100%)!important;height:var(--retroone-media-h,100%)!important;
          left:var(--retroone-media-x,0px)!important;top:var(--retroone-media-y,0px)!important;object-fit:fill!important}
        .retroone-embedded .ejs_canvas_parent{position:relative!important;overflow:hidden!important}
      `;
      (document.head || root).appendChild(style);
    }
    const emulator = window.EJS_emulator;
    const area = emulator?.canvas?.parentElement;
    const box = area?.getBoundingClientRect();
    let ratio = 0;
    if (emulator?.started) {
      try { ratio = Number(manager?.getVideoDimensions?.('aspect')); } catch { /* Not loaded yet. */ }
    }
    if (!(ratio > 0 && ratio < 10)) ratio = emulator?.canvas?.width / emulator?.canvas?.height;
    if (!(ratio > 0 && ratio < 10)) return;
    if (ratio !== aspect) { aspect = ratio; ready(); }
    if (!box?.width || !box?.height) return;
    // Display zoom must not resize the core framebuffer or reload its state.
    const baseW = presentation.fit === 'cover'
      ? Math.max(box.width, box.height * ratio) : Math.min(box.width, box.height * ratio);
    const zoom = presentation.zoom ?? 1;
    const w = (presentation.fit === 'stretch' ? box.width : baseW) * zoom;
    const h = baseW / ratio * zoom;
    for (const [name, value] of Object.entries({w,h,x:(box.width-w)/2,y:(box.height-h)/2}))
      root.style.setProperty('--retroone-media-' + name, value + 'px');
  }
  window.addEventListener('message', event => {
    if (event.origin !== origin || event.source !== window.parent || window.parent === window) return;
    const data = event.data;
    if (!data || data.version !== 1) return;
    if (data.type === 'retroone:presentation') {
      if (data.embedded !== true || !['contain','stretch','cover'].includes(data.fit) || typeof data.touch !== 'boolean' ||
          (data.zoom !== undefined && (typeof data.zoom !== 'number' || !Number.isFinite(data.zoom) || data.zoom < 1 || data.zoom > 3)) ||
          (data.menu !== undefined && typeof data.menu !== 'boolean') ||
          (data.immersive !== undefined && typeof data.immersive !== 'boolean')) return;
      const changedMenu = !!presentation?.menu !== !!data.menu;
      presentation = data;
      if (!data.touch || data.menu) release();
      if (changedMenu) syncMenu();
      present(); return;
    }
    if (data.type !== 'retroone:controls' || !validControls(data) || data.sequence < sequence) return;
    const signature = JSON.stringify([data.buttons, data.axes]);
    if (data.sequence === sequence && signature !== snapshot) return;
    sequence = data.sequence; snapshot = signature;
    if (document.hidden || !supported || presentation?.touch === false || presentation?.menu) return;
    lastInput = performance.now();
    const values = Array(24).fill(0);
    buttonMap.forEach((index, button) => { values[index] = data.buttons[button] > .5 ? 1 : 0; });
    data.axes.forEach((value, axis) => {
      values[16 + axis*2 + (value < 0 ? 1 : 0)] = Math.trunc(Math.abs(value) * 0x7fff);
    });
    apply(values);
  });
  // Parent controls naturally blur this iframe. Only its owner lifecycle,
  // visibility and heartbeat can release touch; iframe blur is not a release.
  window.addEventListener('pagehide', release);
  document.addEventListener('visibilitychange', () => { if (document.hidden) release(); });
  window.addEventListener('resize', present);
  document.addEventListener('DOMContentLoaded', present);
  setInterval(() => { bind(); if (performance.now()-lastInput >= 1200) release(); present(); }, 100);
  bind(); ready();
})();
