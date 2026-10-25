'use strict';

// Both pinned upstream bundles remove the entire origin's "chunks" OPFS folder
// at import time. RetroOne pages may run concurrently and must only remove their
// own storage. Keep all other store behavior intact. Any upstream byte change
// requires a deliberate review, never a broad/heuristic replacement.
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const esbuild = require('esbuild');

const REVIEWED = {
  fsa: 'a278ad8cbe53a808a3ffd97224ba4634d7cc20a968b8c20fabcc48ac6a2ed752',
  webtorrent: 'db4dca98cd135c732eeffdede3cf5f8febd0585a0eba4dc4d1effa1b901f4c3d'
};
const FSA_BOOT_CLEANUP = `// this can be bad when multiple instances of this app are running
if (globalThis.navigator?.storage?.getDirectory) {
  navigator.storage.getDirectory().then(storageDir => {
    storageDir.removeEntry('chunks', { recursive: true }).catch(() => {})
  })
}
`;
const WEBTORRENT_BOOT_CLEANUP = 'globalThis.navigator?.storage?.getDirectory&&navigator.storage.getDirectory().then(e=>{e.removeEntry("chunks",{recursive:!0}).catch(()=>{})});';

function replaceReviewed(bytes, expected, needle) {
  const actual = crypto.createHash('sha256').update(bytes).digest('hex');
  if (actual !== expected) throw Error('Upstream bundle changed; review the OPFS isolation patch before updating');
  const source = bytes.toString('utf8').replace(/\r\n/g, '\n');
  if (source.split(needle).length !== 2) throw Error('Expected exactly one reviewed OPFS cleanup statement');
  return source.replace(needle, '/* RetroOne: origin-wide OPFS cleanup disabled; each session owns its store. */');
}

async function build(outputDirectory = path.join(__dirname, 'dist')) {
  const fsaPath = require.resolve('fsa-chunk-store');
  const torrentPath = path.join(__dirname, 'node_modules/webtorrent/dist/webtorrent.min.js');
  const patchedFsa = replaceReviewed(await fs.readFile(fsaPath), REVIEWED.fsa, FSA_BOOT_CLEANUP);
  const patchedTorrent = replaceReviewed(await fs.readFile(torrentPath), REVIEWED.webtorrent, WEBTORRENT_BOOT_CLEANUP);
  await fs.mkdir(outputDirectory, {recursive: true});
  // WebTorrent 3 ships an ES-module browser bundle. Convert it to the same
  // explicit window.WebTorrent contract used by our dynamically loaded player.
  await esbuild.build({
    stdin: {contents: patchedTorrent, sourcefile: 'webtorrent-upstream.js'},
    outfile: path.join(outputDirectory, 'webtorrent.min.js'), format: 'iife', globalName: 'RetroWebTorrentBundle',
    target: 'es2020', minify: true, sourcemap: false,
    banner: {js: '/* WebTorrent 3.0.21 (MIT). RetroOne removes only the origin-wide import-time OPFS cleanup. */'},
    footer: {js: 'globalThis.WebTorrent = RetroWebTorrentBundle.default;'}
  });
  await esbuild.build({
    stdin: {contents: 'import FSAChunkStore from "fsa-chunk-store"; import MemoryChunkStore from "memory-chunk-store"; globalThis.RetroRomChunkStore = FSAChunkStore; globalThis.RetroRomMemoryStore = MemoryChunkStore;',
      resolveDir: __dirname, sourcefile: 'retroone-rom-chunk-store.js'},
    outfile: path.join(outputDirectory, 'rom-chunk-store.js'), bundle: true, format: 'iife', platform: 'browser',
    target: 'es2020', minify: true, sourcemap: false,
    banner: {js: '/* fsa-chunk-store 1.3.0 (MIT). RetroOne removes only the origin-wide import-time cleanup. */'},
    plugins: [{name: 'retroone-opfs-isolation', setup(build) {
      build.onLoad({filter: /fsa-chunk-store[\\/]index\.js$/}, async args => ({
        contents: patchedFsa, loader: 'js', resolveDir: path.dirname(args.path)
      }));
    }}]
  });
  const licenses = await Promise.all([
    ['webtorrent', 'WebTorrent 3.0.21', 'LICENSE'], ['fsa-chunk-store', 'FSA Chunk Store 1.3.0', 'LICENSE'],
    ['filename-reserved-regex', 'Filename Reserved Regex 3.0.0', 'license'],
    ['memory-chunk-store', 'Memory Chunk Store 1.3.5', 'LICENSE'],
    ['queue-microtask', 'Queue Microtask 1.2.3', 'LICENSE']
  ].map(async ([name, title, filename]) => title + '\n\n' + await fs.readFile(path.join(__dirname, 'node_modules', name, filename), 'utf8')));
  await fs.writeFile(path.join(outputDirectory, 'LICENSES.txt'), licenses.join('\n\n---\n\n'));
  return outputDirectory;
}

module.exports = {build, replaceReviewed, REVIEWED, FSA_BOOT_CLEANUP, WEBTORRENT_BOOT_CLEANUP};
if (require.main === module) build().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
