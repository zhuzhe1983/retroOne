'use strict';

// Private WebTorrent signaling only. ROM bytes stay in the API's HTTP web seed
// or move directly between authorized browsers; this process reads no ROMs.
const http = require('node:http');
const fs = require('node:fs');
const crypto = require('node:crypto');
const {WebSocketServer} = require('ws');

const AUTH = Symbol('retroone tracker authorization');
const HASH = /^[a-f0-9]{40}$/;
const MAX_TOKEN_TTL = 24 * 60 * 60;
const DEFAULT_SECRET = '/library-data/metadata/.webtorrent-secret';

function loadSecret(filename) {
  let fd;
  try {
    fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > 128) throw Error('invalid secret');
    const value = fs.readFileSync(fd, 'utf8').trim();
    if (!/^[a-f0-9]{64}$/i.test(value)) throw Error('invalid secret');
    return Buffer.from(value, 'hex');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

function parseToken(value, now) {
  const match = typeof value === 'string' && /^([1-9][0-9]{0,10})\.([a-f0-9]{64})$/.exec(value);
  if (!match) return null;
  const expires = Number(match[1]);
  if (!Number.isSafeInteger(expires) || expires <= now || expires > now + MAX_TOKEN_TTL) return null;
  return {expires, signature: Buffer.from(match[2], 'hex')};
}

function authenticate(secret, infoHash, token, now) {
  if (!HASH.test(infoHash) || !token || token.expires <= now || token.expires > now + MAX_TOKEN_TTL) return false;
  const expected = crypto.createHmac('sha256', secret).update(`${infoHash}:${token.expires}`, 'utf8').digest();
  return token.signature.length === expected.length && crypto.timingSafeEqual(token.signature, expected);
}

function failure(socket, message) {
  if (socket.readyState === 1) socket.send(JSON.stringify({action: 'announce', 'failure reason': message}));
}

async function createPrivateTracker({secretFile = process.env.RETROONE_TORRENT_SECRET_FILE || DEFAULT_SECRET,
  now = () => Math.floor(Date.now() / 1000), maxHashes = 256, maxConnections = 256,
  cleanupIntervalMs = 1000} = {}) {
  const {default: Server} = await import('bittorrent-tracker/server');
  const activeHashes = new Map();
  const sockets = new Set();
  let stopping = false;

  // The pinned 11.2.3 source maps parsed websocket requests to params.socket
  // before calling filter. The URL token is bound to that actual socket, never
  // read from client-supplied announce JSON. Its filter covers announce only.
  // Reject scrape before upstream parsing (which otherwise permits enumeration).
  class PrivateServer extends Server {
    _onWebSocketRequest(socket, options, raw) {
      let message;
      try { message = JSON.parse(raw); } catch { failure(socket, 'Invalid tracker message'); return; }
      if (!message || typeof message !== 'object' || Array.isArray(message) || message.action !== 'announce') {
        failure(socket, 'Only private announces are supported');
        return;
      }
      const auth = socket[AUTH];
      if (!auth || auth.expires <= now()) { failure(socket, 'Tracker authorization expired'); socket.close(1008, 'Authorization expired'); return; }
      const timestamp = now();
      if (timestamp - auth.rateStart >= 60) { auth.rateStart = timestamp; auth.messages = 0; }
      if (++auth.messages > 120) { socket.close(1008, 'Too many tracker requests'); return; }
      const signal = (value, kind) => value && typeof value === 'object' && !Array.isArray(value) &&
        value.type === kind && typeof value.sdp === 'string' && value.sdp.length <= 16 * 1024;
      if (message.offers !== undefined && (!Array.isArray(message.offers) || message.offers.length > 20 ||
          message.offers.some(item => !item || typeof item !== 'object' || !signal(item.offer, 'offer') ||
            typeof item.offer_id !== 'string' || item.offer_id.length !== 20))) {
        failure(socket, 'Invalid peer offers'); return;
      }
      if (message.answer !== undefined && (!signal(message.answer, 'answer') ||
          typeof message.offer_id !== 'string' || message.offer_id.length !== 20)) {
        failure(socket, 'Invalid peer answer'); return;
      }
      try { super._onWebSocketRequest(socket, options, raw); }
      catch { failure(socket, 'Invalid tracker message'); socket.close(1008, 'Invalid tracker message'); }
    }
  }

  function cleanup() {
    const timestamp = now();
    for (const socket of sockets) {
      const auth = socket[AUTH];
      if (!auth || auth.expires <= timestamp || (!auth.verified && timestamp - auth.connectedAt >= 30)) socket.terminate();
    }
    for (const [infoHash, expires] of activeHashes) {
      if (expires <= timestamp) {
        activeHashes.delete(infoHash);
        delete tracker.torrents[infoHash];
      }
    }
  }

  const tracker = new PrivateServer({http: false, udp: false, ws: false, stats: false,
    interval: 5 * 60 * 1000, peersCacheLength: 256, peersCacheTtl: 10 * 60 * 1000,
    filter(infoHash, params, callback) {
      try {
        const auth = params.socket?.[AUTH];
        if (!authenticate(loadSecret(secretFile), infoHash, auth, now())) return callback(Error('Invalid private tracker authorization'));
        cleanup();
        if (!activeHashes.has(infoHash) && activeHashes.size >= maxHashes) return callback(Error('Private tracker is busy'));
        activeHashes.set(infoHash, Math.max(activeHashes.get(infoHash) || 0, auth.expires));
        auth.verified = true;
        callback(null);
      } catch { callback(Error('Private tracker is unavailable')); }
    }
  });
  // Invalid peer requests are deliberately not logged: offers may contain IPs
  // and URLs contain bearer tokens. Health reports only counts and readiness.
  tracker.on('warning', () => {});
  tracker.on('error', () => {});

  const webSockets = new WebSocketServer({noServer: true, clientTracking: false, perMessageDeflate: false, maxPayload: 256 * 1024});
  const server = http.createServer((request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('Content-Type', 'application/json');
    if (request.method === 'GET' && request.url === '/health') {
      let ready = false;
      try { loadSecret(secretFile); ready = !stopping; } catch {}
      response.writeHead(ready ? 200 : 503);
      response.end(JSON.stringify({ok: ready, private: true, active_torrents: activeHashes.size, connections: sockets.size}));
    } else {
      response.writeHead(404);
      response.end('{"detail":"Not found"}');
    }
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  server.keepAliveTimeout = 5000;
  server.on('upgrade', (request, socket, head) => {
    let url, token;
    try {
      url = new URL(request.url, 'http://retroone.invalid');
      const names = [...url.searchParams.keys()];
      if (url.pathname !== '/rom-tracker' || names.length !== 1 || names[0] !== 'token') throw Error('invalid route');
      token = parseToken(url.searchParams.get('token'), now());
      if (!token || stopping || sockets.size >= maxConnections) throw Error('invalid token');
      loadSecret(secretFile);
    } catch {
      socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    webSockets.handleUpgrade(request, socket, head, peer => {
      peer[AUTH] = {...token, connectedAt: now(), rateStart: now(), messages: 0, verified: false};
      peer.upgradeReq = request;
      sockets.add(peer);
      peer.on('close', () => sockets.delete(peer));
      peer.on('error', () => {});
      tracker.onWebSocketConnection(peer);
    });
  });
  const timer = setInterval(cleanup, cleanupIntervalMs);
  timer.unref();

  return {
    server,
    async listen(port = Number(process.env.RETROONE_TORRENT_PORT || 8088), host = '0.0.0.0') {
      await new Promise((resolve, reject) => {
        const failed = error => { server.off('listening', ready); reject(error); };
        const ready = () => { server.off('error', failed); resolve(); };
        server.once('error', failed); server.once('listening', ready); server.listen(port, host);
      });
      return server.address();
    },
    async close() {
      if (stopping) return;
      stopping = true;
      clearInterval(timer);
      for (const socket of sockets) socket.terminate();
      await new Promise(resolve => webSockets.close(resolve));
      await new Promise(resolve => tracker.close(resolve));
      if (server.listening) await new Promise(resolve => server.close(resolve));
      activeHashes.clear();
    }
  };
}

module.exports = {createPrivateTracker, loadSecret, parseToken, authenticate, MAX_TOKEN_TTL};

if (require.main === module) {
  createPrivateTracker().then(async application => {
    await application.listen();
    process.stdout.write('RetroOne private WebTorrent tracker ready\n');
    const stop = () => application.close().then(() => process.exit(0));
    process.once('SIGTERM', stop); process.once('SIGINT', stop);
  }).catch(() => { process.stderr.write('RetroOne private WebTorrent tracker could not start\n'); process.exitCode = 1; });
}
