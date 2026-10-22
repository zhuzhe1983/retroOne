const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
const context = vm.createContext({URL}); context.window = context;
vm.runInContext(fs.readFileSync('web/library-ui.js', 'utf8'), context);
const Library = context.RetroLibrary;

test('platform rail respects pin order, adds recent platforms, and stays bounded', () => {
  const items = Array.from({length: 61}, (_, i) => ({id: `platform${i}`, last_played_at: i}));
  assert.deepEqual(Array.from(Library.platforms(items, ['platform4', 'platform2'], 'platform0'), p => p.id),
    ['platform4', 'platform2', 'platform60', 'platform59', 'platform58', 'platform57', 'platform0']);
  assert.equal(Library.platforms(items, items.map(p => p.id), 'platform60').length, 7);
  assert.deepEqual(Array.from(Library.platforms(items, ['missing'], ''), p => p.id),
    ['platform60', 'platform59', 'platform58', 'platform57', 'platform56', 'platform55']);
});

test('new profiles get recognizable entry points without an endless platform rail', () => {
  const items = ['switch', 'nes', 'snes', 'gba', 'psp', 'fbneo', '3ds'].map(id => ({id}));
  assert.deepEqual(Array.from(Library.platforms(items, []), p => p.id), ['nes', 'snes', 'gba', 'psp', 'switch']);
  const settings = Library.settings({pinned_platforms:['switch', 'switch', '<script>', null, 'psp'], sort:'bogus', direction:'sideways'});
  assert.deepEqual(JSON.parse(JSON.stringify(settings)), {pinned_platforms:['switch','psp'],sort:'popularity',direction:'desc'});
});

test('external links are HTTPS-only; same-origin screenshots only use the exact image route', () => {
  for (const value of ['javascript:alert(1)', 'data:image/png;base64,foo', '//evil.example/x', '/api/settings', 'http://example.com', 'https://user:pass@example.com']) {
    assert.equal(Library.secureURL(value), ''); assert.equal(Library.imageURL(value), '');
  }
  assert.equal(Library.secureURL('https://example.com/game'), 'https://example.com/game');
  assert.equal(Library.imageURL('/p/player12/api/games/abc123/screenshots/2'), '/p/player12/api/games/abc123/screenshots/2');
  assert.equal(Library.imageURL('/api/games/abc123/screenshots/0?v=0123456789abcdef'), '/api/games/abc123/screenshots/0?v=0123456789abcdef');
  assert.equal(Library.imageURL('/api/games/abc123/screenshots/0'), '/api/games/abc123/screenshots/0');
  for (const value of ['/api/games/../screenshots/0', '/api/games/a/screenshots/0?redirect=x', '/p/player0/api/games/a/screenshots/0']) assert.equal(Library.imageURL(value), '');
});

test('recognizable platform silhouettes differ without incorporating untrusted input into SVG', () => {
  const ids = ['switch', 'psp', 'nes', 'snes', 'n64', 'psx', 'ps2', 'nds', 'gamecube', 'wii', 'dos', 'fbneo'];
  assert.equal(new Set(ids.map(Library.platformKind)).size, ids.length);
  assert.equal(Library.platformKind('gba'), 'psp');
  assert.equal(Library.platformKind('gb'), 'gameboy');
  assert.equal(Library.platformKind('psp'), 'psp');
  assert.equal(Library.platformArt('" onclick="alert(1)'), Library.platformArt('unknown'));
  assert.doesNotMatch(Library.platformArt('<img>'), /<img|onclick|script/);
});

function settingsHarness() {
  const pending = [], calls = [], notices = [];
  const ctx = vm.createContext({Library, URL});
  ctx.state = {profile:{id:'player1'},profileEpoch:1,librarySettingsRequest:0,librarySettings:Library.settings(),libraryFresh:true,librarySaving:false,sort:'default',direction:'desc'};
  ctx.$ = () => ({open:false}); ctx.renderLibrarySort = ctx.renderPlatforms = ctx.renderPlatformChooser = () => {};
  ctx.t = (s,p={}) => s.replace('{message}',p.message||''); ctx.errorMessage = e => e.message;
  ctx.interfaceError = message => new Error(message); ctx.notify = message => notices.push(message);
  ctx.api = (url,options={}) => { calls.push({url,options}); return new Promise((resolve,reject)=>pending.push({resolve,reject})); };
  for(const name of ['loadLibrarySettings','saveLibrarySettings']) {
    const match=source.match(new RegExp(`^  async function ${name}\\([\\s\\S]*?^  }`,'m'));
    assert.ok(match); vm.runInContext(match[0],ctx);
  }
  return {ctx,pending,calls,notices};
}

test('preference writes preserve ordered pins and commit only server-confirmed settings', async () => {
  const {ctx,pending,calls}=settingsHarness();
  ctx.state.librarySettings=Library.settings({pinned_platforms:['switch','nes']});
  const saving=ctx.saveLibrarySettings({sort:'name',direction:'asc'});
  assert.equal(ctx.state.librarySaving,true); assert.equal(ctx.state.sort,'default');
  assert.deepEqual(JSON.parse(calls[0].options.body),{pinned_platforms:['switch','nes'],sort:'name',direction:'asc'});
  pending[0].resolve({settings:JSON.parse(calls[0].options.body)});
  assert.equal(await saving,true); assert.equal(ctx.state.sort,'name'); assert.equal(ctx.state.librarySaving,false);
});

test('failed settings save leaves previous preferences intact and reports failure', async () => {
  const {ctx,pending,notices}=settingsHarness();
  const saving=ctx.saveLibrarySettings({pinned_platforms:['psp']});
  pending[0].reject(new Error('offline'));
  assert.equal(await saving,false); assert.equal(ctx.state.librarySettings.pinned_platforms.length,0);
  assert.equal(ctx.state.librarySaving,false); assert.match(notices[0],/offline/);
});

test('late player response cannot leak settings or unlock another player write', async () => {
  const {ctx,pending}=settingsHarness(); const saving=ctx.saveLibrarySettings({pinned_platforms:['psp']});
  ctx.state.profileEpoch++; ctx.state.librarySettingsRequest++; ctx.state.librarySaving=true;
  ctx.state.librarySettings=Library.settings({pinned_platforms:['snes']});
  pending[0].resolve({settings:{pinned_platforms:['psp'],sort:'name',direction:'asc'}});
  assert.equal(await saving,false); assert.equal(ctx.state.librarySettings.pinned_platforms[0],'snes'); assert.equal(ctx.state.librarySaving,true);
});

test('retry read followed by profile switch cannot submit old-player edits to new player', async () => {
  const {ctx,pending,calls}=settingsHarness(); ctx.state.libraryFresh=false;
  const saving=ctx.saveLibrarySettings({pinned_platforms:['psp']});
  ctx.state.profileEpoch++; ctx.state.librarySettingsRequest++; ctx.state.libraryFresh=true;
  pending[0].resolve({settings:{pinned_platforms:['switch'],sort:'default',direction:'desc'}});
  assert.equal(await saving,false); assert.equal(calls.length,1); assert.equal(calls[0].options.method,undefined);
});

test('detail neighbor requests carry the entire visible filter and sort context', () => {
  const ctx=vm.createContext({URLSearchParams,state:{platform:'switch',query:'mario',mode:'stream',reviewFilter:'pending',favorites:true,recent:true,sort:'play_count',direction:'asc'}});
  vm.runInContext(source.match(/^  function detailQuery\([\s\S]*?^  }/m)[0],ctx);
  const params=ctx.detailQuery();
  assert.deepEqual(Object.fromEntries(params),{direction:'asc',platform:'switch',q:'mario',mode:'stream',review_filter:'pending',favorite:'true',recent:'true',sort:'play_count'});
});
