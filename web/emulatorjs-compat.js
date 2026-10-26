/* Narrow compatibility bridge for the pinned EmulatorJS 4.3.0-pre runtime.
 * Upstream GameManager.loadPpssppAssets uses a document-relative URL instead of
 * config.dataPath. Rebase only that exact internal asset; ROM URLs and other
 * requests retain their original origin, path, arguments and error behavior.
 */
(function (global) {
    'use strict';
    const wrapped = new WeakSet();
    function assetUrl(url, dataPath) {
        if (url !== 'data/cores/ppsspp-assets.zip' || typeof dataPath !== 'string' || !dataPath) return url;
        return dataPath.replace(/\/?$/, '/') + 'cores/ppsspp-assets.zip';
    }
    function enable3dsPointer(emulator) {
        if (!emulator || typeof emulator.getCore !== 'function' || !['azahar', '3ds'].includes(emulator.getCore())) return false;
        // EmulatorJS gives all touch devices its pad overlay and disables canvas
        // hit testing. RetroOne's parent supplies the pad; 3DS still needs native
        // canvas TouchEvents for the lower screen. No coordinate remapping and
        // no global masquerading as a DS core are necessary.
        if (emulator.canvas && emulator.canvas.classList) {
            emulator.canvas.classList.remove('ejs-canvas-no-pointer');
            emulator.canvas.style.touchAction = 'none';
        }
        if (typeof emulator.toggleVirtualGamepad === 'function') emulator.toggleVirtualGamepad(false);
        emulator.enableMouseLock = false;
        return true;
    }
    function install(emulator) {
        const downloader = emulator && emulator.downloader;
        if (!emulator || !/^4\.3\./.test(String(emulator.ejs_version || '')) ||
                !downloader || typeof downloader.downloadFile !== 'function' || wrapped.has(downloader)) return false;
        const original = downloader.downloadFile;
        downloader.downloadFile = function (url, ...args) {
            return original.call(this, assetUrl(url, emulator.config && emulator.config.dataPath), ...args);
        };
        if (typeof emulator.on === 'function') emulator.on('start', () => enable3dsPointer(emulator));
        wrapped.add(downloader);
        return true;
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = {assetUrl, install, enable3dsPointer};
    const prior = global.EJS_ready;
    global.EJS_ready = function (...args) {
        install(global.EJS_emulator);
        if (typeof prior === 'function') return prior.apply(this, args);
    };
})(typeof window !== 'undefined' ? window : globalThis);
