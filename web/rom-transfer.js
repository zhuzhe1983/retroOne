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
  const MiB = 1024 * 1024, MEMORY_LIMIT = 128 * MiB;
  const WORDS = {
    prepare: ['正在准备浏览器游戏', 'Preparing browser play'],
    manifest: ['正在检查游戏文件…', 'Checking the game file…'],
    hashing: ['服务器正在准备文件分片…', 'The server is preparing file pieces…'],
    cached: ['正在读取这台设备上的游戏文件…', 'Reading the game file saved on this device…'],
    persistent: ['当前浏览器无法使用持久游戏缓存。请选择可用的下载目录或云游戏；临时下载不会在下次启动复用。', 'Persistent game storage is unavailable. Choose an accessible download directory or cloud gaming. Temporary downloads are not reused next time.'],
    temporaryLoading: ['当前浏览器无法保留游戏文件，本次临时加载。', 'This browser cannot retain game files. This launch uses a temporary download.'],
    temporary: ['本次使用临时下载，退出后下次需要重新下载。', 'This is a temporary download. The next launch will download the file again.'],
    folder: ['请在“下载与分享”中重新授权缓存目录，文件仍会保留。', 'Grant access to your cache directory in Downloads & sharing. Your files are retained.'],
    busy: ['另一个标签页正在使用此缓存，请在该标签页启动游戏，或暂停分享后重试。', 'Another tab is using this cache. Play there, or pause sharing there and retry.'],
    download: ['正在下载完整 ROM', 'Downloading the complete ROM'],
    loading: ['下载完成，正在加载模拟器…', 'Download complete. Loading the emulator…'],
    cancel: ['取消下载', 'Cancel download'],
    cancelled: ['已取消下载，尚未启动游戏。', 'Download cancelled. The game has not started.'],
    retry: ['重试下载', 'Retry download'],
    http: ['改用 HTTP 下载', 'Use HTTP download'],
    failed: ['下载未完成', 'Download did not finish'],
    fallback: ['没有其他浏览器时，会从 NAS 的 HTTP 节点下载。', 'Without other browser peers, the NAS HTTP node supplies the download.'],
    direct: ['已改为从 NAS 直接下载，仍将在浏览器运行。', 'Downloading directly from the NAS. The game will still run in your browser.'],
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
  function transferError(problem, fallback = 'network') {
    if (problem?.transferCode || problem?.name === 'AbortError') return problem;
    if (['permission', 'ownership', 'directory-changed', 'location-limit'].includes(problem?.code)) return error('folder');
    if (problem?.code === 'busy') return error('busy');
    if (['modified', 'revision-changed', 'mismatch'].includes(problem?.code)) return error('mismatch');
    if (['quota', 'invalid', 'component', 'cancelled'].includes(problem?.code)) return error(problem.code);
    if (problem?.name === 'QuotaExceededError') return error('quota');
    if (problem?.name === 'RangeError' || /out of memory|allocation failed|cannot allocate/i.test(problem?.message || '')) return error('memory');
    if (/failed verification|hash mismatch/i.test(problem?.message || '')) return error('mismatch');
    if (problem?.name === 'SyntaxError' || /invalid torrent|torrent is missing required field|invalid (?:bencode|metadata)/i.test(problem?.message || '')) return error('invalid');
    return error(fallback);
  }
  const bytes = value => `${(Math.max(0, Number(value) || 0) / MiB).toFixed(1)} MiB`;

  function sameOriginURL(value, location, websocket = false) {
    if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || value.includes('\\') || /[\u0000-\u001f]/.test(value)) throw error('invalid');
    const base = new URL(location.href), result = new URL(value, base);
    if (!['https:', 'http:'].includes(base.protocol) || result.origin !== base.origin || result.username || result.password || result.hash) throw error('invalid');
    if (websocket) result.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
    return result.href;
  }
  function validateManifest(value, transport, location) {
    if (!value || typeof value !== 'object' || !Number.isSafeInteger(value.size) || value.size < 1 ||
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
  async function normalizeZipForLaunch(file, normalizedName, env, signal) {
    if (!normalizedName) return file;
    if (typeof normalizedName !== 'string' || normalizedName.length > 200 || !/^[a-zA-Z0-9_-]+\.[a-zA-Z0-9]{1,8}$/.test(normalizedName) || !/\.zip$/i.test(file?.name || '')) throw error('invalid');
    const check = () => { if (signal?.aborted) throw abortError(); };
    const read = async (offset, length) => {
      check();
      if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || length > MiB || offset + length > file.size) throw error('invalid');
      const bytes = new Uint8Array(await file.slice(offset, offset + length).arrayBuffer()); check();
      if (bytes.length !== length) throw error('mismatch'); return bytes;
    };
    const view = bytes => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const tailOffset = Math.max(0, file.size - 65557), tail = await read(tailOffset, file.size - tailOffset), end = view(tail);
    let endOffset = -1;
    for (let index = tail.length - 22; index >= 0; index--) {
      if (end.getUint32(index, true) === 0x06054b50 && index + 22 + end.getUint16(index + 20, true) === tail.length) { endOffset = index; break; }
    }
    if (endOffset < 0) throw error('invalid');
    const count = end.getUint16(endOffset + 10, true), directorySize = end.getUint32(endOffset + 12, true), directoryOffset = end.getUint32(endOffset + 16, true);
    if (end.getUint16(endOffset + 4, true) || end.getUint16(endOffset + 6, true) || count !== end.getUint16(endOffset + 8, true) || !count || count > 4096 || directorySize > MiB || directoryOffset + directorySize !== tailOffset + endOffset) throw error('invalid');
    const directory = await read(directoryOffset, directorySize), records = view(directory), extension = normalizedName.slice(normalizedName.lastIndexOf('.')).toLowerCase();
    let position = 0, selected = null;
    for (let index = 0; index < count; index++) {
      if (position + 46 > directory.length || records.getUint32(position, true) !== 0x02014b50) throw error('invalid');
      const nameSize = records.getUint16(position + 28, true), extraSize = records.getUint16(position + 30, true), commentSize = records.getUint16(position + 32, true), next = position + 46 + nameSize + extraSize + commentSize;
      if (!nameSize || next > directory.length || records.getUint16(position + 34, true)) throw error('invalid');
      const name = directory.subarray(position + 46, position + 46 + nameSize);
      const suffix = String.fromCharCode(...name.subarray(Math.max(0, name.length - extension.length))).toLowerCase();
      if (suffix === extension) {
        if (selected) throw error('invalid');
        selected = {header: directory.slice(position, position + 46), name: name.slice()};
      }
      position = next;
    }
    if (position !== directory.length || !selected) throw error('invalid');
    const record = view(selected.header), flags = record.getUint16(8, true), method = record.getUint16(10, true), crc = record.getUint32(16, true), packedSize = record.getUint32(20, true), size = record.getUint32(24, true), localOffset = record.getUint32(42, true), mode = record.getUint32(38, true) >>> 16;
    // This is a launch-only filename view for a server-confirmed cartridge.
    // ZIP64, encryption and non-regular entries are not silently transformed.
    if (flags & ~0x080e || ![0, 8].includes(method) || !size || [packedSize, size, localOffset].includes(0xffffffff) || mode & 0xf000 && (mode & 0xf000) !== 0x8000 || method === 0 && packedSize !== size || localOffset + 30 > directoryOffset) throw error('invalid');
    const localBytes = await read(localOffset, 30), local = view(localBytes), localNameSize = local.getUint16(26, true), dataOffset = localOffset + 30 + localNameSize + local.getUint16(28, true);
    if (local.getUint32(0, true) !== 0x04034b50 || local.getUint16(6, true) !== flags || local.getUint16(8, true) !== method || localNameSize !== selected.name.length || dataOffset + packedSize > directoryOffset) throw error('invalid');
    const localName = await read(localOffset + 30, localNameSize);
    if (localName.some((byte, index) => byte !== selected.name[index]) || !(flags & 8) && (local.getUint32(14, true) !== crc || local.getUint32(18, true) !== packedSize || local.getUint32(22, true) !== size)) throw error('invalid');
    const asciiName = Uint8Array.from(normalizedName, character => character.charCodeAt(0)), normalizedFlags = (flags & 6) | 0x800;
    local.setUint16(4, 20, true); local.setUint16(6, normalizedFlags, true); local.setUint32(14, crc, true); local.setUint32(18, packedSize, true); local.setUint32(22, size, true); local.setUint16(26, asciiName.length, true); local.setUint16(28, 0, true);
    record.setUint16(4, 20, true); record.setUint16(6, 20, true); record.setUint16(8, normalizedFlags, true); record.setUint16(28, asciiName.length, true); record.setUint16(30, 0, true); record.setUint16(32, 0, true); record.setUint16(34, 0, true); record.setUint32(42, 0, true);
    const newDirectoryOffset = localBytes.length + asciiName.length + packedSize;
    if (newDirectoryOffset >= 0xffffffff) throw error('invalid');
    const endBytes = new Uint8Array(22), newEnd = view(endBytes);
    newEnd.setUint32(0, 0x06054b50, true); newEnd.setUint16(8, 1, true); newEnd.setUint16(10, 1, true); newEnd.setUint32(12, selected.header.length + asciiName.length, true); newEnd.setUint32(16, newDirectoryOffset, true);
    check();
    // Only bounded ZIP metadata is read. The verified compressed bytes stay
    // backed by the original File; no second ROM is written to the device.
    return new env.File([localBytes, asciiName, file.slice(dataOffset, dataOffset + packedSize), selected.header, asciiName, endBytes], file.name, {type: file.type || 'application/zip', lastModified: file.lastModified});
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
      received_bytes: this.received, error: this.failure, temporary: this.config.temporary === true || this.temporaryFallback === true, ...this.stats.snapshot(), sharing: Boolean(this.client && this.phase === 'running')}; }
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
      this.ui = {panel, title, detail, progress, stats, hint, cancel, retry, http};
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
    reportFailure(code) {
      this.failure = code;
      const detail = {type: 'retroone:rom-transfer-error', code, gameId: this.config.gameId || null, profileId: this.config.profileId || null};
      try { if (this.env.parent && this.env.parent !== this.env) this.env.parent.postMessage(detail, new URL(this.env.location.href).origin); } catch {}
    }
    async diskLibrary() {
      // A one-off transfer is opt-in only. Missing permission or storage must
      // never silently turn a persistent download into another full download.
      if (this.config.temporary === true) return null;
      let library;
      try { library = this.env.parent?.RetroRomLibrary; } catch { /* Never use cross-origin storage. */ }
      library ||= this.env.RetroRomLibrary;
      if (!library || !this.config.gameId) throw error('persistent');
      try { await library.init(); } catch (problem) { throw transferError(problem, 'persistent'); }
      const directory = library.snapshot().directory;
      if (directory.permission === 'granted') return library;
      // Preserve the small-ROM automatic path only when the manager has proved
      // capability absence without a saved external location. Quota, denied
      // access and uncertain metadata errors are never temporary fallbacks.
      if (this.config.cachedOnly && directory.temporary_fallback === true && directory.error === 'unsupported' && !directory.storage_kind) {
        this.temporaryFallback = true; return null;
      }
      if (directory.error === 'quota') throw error('quota');
      if (['prompt', 'denied'].includes(directory.permission) || ['permission', 'ownership', 'directory-changed'].includes(directory.error)) throw error('folder');
      throw error('persistent');
    }
    async fetchManifest(url, transport, epoch) {
      const address = new URL(sameOriginURL(url, this.env.location));
      address.searchParams.set('transport', transport);
      const signal = this.abort.signal;
      while (true) {
        const deadline = this.env.setTimeout(() => { if (epoch === this.epoch) { this.failure = 'timeout'; this.abort.abort(); } }, 45000);
        let response;
        try {
          response = await this.env.fetch(address.href, {signal, credentials: 'same-origin', cache: 'no-store'}).catch(problem => {
            if (problem.name === 'AbortError') throw problem;
            throw error('network');
          });
          this.check(epoch);
          if (response.status !== 202) {
            if (!response.ok) throw error('network');
            return validateManifest(await response.json(), transport, this.env.location);
          }
        } finally { this.env.clearTimeout(deadline); }
        if (this.ui) this.ui.detail.textContent = this.text('hashing');
        const delay = Math.max(1, Math.min(30, Number(response.headers.get('retry-after')) || 2)) * 1000;
        await new Promise((resolve, reject) => {
          const finish = problem => { this.env.clearTimeout(timer); signal.removeEventListener('abort', cancel); problem ? reject(problem) : resolve(); };
          const cancel = () => finish(abortError()), timer = this.env.setTimeout(() => finish(), delay);
          signal.addEventListener('abort', cancel, {once: true}); if (signal.aborted) cancel();
        });
        this.check(epoch);
      }
    }
    async diskFile(library, manifest, epoch) {
      this.ui.detail.textContent = this.text('cached');
      const unsubscribe = library.subscribe?.(snapshot => {
        if (epoch !== this.epoch || this.unloaded) return;
        const entry = snapshot.entries.find(entry => entry.gameId === this.config.gameId);
        if (!entry) return;
        this.ui.progress.max = entry.size || manifest.size; this.ui.progress.value = entry.received || 0;
        this.ui.detail.textContent = entry.status === 'queued' ? this.text('hashing') : entry.status === 'downloading' ?
          `${this.text('download')} · ${bytes(entry.received)} / ${bytes(entry.size)}` : this.text('cached');
        this.ui.stats.textContent = `${this.text('server')} ${bytes(entry.http_bytes)} · ${this.text('peer')} ${bytes(entry.peer_bytes)} · ${this.text('peers')} ${entry.peers || 0}`;
      });
      try {
        const file = await library.ensureFile(manifest, {gameId: this.config.gameId, title: this.config.title,
          profileId: this.config.profileId, manifestUrl: this.config.downloadManifestUrl,
          allowP2P: this.config.transport === 'p2p', signal: this.abort.signal});
        this.check(epoch);
        if (!file || file.size !== manifest.size || file.name !== manifest.name) throw error('mismatch');
        // EmulatorJS checks `instanceof File/Blob` in its iframe. A parent File
        // needs a local wrapper; structured clone retains its backing bytes
        // without reading the ROM into a JavaScript ArrayBuffer.
        if (file instanceof this.env.File) return file;
        return typeof this.env.structuredClone === 'function' ? this.env.structuredClone(file) :
          new this.env.File([file], file.name, {type: file.type, lastModified: file.lastModified});
      } finally { unsubscribe?.(); }
    }
    temporaryNotice() {
      const doc = this.env.document, note = doc.createElement('span');
      doc.getElementById('rom-temporary-notice')?.remove();
      note.id = 'rom-temporary-notice'; note.setAttribute('role', 'status'); note.textContent = this.text('temporaryLoading');
      note.style.cssText = 'display:inline-block;padding:4px 8px;color:#f7d18a;font:13px/1.5 system-ui';
      const status = doc.getElementById('status'); if (status) status.after(note); else doc.body.append(note);
    }
    async startCachedOrDefault() {
      this.cancel(false); if (this.unloaded) return;
      this.abort = new this.env.AbortController(); const epoch = this.epoch;
      this.phase = 'preparing'; this.failure = null; this.temporaryFallback = false;
      this.env.document.getElementById('rom-temporary-notice')?.remove();
      try {
        const library = await this.diskLibrary(); this.check(epoch);
        // Automatic browser launches must populate the same device library as
        // explicit downloads. Checking for an existing entry here skipped the
        // first launch entirely and left EmulatorJS downloading a player-scoped
        // URL again after each profile switch.
        if (library) {
          this.render();
          const manifest = await this.fetchManifest(this.config.downloadManifestUrl, 'http', epoch);
          this.manifest = manifest;
          const file = await this.diskFile(library, manifest, epoch);
          this.env.EJS_gameUrl = await normalizeZipForLaunch(file, this.config.normalizedRomName, this.env, this.abort.signal); this.check(epoch);
          this.env.EJS_CacheLimit = 0; this.env.EJS_cacheConfig = {enabled: false};
          this.env.document.getElementById('game').replaceChildren();
        }
        if (!library && (this.temporaryFallback || this.config.temporary === true)) this.temporaryNotice();
        this.phase = 'loading';
        await loadScript(this.env, this.config.loaderUrl, this.abort.signal, 'emulator');
        this.check(epoch); this.phase = 'running';
      } catch (problem) {
        if (epoch !== this.epoch || this.unloaded) return;
        this.phase = 'error'; if (!this.ui?.panel.isConnected) this.render();
        this.reportFailure(this.failure || transferError(problem, 'invalid').transferCode || 'network');
        this.ui.title.textContent = this.text('failed'); this.ui.detail.textContent = this.text(this.failure);
        this.ui.cancel.hidden = true; this.ui.retry.hidden = false;
      }
    }
    async start(transport = this.config.transport, fallbackFrom = null) {
      if (this.config.cachedOnly) return this.startCachedOrDefault();
      this.cancel(false); this.sharing?.wrapper.remove(); this.sharing = null;
      if (this.unloaded) return;
      if (!['http', 'p2p'].includes(transport)) { this.phase = 'error'; return; }
      this.config.transport = transport; this.phase = 'preparing'; this.stats = createStats(); this.received = 0; this.manifest = null; this.failure = null;
      this.abort = new this.env.AbortController(); const epoch = this.epoch, signal = this.abort.signal;
      this.render(); let library;
      if (this.config.temporary === true) this.ui.hint.textContent = `${this.text('temporary')} ${this.text('hint')}`;
      if (fallbackFrom) this.ui.hint.textContent = `${this.text('direct')} ${this.text('hint')}`;
      try {
        library = await this.diskLibrary(); this.check(epoch);
        // Persistent downloads fetch their own verified distribution manifest.
        // Avoid waiting for the browser P2P manifest to hash the same file first.
        const manifest = await this.fetchManifest(this.config.manifestUrl, library ? 'http' : transport, epoch);
        this.check(epoch); this.manifest = manifest;
        let webgl2 = true;
        if (manifest.requires_webgl2) {
          const context = this.env.document.createElement('canvas').getContext('webgl2'); webgl2 = Boolean(context);
          context?.getExtension('WEBGL_lose_context')?.loseContext();
        }
        const disk = Boolean(library) || supportsDisk(this.env);
        const unsupported = clientEligibility(manifest, library ? 'http' : transport, {disk, memory: this.env.navigator?.deviceMemory,
          secure: this.env.isSecureContext, threads: this.env.crossOriginIsolated && typeof this.env.SharedArrayBuffer === 'function',
          rtc: typeof this.env.RTCPeerConnection === 'function', crypto: Boolean(this.env.crypto?.subtle), webgl2});
        if (unsupported) throw error(unsupported);
        if (!library && disk && this.env.navigator.storage.estimate) {
          const estimate = await this.env.navigator.storage.estimate(); this.check(epoch);
          if (Number.isFinite(estimate.quota) && Number.isFinite(estimate.usage) && estimate.quota - estimate.usage < manifest.size * 1.15) throw error('quota');
        }
        this.phase = 'downloading'; this.lastProgress = Date.now(); this.update();
        if (!library) this.timer = this.env.setInterval(() => {
          if (this.phase === 'downloading' && Date.now() - this.lastProgress > 120000) { this.failure = 'timeout'; this.abort?.abort(); this.destroyClient(); }
          this.update();
        }, 1000);
        const blob = library ? await this.diskFile(library, manifest, epoch) : transport === 'p2p' ? await this.downloadTorrent(manifest, epoch, disk) : await this.downloadHTTP(manifest, epoch);
        this.check(epoch);
        if (!blob || blob.size !== manifest.size) throw error('mismatch');
        this.phase = 'loading'; this.ui.detail.textContent = this.text('loading'); this.ui.cancel.hidden = false;
        const file = library ? blob : new this.env.File([blob], manifest.name, {type: 'application/octet-stream'});
        this.env.EJS_gameUrl = await normalizeZipForLaunch(file, this.config.normalizedRomName, this.env, signal);
        this.env.EJS_CacheLimit = 0; this.env.EJS_cacheConfig = {enabled: false}; // Avoid another whole-ROM cache in EmulatorJS.
        this.check(epoch);
        this.env.document.getElementById('game').replaceChildren();
        await loadScript(this.env, this.config.loaderUrl, signal, 'emulator'); this.check(epoch);
        this.phase = 'running'; this.ui = null;
        if (this.client) this.sharingStatus(); else this.clearTimers();
      } catch (problem) {
        if (epoch !== this.epoch || this.unloaded) return;
        const code = this.failure || transferError(problem, 'invalid').transferCode;
        // Both transports serve the same browser game. A failed peer client
        // must not force a new launch choice, but integrity/resource failures
        // and explicit cancellation must never be bypassed by an HTTP retry.
        const retryDirect = !library && transport === 'p2p' && this.phase !== 'loading' &&
          ['unsupported', 'component', 'network', 'timeout'].includes(code);
        this.clearTimers(); this.destroyClient();
        if (retryDirect) {
          return this.start('http', code);
        }
        this.phase = 'error'; this.reportFailure(code || 'network');
        if (!this.ui?.panel.isConnected) this.render();
        this.ui.title.textContent = this.text('failed');
        this.ui.detail.textContent = this.text(code || 'network');
        this.ui.cancel.hidden = true; this.ui.retry.hidden = false; this.ui.http.hidden = true;
      }
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
        const failed = problem => {
          if (!ownsClient()) { finish(abortError()); return; }
          if (this.phase === 'running') this.stopSharing(true);
          else if (settled) this.destroyClient();
          else finish(transferError(problem));
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
          // Tracker warnings leave the HTTP Web Seed usable. A rejected piece
          // must not later trigger a direct download that bypasses its hash.
          torrent.on('warning', problem => {
            if (transferError(problem).transferCode === 'mismatch') failed(error('mismatch'));
          });
          if (signal.aborted) aborted();
        } catch (problem) { finish(transferError(problem, 'component')); }
      });
    }
  }
  let active;
  return {sameOriginURL, validateManifest, clientEligibility, webtorrentOptions, createStats, normalizeZipForLaunch, DownloadController,
    start(config) { if (!root) throw new Error('Browser required'); active?.cancel(false); active = new DownloadController(config, root); return active.start(); },
    cancel() { active?.cancel(); }, stopSharing() { active?.stopSharing(); }, snapshot() { return active?.snapshot() || {phase: 'idle'}; },
  };
});
