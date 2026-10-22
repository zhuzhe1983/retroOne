/* Small, dependency-free helpers shared by library navigation and its tests. */
(() => {
  'use strict';
  const sorts = ['default', 'popularity', 'release_date', 'added_at', 'play_count', 'last_played', 'name'];
  function settings(value = {}) {
    return {
      pinned_platforms: [...new Set((Array.isArray(value.pinned_platforms) ? value.pinned_platforms : []).filter(id => typeof id === 'string' && /^[a-z0-9_-]+$/i.test(id)))].slice(0, 64),
      sort: sorts.includes(value.sort) ? value.sort : 'popularity',
      direction: value.direction === 'asc' ? 'asc' : 'desc',
    };
  }
  function platforms(items, pins, active = '') {
    const byId = new Map(items.map(item => [item.id, item]));
    const pinned = pins.map(id => byId.get(id)).filter(Boolean).slice(0, 6);
    const recent = items.filter(item => !pins.includes(item.id) && Number(item.last_played_at) > 0)
      .sort((a, b) => Number(b.last_played_at) - Number(a.last_played_at)).slice(0, Math.max(0, 6 - pinned.length));
    // New libraries get a few recognizable entry points; the full list stays in the chooser.
    const starterIds = ['nes', 'snes', 'gba', 'psp', 'switch', 'fbneo'];
    const starter = !pinned.length && !recent.length ? starterIds.map(id => byId.get(id)).filter(Boolean).slice(0, 5) : [];
    const visible = [...pinned, ...recent, ...starter];
    if (active && !visible.some(item => item.id === active) && byId.has(active)) visible.push(byId.get(active));
    return visible;
  }
  function secureURL(value) {
    if (typeof value !== 'string') return '';
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
    } catch { return ''; }
  }
  function imageURL(value) {
    // Only the dedicated media route is allowed as a relative catalog URL.
    if (typeof value === 'string' && /^\/(?:p\/player[1-9][0-9]*\/)?api\/games\/[a-zA-Z0-9_-]+\/screenshots\/[0-9]+(?:\?v=[a-f0-9]{16})?$/.test(value)) return value;
    return secureURL(value);
  }
  const shapes = {
    switch: '<rect x="14" y="15" width="36" height="34" rx="2"/><path d="M14 12H9a6 6 0 0 0-6 6v28a6 6 0 0 0 6 6h5m36-40h5a6 6 0 0 1 6 6v28a6 6 0 0 1-6 6h-5"/><circle cx="9" cy="24" r="2"/><path d="M7 38h4m-2-2v4m44-15h4m-2-2v4"/><circle cx="55" cy="40" r="2"/>',
    ds: '<rect x="10" y="3" width="44" height="27" rx="4"/><rect x="17" y="7" width="30" height="19" rx="1"/><rect x="7" y="33" width="50" height="27" rx="4"/><rect x="18" y="37" width="28" height="19" rx="1"/><path d="M10 44h5m-2.5-2.5v5M50 43h3m-1.5 6h.1"/>',
    gameboy: '<path d="M17 3h30v44c0 8-7 14-15 14H17a4 4 0 0 1-4-4V7a4 4 0 0 1 4-4Z"/><rect x="19" y="10" width="22" height="19" rx="2"/><path d="M18 40h10m-5-5v10m5 8 4-1m3 0 4-1"/><circle cx="40" cy="37" r="2"/><circle cx="34" cy="42" r="2"/>',
    psp: '<path d="M11 16h42c8 0 9 10 9 16s-1 16-9 16H11C3 48 2 38 2 32s1-16 9-16Z"/><rect x="17" y="21" width="30" height="22" rx="1"/><path d="M6 30h8m-4-4v8m42-4h6m-3-3v6"/><circle cx="10" cy="40" r="2"/>',
    nes: '<path d="M6 16h52v33H6zM6 25h52M13 32h25v17M42 16v9M47 16v9M52 16v9"/><path d="M10 43h9m3 0h6M6 49v4m52-4v4"/>',
    snes: '<path d="M9 18h46l6 10v22H3V28zM9 18v12h46V18M21 18v9h22v-9"/><path d="M10 37h12m20 0h12"/><rect x="17" y="44" width="11" height="4" rx="1"/><rect x="36" y="44" width="11" height="4" rx="1"/>',
    n64: '<path d="M10 23h44l6 21H4zM21 23v-7h22v7M8 44v6h48v-6"/><circle cx="16" cy="37" r="3"/><circle cx="27" cy="37" r="3"/><circle cx="38" cy="37" r="3"/><circle cx="49" cy="37" r="3"/>',
    cube: '<path d="m12 15 20-9 20 9v34l-20 9-20-9zM12 15l20 9 20-9M32 24v34"/><ellipse cx="32" cy="15" rx="10" ry="4"/><circle cx="19" cy="37" r="2"/><circle cx="26" cy="40" r="2"/><path d="M43 8V3H28"/>',
    wii: '<path d="m14 8 18-3v49l-18 5zM22 15v23M22 47v2M8 59h33"/><rect x="45" y="20" width="10" height="35" rx="3"/><path d="M47 28h6m-3-3v6M49 39h2m-2 7h2"/>',
    ps: '<rect x="5" y="15" width="54" height="37" rx="3"/><circle cx="32" cy="32" r="13"/><circle cx="12" cy="25" r="3"/><circle cx="52" cy="25" r="3"/><path d="M10 47h16m12 0h16M10 35h6m-6 5h6m32-5h6m-6 5h6"/>',
    ps2: '<path d="m13 10 30-5v47l-30 7zM43 5l8 5v42l-8 0M19 17l18-3m-18 8 18-3M20 31l16-3m-16 8 16-3m-16 8 16-3m-16 8 16-3"/>',
    xbox: '<path d="M7 16h50v33H7zM7 23h50M9 49v4m46-4v4"/><path d="m16 18 31 25m0-25L16 43"/><circle cx="32" cy="32" r="6"/>',
    disc: '<path d="M12 14h40l7 33H5zM5 47v5h54v-5"/><ellipse cx="32" cy="28" rx="15" ry="11"/><circle cx="32" cy="28" r="3"/><path d="M10 44h12m20 0h12"/>',
    arcade: '<path d="M16 3h32l3 11-4 22 9 12v13H8V48l9-12-4-22z"/><path d="M13 14h38M20 19h24l-3 15H23zM17 36h30M8 48h48M19 44v-5m17 4h2m6 0h2M19 54h25"/>',
    pc: '<rect x="9" y="6" width="46" height="34" rx="3"/><rect x="14" y="11" width="36" height="24" rx="1"/><path d="M27 40v7m10-7v7M8 48h48l5 9H3zM13 52h38"/>',
    controller: '<path d="M19 18h26c10 0 18 30 12 34-5 4-14-8-18-8H25c-4 0-13 12-18 8-6-4 2-34 12-34Z"/><path d="M14 30h12m-6-6v12m23-8h.1m8 6h.1m-12 0h.1m6 6h.1"/>',
  };
  function platformKind(id = '') {
    if (id === 'switch') return 'switch';
    if (['nds', '3ds'].includes(id)) return 'ds';
    if (/^(gb|gbc|gameboy|virtualboy|ngp|ngpc|pokemini|supervision)$/.test(id)) return 'gameboy';
    if (/^(gba|psp|psvita|gamegear|lynx|wonderswan|wonderswancolor|ws|wsc)$/.test(id)) return 'psp';
    if (/arcade|mame|fbneo|fba|cps|lightgun|teknoparrot|model2|model3|naomi/.test(id)) return 'arcade';
    if (/dos|windows|pc98|scumm|amiga|msx|pc$|c64|zx|amstrad/.test(id)) return 'pc';
    if (['nes', 'fds'].includes(id)) return 'nes';
    if (id === 'snes') return 'snes';
    if (id === 'n64') return 'n64';
    if (id === 'gamecube') return 'cube';
    if (/^wii/.test(id)) return 'wii';
    if (['psx', 'ps1', 'playstation'].includes(id)) return 'ps';
    if (/^ps[2345]/.test(id)) return 'ps2';
    if (/xbox/.test(id)) return 'xbox';
    if (/dreamcast|saturn|3do|segacd|pcenginecd|pcecd|pcfx|neogeocd/.test(id)) return 'disc';
    return 'controller';
  }
  function platformArt(id) {
    return `<svg class="platform-art" viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[platformKind(id)]}</svg>`;
  }
  window.RetroLibrary = {settings, platforms, secureURL, imageURL, platformArt, platformKind, sorts};
})();
