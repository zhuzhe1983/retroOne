/* PSP in-game saves only. No ROM, firmware, savestate or transport dependency. */
(function (global) {
    'use strict';
    // RetroArch sorts saves by the PPSSPP core name. PPSSPP then appends PSP/SAVEDATA.
    const ROOT = '/data/saves/PPSSPP/PSP/SAVEDATA', LEGACY_ROOT = '/data/saves/PSP/SAVEDATA';
    const META = '/data/saves/.retroone-cloud.json';
    const MAX = 32 * 1024 * 1024, MAX_FILES = 256;
    const cfg = global.RetroCloudSaveConfig;
    const english = cfg && cfg.locale === 'en';
    const words = english ? {
        starting: 'Checking cloud saves…', syncing: 'Syncing in-game saves…', synced: 'In-game saves synced',
        offline: 'Save sync failed. Your local progress is retained; retry before switching devices.',
        busy: 'This game is running elsewhere. Close it there, then retry.',
        conflict: 'Local and cloud progress differ. Neither has been overwritten. Export your local backup before choosing cloud progress.',
        localConflict: 'Two browser save folders contain different progress. Both are retained. Export the backup of both folders before choosing cloud progress.',
        legacy: 'Older browser PSP saves are shared between games. They cannot be assigned safely. Keep the old database and use this game’s cloud saves?',
        retry: 'Retry', keep: 'Keep old database; use cloud saves', export: 'Export local backup', useCloud: 'Restart with cloud version (keep local backup)',
        stopped: 'Cloud save sync stopped. Local progress is retained.', unsupported: 'This emulator does not expose a supported save filesystem.'
    } : {
        starting: '正在检查云存档…', syncing: '正在同步游戏内存档…', synced: '游戏内存档已同步',
        offline: '存档同步失败，本地进度已保留；切换设备前请重试。',
        busy: '这个游戏正在别处运行，请先在那里退出，再重试。',
        conflict: '本地与云端进度不同，两份都未覆盖。请先导出本地备份，再选择是否使用云端进度。',
        localConflict: '浏览器内两份存档目录的进度不同，均已保留。请先导出包含两份进度的备份，再选择是否使用云端进度。',
        legacy: '发现旧版浏览器 PSP 存档，它们由多个游戏共用，无法安全确定归属。是否保留旧数据库，并为当前游戏启用独立云存档？',
        retry: '重试', keep: '保留旧数据库，使用云存档', export: '导出本地备份', useCloud: '用云端进度重新启动（保留本地备份）',
        stopped: '同步已停止，本地进度已保留。', unsupported: '当前模拟器没有可用的存档文件系统。'
    };
    let fs, manager, lease, remote, meta = {}, state = 'starting', panel, message, actions;
    let timer, heartbeatTimer, busy = false, started = false, released = false, observed, observedAt = 0, pendingDecision;
    let client = global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() : null;
    let unlockBrowser, cancelled = false, exiting = false, exitPromise, acknowledged, completedExit;
    let mountIdentity, saveSpaceReady = false, pageUnloading = false, dispatchingExit = false, preparation;
    const requests = new Map();
    const cancellation = () => Object.assign(Error('Cloud save startup cancelled'), {code: 'startup_cancelled'});
    function checkCancelled() { if (cancelled) throw cancellation(); }
    function stopTimers() { clearInterval(timer); clearInterval(heartbeatTimer); }
    function unlock() { if (unlockBrowser) { unlockBrowser(); unlockBrowser = null; } }
    function startHeartbeat() {
        clearInterval(heartbeatTimer);
        heartbeatTimer = setInterval(async () => {
            if (cancelled || exiting || released) return;
            try { await request('/heartbeat', 'POST', {lease_id: lease}); } catch (error) { failure(error); }
        }, 20000);
    }
    if (cfg && client && global.navigator && navigator.locks) {
        const key = 'retroone-cloud-save-tab:' + cfg.profileId + ':' + cfg.gameId;
        try { client = sessionStorage.getItem(key) || client; sessionStorage.setItem(key, client); } catch (_) {}
    }
    const textEncoder = new TextEncoder();
    function show(next, detail) {
        state = next;
        if (cfg && global.document) {
            if (!panel) {
                panel = document.createElement('aside'); panel.setAttribute('aria-live', 'polite');
                panel.style.cssText = 'background:#12253b;color:#e3efff;padding:8px 12px;font:13px system-ui;z-index:9999;flex-shrink:0';
                message = document.createElement('span'); actions = document.createElement('span');
                actions.style.cssText = 'display:inline-flex;gap:8px;margin-left:10px;flex-wrap:wrap';
                panel.append(message, actions); document.body.prepend(panel);
            }
            panel.hidden = next === 'synced';
            message.textContent = detail || words[next] || next;
            actions.replaceChildren();
            if (['offline', 'busy', 'conflict'].includes(next)) button(words.retry, retry);
            if (next === 'legacy') button(words.keep, () => { meta.legacyAccepted = true; persistMeta().then(() => decide()); });
            if (next === 'conflict') {
                button(words.export, exportLocal);
                button(words.useCloud, useCloudVersion);
            }
            if (global.parent !== global) global.parent.postMessage({type: 'retroone:cloud-save', gameId: cfg.gameId,
                profileId: cfg.profileId, status: next, message: message.textContent}, global.location.origin);
        }
    }
    function button(label, action) {
        const element = document.createElement('button'); element.textContent = label;
        element.onclick = () => Promise.resolve().then(action).catch(() => show('offline'));
        actions.append(element);
    }
    function decide() { if (pendingDecision) { const resolve = pendingDecision; pendingDecision = null; resolve(); } }
    function waitDecision() {
        checkCancelled();
        return new Promise((resolve, reject) => { pendingDecision = resolve; pendingDecision.cancel = () => reject(cancellation()); });
    }
    function exists(FS, path) { try { return FS.analyzePath(path).exists; } catch (_) { return false; } }
    function mkdir(FS, path) {
        let current = '';
        for (const piece of path.split('/').filter(Boolean)) { current += '/' + piece; if (!exists(FS, current)) FS.mkdir(current); }
    }
    function toBase64(data) {
        let result = '';
        for (let i = 0; i < data.length; i += 32768) result += String.fromCharCode(...data.subarray(i, i + 32768));
        return btoa(result);
    }
    function fromBase64(data) { const decoded = atob(data); return Uint8Array.from(decoded, character => character.charCodeAt(0)); }
    function assertSaveSpace() {
        if (!fs) throw Error('Save filesystem is unavailable');
        if (mountIdentity && fs.lookupPath('/data/saves').node.mount !== mountIdentity) throw Error('Save filesystem was unmounted');
        if (saveSpaceReady && !exists(fs, META)) throw Error('Save sync metadata disappeared; refusing an empty upload');
    }
    function snapshot(FS, root = ROOT) {
        if (FS === fs && saveSpaceReady) assertSaveSpace();
        const files = []; let total = 0;
        function walk(path, prefix) {
            for (const name of FS.readdir(path).filter(name => name !== '.' && name !== '..').sort()) {
                if (!/^[A-Za-z0-9_][A-Za-z0-9_. -]{0,95}$/.test(name)) throw Error('unsupported save path');
                const full = path + '/' + name, relative = prefix + name, info = FS.lstat(full);
                if (relative.split('/').length > 4) throw Error('save directory depth');
                if (FS.isDir(info.mode)) walk(full, relative + '/');
                else if (FS.isFile(info.mode)) {
                    if (files.length >= MAX_FILES || info.size + total > MAX || relative.split('/').length < 2) throw Error('save limit');
                    const data = FS.readFile(full); total += data.length;
                    files.push({path: relative, data: toBase64(data)});
                } else throw Error('save links are unsupported');
            }
        }
        if (exists(FS, root)) walk(root, '');
        return files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
    }
    async function digest(files) {
        const bytes = await crypto.subtle.digest('SHA-256', textEncoder.encode(JSON.stringify(files)));
        return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, '0')).join('');
    }
    function syncfs(FS) { return new Promise((resolve, reject) => FS.syncfs(false, error => error ? reject(error) : resolve())); }
    async function persistMeta() { assertSaveSpace(); fs.writeFile(META, JSON.stringify(meta)); await syncfs(fs); saveSpaceReady = true; assertSaveSpace(); }
    async function request(path, method, body, timeoutMs = 20000) {
        if (path !== '/release') checkCancelled();
        const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), timeoutMs);
        requests.set(controller, path);
        try {
            const response = await fetch(cfg.url + path, {method, credentials: 'same-origin', cache: 'no-store',
                headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal, keepalive: path === '/release'});
            const data = await response.json();
            if (!response.ok) { const error = Error(data.detail?.message || 'cloud save error'); error.code = data.detail?.code; throw error; }
            if (cancelled && path !== '/release') {
                // Fetch can resolve despite cancellation; never let a late lease
                // restart the core or overwrite the preserved local filesystem.
                if (path === '/lease' && data.lease_id) request('/release', 'POST', {lease_id: data.lease_id}, 2000).catch(() => {});
                throw cancellation();
            }
            return data;
        } finally { clearTimeout(timeout); requests.delete(controller); }
    }
    function failure(error) {
        if (cancelled) return;
        const code = error && error.code;
        show(['revision_conflict', 'save_root_conflict'].includes(code) ? 'conflict' : ['native_active', 'lease_busy', 'browser_active'].includes(code) ? 'busy' : 'offline',
            code === 'save_root_conflict' ? words.localConflict : undefined);
    }
    async function legacyExists() {
        if (meta.legacyAccepted) return false;
        if (!indexedDB.databases) return true; // Require an explicit choice when old databases cannot be enumerated.
        const databases = await indexedDB.databases();
        if (!databases.some(item => item.name === cfg.legacyDatabase)) return false;
        return new Promise((resolve, reject) => {
            // Bypass the iframe's intentional current-game IDB name wrapper.
            const opening = IDBFactory.prototype.open.call(indexedDB, cfg.legacyDatabase);
            opening.onerror = () => reject(opening.error);
            opening.onsuccess = () => {
                const db = opening.result;
                if (!db.objectStoreNames.contains('FILE_DATA')) { db.close(); resolve(false); return; }
                const tx = db.transaction('FILE_DATA', 'readonly'); let found = false;
                const cursor = tx.objectStore('FILE_DATA').openKeyCursor();
                cursor.onsuccess = () => { const value = cursor.result;
                    if (!value) return;
                    if ([ROOT, LEGACY_ROOT].some(root => String(value.key).startsWith(root + '/'))) found = true;
                    else value.continue();
                };
                tx.oncomplete = () => { db.close(); resolve(found); };
                tx.onerror = () => { db.close(); reject(tx.error); };
            };
        });
    }
    function replaceFiles(FS, files) {
        // Local data is already backed up or proven clean. Only this game's SAVEDATA is touched.
        function clear(path) { for (const name of FS.readdir(path).filter(n => n !== '.' && n !== '..')) {
            const child = path + '/' + name, info = FS.lstat(child);
            if (FS.isDir(info.mode)) { clear(child); FS.rmdir(child); } else FS.unlink(child);
        } }
        mkdir(FS, ROOT); clear(ROOT);
        for (const item of files) { const path = ROOT + '/' + item.path; mkdir(FS, path.substring(0, path.lastIndexOf('/'))); FS.writeFile(path, fromBase64(item.data)); }
    }
    async function migrateSaveRoot() {
        if (meta.saveRoot === ROOT) return;
        const previous = snapshot(fs, LEGACY_ROOT), current = snapshot(fs);
        if (previous.length && current.length && JSON.stringify(previous) !== JSON.stringify(current) && !meta.allowCloud) {
            throw Object.assign(Error('Both local save directories contain different progress'), {code: 'save_root_conflict'});
        }
        // Only this already-isolated per-game IDBFS is examined. Keep the old
        // tree intact and preserve the revision/dirty baseline for the CAS check.
        // Never resurrect the old copy after a subsequent legitimate deletion.
        if (previous.length && !current.length && !meta.allowCloud) replaceFiles(fs, previous);
        meta.saveRoot = ROOT;
        if (previous.length || current.length || exists(fs, META)) await persistMeta();
    }
    function configureSaveDirectory(instance) {
        const FS = instance.FS, path = '/home/web_user/.config/retroarch/retroarch.cfg';
        const managed = {
            savefile_directory: '"/data/saves"', sort_savefiles_enable: 'true',
            sort_savefiles_by_content_enable: 'false', savefiles_in_content_dir: 'false'
        };
        // Both supported GameManager versions create this before the awaited
        // mount/initialization hook. Pin the actual core path, not a guessed tree.
        const config = FS.readFile(path, {encoding: 'utf8'});
        const lines = config.split(/\r?\n/).filter(line => !Object.keys(managed).some(key => new RegExp('^\\s*' + key + '\\s*=').test(line)));
        FS.writeFile(path, lines.join('\n').replace(/\n*$/, '\n') + Object.entries(managed).map(([key, value]) => key + ' = ' + value).join('\n') + '\n');
    }
    async function connect() {
        checkCancelled();
        remote = await request('/lease', 'POST', {client_id: client}); lease = remote.lease_id; released = false;
        startHeartbeat();
        const local = snapshot(fs), localDigest = await digest(local);
        checkCancelled();
        if (localDigest === remote.digest) {
            meta.baseRevision = remote.revision; meta.baseDigest = remote.digest; meta.dirty = false;
        } else if (meta.allowCloud || (((!local.length && meta.baseRevision === undefined && !meta.baseDigest) || meta.baseDigest === localDigest) && !meta.dirty)) {
            replaceFiles(fs, remote.files); meta.baseRevision = remote.revision; meta.baseDigest = remote.digest; meta.dirty = false; delete meta.allowCloud;
        } else if (meta.baseRevision === remote.revision && meta.baseDigest === remote.digest) {
            meta.dirty = true; await persistMeta(); await upload(local, localDigest);
        } else {
            show('conflict'); await waitDecision(); return connect();
        }
        if (meta.baseDigest === remote.digest) acknowledged = JSON.stringify(remote.files);
        delete meta.allowCloud;
        await persistMeta(); checkCancelled(); show('synced');
    }
    async function upload(files, hash) {
        checkCancelled();
        if (!lease || released) throw Error('No active lease');
        meta.dirty = true; await persistMeta(); checkCancelled(); show('syncing');
        const result = await request('', 'PUT', {lease_id: lease, base_revision: meta.baseRevision, files});
        checkCancelled();
        acknowledged = JSON.stringify(files);
        meta.baseRevision = result.revision; meta.baseDigest = result.digest;
        // New writes while a request was in flight remain dirty for the next flush.
        meta.dirty = await digest(snapshot(fs)) !== hash;
        await persistMeta(); checkCancelled(); show(meta.dirty ? 'syncing' : 'synced');
    }
    function openWriter() {
        return (fs.streams || []).some(stream => stream && stream.path && stream.path.startsWith(ROOT + '/') && (stream.flags & 3) !== 0);
    }
    async function flush() {
        if (!fs || !lease || released) return {synced: !fs, reason: state};
        if (busy) return {synced: false, reason: 'syncing'};
        busy = true;
        try {
            if (openWriter()) return {synced: false, reason: 'save_in_progress'};
            const files = snapshot(fs), hash = await digest(files);
            if (hash !== meta.baseDigest) await upload(files, hash);
            else if (meta.dirty) { meta.dirty = false; await persistMeta(); }
            return {synced: !meta.dirty};
        } catch (error) { failure(error); return {synced: false, reason: state}; }
        finally { busy = false; }
    }
    async function poll() {
        if (cancelled || exiting || busy || released || !started || ['conflict', 'busy'].includes(state)) return;
        try {
            const files = snapshot(fs), hash = await digest(files);
            if (hash === meta.baseDigest) return;
            if (!meta.dirty) { meta.dirty = true; await persistMeta(); }
            if (hash !== observed) { observed = hash; observedAt = Date.now(); return; }
            if (Date.now() - observedAt >= 2000 && !openWriter()) await flush();
        } catch (error) { failure(error); }
    }
    async function retry() {
        if (cancelled || exiting) return;
        if (pendingDecision) { decide(); return; }
        if (!fs || busy) return;
        busy = true;
        try {
            if (started) {
                remote = await request('/lease', 'POST', {client_id: client}); lease = remote.lease_id; released = false; startHeartbeat();
                const local = snapshot(fs), localDigest = await digest(local);
                checkCancelled();
                if (localDigest === remote.digest) {
                    acknowledged = JSON.stringify(local);
                    meta.baseRevision = remote.revision; meta.baseDigest = remote.digest; meta.dirty = false; await persistMeta(); show('synced');
                } else if (meta.baseRevision === remote.revision && meta.baseDigest === remote.digest) await upload(local, localDigest);
                else show('conflict');
            } else await connect();
            clearInterval(timer); timer = setInterval(poll, 3000);
        } catch (error) { failure(error); } finally { busy = false; }
    }
    async function cancelStartup() {
        cancelled = true; exiting = true; stopTimers();
        for (const [controller, path] of requests) if (path !== '/release') controller.abort();
        if (pendingDecision) { const decision = pendingDecision; pendingDecision = null; decision.cancel(); }
        unlock();
        // No game has started. Preserve existing dirty/legacy bytes without
        // inventing a successful cloud upload or replacing their metadata.
        try { if (fs && fs.syncfs) await syncfs(fs); }
        catch (_) { return {safeToExit: false, synced: false, reason: 'local_persist_failed'}; }
        let releasePending = false;
        if (lease && !released) {
            try { await request('/release', 'POST', {lease_id: lease}, 2000); }
            catch (_) { releasePending = true; } // Bounded server TTL handles lost replies.
        }
        released = true; show('stopped');
        return {safeToExit: true, synced: false, cancelled: true, pendingLocal: !!meta.dirty,
            releasePending, reason: 'startup_cancelled'};
    }
    function pauseCore() {
        if (!manager || manager.EJS?.paused) return () => {};
        const runtime = manager.Module;
        if (runtime && typeof runtime.pauseMainLoop === 'function') {
            runtime.pauseMainLoop();
            return () => { if (typeof runtime.resumeMainLoop === 'function') runtime.resumeMainLoop(); };
        }
        if (typeof manager.toggleMainLoop === 'function') {
            manager.toggleMainLoop(0); return () => manager.toggleMainLoop(1);
        }
        throw Error('Cannot safely pause the running core');
    }
    function matchesAcknowledged() {
        // Synchronous comparison after each await closes the release-window race;
        // do not let an asynchronous SHA operation create another unchecked gap.
        return !openWriter() && acknowledged !== undefined && JSON.stringify(snapshot(fs)) === acknowledged;
    }
    async function exitRunningGame() {
        exiting = true;
        let resume = () => {}, safe = false;
        try {
            resume = pauseCore();
            const result = await flush();
            if (!result.synced) return {safeToExit: false, ...result};
            if (!matchesAcknowledged()) {
                meta.dirty = true; await persistMeta();
                return {safeToExit: false, synced: false, reason: 'save_changed_during_exit'};
            }
            if (lease && !released) await request('/release', 'POST', {lease_id: lease});
            released = true; stopTimers();
            if (!matchesAcknowledged()) {
                meta.dirty = true; await persistMeta(); show('offline');
                return {safeToExit: false, synced: false, reason: 'save_changed_during_exit'};
            }
            await syncfs(fs);
            if (!matchesAcknowledged()) {
                meta.dirty = true; await persistMeta(); show('offline');
                return {safeToExit: false, synced: false, reason: 'save_changed_during_exit'};
            }
            safe = true; unlock(); return {safeToExit: true, synced: true};
        } catch (error) { failure(error); return {safeToExit: false, synced: false, reason: 'exit_sync_failed'}; }
        finally { if (!safe) { exiting = false; resume(); } }
    }
    async function prepareExit() {
        if (completedExit) return completedExit;
        if (exitPromise) return exitPromise;
        const operation = !started ? cancelStartup() : exitRunningGame();
        exitPromise = operation;
        try { const result = await operation; if (result.safeToExit) completedExit = result; return result; } finally { exitPromise = null; }
    }
    async function useCloudVersion() {
        if (exiting || busy || cancelled) return;
        exiting = true;
        let resume = () => {}, navigating = false;
        try {
            if (started) resume = pauseCore();
            if (openWriter()) throw Error('Save is still being written; retry after it completes');
            const local = snapshot(fs), legacyFiles = snapshot(fs, LEGACY_ROOT), before = JSON.stringify(local);
            fs.writeFile('/data/saves/.retroone-conflict-backup-' + Date.now() + '.json', JSON.stringify({files: local, legacyFiles, meta}));
            await syncfs(fs);
            if (openWriter() || JSON.stringify(snapshot(fs)) !== before) throw Error('Save changed while backing up');
            meta.allowCloud = true; await persistMeta();
            if (openWriter() || JSON.stringify(snapshot(fs)) !== before) throw Error('Save changed while backing up');
            if (started) {
                await request('/release', 'POST', {lease_id: lease});
                released = true; stopTimers();
                if (openWriter() || JSON.stringify(snapshot(fs)) !== before) throw Error('Save changed while releasing');
                navigating = true; global.location.reload();
            } else decide();
        } catch (error) {
            delete meta.allowCloud; meta.dirty = true;
            try { await persistMeta(); } catch (_) {}
            failure(error);
        } finally { if (!navigating) { exiting = false; resume(); } }
    }
    function exportLocal() {
        const blob = new Blob([JSON.stringify({format: 'retroone-psp-save-backup-v1', gameId: cfg.gameId, meta, files: snapshot(fs), legacyFiles: snapshot(fs, LEGACY_ROOT)})], {type: 'application/json'});
        const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = cfg.gameId + '-psp-save-backup.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 10000);
    }
    async function prepare(FS) {
        checkCancelled();
        // Session storage survives reloads; a same-origin Web Lock stops a cloned
        // tab (which can inherit sessionStorage) from sharing that lease token.
        if (global.navigator && navigator.locks && !unlockBrowser) {
            const acquired = await new Promise((resolve, reject) => {
                navigator.locks.request('retroone-cloud-save:' + cfg.profileId + ':' + cfg.gameId,
                    {ifAvailable: true}, lock => {
                        if (cancelled) { reject(cancellation()); return; }
                        if (!lock) { reject(Object.assign(Error('Game open in another tab'), {code: 'lease_busy'})); return; }
                        return new Promise(unlock => { unlockBrowser = unlock; resolve(true); });
                    }).catch(reject);
            }).catch(async error => { checkCancelled(); failure(error); await waitDecision(); return false; });
            if (!acquired) return prepare(FS);
        }
        checkCancelled();
        fs = FS;
        mountIdentity = typeof fs?.lookupPath === 'function' ? fs.lookupPath('/data/saves').node.mount : null;
        if (!fs || !fs.syncfs || !fs.lstat || !client) { show('unsupported'); throw Error(words.unsupported); }
        try { meta = JSON.parse(fs.readFile(META, {encoding: 'utf8'})); } catch (error) {
            if (exists(fs, META)) throw Error('Cloud save metadata is unreadable; local files retained');
            meta = {};
        }
        while (true) {
            try {
                await migrateSaveRoot(); checkCancelled();
                const legacy = await legacyExists(); checkCancelled();
                if (legacy) { show('legacy'); await waitDecision(); continue; }
                await connect(); break;
            } catch (error) { checkCancelled(); failure(error); await waitDecision(); }
        }
        checkCancelled();
        started = true; timer = setInterval(poll, 3000);
    }
    function prepareOnce(instance) {
        manager = instance;
        guardEmulator(instance.EJS || global.EJS_emulator);
        if (!preparation) {
            configureSaveDirectory(instance);
            preparation = prepare(instance.FS);
        }
        return preparation;
    }
    function guardEmulator(emulator) {
        if (!emulator || emulator.__retroCloudSave) return;
        emulator.__retroCloudSave = true;
        if (typeof emulator.callEvent === 'function') {
            const originalEvent = emulator.callEvent;
            emulator.callEvent = function (name, ...args) {
                if (name !== 'exit') return originalEvent.call(this, name, ...args);
                // The upstream synchronous exit callback restarts the core and
                // unmounts IDBFS. Never run it before an asynchronous save flush,
                // nor after releasing the write lease. Page/frame destruction
                // releases the paused runtime after our navigation callback.
                if (pageUnloading || dispatchingExit) return 0;
                dispatchingExit = true;
                prepareExit().then(result => {
                    if (result.safeToExit) {
                        if (typeof global.EJS_onExit === 'function') global.EJS_onExit();
                        else show('stopped');
                    } else dispatchingExit = false;
                }).catch(error => { dispatchingExit = false; failure(error); });
                return 0;
            };
        }
        // 4.3's GameManager is an internal ES module; this public awaited
        // initialization boundary replaces the 4.2 global-class hook.
        if (typeof emulator.initializeGameManager === 'function') {
            const originalInitialize = emulator.initializeGameManager;
            emulator.initializeGameManager = async function (...args) {
                const result = await originalInitialize.apply(this, args);
                try { await prepareOnce(this.gameManager); checkCancelled(); return result; }
                catch (error) { if (!cancelled) throw error; return new Promise(() => {}); }
            };
        }
    }
    function install(Manager) {
        if (!Manager || !Manager.prototype || Manager.prototype.__retroCloudSave) return;
        const original = Manager.prototype.mountFileSystems;
        if (typeof original !== 'function') return;
        Manager.prototype.__retroCloudSave = true;
        Manager.prototype.mountFileSystems = async function (...args) {
            const result = await original.apply(this, args);
            try { await prepareOnce(this); checkCancelled(); return result; }
            catch (error) {
                if (!cancelled) throw error;
                return new Promise(() => {});
            }
        };
    }
    const api = {flush, prepareExit, retry, status: () => ({status: state, dirty: !!meta.dirty}),
        // Pure adapter helpers are exported for synthetic and real-WASM FS tests.
        _test: {snapshot, digest, replaceFiles, install, prepare, guardEmulator, useCloudVersion, configureSaveDirectory, legacyExists}};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (!cfg) return;
    global.RetroCloudSaveSync = api;
    const prior = global.EJS_ready;
    global.EJS_ready = function (...args) { guardEmulator(global.EJS_emulator); install(global.EJS_GameManager); if (prior) return prior.apply(this, args); };
    const descriptor = Object.getOwnPropertyDescriptor(global, 'EJS_GameManager');
    if (!descriptor || descriptor.configurable) {
        let value = global.EJS_GameManager;
        Object.defineProperty(global, 'EJS_GameManager', {configurable: true, get: () => value, set: next => { value = next; install(next); }});
        install(value);
    }
    global.addEventListener('pagehide', () => { clearInterval(timer); clearInterval(heartbeatTimer); });
    global.addEventListener('pageshow', event => {
        if (event.persisted && fs && started && !released && !cancelled) {
            clearInterval(timer); timer = setInterval(poll, 3000);
            clearInterval(heartbeatTimer); heartbeatTimer = setInterval(async () => {
                try { await request('/heartbeat', 'POST', {lease_id: lease}); } catch (error) { failure(error); }
            }, 20000);
            retry();
        }
    });
    global.addEventListener('beforeunload', event => {
        // Browsers do not await asynchronous beforeunload work. Preserve IDBFS
        // and avoid the upstream synchronous unmount; only an already-persisted
        // outbox can be relied on after a tab is forcibly closed or reloaded.
        pageUnloading = true; setTimeout(() => { pageUnloading = false; }, 0);
        if (meta.dirty && !meta.allowCloud && !cancelled) { event.preventDefault(); event.returnValue = ''; }
    });
    show('starting');
})(typeof window !== 'undefined' ? window : globalThis);
