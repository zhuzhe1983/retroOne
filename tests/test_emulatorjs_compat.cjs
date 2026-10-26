const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {assetUrl, install, enable3dsPointer} = require('../web/emulatorjs-compat.js');

test('only the upstream PSP relative asset is rebased under versioned player data', () => {
    assert.equal(assetUrl('data/cores/ppsspp-assets.zip', '/p/player2/emulatorjs/4.3.0-pre/data/'),
        '/p/player2/emulatorjs/4.3.0-pre/data/cores/ppsspp-assets.zip');
    assert.equal(assetUrl('data/cores/ppsspp-assets.zip', '/runtime/data'), '/runtime/data/cores/ppsspp-assets.zip');
    for (const url of ['/api/rom/id', 'https://example.test/game.pbp', 'data/cores/other.zip', '/data/cores/ppsspp-assets.zip'])
        assert.equal(assetUrl(url, '/runtime/data/'), url);
    assert.equal(assetUrl('data/cores/ppsspp-assets.zip', undefined), 'data/cores/ppsspp-assets.zip');
});

test('4.3 downloader preserves receiver, arguments, return identity and one installation', async () => {
    const calls = [], result = Promise.resolve({files: []});
    const downloader = {downloadFile(...args) { calls.push([this, args]); return result; }};
    const emulator = {ejs_version: '4.3.0-pre', downloader, config: {dataPath: '/versioned/'}};
    assert.equal(install(emulator), true);
    const fn = downloader.downloadFile;
    assert.equal(install(emulator), false);
    assert.equal(downloader.downloadFile, fn);
    assert.equal(downloader.downloadFile('data/cores/ppsspp-assets.zip', 'CORE', 'GET', {a: 'b'}), result);
    assert.equal(calls[0][0], downloader);
    assert.deepEqual(calls[0][1], ['/versioned/cores/ppsspp-assets.zip', 'CORE', 'GET', {a: 'b'}]);
    await result;
});

test('stable runtime and missing downloader are untouched, failures remain visible', () => {
    const original = () => {};
    const stable = {ejs_version: '4.2.3', downloader: {downloadFile: original}};
    assert.equal(install(stable), false); assert.equal(stable.downloader.downloadFile, original);
    assert.equal(install({ejs_version: '4.3.0-pre'}), false);
    const error = Error('download denied');
    const next = {ejs_version: '4.3.0-pre', config: {dataPath: '/new/'}, downloader: {downloadFile() { throw error; }}};
    install(next); assert.throws(() => next.downloader.downloadFile('data/cores/ppsspp-assets.zip'), value => value === error);
});

test('EJS_ready chains the existing cloud-save hook and preserves its promise', async () => {
    const result = Promise.resolve('cloud save ready'), calls = [];
    const context = {EJS_ready(...args) { calls.push([this, args]); return result; },
        EJS_emulator: {ejs_version: '4.3.0-pre', config: {dataPath: '/data/'}, downloader: {downloadFile() {}}}};
    context.window = context;
    vm.runInNewContext(fs.readFileSync(require.resolve('../web/emulatorjs-compat.js'), 'utf8'), context);
    const receiver = {};
    assert.equal(context.EJS_ready.call(receiver, 'event'), result);
    assert.equal(calls[0][0], receiver); assert.deepEqual(calls[0][1], ['event']);
    assert.equal(await result, 'cloud save ready');
});


test('3DS start restores lower-screen touch without impersonating another core', () => {
    const removed = [], pad = [], handlers = [];
    const emulator = {ejs_version:'4.3.0-pre', config:{dataPath:'/data/'}, downloader:{downloadFile(){}},
        getCore:()=> 'azahar', canvas:{classList:{remove:value=>removed.push(value)},style:{}},
        toggleVirtualGamepad:value=>pad.push(value), enableMouseLock:true,
        on:(name,handler)=>handlers.push([name,handler])};
    assert.equal(install(emulator),true); assert.equal(handlers.length,1);
    assert.equal(handlers[0][0],'start'); handlers[0][1]();
    assert.deepEqual(removed,['ejs-canvas-no-pointer']); assert.deepEqual(pad,[false]);
    assert.equal(emulator.canvas.style.touchAction,'none'); assert.equal(emulator.enableMouseLock,false);
    assert.equal(emulator.getCore(),'azahar');
    assert.equal(enable3dsPointer({...emulator,getCore:()=> 'ppsspp'}),false);
    assert.deepEqual(removed,['ejs-canvas-no-pointer']);
});
