/* Persistent, directory-backed browser ROM library. No ROM or directory leaves this browser. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RetroRomLibrary = new api.LibraryManager(root, root.RetroRomLibraryConfig || {});
})(typeof window === 'object' ? window : null, function () {
  'use strict';
  const fail = (code, message = code) => Object.assign(new Error(message), {code});
  const aborted = () => Object.assign(fail('cancelled'), {name: 'AbortError'});
  const missing = error => error?.name === 'NotFoundError';
  const safeName = name => typeof name === 'string' && /^[a-zA-Z0-9_-]+\.[a-zA-Z0-9]{1,8}$/.test(name);
  const serializable = entry => Object.fromEntries(Object.entries(entry).filter(([key]) => !key.startsWith('_')));
  function localURL(path, env) {
    if (typeof path !== 'string' || path.length > 8192 || !path.startsWith('/') || path.startsWith('//') || /[\\\x00-\x1f]/.test(path)) throw fail('invalid');
    const url = new URL(path, env.location.href);
    if (url.origin !== env.location.origin || url.hash || url.username || url.password) throw fail('invalid');
    return url;
  }
  function manifestValue(value, env) {
    if (!value || !Number.isSafeInteger(value.size) || value.size < 1 || !safeName(value.name) || !/^[a-zA-Z0-9_-]{1,128}$/.test(value.revision || '') || !/^[0-9a-f]{40}$/i.test(value.info_hash || '') || typeof value.torrent_base64 !== 'string' || value.torrent_base64.length > 1024 * 1024 || !/^[a-zA-Z0-9+/]+={0,2}$/.test(value.torrent_base64)) throw fail('invalid');
    if (value.p2p_available !== undefined && typeof value.p2p_available !== 'boolean') throw fail('invalid');
    const result = {...value, downloadURL: localURL(value.download_url, env).href};
    if (value.tracker_path) {
      const tracker = localURL(value.tracker_path, env); tracker.protocol = tracker.protocol === 'https:' ? 'wss:' : 'ws:'; result.trackerURL = tracker.href;
    }
    if (!Array.isArray(value.ice_servers) || value.ice_servers.length > 8) throw fail('invalid');
    result.ice_servers = value.ice_servers.map(item => {
      if (!item || typeof item !== 'object' || Array.isArray(item) || Object.keys(item).some(key => !['urls', 'username', 'credential'].includes(key))) throw fail('invalid');
      const urls = typeof item.urls === 'string' ? [item.urls] : item.urls;
      if (!Array.isArray(urls) || !urls.length || urls.length > 8 || urls.some(url => typeof url !== 'string' || url.length > 2048 || !/^(stun|stuns|turn|turns):(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?(?:\?transport=(?:udp|tcp))?$/.test(url) || /:([0-9]+)(?:\?|$)/.test(url) && Number(url.match(/:([0-9]+)(?:\?|$)/)[1]) > 65535)) throw fail('invalid');
      if (['username', 'credential'].some(key => item[key] !== undefined && (typeof item[key] !== 'string' || item[key].length > 1024 || /[\x00-\x1f]/.test(item[key])))) throw fail('invalid');
      return {...item, urls: [...urls]};
    });
    return result;
  }
  const codeOf = problem => problem?.code || (problem?.name === 'AbortError' ? 'cancelled' : problem?.name === 'QuotaExceededError' ? 'quota' : problem?.name === 'NotAllowedError' || problem?.name === 'SecurityError' ? 'permission' : /verification|hash mismatch|missing required field|invalid (torrent|data)/i.test(problem?.message || '') ? 'mismatch' : 'download');
  class LibraryManager {
    constructor(env, options = {}) {
      this.env = env; this.options = options; this.db = null; this.state = null; this.root = null;
      this.entries = new Map(); this.jobs = new Map(); this.listeners = new Set(); this.closed = false;
      this.permission = 'unselected'; this.sharing = true;
      try { this.sharing = env.localStorage?.getItem('retroone:browser-p2p') !== 'false'; } catch {}
      this.pendingSave = Promise.resolve(); this.initializing = null; this.savedEntries = new Map();
      this.onLeave = () => { this.closed = true; for (const job of this.jobs.values()) job.abort.abort(); };
      env.addEventListener?.('pagehide', this.onLeave);
      env.addEventListener?.('beforeunload', this.onLeave);
    }
    supported() { return Boolean(this.env.isSecureContext && this.env.showDirectoryPicker && this.env.indexedDB && this.env.navigator?.locks); }
    snapshot() { return {directory: {supported: this.supported(), name: this.state?.directory?.name || '', permission: this.permission}, sharing: this.sharing, entries: [...this.entries.values()].map(serializable)}; }
    subscribe(callback) { this.listeners.add(callback); callback(this.snapshot()); return () => this.listeners.delete(callback); }
    emit() { const value = this.snapshot(); for (const callback of this.listeners) { try { callback(value); } catch {} } }
    async database() {
      if (this.options.persistence) return this.options.persistence;
      if (!this.db) this.db = await new Promise((resolve, reject) => {
        const request = this.env.indexedDB.open(this.options.dbName || 'retroone-rom-library', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('state');
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      const transact = (mode, operation) => new Promise((resolve, reject) => {
        const transaction = this.db.transaction('state', mode), request = operation(transaction.objectStore('state'));
        let value; request.onsuccess = () => { value = request.result; }; transaction.oncomplete = () => resolve(value);
        transaction.onerror = transaction.onabort = () => reject(transaction.error || request.error);
      });
      return {load: () => transact('readonly', store => store.get('library')), save: value => transact('readwrite', store => store.put(value, 'library'))};
    }
    persist(replaceDirectory = false) {
      if (!this.state) return Promise.resolve();
      const value = {...this.state, entries: [...this.entries.values()].map(serializable)};
      this.pendingSave = this.pendingSave.catch(() => {}).then(async () => {
        const commit = async () => {
          const database = await this.database(), latest = await database.load();
          if (!replaceDirectory && latest && latest.owner !== value.owner) throw fail('directory-changed');
          const merged = new Map((replaceDirectory ? [] : latest?.entries || []).map(entry => [entry.id, entry]));
          for (const entry of value.entries) {
            if (replaceDirectory || JSON.stringify(entry) !== this.savedEntries.get(entry.id)) merged.set(entry.id, entry);
          }
          await database.save({...value, entries: [...merged.values()]});
          for (const entry of value.entries) this.savedEntries.set(entry.id, JSON.stringify(entry));
        };
        // Per-ROM locks protect bytes; this separate short lock prevents two tabs
        // from dropping each other's IndexedDB entries or reverting a directory change.
        if (this.env.navigator?.locks) await this.env.navigator.locks.request('retroone-rom-library-state', {mode: 'exclusive'}, commit);
        else await commit();
      });
      return this.pendingSave;
    }
    async init() {
      if (this.initializing) return this.initializing;
      this.initializing = (async () => {
        if (!this.supported() && !this.options.persistence) { this.permission = 'unsupported'; this.emit(); return this.snapshot(); }
        this.state = await (await this.database()).load() || null;
        for (const value of this.state?.entries || []) {
          this.savedEntries.set(value.id, JSON.stringify(value));
          const entry = {...value, cachedComplete: Boolean(value.cachedComplete || value.complete), complete: false, received: 0, http_bytes: 0, peer_bytes: 0, peers: 0, status: value.paused ? 'paused' : 'permission', error: null};
          this.entries.set(entry.id, entry);
        }
        if (this.state?.directory) {
          this.permission = await this.state.directory.queryPermission({mode: 'readwrite'});
          if (this.permission === 'granted') { await this.ownedRoot(); for (const entry of this.entries.values()) if (!entry.paused) this.resume(entry.id).catch(() => {}); }
        }
        this.emit(); return this.snapshot();
      })();
      return this.initializing;
    }
    async ownedRoot() {
      if (!this.state || this.permission !== 'granted') throw fail('permission');
      const root = await this.state.directory.getDirectoryHandle(this.state.subdirectory);
      const marker = JSON.parse(await (await (await root.getFileHandle('.retroone-owner.json')).getFile()).text());
      if (marker.owner !== this.state.owner || marker.version !== 1) throw fail('ownership');
      this.root = root; return root;
    }
    async chooseDirectory() {
      // Must be called directly from a click; invoke the picker before awaiting anything.
      const selected = await this.env.showDirectoryPicker({id: 'retroone-rom-library', mode: 'readwrite', startIn: 'downloads'});
      await this.init();
      if (this.state?.directory && await selected.isSameEntry(this.state.directory)) { this.permission = 'granted'; await this.ownedRoot(); this.emit(); return this.snapshot(); }
      await this.pauseAll();
      const owner = this.env.crypto.randomUUID(); let subdirectory = 'RetroOne';
      try { await selected.getDirectoryHandle(subdirectory); subdirectory = `RetroOne-${owner}`; }
      catch (problem) { if (!missing(problem)) throw problem; }
      const root = await selected.getDirectoryHandle(subdirectory, {create: true});
      const marker = await root.getFileHandle('.retroone-owner.json', {create: true}), writer = await marker.createWritable();
      await writer.write(JSON.stringify({version: 1, owner})); await writer.close();
      this.state = {directory: selected, subdirectory, owner}; this.root = root; this.permission = 'granted'; this.entries.clear();
      await this.persist(true); this.emit(); return this.snapshot();
    }
    async reconnect() {
      if (!this.state?.directory) return this.chooseDirectory();
      // Explicit UI action only. init never calls requestPermission.
      this.permission = await this.state.directory.requestPermission({mode: 'readwrite'});
      if (this.permission === 'granted') { await this.ownedRoot(); for (const entry of this.entries.values()) if (!entry.paused) this.resume(entry.id).catch(() => {}); }
      this.emit(); return this.snapshot();
    }
    async script(path, global) {
      if (this.env[global]) return;
      if (this.options.loadScript) { await this.options.loadScript(path, global); return; }
      const url = localURL(path, this.env).href;
      await new Promise((resolve, reject) => {
        const script = this.env.document.createElement('script'); script.src = url; script.async = true;
        const timer = this.env.setTimeout(() => done(fail('component')), 30000);
        const done = problem => { this.env.clearTimeout(timer); script.onload = script.onerror = null; if (problem) { script.remove(); reject(problem); } else resolve(); };
        script.onload = () => done(); script.onerror = () => done(fail('component')); this.env.document.head.append(script);
      });
      if (!this.env[global]) throw fail('component');
    }
    async fetchManifest(url, signal) {
      const address = localURL(url, this.env); address.searchParams.set('transport', 'http');
      while (true) {
        if (signal?.aborted || this.closed) throw aborted();
        const response = await this.env.fetch(address.href, {signal, credentials: 'same-origin', cache: 'no-store'});
        if (response.status === 202) {
          const seconds = Math.max(1, Math.min(30, Number(response.headers.get('retry-after')) || 2));
          await this.delay(seconds * 1000, signal); continue;
        }
        if (!response.ok) throw fail('network');
        let value; try { value = await response.json(); } catch { throw fail('invalid'); }
        return manifestValue(value, this.env);
      }
    }
    delay(ms, signal) {
      return new Promise((resolve, reject) => {
        const finish = problem => { this.env.clearTimeout(timer); signal?.removeEventListener('abort', cancel); problem ? reject(problem) : resolve(); };
        const cancel = () => finish(aborted()), timer = this.env.setTimeout(() => finish(), ms);
        signal?.addEventListener('abort', cancel, {once: true}); if (signal?.aborted) cancel();
      });
    }
    findEntry(gameId, revision) {
      return [...this.entries.values()].filter(entry => entry.gameId === gameId && (!revision || !entry.revision || entry.revision === revision))
        .sort((a, b) => Number(this.jobs.has(b.id)) - Number(this.jobs.has(a.id)) || Number(b.complete) - Number(a.complete))[0];
    }
    async add({gameId, title, profileId, manifestUrl, allowP2P, revision, _ownerToken}) {
      await this.init(); if (!this.root || this.permission !== 'granted') throw fail('permission');
      if (!/^[a-zA-Z0-9_-]{1,128}$/.test(gameId || '') || revision !== undefined && !/^[a-zA-Z0-9_-]{1,128}$/.test(revision)) throw fail('invalid'); localURL(manifestUrl, this.env);
      const base = `${profileId || 'default'}-${gameId}`;
      if (!/^[a-zA-Z0-9_-]{1,200}$/.test(base)) throw fail('invalid');
      let entry = this.findEntry(gameId, revision);
      if (!entry) {
        const id = this.entries.has(base) ? `${base}-${this.env.crypto.randomUUID()}` : base;
        entry = {id, gameId, title: String(title || gameId).slice(0, 200), profileId: profileId || '', manifestUrl, revision: revision || '', name: '', size: 0, received: 0, http_bytes: 0, peer_bytes: 0, uploaded: 0, peers: 0, cachedComplete: false, status: 'queued', error: null, complete: false, paused: false}; this.entries.set(id, entry);
      } else { entry.manifestUrl = manifestUrl; entry.paused = false; }
      // Reserve the start before awaiting persistence: simultaneous profiles must
      // share one transfer, and only its creator may cancel that incomplete job.
      if (!this.jobs.has(entry.id) && !entry._starting) {
        entry._starting = true; entry._ownerToken = _ownerToken;
        if (typeof allowP2P === 'boolean') entry._allowP2P = allowP2P;
      }
      await this.persist(); await this.resume(entry.id); return serializable(entry);
    }
    async entryDirectory(entry, manifest) {
      // Each revision has its own immutable namespace; changed ROMs cannot overwrite an old revision.
      const folder = `rom-${manifest.info_hash.toLowerCase()}`;
      const directory = await this.root.getDirectoryHandle(folder, {create: true});
      let marker;
      try { marker = JSON.parse(await (await (await directory.getFileHandle('.entry.json')).getFile()).text()); }
      catch (problem) { if (!missing(problem)) throw problem; }
      if (marker) { if (marker.owner !== this.state.owner || marker.infoHash !== manifest.info_hash.toLowerCase()) throw fail('ownership'); }
      else {
        // A folder created by a user without our marker is not ours to populate.
        for await (const _ of directory.values()) throw fail('ownership');
        const stream = await (await directory.getFileHandle('.entry.json', {create: true})).createWritable();
        await stream.write(JSON.stringify({owner: this.state.owner, infoHash: manifest.info_hash.toLowerCase()})); await stream.close();
      }
      return directory;
    }
    async resume(id, refresh = null) {
      await this.init(); const entry = this.entries.get(id); if (!entry) throw fail('not-found');
      if (this.closed) throw aborted();
      if (refresh && (entry.paused || entry._refreshGuard !== refresh.token)) return serializable(entry);
      delete entry._refreshGuard;
      const shared = this.findEntry(entry.gameId, entry.revision);
      if (shared && shared.id !== id && this.jobs.has(shared.id)) return serializable(shared);
      const previous = this.jobs.get(id);
      if (previous?.abort.signal.aborted) await previous.done;
      else if (previous) return serializable(entry);
      if (this.permission !== 'granted' || !this.root) { entry.status = 'permission'; this.emit(); return serializable(entry); }
      const job = {abort: new this.env.AbortController(), ownerToken: entry._ownerToken, manifest: refresh?.manifest || null, allowP2P: refresh ? refresh.allowP2P : entry._allowP2P !== false, client: null, store: null, timer: null, release: null, done: null};
      delete entry._allowP2P; delete entry._ownerToken; delete entry._starting; this.jobs.set(id, job); entry.paused = false; entry.complete = false; entry.status = 'queued'; entry.error = null; this.emit();
      job.done = this.run(entry, job).finally(async () => {
        if (this.jobs.get(id) !== job) return;
        this.jobs.delete(id);
        if (job.nextManifest && !job.abort.signal.aborted && !job.invalidated && !entry.paused && !this.closed) {
          const token = {}; entry._refreshGuard = token;
          await this.resume(id, {token, manifest: job.nextManifest, allowP2P: job.allowP2P});
        }
      });
      job.done.catch(() => {}); return serializable(entry);
    }
    async run(entry, job) {
      const signal = job.abort.signal;
      try {
        const manifest = job.manifest || await this.fetchManifest(entry.manifestUrl, signal);
        job.manifest = null;
        const lockName = `retroone-rom:${this.state.owner}:${manifest.info_hash.toLowerCase()}`;
        await this.env.navigator.locks.request(lockName, {mode: 'exclusive', ifAvailable: true}, async lock => {
          if (!lock) { entry.status = 'busy'; this.emit(); return; }
          if (signal.aborted) throw aborted();
          const directory = await this.entryDirectory(entry, manifest);
          if (entry.revision && entry.revision !== manifest.revision) entry.cachedComplete = false;
          Object.assign(entry, {revision: manifest.revision, infoHash: manifest.info_hash.toLowerCase(), name: manifest.name, size: manifest.size, status: 'verifying', received: 0, http_bytes: 0, peer_bytes: 0, complete: false}); this.emit(); await this.persist();
          await this.script(this.options.webtorrentUrl || '/vendor/webtorrent.min.js', 'WebTorrent');
          await this.script(this.options.storeUrl || '/rom-directory-store.js', 'RetroRomDirectoryStore');
          if (signal.aborted) throw aborted();
          const allowPeers = this.sharing && job.allowP2P && manifest.p2p_available !== false && typeof this.env.RTCPeerConnection === 'function' && Boolean(manifest.trackerURL);
          const client = new this.env.WebTorrent({dht: false, lsd: false, utPex: false, natUpnp: false, natPmp: false, utp: false, webSeeds: true, seedOutgoingConnections: true, maxConns: 8, tracker: allowPeers ? {announce: [], rtcConfig: {iceServers: manifest.ice_servers}} : false});
          job.client = client;
          const now = () => this.options.now ? this.options.now() : Date.now();
          const token = manifest.trackerURL ? new URL(manifest.trackerURL).searchParams.get('token') || '' : '';
          const expiry = /^([0-9]+)\./.exec(token);
          const refreshInterval = this.options.ticketRefreshMs || 6 * 60 * 60 * 1000;
          job.refreshAt = Math.max(now() + 1000, Math.min(now() + refreshInterval, expiry ? Number(expiry[1]) * 1000 - 5 * 60 * 1000 : Infinity));
          const shutdown = () => {
            if (job.timer !== null) this.env.clearInterval(job.timer); job.timer = null;
            if (!job.shutdown) job.shutdown = new Promise(resolve => { try { client.destroy(() => resolve()); } catch { resolve(); } });
            return job.shutdown;
          };
          const Store = this.env.RetroRomDirectoryStore;
          const PieceStore = function (length) { job.store = new Store(length, {directory, name: manifest.name, length: manifest.size}); return job.store; };
          try { await new Promise((resolve, reject) => {
            let completed = false, settling = false;
            const own = () => this.jobs.get(entry.id) === job && !signal.aborted && !this.closed;
            const stop = () => { if (job.timer !== null) this.env.clearInterval(job.timer); job.timer = null; completed ? resolve() : reject(aborted()); };
            signal.addEventListener('abort', stop, {once: true});
            const failure = problem => { if (!own()) return; signal.removeEventListener('abort', stop); reject(problem); };
            client.on('error', failure);
            const refreshTicket = async () => {
              if (!own() || !completed || !allowPeers || job.refreshing || now() < job.refreshAt) return;
              job.refreshing = true;
              try {
                const fresh = await this.fetchManifest(entry.manifestUrl, signal);
                if (!own() || entry.paused) return;
                if (fresh.revision !== manifest.revision || fresh.info_hash.toLowerCase() !== manifest.info_hash.toLowerCase() || fresh.size !== manifest.size || fresh.name !== manifest.name) { failure(fail('revision-changed')); return; }
                // The old client and its store fully close before run releases
                // the Web Lock. Only then may resume create the replacement.
                job.nextManifest = fresh; signal.removeEventListener('abort', stop); resolve();
              } catch (problem) {
                if (!own()) return;
                if (['invalid', 'mismatch', 'permission'].includes(codeOf(problem))) failure(problem);
                else job.refreshAt = now() + 60 * 1000;
              } finally { job.refreshing = false; }
            };
            const ready = torrent => {
              if (!own()) return;
              if (torrent.infoHash?.toLowerCase() !== manifest.info_hash.toLowerCase() || torrent.files.length !== 1 || torrent.length !== manifest.size || torrent.files[0].name !== manifest.name) { failure(fail('mismatch')); return; }
              entry.status = 'downloading'; this.emit();
              const complete = async () => {
                if (!own() || settling) return; settling = true;
                try {
                  const file = await job.store.finalize(); if (!own()) return;
                  entry._fileStat = {size: file.size, lastModified: file.lastModified}; entry.complete = true; entry.cachedComplete = true; entry.received = entry.size; entry.status = allowPeers ? 'sharing' : 'ready'; entry.error = null; completed = true; this.emit(); await this.persist();
                  if (!allowPeers) { signal.removeEventListener('abort', stop); resolve(); }
                } catch (problem) { failure(problem); }
              };
              torrent.on('done', complete); if (torrent.done) complete();
            };
            let torrent;
            try {
              const bytes = Uint8Array.from(this.env.atob(manifest.torrent_base64), character => character.charCodeAt(0));
              torrent = client.add(bytes, {announce: allowPeers ? [manifest.trackerURL] : [], urlList: [manifest.downloadURL], private: true, store: PieceStore, destroyStoreOnDestroy: false, storeCacheSlots: 0, maxWebConns: 2}, ready);
              torrent.on('error', failure);
              const seen = new WeakSet();
              const wire = channel => {
                if (!channel || seen.has(channel)) return; seen.add(channel);
                channel.on('download', bytes => {
                  if (!own() || !Number.isSafeInteger(bytes) || bytes <= 0) return;
                  entry[channel.type === 'webSeed' ? 'http_bytes' : 'peer_bytes'] += bytes; this.emit();
                });
              };
              torrent.on('wire', wire); for (const channel of torrent.wires || []) wire(channel);
              torrent.on('warning', problem => { if (/verification|hash mismatch/i.test(problem?.message || '')) failure(fail('mismatch')); });
              torrent.on('download', () => { if (own()) { entry.received = torrent.downloaded; this.emit(); } });
              job.timer = this.env.setInterval(async () => {
                if (!own()) return;
                entry.uploaded = torrent.uploaded || 0; entry.peers = (torrent.wires || []).filter(wire => wire.type !== 'webSeed' && !wire.destroyed).length;
                if (entry.complete) {
                  try { await job.store.file(entry._fileStat); } catch (problem) { failure(problem); return; }
                  await refreshTicket();
                }
                this.emit();
              }, 2000);
            } catch (problem) { failure(problem); }
          }); } finally { await shutdown(); await job.store?.queue; }
        });
      } catch (problem) {
        if (job.invalidated) { entry.status = 'error'; entry.error = codeOf(job.invalidated); entry.complete = false; }
        else if (signal.aborted || this.closed) { if (!entry.complete) entry.status = 'paused'; }
        else { entry.status = codeOf(problem) === 'permission' ? 'permission' : 'error'; entry.error = codeOf(problem); entry.complete = false; }
      } finally {
        if (job.timer !== null) this.env.clearInterval(job.timer);
        if (job.client && !job.client.destroyed) await new Promise(resolve => { try { job.client.destroy(() => resolve()); } catch { resolve(); } });
        entry.peers = 0; this.emit(); await this.persist().catch(() => {});
      }
    }
    async pause(id) {
      const entry = this.entries.get(id); if (!entry) return;
      entry.paused = true; delete entry._refreshGuard; const job = this.jobs.get(id); job?.abort.abort(); if (job?.done) await job.done;
      entry.status = 'paused'; entry.peers = 0; await this.persist(); this.emit(); return serializable(entry);
    }
    async pauseAll() { await Promise.all([...this.jobs.keys()].map(id => this.pause(id))); }
    async setSharing(enabled) {
      this.sharing = Boolean(enabled); try { this.env.localStorage?.setItem('retroone:browser-p2p', String(this.sharing)); } catch {}
      const resume = [...this.entries.values()].filter(entry => !entry.paused).map(entry => entry.id);
      await this.pauseAll(); for (const id of resume) { const entry = this.entries.get(id); entry.paused = false; this.resume(id).catch(() => {}); }
      this.emit(); return this.snapshot();
    }
    async getFile({gameId, revision}) {
      await this.init();
      const entry = [...this.entries.values()].find(value => value.gameId === gameId && value.revision === revision && value.complete);
      if (!entry) return null;
      try {
        const directory = await this.root.getDirectoryHandle(`rom-${entry._infoHash || entry.infoHash}`);
        const file = await (await directory.getFileHandle(entry.name)).getFile();
        if (!entry._fileStat || file.size !== entry._fileStat.size || file.lastModified !== entry._fileStat.lastModified) throw fail('modified');
        return file;
      } catch (problem) {
        entry.complete = false; entry.cachedComplete = false; entry.status = 'error'; entry.error = codeOf(problem);
        const job = this.jobs.get(entry.id); if (job) { job.invalidated = problem; job.abort.abort(); }
        this.emit(); return null;
      }
    }
    async ensureFile(manifest, {gameId, title, profileId, manifestUrl, allowP2P, signal} = {}) {
      if (signal?.aborted) throw aborted(); await this.init();
      const cached = await this.getFile({gameId, revision: manifest.revision}); if (signal?.aborted) throw aborted(); if (cached) return cached;
      if (!manifestUrl) {
        const source = manifest.download_url?.match(/^(.*\/api\/games\/[^/]+)\/browser-rom\//)?.[1];
        if (!source) throw fail('invalid'); manifestUrl = `${source}/download-manifest?transport=p2p`;
      }
      const ownerToken = Symbol('game launch transfer');
      const entry = await this.add({gameId, title, profileId, manifestUrl, allowP2P, revision: manifest.revision, _ownerToken: ownerToken});
      const ownedJob = this.jobs.get(entry.id);
      const ownsTransfer = ownedJob?.ownerToken === ownerToken;
      return new Promise((resolve, reject) => {
        let unsubscribe = () => {}, finished = false;
        const finish = (problem, file) => { if (finished) return; finished = true; unsubscribe(); signal?.removeEventListener('abort', cancel); problem ? reject(problem) : resolve(file); };
        const cancel = () => {
          // Cancelling a game launch only cancels the new, incomplete transfer it
          // owns. Existing background downloads and finished seeding stay alive.
          if (ownsTransfer && this.jobs.get(entry.id) === ownedJob && !this.entries.get(entry.id)?.complete) this.pause(entry.id).catch(() => {});
          finish(aborted());
        };
        signal?.addEventListener('abort', cancel, {once: true});
        unsubscribe = this.subscribe(async snapshot => {
          const current = snapshot.entries.find(value => value.id === entry.id); if (!current || finished) return;
          if (current.complete && current.revision !== manifest.revision) { finish(fail('mismatch')); return; }
          if (current.complete) { const file = await this.getFile({gameId, revision: current.revision}); if (file) finish(null, file); }
          else if (['error', 'permission', 'busy'].includes(current.status)) finish(fail(current.error || current.status));
        });
        if (finished) unsubscribe(); if (signal?.aborted) cancel();
      });
    }
  }
  return {LibraryManager, manifestValue, localURL};
});
