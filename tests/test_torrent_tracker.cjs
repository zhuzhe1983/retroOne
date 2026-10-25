'use strict';
// Real localhost WebSocket announces, generated identities and SDP fixtures only.
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {createRequire} = require('node:module');
const localRequire = createRequire(path.join(__dirname, '../container/p2p/package.json'));
const {WebSocket} = localRequire('ws');
const {createPrivateTracker, MAX_TOKEN_TTL} = require('../container/p2p/tracker.cjs');

const hashA = 'a'.repeat(40), hashB = 'b'.repeat(40);
const binary = value => Buffer.from(value, 'hex').toString('latin1');

async function fixture(t, options = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'retroone-tracker-test-'));
  const secret = crypto.randomBytes(32);
  const secretFile = path.join(directory, 'secret');
  await fs.writeFile(secretFile, secret.toString('hex'), {mode: 0o600});
  let timestamp = Math.floor(Date.now() / 1000);
  const app = await createPrivateTracker({secretFile, now: () => timestamp, cleanupIntervalMs: 20, ...options});
  const address = await app.listen(0, '127.0.0.1');
  const base = `http://127.0.0.1:${address.port}`;
  t.after(async () => { await app.close(); await fs.rm(directory, {recursive: true, force: true}); });
  function token(hash, expires = timestamp + 600) {
    return `${expires}.${crypto.createHmac('sha256', secret).update(`${hash}:${expires}`).digest('hex')}`;
  }
  return {app, base, secretFile, token, now: () => timestamp, advance: seconds => { timestamp += seconds; },
    health: async () => (await fetch(base + '/health')).json()};
}

async function client(t, fixture, token) {
  const socket = new WebSocket(fixture.base.replace('http:', 'ws:') + '/rom-tracker?token=' + token);
  const inbox = [], pending = new Set();
  socket.on('error', () => {});
  socket.on('message', data => {
    const value = JSON.parse(data);
    for (const item of pending) {
      if (item.match(value)) { pending.delete(item); clearTimeout(item.timer); item.resolve(value); return; }
    }
    inbox.push(value);
  });
  t.after(() => { socket.terminate(); for (const item of pending) clearTimeout(item.timer); });
  await new Promise((resolve, reject) => {
    socket.once('open', resolve);
    socket.once('error', reject);
  });
  return {socket, send: value => socket.send(JSON.stringify(value)), receive(match = () => true) {
    const index = inbox.findIndex(match);
    if (index >= 0) return Promise.resolve(inbox.splice(index, 1)[0]);
    return new Promise((resolve, reject) => {
      const item = {match, resolve, timer: setTimeout(() => { pending.delete(item); reject(Error('Tracker response timeout')); }, 3000)};
      pending.add(item);
    });
  }};
}

function announce(hash, peer, values = {}) {
  return {action: 'announce', info_hash: binary(hash), peer_id: peer.repeat(20), event: 'started',
    uploaded: 0, downloaded: 0, left: 1, offers: [], ...values};
}

test('private tokens authorize the same swarm across different browser tokens and exchange offer/answer', {timeout: 10000}, async t => {
  const f = await fixture(t);
  const a = await client(t, f, f.token(hashA));
  const b = await client(t, f, f.token(hashA, f.now() + 1200));
  a.send(announce(hashA, 'a'));
  const first = await a.receive();
  assert.equal(first.action, 'announce');
  assert.equal(first['failure reason'], undefined);
  const offer = {type: 'offer', sdp: 'v=0\r\no=generated-fixture\r\n'};
  b.send(announce(hashA, 'b', {offers: [{offer, offer_id: 'o'.repeat(20)}]}));
  const incoming = await a.receive(value => Boolean(value.offer));
  assert.deepEqual(incoming.offer, offer);
  assert.equal(incoming.peer_id, 'b'.repeat(20));
  assert.equal(incoming.info_hash, binary(hashA));
  const answer = {type: 'answer', sdp: 'v=0\r\no=generated-answer\r\n'};
  a.send(announce(hashA, 'a', {event: 'update', answer, offer_id: incoming.offer_id, to_peer_id: incoming.peer_id}));
  const returned = await b.receive(value => Boolean(value.answer));
  assert.deepEqual(returned.answer, answer);
  assert.equal(returned.peer_id, 'a'.repeat(20));
  const health = await f.health();
  assert.equal(health.active_torrents, 1);
  assert.equal(health.connections, 2);
  assert.equal(JSON.stringify(health).includes('signature'), false);
});

test('unknown hash, forged token, and announce-supplied token cannot join or create a swarm', async t => {
  const f = await fixture(t);
  const wrongHash = await client(t, f, f.token(hashA));
  wrongHash.send(announce(hashB, 'a'));
  assert.match((await wrongHash.receive())['failure reason'], /authorization/);
  const forged = await client(t, f, `${f.now() + 600}.${'0'.repeat(64)}`);
  forged.send(announce(hashA, 'b', {token: f.token(hashA)}));
  assert.match((await forged.receive())['failure reason'], /authorization/);
  assert.equal((await f.health()).active_torrents, 0);
});

test('scrape and malformed peer offers do not expose swarms or crash the service', async t => {
  const f = await fixture(t);
  const a = await client(t, f, f.token(hashA));
  a.send({action: 'scrape'});
  const refused = await a.receive();
  assert.match(refused['failure reason'], /private announces/);
  assert.equal(refused.files, undefined);
  a.send(announce(hashA, 'a', {offers: [null]}));
  assert.match((await a.receive())['failure reason'], /Invalid peer offers/);
  a.send(announce(hashA, 'a'));
  assert.equal((await a.receive())['failure reason'], undefined);
  assert.equal((await f.health()).active_torrents, 1);
});

test('expired connections are closed and swarms are removed; a fresh token can reconnect', async t => {
  const f = await fixture(t);
  const a = await client(t, f, f.token(hashA, f.now() + 5));
  a.send(announce(hashA, 'a'));
  await a.receive();
  const closed = new Promise(resolve => a.socket.once('close', resolve));
  f.advance(6);
  await closed;
  assert.equal((await f.health()).active_torrents, 0);
  const b = await client(t, f, f.token(hashA));
  b.send(announce(hashA, 'b'));
  assert.equal((await b.receive())['failure reason'], undefined);
});

test('bounded hash registry rejects extra valid hashes and accepts them after expiry', async t => {
  const f = await fixture(t, {maxHashes: 1});
  const a = await client(t, f, f.token(hashA, f.now() + 5));
  const b = await client(t, f, f.token(hashB));
  a.send(announce(hashA, 'a'));
  await a.receive();
  b.send(announce(hashB, 'b'));
  assert.match((await b.receive())['failure reason'], /busy/);
  f.advance(6);
  b.send(announce(hashB, 'b'));
  assert.equal((await b.receive())['failure reason'], undefined);
  assert.equal((await f.health()).active_torrents, 1);
});

test('missing, expired, overlong-lived, or duplicated URL tokens are rejected before upgrade', async t => {
  const f = await fixture(t);
  const invalid = ['', 'bad-token', f.token(hashA, f.now() - 1), f.token(hashA, f.now() + MAX_TOKEN_TTL + 1), f.token(hashA) + '&token=' + f.token(hashA)];
  for (const token of invalid) {
    const status = await new Promise((resolve, reject) => {
      const socket = new WebSocket(f.base.replace('http:', 'ws:') + '/rom-tracker?token=' + token);
      socket.on('error', () => {});
      socket.on('open', () => { socket.terminate(); reject(Error('Unauthorized upgrade accepted')); });
      socket.on('unexpected-response', (_, response) => { response.resume(); socket.terminate(); resolve(response.statusCode); });
    });
    assert.equal(status, 403);
  }
  assert.equal((await f.health()).connections, 0);
});

test('HTTP announce, scrape, stats and registration are unavailable', async t => {
  const f = await fixture(t);
  for (const route of ['/announce', '/scrape', '/stats', '/register', '/rom-tracker']) {
    assert.equal((await fetch(f.base + route)).status, 404);
  }
  assert.equal((await fetch(f.base + '/register', {method: 'POST', body: '{}'})).status, 404);
  assert.deepEqual(await f.health(), {ok: true, private: true, active_torrents: 0, connections: 0});
  await fs.writeFile(f.secretFile, 'invalid');
  assert.equal((await fetch(f.base + '/health')).status, 503);
});

test('unverified websocket connections expire rather than occupying capacity forever', async t => {
  const f = await fixture(t, {maxConnections: 1});
  const a = await client(t, f, `${f.now() + 600}.${'0'.repeat(64)}`);
  const closed = new Promise(resolve => a.socket.once('close', resolve));
  f.advance(31);
  await closed;
  const b = await client(t, f, f.token(hashA));
  b.send(announce(hashA, 'b'));
  assert.equal((await b.receive())['failure reason'], undefined);
});

test('patched browser bundles are inert on import and FSA stores delete only their own session', async t => {
  const vm = require('node:vm');
  const {build} = require('../container/p2p/build.cjs');
  const output = await fs.mkdtemp(path.join(os.tmpdir(), 'retroone-torrent-bundle-test-'));
  t.after(() => fs.rm(output, {recursive: true, force: true}));
  await build(output);
  let storageReads = 0;
  const removed = [], directories = new Map();
  const directory = () => ({files: new Map(), async getFileHandle(name) {
    if (!this.files.has(name)) {
      let bytes = new Uint8Array();
      this.files.set(name, {async createWritable() { return {
        async write(data) { bytes = new Uint8Array(data); }, async close() {}
      }; }, async getFile() { return new Blob([bytes]); }});
    }
    return this.files.get(name);
  }});
  directories.set('chunks', directory()); // another application's existing data
  const root = {async getDirectoryHandle(name) {
    if (!directories.has(name)) directories.set(name, directory());
    return directories.get(name);
  }, async removeEntry(name) { removed.push(name); directories.delete(name); }};
  const context = {console, Uint8Array, ArrayBuffer, Blob, WritableStream, ReadableStream, TransformStream,
    TextEncoder, TextDecoder, URL, URLSearchParams, WebSocket, crypto: crypto.webcrypto,
    navigator: {storage: {async getDirectory() { storageReads++; return root; }}, userAgent: 'test'},
    setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask};
  context.window = context; context.self = context;
  vm.createContext(context);
  for (const filename of ['webtorrent.min.js', 'rom-chunk-store.js']) {
    vm.runInContext(await fs.readFile(path.join(output, filename), 'utf8'), context);
  }
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(storageReads, 0, 'Importing bundles must not access or delete OPFS');
  assert.equal(typeof context.WebTorrent, 'function');
  assert.equal(typeof context.RetroRomChunkStore, 'function');
  assert.equal(typeof context.RetroRomMemoryStore, 'function');
  const first = new context.RetroRomChunkStore(4, {name: 'retroone-session-one', length: 4});
  const second = new context.RetroRomChunkStore(4, {name: 'retroone-session-two', length: 4});
  await first.put(0, new Uint8Array([1, 2, 3, 4]));
  await second.put(0, new Uint8Array([5, 6, 7, 8]));
  assert.deepEqual(Array.from(await first.get(0)), [1, 2, 3, 4]);
  await new Promise((resolve, reject) => first.destroy(error => error ? reject(error) : resolve()));
  assert.deepEqual(removed, ['retroone-session-one']);
  assert.equal(directories.has('chunks'), true);
  assert.deepEqual(Array.from(await second.get(0)), [5, 6, 7, 8]);
  await new Promise((resolve, reject) => second.destroy(error => error ? reject(error) : resolve()));
  assert.deepEqual(removed, ['retroone-session-one', 'retroone-session-two']);
  const beforeMemory = storageReads;
  const memoryOne = new context.RetroRomMemoryStore(4, {length: 4});
  const memoryTwo = new context.RetroRomMemoryStore(4, {length: 4});
  const put = (store, values) => new Promise((resolve, reject) => store.put(0, new Uint8Array(values), error => error ? reject(error) : resolve()));
  const get = store => new Promise((resolve, reject) => store.get(0, (error, value) => error ? reject(error) : resolve(value)));
  await put(memoryOne, [8, 7, 6, 5]); await put(memoryTwo, [4, 3, 2, 1]);
  assert.deepEqual(Array.from(await get(memoryOne)), [8, 7, 6, 5]);
  await new Promise(resolve => memoryOne.destroy(resolve));
  assert.deepEqual(Array.from(await get(memoryTwo)), [4, 3, 2, 1]);
  await new Promise(resolve => memoryTwo.destroy(resolve));
  assert.equal(storageReads, beforeMemory, 'Memory fallback never uses OPFS');
  assert.deepEqual(removed, ['retroone-session-one', 'retroone-session-two']);
});

test('OPFS build patches fail closed if reviewed bytes or the unique target change', () => {
  const {replaceReviewed} = require('../container/p2p/build.cjs');
  const value = Buffer.from('before;remove-danger;after;');
  const digest = crypto.createHash('sha256').update(value).digest('hex');
  assert.match(replaceReviewed(value, digest, 'remove-danger;'), /origin-wide OPFS cleanup disabled/);
  assert.throws(() => replaceReviewed(Buffer.from('changed'), digest, 'remove-danger;'), /Upstream bundle changed/);
  assert.throws(() => replaceReviewed(value, digest, 'missing;'), /exactly one/);
  const doubled = Buffer.from('remove-danger;remove-danger;');
  assert.throws(() => replaceReviewed(doubled, crypto.createHash('sha256').update(doubled).digest('hex'), 'remove-danger;'), /exactly one/);
});
