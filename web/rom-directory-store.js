/* Disk-backed WebTorrent chunk store. Only touches files in a manager-owned entry directory. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.RetroRomDirectoryStore = api.DirectoryStore;
})(typeof window === 'object' ? window : null, function () {
  'use strict';
  const fault = (code, message = code) => Object.assign(new Error(message), {code});
  const missing = problem => problem?.name === 'NotFoundError';
  const stat = file => ({size: file.size, lastModified: file.lastModified});
  class DirectoryStore {
    constructor(chunkLength, opts) {
      if (!Number.isSafeInteger(chunkLength) || chunkLength < 1 || !Number.isSafeInteger(opts.length) || opts.length < 1 || !opts.directory || !/^[a-zA-Z0-9_-]+\.[a-zA-Z0-9]{1,8}$/.test(opts.name)) throw fault('invalid');
      this.chunkLength = chunkLength; this.length = opts.length; this.directory = opts.directory;
      this.name = opts.name; this.count = Math.ceil(this.length / chunkLength);
      this.queue = Promise.resolve(); this.closed = false; this.finalStat = null; this.observedFinalStat = null; this.usedPieces = false;
    }
    size(index) {
      if (!Number.isSafeInteger(index) || index < 0 || index >= this.count) throw fault('invalid-index');
      return Math.min(this.chunkLength, this.length - index * this.chunkLength);
    }
    pieceName(index) { this.size(index); return `piece-${index}.bin`; }
    serial(fn) {
      const result = this.queue.then(fn); this.queue = result.catch(() => {}); return result;
    }
    put(index, bytes, callback) {
      let expected;
      try { expected = this.size(index); if (this.closed || bytes.byteLength !== expected) throw fault('invalid-write'); }
      catch (problem) { queueMicrotask(() => callback(problem)); return; }
      this.serial(async () => {
        const handle = await this.directory.getFileHandle(this.pieceName(index), {create: true});
        const stream = await handle.createWritable();
        try { await stream.write(bytes); await stream.close(); this.usedPieces = true; }
        catch (problem) { await stream.abort().catch(() => {}); throw problem; }
      }).then(() => callback(null), callback);
    }
    async read(index, opts = {}) {
      const size = this.size(index), offset = opts.offset ?? 0, length = opts.length ?? (size - offset);
      if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < 0 || length < 0 || offset + length > size) throw fault('invalid-range');
      let file;
      try { file = await (await this.directory.getFileHandle(this.pieceName(index))).getFile(); }
      catch (problem) { if (!missing(problem)) throw problem; }
      if (file) {
        this.usedPieces = true;
        if (file.size !== size) throw fault('missing-piece');
        return new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
      }
      const final = await (await this.directory.getFileHandle(this.name)).getFile();
      if (final.size !== this.length) throw fault('missing-piece');
      const current = stat(final);
      if (this.observedFinalStat && (current.size !== this.observedFinalStat.size || current.lastModified !== this.observedFinalStat.lastModified)) throw fault('modified');
      this.observedFinalStat = current;
      const start = index * this.chunkLength + offset;
      return new Uint8Array(await final.slice(start, start + length).arrayBuffer());
    }
    get(index, opts, callback) {
      if (typeof opts === 'function') { callback = opts; opts = {}; }
      if (this.closed) { queueMicrotask(() => callback(fault('closed'))); return; }
      this.queue.then(() => this.read(index, opts)).then(data => callback(null, data), callback);
    }
    // Never form a whole-ROM Blob or ArrayBuffer. Keep each transfer bounded to one piece.
    finalize() {
      return this.serial(async () => {
        if (this.closed) throw fault('closed');
        const handle = await this.directory.getFileHandle(this.name, {create: true});
        if (this.observedFinalStat) {
          const existing = await handle.getFile();
          if (existing.size !== this.observedFinalStat.size || existing.lastModified !== this.observedFinalStat.lastModified) throw fault('modified');
          if (!this.usedPieces) { this.finalStat = stat(existing); return existing; }
        }
        const stream = await handle.createWritable();
        try {
          for (let index = 0; index < this.count; index++) await stream.write(await this.read(index));
          await stream.close();
        } catch (problem) { await stream.abort().catch(() => {}); throw problem; }
        const file = await handle.getFile();
        if (file.size !== this.length) throw fault('mismatch');
        this.finalStat = stat(file); this.observedFinalStat = this.finalStat; this.usedPieces = false;
        // These exact filenames are ours; unrelated directory entries are never listed or removed.
        for (let index = 0; index < this.count; index++) {
          try { await this.directory.removeEntry(this.pieceName(index)); }
          catch (problem) { if (!missing(problem)) throw problem; }
        }
        return file;
      });
    }
    async file(expected = this.finalStat) {
      await this.queue;
      const file = await (await this.directory.getFileHandle(this.name)).getFile();
      if (!expected || file.size !== expected.size || file.lastModified !== expected.lastModified) throw fault('modified');
      return file;
    }
    close(callback = () => {}) { this.closed = true; this.queue.then(() => callback(null), callback); }
    // WebTorrent cancellation must retain resumable pieces and completed files.
    destroy(callback = () => {}) { this.close(callback); }
  }
  return {DirectoryStore, stat};
});
