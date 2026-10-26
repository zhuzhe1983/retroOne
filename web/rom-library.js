/* Directory-backed ROM library. Directory handles stay here; verified ROM pieces may be shared by P2P. */
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
  const codeOf = problem => typeof problem?.code === 'string' && problem.code ? problem.code : (problem?.name === 'AbortError' ? 'cancelled' : problem?.name === 'QuotaExceededError' ? 'quota' : problem?.name === 'NotSupportedError' ? 'unsupported' : problem?.name === 'NotAllowedError' || problem?.name === 'SecurityError' ? 'permission' : /verification|hash mismatch|missing required field|invalid (torrent|data)/i.test(problem?.message || '') ? 'mismatch' : 'download');
  class LibraryManager {
    constructor(env, options = {}) {
      this.env = env; this.options = options; this.db = null; this.state = null; this.root = null;
      this.entries = new Map(); this.jobs = new Map(); this.listeners = new Set(); this.closed = false;
      this.permission = 'unselected'; this.storageError = null; this.temporaryFallback = false; this.locationEpoch = 0; this.locationSwitching = false; this.sharing = true;
      try { this.sharing = env.localStorage?.getItem('retroone:browser-p2p') !== 'false'; } catch {}
      this.pendingSave = Promise.resolve(); this.initializing = null; this.savedEntries = new Map();
      this.onLeave = () => { this.closed = true; for (const job of this.jobs.values()) job.abort.abort(); };
      env.addEventListener?.('pagehide', this.onLeave);
      env.addEventListener?.('beforeunload', this.onLeave);
    }
    supported() { return Boolean(this.env.isSecureContext && this.env.indexedDB && this.env.navigator?.locks && (this.env.navigator.storage?.getDirectory || this.env.showDirectoryPicker)); }
    snapshot() { return {directory: {supported: this.supported(), name: this.state?.directory?.name || '', permission: this.permission,
      storage_kind: this.state ? this.state.storage_kind || 'external' : null, user_selected: Boolean(this.state?.user_selected), selection_required: !this.state?.user_selected, location_change_blocked: this.hasUnfinished(), can_choose_directory: typeof this.env.showDirectoryPicker === 'function', can_use_browser_storage: Boolean(this.env.navigator?.storage?.getDirectory), temporary_fallback: this.temporaryFallback, error: this.storageError},
      sharing: this.sharing, entries: [...this.entries.values()].map(serializable)}; }
    hasUnfinished(entries = null) {
      if (entries === null && [...this.jobs.values()].some(job => job.activityHeld)) return true;
      return (entries || [...this.entries.values()]).some(entry => Boolean(entry._starting) || ['queued', 'verifying', 'downloading'].includes(entry.status) || !entry.complete && !entry.cachedComplete);
    }
    stateLock(callback) {
      return this.env.navigator?.locks ? this.env.navigator.locks.request('retroone-rom-library-state', {mode: 'exclusive'}, callback) : callback();
    }
    locationState(value) {
      if (!value || !value.directory?.getDirectoryHandle || !/^[a-zA-Z0-9_-]{1,200}$/.test(value.owner || '') || !/^[a-zA-Z0-9_-]{1,200}$/.test(value.subdirectory || '') ||
          value.storage_kind !== undefined && !['opfs', 'external'].includes(value.storage_kind) || value.user_selected !== undefined && typeof value.user_selected !== 'boolean' || value.entries !== undefined && !Array.isArray(value.entries)) throw fail('ownership');
      const storage_kind = value.storage_kind || 'external';
      return {...value, storage_kind, user_selected: value.user_selected ?? storage_kind === 'external', entries: value.entries || []};
    }
    adopt(value) {
      this.state = this.locationState(value); this.root = null; this.entries.clear(); this.savedEntries.clear(); this.storageError = null; this.temporaryFallback = false;
      for (const value of this.state.entries) {
        this.savedEntries.set(value.id, JSON.stringify(value));
        this.entries.set(value.id, {...value, cachedComplete: Boolean(value.cachedComplete || value.complete), complete: false, received: 0, http_bytes: 0, peer_bytes: 0, peers: 0, status: value.paused ? 'paused' : 'permission', error: null});
      }
    }
    resumeUnpaused() {
      for (const entry of this.entries.values()) {
        if (entry.paused) { entry.status = 'paused'; continue; }
        if (this.jobs.has(entry.id)) continue;
        entry.status = entry.cachedComplete ? 'verifying' : 'queued';
        this.resume(entry.id).catch(() => {});
      }
    }
    async createLocation(directory, storage_kind) {
      const owner = this.env.crypto.randomUUID(); let subdirectory = storage_kind === 'opfs' ? 'retroone-rom-library' : 'RetroOne';
      try { await directory.getDirectoryHandle(subdirectory); subdirectory += `-${owner}`; }
      catch (problem) { if (!missing(problem)) throw problem; }
      const root = await directory.getDirectoryHandle(subdirectory, {create: true});
      const marker = await root.getFileHandle('.retroone-owner.json', {create: true});
      if (typeof marker.createWritable !== 'function') throw fail('unsupported');
      const writer = await marker.createWritable();
      try { await writer.write(JSON.stringify({version: 1, owner})); await writer.close(); }
      catch (problem) { await writer.abort?.().catch(() => {}); throw problem; }
      return {directory, storage_kind, subdirectory, owner, user_selected: false, entries: []};
    }
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
          // A second tab may have switched away and back to this same owner.
          // Retain the newest location history instead of restoring our stale copy.
          const user_selected = Boolean(value.user_selected || latest && this.locationState(latest).user_selected);
          await database.save({...value, user_selected, previous_locations: latest?.previous_locations || value.previous_locations || [], entries: [...merged.values()]});
          if (this.state?.owner === value.owner) this.state.user_selected = user_selected;
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
        try {
          if (!this.supported() && !this.options.persistence) {
            // Read existing metadata when possible even without write capability:
            // an inaccessible external cache must not be mistaken for no cache.
            const existing = this.env.indexedDB ? await (await this.database()).load() : null;
            if (existing) this.adopt(existing);
            else this.temporaryFallback = true;
            throw fail('unsupported');
          }
          const database = await this.database();
          // Check again inside the same short cross-tab lock used by metadata
          // writes. Two fresh tabs must adopt one owner, never replace a library.
          const value = await this.stateLock(async () => {
            const existing = await database.load();
            if (existing) return existing;
            if (!this.env.navigator?.storage?.getDirectory) return null;
            let created;
            try { created = await this.createLocation(await this.env.navigator.storage.getDirectory(), 'opfs'); }
            catch (problem) { if (codeOf(problem) === 'unsupported') this.temporaryFallback = true; throw problem; }
            await database.save(created); return created;
          });
          if (!value) throw fail('unsupported');
          this.adopt(value);
          this.permission = this.state.storage_kind === 'opfs' ? 'granted' : await this.state.directory.queryPermission({mode: 'readwrite'});
          if (this.permission === 'granted') {
            await this.ownedRoot();
            this.resumeUnpaused();
          } else this.storageError = 'permission';
        } catch (problem) {
          this.storageError = codeOf(problem); this.permission = this.storageError === 'unsupported' ? 'unsupported' : 'error'; this.root = null;
        }
        this.emit(); return this.snapshot();
      })();
      return this.initializing;
    }
    async ownedRoot() {
      if (!this.state || this.permission !== 'granted') throw fail('permission');
      let root;
      try { root = await this.state.directory.getDirectoryHandle(this.state.subdirectory); }
      catch (problem) {
        // A user may clear only OPFS files while leaving IndexedDB. Recreate our
        // recorded root under the metadata lock, then re-verify/download bytes;
        // never clear a still-present directory with an invalid owner marker.
        if (!missing(problem) || this.state.storage_kind !== 'opfs') throw problem;
        root = await this.stateLock(async () => {
          const latest = await (await this.database()).load();
          if (latest?.owner !== this.state.owner) throw fail('directory-changed');
          let directory;
          try { return await this.state.directory.getDirectoryHandle(this.state.subdirectory); }
          catch (error) { if (!missing(error)) throw error; directory = await this.state.directory.getDirectoryHandle(this.state.subdirectory, {create: true}); }
          const writer = await (await directory.getFileHandle('.retroone-owner.json', {create: true})).createWritable();
          try { await writer.write(JSON.stringify({version: 1, owner: this.state.owner})); await writer.close(); }
          catch (error) { await writer.abort?.().catch(() => {}); throw error; }
          return directory;
        });
      }
      const marker = JSON.parse(await (await (await root.getFileHandle('.retroone-owner.json')).getFile()).text());
      if (marker.owner !== this.state.owner || marker.version !== 1) throw fail('ownership');
      this.root = root; return root;
    }
    async chooseDirectory() {
      if (typeof this.env.showDirectoryPicker !== 'function') throw fail('unsupported');
      // Invoke the picker in the click turn, before any await consumes its gesture.
      const selected = await this.env.showDirectoryPicker({id: 'retroone-rom-library', mode: 'readwrite', startIn: 'downloads'});
      return this.selectLocation(selected, 'external');
    }
    async useBrowserStorage() {
      if (!this.env.navigator?.storage?.getDirectory) throw fail('unsupported');
      try { return await this.selectLocation(await this.env.navigator.storage.getDirectory(), 'opfs'); }
      catch (problem) { throw fail(codeOf(problem)); }
    }
    async selectLocation(selected, storage_kind) {
      await this.init();
      if (!this.env.navigator?.locks) throw fail('unsupported');
      if (this.locationSwitching) throw fail('busy');
      const previousOwner = this.state?.owner;
      const previousSharing = [...this.entries.values()].filter(entry => entry.complete && !entry.paused && entry.status === 'sharing' && this.jobs.has(entry.id));
      this.locationSwitching = true;
      try {
        await this.activateLocation(selected, storage_kind);
        this.locationSwitching = false; this.resumeUnpaused(); return this.snapshot();
      } catch (problem) {
        this.locationSwitching = false;
        // A new reservation in another tab can make the final check refuse the
        // switch after our completed seeders were stopped. Restore only those
        // original seeders, never manual pauses or incomplete downloads.
        if (this.state?.owner === previousOwner) for (const entry of previousSharing) {
          if (this.entries.get(entry.id) === entry && entry.cachedComplete && !entry.paused && entry.status === 'paused' && !this.jobs.has(entry.id)) this.resume(entry.id).catch(() => {});
        }
        throw problem;
      }
      finally { this.locationSwitching = false; this.emit(); }
    }
    async switchLock(owners, callback) {
      const names = [...new Set(owners.filter(Boolean))].sort();
      const take = index => index === names.length ? callback() : this.env.navigator.locks.request(
        `retroone-rom-library-active:${names[index]}`, {mode: 'exclusive', ifAvailable: true}, lock => {
          if (!lock) throw fail('location-active');
          return take(index + 1);
        });
      return take(0);
    }
    async activateLocation(selected, storage_kind) {
      const database = await this.database();
      if (this.state && await selected.isSameEntry(this.state.directory) && await this.stateLock(async () => (await database.load())?.owner === this.state.owner)) {
        // Reauthorizing the same folder is allowed even during a transfer.
        this.permission = 'granted'; this.storageError = null; await this.ownedRoot();
        await this.stateLock(async () => {
          const latest = await database.load();
          if (latest?.owner !== this.state.owner) throw fail('directory-changed');
          await database.save({...latest, user_selected: true});
          this.state.user_selected = true;
        });
        this.emit(); return this.snapshot();
      }
      const observed = await database.load();
      const owners = [this.state?.owner, observed?.owner];
      return this.switchLock(owners, async () => {
        const assertCanSwitch = latest => {
          if (latest && !owners.includes(latest.owner)) throw fail('location-active');
          if (this.hasUnfinished() || latest && this.hasUnfinished(latest.entries || [])) throw fail('location-active');
        };
        // Check the shared index before touching jobs or launch listeners. Paused
        // partial files are unfinished too; switching never discards their resume
        // path. Reservations are persisted under this same lock before ROM I/O.
        await this.stateLock(async () => assertCanSwitch(await database.load()));
        await this.pauseAll({allowStaleLocation: true, preserveIntent: true});
        const value = await this.stateLock(async () => {
          const latest = await database.load();
          assertCanSwitch(latest);
          if (latest && await selected.isSameEntry(latest.directory)) {
            const saved = {...latest, user_selected: true}; await database.save(saved); return saved;
          }
          const locations = latest ? [...(latest.previous_locations || []), this.locationState(latest)] : [];
          let chosen = -1;
          for (let index = 0; index < locations.length; index++) if (await selected.isSameEntry(locations[index].directory)) { chosen = index; break; }
          if (chosen < 0 && locations.length >= 16) throw fail('location-limit');
          const next = chosen < 0 ? await this.createLocation(selected, storage_kind) : locations.splice(chosen, 1)[0];
          const previous_locations = locations.map(({previous_locations: _, ...location}) => location);
          const saved = {...next, user_selected: true, previous_locations}; await database.save(saved); return saved;
        });
        // Only a committed location change invalidates waiting file references.
        this.locationEpoch++; this.emit();
        this.adopt(value); this.permission = 'granted';
        try { await this.ownedRoot(); } catch (problem) { this.storageError = codeOf(problem); this.permission = 'error'; this.emit(); throw problem; }
        this.emit(); return this.snapshot();
      });
    }
    async reconnect() {
      if (!this.state?.directory) return this.chooseDirectory();
      // Explicit UI action only. init never calls requestPermission.
      this.permission = this.state.storage_kind === 'opfs' ? 'granted' : await this.state.directory.requestPermission({mode: 'readwrite'});
      this.storageError = this.permission === 'granted' ? null : 'permission';
      if (this.permission === 'granted') {
        try { await this.ownedRoot(); } catch (problem) { this.storageError = codeOf(problem); this.permission = 'error'; this.emit(); throw problem; }
        this.resumeUnpaused();
      }
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
      await this.init(); if (this.locationSwitching) throw fail('directory-changed');
      const locationEpoch = this.locationEpoch;
      if (!this.root || this.permission !== 'granted') throw fail(this.storageError || 'permission');
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
        if (typeof allowP2P === 'boolean') { entry._allowP2P = allowP2P; entry.allowP2P = allowP2P; }
      }
      await this.persist();
      if (this.locationSwitching || locationEpoch !== this.locationEpoch) throw fail('directory-changed');
      await this.resume(entry.id); return serializable(entry);
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
      await this.init(); const entry = this.entries.get(id), owner = this.state?.owner; if (!entry) throw fail('not-found');
      if (this.closed) throw aborted();
      if (this.locationSwitching) throw fail('location-active');
      if (refresh && (entry.paused || entry._refreshGuard !== refresh.token)) return serializable(entry);
      delete entry._refreshGuard;
      const shared = this.findEntry(entry.gameId, entry.revision);
      if (shared && shared.id !== id && this.jobs.has(shared.id)) return serializable(shared);
      const previous = this.jobs.get(id);
      if (previous?.abort.signal.aborted) await previous.done;
      else if (previous) return serializable(entry);
      if (this.locationSwitching || this.state?.owner !== owner || this.entries.get(id) !== entry) throw fail('directory-changed');
      if (this.permission !== 'granted' || !this.root) { entry.status = 'permission'; this.emit(); return serializable(entry); }
      const job = {owner, abort: new this.env.AbortController(), ownerToken: entry._ownerToken, manifest: refresh?.manifest || null, allowP2P: refresh ? refresh.allowP2P : (entry._allowP2P ?? entry.allowP2P) !== false, client: null, store: null, timer: null, release: null, done: null};
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
      // Keep an owner-level shared lease from preparation through verification.
      // The ROM lock still protects each file; this lease only fences location
      // switches, and is released once complete so ongoing seeding can move.
      // Reserve queued work under the metadata lock before taking the lease,
      // including restarts of previously completed files.
      try { await this.persist(); }
      catch (problem) { entry.status = 'error'; entry.error = codeOf(problem); this.emit(); return; }
      let enter, reject, release;
      const acquired = new Promise((resolve, fail) => { enter = resolve; reject = fail; });
      const lease = this.env.navigator.locks.request(`retroone-rom-library-active:${job.owner}`, {mode: 'shared', signal: job.abort.signal}, async () => {
        await new Promise(resolve => { release = resolve; job.activityHeld = true; enter(); });
      });
      lease.catch(reject); job.activityDone = lease;
      job.releaseActivity = () => { if (release) { release(); release = null; } };
      try { await acquired; await this.runTransfer(entry, job); }
      catch (problem) {
        entry.status = job.abort.signal.aborted ? 'paused' : 'error'; entry.error = job.abort.signal.aborted ? null : codeOf(problem);
        await this.persist().catch(() => {});
      }
      finally { job.releaseActivity(); await lease.catch(() => {}); job.activityHeld = false; this.emit(); }
    }
    async runTransfer(entry, job) {
      const signal = job.abort.signal;
      try {
        if (this.state?.owner !== job.owner || this.entries.get(entry.id) !== entry) throw fail('directory-changed');
        if (signal.aborted) throw aborted();
        const manifest = job.manifest || await this.fetchManifest(entry.manifestUrl, signal);
        job.manifest = null;
        const lockName = `retroone-rom:${this.state.owner}:${manifest.info_hash.toLowerCase()}`;
        await this.env.navigator.locks.request(lockName, {mode: 'exclusive', ifAvailable: true}, async lock => {
          if (!lock) { entry.status = 'busy'; this.emit(); return; }
          if (signal.aborted) throw aborted();
          const directory = await this.entryDirectory(entry, manifest);
          if (entry.revision && entry.revision !== manifest.revision) entry.cachedComplete = false;
          Object.assign(entry, {revision: manifest.revision, infoHash: manifest.info_hash.toLowerCase(), name: manifest.name, size: manifest.size, status: 'verifying', received: 0, http_bytes: 0, peer_bytes: 0, complete: false}); this.emit(); await this.persist();
          if (this.state.storage_kind === 'opfs' && this.env.navigator.storage?.estimate) {
            let stored = 0;
            try { stored = Math.min(manifest.size, (await (await directory.getFileHandle(manifest.name)).getFile()).size); }
            catch (problem) { if (!missing(problem)) throw problem; }
            const estimate = await this.env.navigator.storage.estimate();
            if (stored < manifest.size && Number.isFinite(estimate.quota) && Number.isFinite(estimate.usage) && estimate.quota - estimate.usage < (manifest.size - stored) * 1.15) throw fail('quota');
          }
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
                  job.releaseActivity(); await job.activityDone; job.activityHeld = false; this.emit();
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
    async pause(id, {preserveIntent = false} = {}) {
      const entry = this.entries.get(id); if (!entry) return;
      const wasPaused = Boolean(entry.paused);
      entry.paused = true; delete entry._refreshGuard; const job = this.jobs.get(id); job?.abort.abort(); if (job?.done) await job.done;
      if (preserveIntent) entry.paused = wasPaused;
      entry.status = 'paused'; entry.peers = 0; await this.persist(); this.emit(); return serializable(entry);
    }
    async pauseAll({allowStaleLocation = false, preserveIntent = false} = {}) {
      await Promise.all([...this.jobs.keys()].map(async id => {
        try { await this.pause(id, {preserveIntent}); }
        catch (problem) {
          // An explicit switch may adopt the registry changed by another tab,
          // after our old jobs have stopped. Never write that old owner back.
          if (!allowStaleLocation || codeOf(problem) !== 'directory-changed') throw problem;
        }
      }));
    }
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
      const locationEpoch = this.locationEpoch, owner = this.state?.owner;
      const sameLocation = () => this.locationEpoch === locationEpoch && this.state?.owner === owner;
      if (this.locationSwitching || !sameLocation()) throw fail('directory-changed');
      const cached = await this.getFile({gameId, revision: manifest.revision});
      if (signal?.aborted) throw aborted(); if (!sameLocation()) throw fail('directory-changed'); if (cached) return cached;
      if (!manifestUrl) {
        const source = manifest.download_url?.match(/^(.*\/api\/games\/[^/]+)\/browser-rom\//)?.[1];
        if (!source) throw fail('invalid'); manifestUrl = `${source}/download-manifest?transport=p2p`;
      }
      const ownerToken = Symbol('game launch transfer');
      const entry = await this.add({gameId, title, profileId, manifestUrl, allowP2P, revision: manifest.revision, _ownerToken: ownerToken});
      if (!sameLocation()) throw fail('directory-changed');
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
          if (finished) return;
          if (!sameLocation()) { finish(fail('directory-changed')); return; }
          const current = snapshot.entries.find(value => value.id === entry.id);
          if (!current) { finish(fail('directory-changed')); return; }
          if (current.complete && current.revision !== manifest.revision) { finish(fail('mismatch')); return; }
          if (current.complete) {
            const file = await this.getFile({gameId, revision: current.revision});
            if (!sameLocation()) finish(fail('directory-changed')); else if (file) finish(null, file);
          }
          else if (current.status === 'paused') finish(fail('cancelled'));
          else if (['error', 'permission', 'busy'].includes(current.status)) finish(fail(current.error || current.status));
        });
        if (finished) unsubscribe(); if (signal?.aborted) cancel();
      });
    }
  }
  return {LibraryManager, manifestValue, localURL};
});
