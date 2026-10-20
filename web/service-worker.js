/* Only the offline notice and application icons are cached. No game data. */
'use strict';
const CACHE_PREFIX = 'retroone-pwa-';
const CACHE_NAME = CACHE_PREFIX + '__RETROONE_PWA_VERSION__';
const PUBLIC_ASSETS = ['/offline.html', '/favicon.svg', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon-maskable-512.png'];
const PUBLIC_PATHS = new Set(PUBLIC_ASSETS);

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      await Promise.all(PUBLIC_ASSETS.map(async (path) => {
        const response = await fetch(new Request(path, {credentials: 'omit', cache: 'reload', redirect: 'error'}));
        if (!response.ok || response.redirected || !['basic', 'default'].includes(response.type)) throw new Error('Offline resource unavailable');
        await cache.put(path, response);
      }));
    } catch (error) {
      await caches.delete(CACHE_NAME);
      throw error;
    }
    // Keep the normal waiting phase. Existing games must not be interrupted.
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE_NAME).map((name) => caches.delete(name)));
    // Existing pages keep their current controller until their next navigation.
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (!url.search && PUBLIC_PATHS.has(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      return (await cache.match(url.pathname)) || fetch(request);
    })());
    return;
  }
  const libraryNavigation = url.pathname === '/' || url.pathname === '/index.html' ||
    /^\/p\/player[1-9][0-9]*\/(?:index\.html)?$/.test(url.pathname);
  if (request.mode !== 'navigate' || !libraryNavigation) return;
  event.respondWith((async () => {
    try {
      return await fetch(request); // Never put library HTML in a cache.
    } catch (_) {
      const cache = await caches.open(CACHE_NAME);
      return (await cache.match('/offline.html')) || new Response('RetroOne is offline. Reconnect to your game server to play.',
        {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'}});
    }
  })());
});
