/* Explicit local WASM downloads. Transport: upstream WebTorrent 3.0.21.
 * No service worker, public tracker defaults, save namespace, or input changes.
 */
(function (root, factory) {
  'use strict';
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root?.document) {
    root.RetroRomTransfer = api;
    if (root.RetroRomTransferConfig) api.start(root.RetroRomTransferConfig);
  }
})(typeof window === 'object' ? window : null, function (root) {
  'use strict';
  const MiB = 1024 * 1024, HARD_LIMIT = 1024 * MiB, MEMORY_LIMIT = 128 * MiB;
  const WORDS = {
    prepare: ['正在准备本机游戏', 'Preparing local play'],
    manifest: ['正在检查游戏文件…', 'Checking the game file…'],
    download: ['正在下载完整 ROM', 'Downloading the complete ROM'],
    loading: ['下载完成，正在加载模拟器…', 'Download complete. Loading the emulator…'],
    cancel: ['取消下载', 'Cancel download'],
    cancelled: ['已取消下载，尚未启动游戏。', 'Download cancelled. The game has not started.'],
    retry: ['重试下载', 'Retry download'],
    http: ['改用 HTTP 下载', 'Use HTTP download'],
    failed: ['下载未完成', 'Download did not finish'],
    fallback: ['没有其他浏览器时，会从 NAS 的 HTTP 节点下载。', 'Without other browser peers, the NAS HTTP node supplies the download.'],
    hint: ['完整下载后才开始模拟；游戏运行时会占用更多内存。', 'Emulation starts after the full download and uses additional memory.'],
    server: ['NAS HTTP', 'NAS HTTP'],
    peer: ['浏览器互传', 'Browser peers'],
    peers: ['连接设备', 'Peers'],
    upload: ['已分享', 'Shared'],
    stop: ['停止分享', 'Stop sharing'],
    stopped: ['已停止分享，游戏继续运行。', 'Sharing stopped. Your game keeps running.'],
    sharingError: ['分享已中断，游戏继续运行。', 'Sharing stopped after a connection error. Your game keeps running.'],
    invalid: ['游戏下载信息无效，请返回游戏库重试。', 'Invalid download information. Return to the library and try again.'],
    unsupported: ['当前浏览器不支持此下载方式，请选择 HTTP 或云游戏。', 'This browser cannot use this download mode. Choose HTTP or cloud gaming.'],
    threads: ['此模拟器需要 HTTPS、跨源隔离和共享内存，请使用受支持的浏览器或选择云游戏。', 'This emulator needs HTTPS, cross-origin isolation and shared memory. Use a supported browser or cloud gaming.'],
    graphics: ['此模拟器需要 WebGL 2，请使用受支持的浏览器或选择云游戏。', 'This emulator needs WebGL 2. Use a supported browser or cloud gaming.'],
    memory: ['这台设备的内存不足以安全加载此 ROM，请选择云游戏或更小的游戏。', 'This ROM exceeds the memory allowance for this device. Choose cloud gaming or a smaller game.'],
    storage: ['此浏览器无法使用磁盘分片缓存；仅允许不超过 128 MiB 的 ROM，请选择云游戏。', 'Disk-backed piece storage is unavailable. ROMs are limited to 128 MiB; choose cloud gaming for larger files.'],
    quota: ['浏览器可用存储空间不足，请清理空间或选择云游戏。', 'The browser has insufficient free storage. Free some space or choose cloud gaming.'],
    component: ['下载组件未能加载，请重试或改用 HTTP 下载。', 'The download component could not load. Retry or use HTTP download.'],
    mismatch: ['文件大小或内容标识不一致，请刷新游戏库后重试。', 'The file size or content identity changed. Refresh the library and try again.'],
    timeout: ['下载长时间没有进展，请重试或改用 HTTP 下载。', 'The download stalled. Retry or use HTTP download.'],
    network: ['下载连接失败，请检查网络后重试。', 'The download connection failed. Check your network and retry.'],
    emulator: ['模拟器组件未能加载，请返回游戏库重试。', 'The emulator component could not load. Return to the library and retry.'],
  };
  const message = (key, locale) => (WORDS[key] || WORDS.network)[locale === 'en' ? 1 : 0];
  const error = key => Object.assign(new Error(key), {transferCode: key});
  const abortError = () => Object.assign(new Error('Cancelled'), {name: 'AbortError'});
  const bytes = value => `${(Math.max(0, Number(value) || 0) / MiB).toFixed(1)} MiB`;

  function sameOriginURL(value, location, websocket = false) {
    if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\') || /[\u0000-\u001f]/.test(value)) throw error('invalid');
    const base = new URL(location.href), result = new URL(value, base);
    if (!['https:', 'http:'].includes(base.protocol) || result.origin !== base.origin || result.username || result.password || result.hash) throw error('invalid');
    if (websocket) result.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
    return result.href;
  }
  function validateManifest(value, transport, location) {
    if (!value || typeof value !== 'object' || !Number.isSafeInteger(value.size) || value.size < 1 || value.size > HARD_LIMIT ||
        typeof value.name !== 'string' || !/^[a-zA-Z0-9_-]+\.[a-zA-Z0-9]{1,8}$/.test(value.name) ||
        typeof value.revision !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(value.revision)) throw error('invalid');
    const result = {...value, downloadURL: sameOriginURL(value.download_url, location)};
    if (transport === 'p2p') {
      if (typeof value.torrent_base64 !== 'string' || value.torrent_base64.length > 4 * MiB ||
          !/^[a-zA-Z0-9+/]+={0,2}$/.test(value.torrent_base64) || !/^[0-9a-f]{40}$/i.test(value.info_hash) ||
          !Array.isArray(value.ice_servers) || value.ice_servers.length > 8) throw error('invalid');
      result.trackerURL = sameOriginURL(value.tracker_path, location, true);
      result.ice_servers = value.ice_servers.map(item => {
        if (!item || typeof item !== 'object' || Object.keys(item).some(key => !['urls', 'username', 'credential'].includes(key))) throw error('invalid');
        const urls = typeof item.urls === 'string' ? [item.urls] : item.urls;
        if (!Array.isArray(urls) || !urls.length || urls.length > 8 || urls.some(url => typeof url !== 'string' || !/^(stun|stuns|turn|turns):[^\s]+$/.test(url))) throw error('invalid');
        if (['username', 'credential'].some(key => item[key] !== undefined && typeof item[key] !== 'string')) throw error('invalid');
        return {...item, urls: [...urls]};
      });
    }
    return result;
  }
  function supportsDisk(env) {
    return Boolean(env.navigator?.storage?.getDirectory && env.FileSystemFileHandle?.prototype?.createWritable);
  }
  function clientEligibility(manifest, transport, capabilities) {
    if (manifest.requires_threads && (!capabilities.secure || !capabilities.threads)) return 'threads';
    if (manifest.requires_webgl2 && !capabilities.webgl2) return 'graphics';
    if (transport === 'p2p' && (!capabilities.secure || !capabilities.rtc || !capabilities.crypto)) return 'unsupported';
    const footprint = Math.max(manifest.size, Number(manifest.unpacked_bytes) || 0);
    if (Number.isFinite(capabilities.memory) && capabilities.memory > 0 &&
        (capabilities.memory <= 2 && footprint > 64 * MiB || capabilities.memory <= 4 && footprint > 256 * MiB)) return 'memory';
    if (!capabilities.disk && footprint > MEMORY_LIMIT) return 'storage';
    return null;
  }
  function webtorrentOptions(manifest, store) {
    return {
      client: {dht: false, lsd: false, utPex: false, natUpnp: false, natPmp: false, utp: false,
        maxConns: 8, webSeeds: true, seedOutgoingConnections: true,
        tracker: {announce: [], rtcConfig: {iceServers: manifest.ice_servers || []}}},
      torrent: {announce: [manifest.trackerURL], urlList: [manifest.downloadURL], private: true,
        destroyStoreOnDestroy: true, storeCacheSlots: 2, maxWebConns: 2, ...(store ? {store} : {})},
    };
  }
  function createStats() {
    const seen = new WeakSet(), wires = new Set();
    let http = 0, peers = 0, uploaded = 0;
    const count = value => Number.isSafeInteger(value) && value > 0 ? value : 0;
    return {
      attach(wire) {
        if (!wire || seen.has(wire)) return;
        seen.add(wire); wires.add(wire);
        wire.on('download', value => { if (wire.type === 'webSeed') http += count(value); else peers += count(value); });
        wire.on('upload', value => { if (wire.type !== 'webSeed') uploaded += count(value); });
        wire.once('close', () => wires.delete(wire));
      },
      http(value) { http += count(value); },
      snapshot() { return {http_bytes: http, peer_bytes: peers, uploaded_bytes: uploaded,
        peer_count: [...wires].filter(wire => wire.type !== 'webSeed' && !wire.destroyed).length}; },
    };
  }
  function loadScript(env, path, signal, failure = 'component') {
    const source = sameOriginURL(path, env.location);
    return new Promise((resolve, reject) => {
      const script = env.document.createElement('script');
      let settled = false;
      const finish = (problem) => {
        if (settled) return;
        settled = true; env.clearTimeout(timer); signal.removeEventListener('abort', aborted);
        script.onload = script.onerror = null;
        if (problem) { script.remove(); reject(problem); } else resolve();
      };
      const aborted = () => finish(abortError());
      const timer = env.setTimeout(() => finish(error(failure)), 30000);
      script.async = true; script.src = source;
      script.onload = () => finish(); script.onerror = () => finish(error(failure));
      signal.addEventListener('abort', aborted, {once: true});
      if (signal.aborted) { aborted(); return; }
      env.document.body.append(script);
    });
  }

  class DownloadController {
    constructor(config, env) {
      this.env = env; this.config = {...config}; this.locale = config.locale === 'en' ? 'en' : 'zh-CN';
      this.epoch = 0; this.phase = 'idle'; this.client = null; this.torrent = null;
      this.abort = null; this.timer = null; this.stats = createStats(); this.received = 0;
      this.manifest = null; this.ui = null; this.sharing = null; this.failure = null; this.unloaded = false;
      this.onLeave = () => { this.unloaded = true; this.cancel(false); };
      env.addEventListener?.('pagehide', this.onLeave);
      env.addEventListener?.('beforeunload', this.onLeave);
    }
    text(key) { return message(key, this.locale); }
    check(epoch) { if (this.unloaded || epoch !== this.epoch || this.abort?.signal.aborted) throw abortError(); }
    snapshot() { return {phase: this.phase, transport: this.config.transport, size: this.manifest?.size || 0,
      received_bytes: this.received, ...this.stats.snapshot(), sharing: Boolean(this.client && this.phase === 'running')}; }
    render() {
      const doc = this.env.document, game = doc.getElementById('game');
      if (!game) throw error('invalid');
      const panel = doc.createElement('section');
      panel.setAttribute('aria-label', this.text('prepare'));
      panel.style.cssText = 'box-sizing:border-box;max-width:680px;margin:auto;padding:24px;display:flex;flex-direction:column;gap:16px;justify-content:center;height:100%;font:16px/1.6 system-ui;color:#deedf7;background:#08111e';
      const title = doc.createElement('h2'); title.textContent = this.text('prepare'); title.style.cssText = 'font-size:22px;margin:0';
      const detail = doc.createElement('p'); detail.textContent = this.text('manifest'); detail.setAttribute('role', 'status'); detail.style.margin = '0';
      const progress = doc.createElement('progress'); progress.max = 1; progress.value = 0; progress.style.cssText = 'width:100%;height:18px;accent-color:#9bdfff';
      progress.setAttribute('aria-label', this.text('download'));
      const stats = doc.createElement('p'); stats.style.cssText = 'font-size:13px;color:#a8bdd0;margin:0;overflow-wrap:anywhere';
      const hint = doc.createElement('p'); hint.textContent = `${this.text('hint')} ${this.config.transport === 'p2p' ? this.text('fallback') : ''}`; hint.style.cssText = 'font-size:13px;color:#a8bdd0;margin:0';
      const actions = doc.createElement('div'); actions.style.cssText = 'display:flex;flex-wrap:wrap;gap:12px';
      const button = (label, callback) => { const value = doc.createElement('button'); value.type = 'button'; value.textContent = label; value.style.cssText = 'min-height:48px;padding:10px 18px;border:1px solid #426178;border-radius:10px;background:#152b40;color:#deedf7;font:inherit;cursor:pointer'; value.addEventListener('click', callback); actions.append(value); return value; };
      const cancel = button(this.text('cancel'), () => this.cancel());
      const retry = button(this.text('retry'), () => this.start(this.config.transport)); retry.hidden = true;
      const http = button(this.text('http'), () => this.start('http')); http.hidden = true;
      panel.append(title, detail, progress, stats, hint, actions); game.replaceChildren(panel);
      this.ui = {panel, title, detail, progress, stats, cancel, retry, http};
    }
    update() {
      const size = this.manifest?.size || 0, stats = this.stats.snapshot();
      if (this.ui && this.phase !== 'running') {
        this.ui.progress.max = size || 1; this.ui.progress.value = Math.min(size, this.received);
        if (this.phase === 'downloading') this.ui.detail.textContent = `${this.text('download')} · ${bytes(this.received)} / ${bytes(size)} (${size ? Math.floor(this.received / size * 100) : 0}%)`;
        this.ui.stats.textContent = `${this.text('server')} ${bytes(stats.http_bytes)} · ${this.text('peer')} ${bytes(stats.peer_bytes)} · ${this.text('peers')} ${stats.peer_count}`;
      }
      if (this.sharing && this.client) this.sharing.label.textContent = `${this.text('upload')} ${bytes(stats.uploaded_bytes)} · ${this.text('peers')} ${stats.peer_count} `;
    }
    destroyClient() {
      const client = this.client; this.client = null; this.torrent = null;
      if (client && !client.destroyed) { try { client.destroy(() => {}); } catch { /* The client may have failed first. */ } }
    }
    clearTimers() { if (this.timer !== null) this.env.clearInterval(this.timer); this.timer = null; }
    cancel(show = true) {
      this.epoch++; this.abort?.abort(); this.clearTimers(); this.destroyClient();
      if (this.phase === 'running') { this.phase = 'closed'; return; }
      this.phase = 'cancelled';
      if (show && this.ui) {
        this.ui.detail.textContent = this.text('cancelled'); this.ui.cancel.hidden = true;
        this.ui.retry.hidden = false; this.ui.http.hidden = this.config.transport !== 'p2p';
      }
    }
    stopSharing(problem = false) {
      if (this.phase !== 'running') return;
      this.destroyClient(); this.clearTimers();
      if (this.sharing) { this.sharing.label.textContent = this.text(problem ? 'sharingError' : 'stopped'); this.sharing.button.hidden = true; }
    }
    sharingStatus() {
      const doc = this.env.document, status = doc.getElementById('status');
      if (!status || !this.client) return;
      const wrapper = doc.createElement('span'), label = doc.createElement('span'), button = doc.createElement('button');
      wrapper.id = 'rom-sharing'; wrapper.style.cssText = 'display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap';
      button.type = 'button'; button.textContent = this.text('stop'); button.style.minHeight = '44px';
      button.addEventListener('click', () => this.stopSharing()); wrapper.append(label, button);
      // A sibling preserves the game's existing save-status text and controls.
      status.after(wrapper); this.sharing = {wrapper, label, button}; this.update();
    }
    async start(transport = this.config.transport) {
      this.cancel(false); this.sharing?.wrapper.remove(); this.sharing = null;
      if (this.unloaded) return;
      if (!['http', 'p2p'].includes(transport)) { this.phase = 'error'; return; }
      this.config.transport = transport; this.phase = 'preparing'; this.stats = createStats(); this.received = 0; this.manifest = null; this.failure = null;
      this.abort = new this.env.AbortController(); const epoch = this.epoch, signal = this.abort.signal;
      this.render(); let deadline;
      try {
        const manifestURL = new URL(sameOriginURL(this.config.manifestUrl, this.env.location));
        manifestURL.searchParams.set('transport', transport);
        deadline = this.env.setTimeout(() => { if (epoch === this.epoch) { this.failure = 'timeout'; this.abort?.abort(); } }, 120000);
        const response = await this.env.fetch(manifestURL.href, {signal, credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw error('network');
        const manifest = validateManifest(await response.json(), transport, this.env.location);
        this.check(epoch); this.manifest = manifest;
        let webgl2 = true;
        if (manifest.requires_webgl2) {
          const context = this.env.document.createElement('canvas').getContext('webgl2'); webgl2 = Boolean(context);
          context?.getExtension('WEBGL_lose_context')?.loseContext();
        }
        const disk = supportsDisk(this.env);
        const unsupported = clientEligibility(manifest, transport, {disk, memory: this.env.navigator?.deviceMemory,
          secure: this.env.isSecureContext, threads: this.env.crossOriginIsolated && typeof this.env.SharedArrayBuffer === 'function',
          rtc: typeof this.env.RTCPeerConnection === 'function', crypto: Boolean(this.env.crypto?.subtle), webgl2});
        if (unsupported) throw error(unsupported);
        if (disk && this.env.navigator.storage.estimate) {
          const estimate = await this.env.navigator.storage.estimate(); this.check(epoch);
          if (Number.isFinite(estimate.quota) && Number.isFinite(estimate.usage) && estimate.quota - estimate.usage < manifest.size * 1.15) throw error('quota');
        }
        this.env.clearTimeout(deadline); deadline = null;
        this.phase = 'downloading'; this.lastProgress = Date.now(); this.update();
        this.timer = this.env.setInterval(() => {
          if (this.phase === 'downloading' && Date.now() - this.lastProgress > 120000) { this.failure = 'timeout'; this.abort?.abort(); this.destroyClient(); }
          this.update();
        }, 1000);
        const blob = transport === 'p2p' ? await this.downloadTorrent(manifest, epoch, disk) : await this.downloadHTTP(manifest, epoch);
        this.check(epoch);
        if (!blob || blob.size !== manifest.size) throw error('mismatch');
        this.phase = 'loading'; this.ui.detail.textContent = this.text('loading'); this.ui.cancel.hidden = false;
        this.env.EJS_gameUrl = new this.env.File([blob], manifest.name, {type: 'application/octet-stream'});
        this.env.EJS_CacheLimit = 0; // Avoid another whole-ROM cache in EmulatorJS.
        this.check(epoch);
        this.env.document.getElementById('game').replaceChildren();
        await loadScript(this.env, this.config.loaderUrl, signal, 'emulator'); this.check(epoch);
        this.phase = 'running'; this.ui = null;
        if (this.client) this.sharingStatus(); else this.clearTimers();
      } catch (problem) {
        if (epoch !== this.epoch || this.unloaded) return;
        this.clearTimers(); this.destroyClient();
        this.phase = 'error';
        if (!this.ui?.panel.isConnected) this.render();
        this.ui.title.textContent = this.text('failed');
        this.ui.detail.textContent = this.text(this.failure || problem.transferCode || 'network');
        this.ui.cancel.hidden = true; this.ui.retry.hidden = false; this.ui.http.hidden = transport !== 'p2p';
      } finally { if (deadline !== null && deadline !== undefined) this.env.clearTimeout(deadline); }
    }
    async downloadHTTP(manifest, epoch) {
      const response = await this.env.fetch(manifest.downloadURL, {signal: this.abort.signal, credentials: 'same-origin', cache: 'no-store'});
      this.check(epoch);
      if (!response.ok) throw error('network');
      const length = response.headers.get('content-length');
      if (length !== null && Number(length) !== manifest.size) throw error('mismatch');
      const reader = response.body?.getReader();
      if (!reader) {
        const blob = await response.blob(); this.check(epoch);
        if (blob.size !== manifest.size) throw error('mismatch');
        this.received = blob.size; this.stats.http(blob.size); this.update(); return blob;
      }
      const chunks = [];
      try {
        while (true) {
          const {done, value} = await reader.read(); this.check(epoch);
          if (done) break;
          this.received += value.byteLength;
          if (this.received > manifest.size) throw error('mismatch');
          chunks.push(value); this.stats.http(value.byteLength); this.lastProgress = Date.now();
        }
        if (this.received !== manifest.size) throw error('mismatch');
        this.update(); return new this.env.Blob(chunks, {type: 'application/octet-stream'});
      } finally { reader.cancel().catch(() => {}); reader.releaseLock(); chunks.length = 0; }
    }
    async downloadTorrent(manifest, epoch, disk) {
      const signal = this.abort.signal;
      if (!this.env.WebTorrent) await loadScript(this.env, this.config.webtorrentUrl, signal);
      this.check(epoch);
      if (!this.env.RetroRomChunkStore || !this.env.RetroRomMemoryStore) await loadScript(this.env, this.config.chunkStoreUrl, signal);
      this.check(epoch);
      // The upstream bundle may add a browser FSA compatibility shim.
      disk = supportsDisk(this.env);
      if (typeof this.env.WebTorrent !== 'function' || typeof this.env.RetroRomMemoryStore !== 'function' || disk && typeof this.env.RetroRomChunkStore !== 'function') throw error('component');
      let store = this.env.RetroRomMemoryStore;
      if (disk) {
        const Store = this.env.RetroRomChunkStore, name = `retroone-rom-${this.env.crypto.randomUUID()}`;
        // Reuse upstream FSA storage, but isolate each transfer's directory.
        // Do not pass rootDir: upstream's shared /chunks cache is unsuitable
        // for multiple player tabs, and the runtime bundle removes its purge.
        store = function (chunkLength, opts) { const {rootDir, ...owned} = opts; return new Store(chunkLength, {...owned, name}); };
      }
      const opts = webtorrentOptions(manifest, store), client = new this.env.WebTorrent(opts.client);
      this.client = client;
      const decoded = this.env.atob(manifest.torrent_base64), torrentBytes = Uint8Array.from(decoded, character => character.charCodeAt(0));
      return new Promise((resolve, reject) => {
        let settled = false, completing = false;
        const ownsClient = () => epoch === this.epoch && this.client === client && !this.unloaded;
        const finish = (problem, blob) => {
          if (settled) return;
          settled = true; signal.removeEventListener('abort', aborted);
          if (problem) reject(problem); else resolve(blob);
        };
        const aborted = () => finish(abortError());
        const failed = () => {
          if (!ownsClient()) { finish(abortError()); return; }
          if (this.phase === 'running') this.stopSharing(true);
          else if (settled) this.destroyClient();
          else finish(error('network'));
        };
        signal.addEventListener('abort', aborted, {once: true});
        client.on('error', failed);
        let torrent;
        const ready = async value => {
          try {
            this.check(epoch); if (!ownsClient()) throw abortError(); torrent = value;
            if (torrent.infoHash.toLowerCase() !== manifest.info_hash.toLowerCase() || torrent.files.length !== 1 ||
                torrent.files[0].name !== manifest.name || torrent.files[0].length !== manifest.size || torrent.length !== manifest.size) throw error('mismatch');
            for (const wire of torrent.wires || []) this.stats.attach(wire);
            const complete = async () => {
              if (completing || settled) return; completing = true;
              try {
                this.check(epoch); if (!ownsClient()) throw abortError();
                const blob = await torrent.files[0].blob(); this.check(epoch);
                if (!ownsClient()) throw abortError();
                this.received = manifest.size; this.update(); finish(null, blob);
              } catch (problem) { finish(problem); }
            };
            torrent.once('done', complete);
            if (torrent.done) await complete();
          } catch (problem) { finish(problem); }
        };
        try {
          torrent = client.add(torrentBytes, opts.torrent, ready); this.torrent = torrent;
          torrent.on('error', failed);
          torrent.on('wire', wire => { if (ownsClient()) this.stats.attach(wire); });
          torrent.on('download', () => { if (ownsClient()) { this.received = Math.min(manifest.size, torrent.downloaded); this.lastProgress = Date.now(); } });
          // Tracker warnings do not fail a download: HTTP Web Seed is enough.
          torrent.on('warning', () => {});
          if (signal.aborted) aborted();
        } catch (problem) { finish(error('component')); }
      });
    }
  }
  let active;
  return {sameOriginURL, validateManifest, clientEligibility, webtorrentOptions, createStats, DownloadController,
    start(config) { if (!root) throw new Error('Browser required'); active?.cancel(false); active = new DownloadController(config, root); return active.start(); },
    cancel() { active?.cancel(); }, stopSharing() { active?.stopSharing(); }, snapshot() { return active?.snapshot() || {phase: 'idle'}; },
  };
});
