const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync('web/service-worker.js', 'utf8').replace('__RETROONE_PWA_VERSION__', 'test-version');
const origin = 'https://retroone.example';

function worker() {
  const listeners = new Map(), stores = new Map(), fetched = [], writes = [];
  let offline = false;
  const normalized = input => new URL(typeof input === 'string' ? input : input.url, origin).href;
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const store = stores.get(name);
      return {
        async put(key, response) { writes.push(normalized(key)); store.set(normalized(key), response.clone()); },
        async match(key) { return store.get(normalized(key))?.clone(); },
      };
    },
    async keys() { return [...stores.keys()]; },
    async delete(name) { return stores.delete(name); },
  };
  const context = {
    self: {location: {origin}, addEventListener: (name, handler) => listeners.set(name, handler)},
    caches, URL, Response,
    Request: class extends Request { constructor(path, options) { super(new URL(path, origin), options); } },
    fetch: async (request) => {
      fetched.push(request);
      if (offline) throw new TypeError('synthetic network offline');
      return new Response(normalized(request).endsWith('/offline.html') ? 'honest offline notice' : 'network-only content');
    },
  };
  vm.runInNewContext(source, context);
  async function lifecycle(name) { let promise; listeners.get(name)({waitUntil: value => { promise = value; }}); await promise; }
  function request(path, options = {}) {
    let response;
    listeners.get('fetch')({request: {url: new URL(path, origin).href, method: 'GET', mode: 'cors', ...options}, respondWith: value => { response = value; }});
    return response;
  }
  return {listeners, stores, fetched, writes, caches, lifecycle, request, goOffline: () => { offline = true; }};
}

test('install caches exactly five public resources and omits credentials', async () => {
  const sw = worker(); await sw.lifecycle('install');
  assert.equal(sw.writes.length, 5);
  assert.deepEqual(sw.writes.map(value => new URL(value).pathname).sort(), [
    '/favicon.svg', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon-maskable-512.png', '/offline.html',
  ]);
  assert(sw.fetched.every(request => request.credentials === 'omit' && request.cache === 'reload' && request.redirect === 'error'));
  assert.deepEqual([...sw.listeners.keys()], ['install', 'activate', 'fetch']);
});

test('private APIs, ROMs, saves, emulators, streams and cross-origin requests are never intercepted', async () => {
  const sw = worker(); await sw.lifecycle('install');
  for (const path of ['/api/profiles', '/api/games/id/rom', '/api/games/id/save-state', '/p/player2/api/settings/interface',
    '/p/player1/stream/', '/stream/video', '/play/id', '/emulatorjs/data/cores/fceumm-wasm.data',
    '/app.js', '/offline.html?private-query=1', 'https://other.example/offline.html']) {
    assert.equal(sw.request(path), undefined, path);
    assert.equal(sw.request(path, {mode: 'navigate'}), undefined, path);
  }
  assert.equal(sw.request('/offline.html', {method: 'POST'}), undefined);
  assert.equal(sw.fetched.length, 5);
});

test('library navigation is network first and never cached; offline fallback contains no catalog', async () => {
  const sw = worker(); await sw.lifecycle('install');
  assert.equal(await (await sw.request('/?game=private-id', {mode: 'navigate'})).text(), 'network-only content');
  assert.equal(sw.writes.length, 5);
  sw.goOffline();
  for (const path of ['/', '/index.html', '/p/player1/', '/p/player24/index.html?game=private-id']) {
    assert.equal(await (await sw.request(path, {mode: 'navigate'})).text(), 'honest offline notice');
  }
  assert.equal(sw.writes.length, 5);
});

test('activation removes only our old offline cache, leaving emulator databases/cache untouched', async () => {
  const sw = worker();
  await sw.caches.open('retroone-pwa-old');
  await sw.caches.open('EmulatorJS-cores');
  await sw.caches.open('other-application');
  await sw.lifecycle('install'); await sw.lifecycle('activate');
  assert.deepEqual((await sw.caches.keys()).sort(), ['EmulatorJS-cores', 'other-application', 'retroone-pwa-test-version']);
  // self has no skipWaiting/clients.claim: executing both handlers would fail if either were called.
});

test('failed install removes only its incomplete cache and leaves current version', async () => {
  const sw = worker(); await sw.caches.open('retroone-pwa-current'); sw.goOffline();
  await assert.rejects(sw.lifecycle('install'), /network offline/);
  assert.deepEqual(await sw.caches.keys(), ['retroone-pwa-current']);
});

test('static icon is available offline without extra writes or network requests', async () => {
  const sw = worker(); await sw.lifecycle('install'); sw.goOffline();
  const count = sw.fetched.length;
  assert.equal(await (await sw.request('/icons/icon-192.png')).text(), 'network-only content');
  assert.equal(sw.fetched.length, count); assert.equal(sw.writes.length, 5);
});

function installer({secure = true, standalone = false} = {}) {
  const handlers = {}, documentHandlers = {}, registrations = [], observers = [];
  const button = {textContent: '', disabled: false, parentNode: {}, setAttribute() {}, insertAdjacentElement() {}};
  const status = {textContent: ''};
  const document = {
    documentElement: {lang: 'en'},
    querySelectorAll(selector) { return selector === '[data-retroone-install]' ? [button] : [status]; },
    querySelector() { return status; },
    addEventListener(name, handler) { documentHandlers[name] = handler; },
  };
  const registration = {waiting: null, addEventListener(name, handler) { handlers['registration:' + name] = handler; }};
  const global = {
    document, isSecureContext: secure, matchMedia: () => ({matches: standalone}),
    addEventListener(name, handler) { handlers[name] = handler; },
    navigator: {serviceWorker: {register: async (...args) => { registrations.push(args); return registration; }}},
  };
  vm.runInNewContext(fs.readFileSync('web/pwa-install.js', 'utf8'), {
    window: global, MutationObserver: class { constructor(handler) { observers.push(handler); } observe() {} },
  });
  const click = () => documentHandlers.click({target: {closest: () => button}, preventDefault() {}});
  return {handlers, registrations, button, status, click, registration, document, observers};
}

test('insecure HTTP stays usable and never registers a worker', async () => {
  const app = installer({secure: false}); await app.click();
  assert.equal(app.registrations.length, 0);
  assert.match(app.status.textContent, /requires HTTPS/);
});

test('secure install prompt requires explicit click and is consumed once', async () => {
  const app = installer(); await Promise.resolve();
  assert.equal(app.registrations[0][0], '/service-worker.js');
  assert.equal(app.registrations[0][1].scope, '/');
  assert.equal(app.registrations[0][1].updateViaCache, 'none');
  let prompts = 0, prevented = false;
  app.handlers.beforeinstallprompt({preventDefault() { prevented = true; }, async prompt() { prompts++; }, userChoice: Promise.resolve({outcome: 'dismissed'})});
  assert(prevented); assert.equal(prompts, 0);
  await app.click(); assert.equal(prompts, 1); assert.match(app.status.textContent, /cancelled/);
  await app.click(); assert.equal(prompts, 1); assert.match(app.status.textContent, /browser menu/);
});

test('waiting update only explains safe reopen; no activation or navigation API is used', async () => {
  const app = installer(); await Promise.resolve();
  app.registration.active = {state: 'activated'};
  app.registration.waiting = {postMessage() { throw new Error('must not activate waiting worker'); }};
  app.registration.installing = {addEventListener(name, handler) { handler(); }};
  app.handlers['registration:updatefound']();
  assert.match(app.status.textContent, /current game will not reload/);
  assert.equal(app.handlers.controllerchange, undefined);
});

test('first installation is not mislabeled as a waiting update', async () => {
  const app = installer(); await Promise.resolve();
  app.registration.waiting = {state: 'installed'};
  app.registration.installing = {addEventListener(name, handler) { handler(); }};
  app.handlers['registration:updatefound']();
  assert(!app.status.textContent.includes('update is ready'));
});

test('install affordance follows interface language and disables in standalone mode', async () => {
  const app = installer(); app.document.documentElement.lang = 'zh-CN'; app.observers[0]();
  assert.equal(app.button.textContent, '安装应用');
  const installed = installer({standalone: true});
  assert(installed.button.disabled); assert.equal(installed.button.textContent, 'Installed');
  app.document.documentElement.lang = 'en'; app.handlers.appinstalled();
  assert(app.button.disabled); assert.equal(app.button.textContent, 'Installed');
});
