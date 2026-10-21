const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('  async function requestGameLaunch(');
const end = source.indexOf('\n  async function launchGame(', start);
function harness(responses) {
  const calls = [];
  const context = vm.createContext({api: async (url, options) => {
    calls.push({url, body: JSON.parse(options.body)});
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return value;
  }});
  vm.runInContext(source.slice(start, end), context);
  return {calls, launch: context.requestGameLaunch};
}
const payload = {mode:'stream', device_id:'device', replace_session_id:'current-session', network_room_id:'room'};
const rejectedDevice = () => Object.assign(new Error('validation'), {status:422, detail:[{type:'extra_forbidden', loc:['body','device_id']}]});
test('current workers receive the device preference and are called once', async () => {
  const h = harness([{mode:'stream'}]);
  await h.launch('game', payload);
  assert.deepEqual(h.calls, [{url:'/api/games/game/launch', body:payload}]);
});
test('only rejected optional device preference is removed, retaining explicit replacement and network constraints', async () => {
  const h = harness([rejectedDevice(), {mode:'stream'}]);
  const result = await h.launch('game', payload);
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.calls[1].body, {mode:'stream', replace_session_id:'current-session', network_room_id:'room'});
  assert.equal(result.device_preferences_supported, false);
  assert.equal(payload.device_id, 'device');
});
test('timeouts, conflicts, invalid UUIDs and other or combined schema errors never replay a launch', async () => {
  for (const error of [new Error('timeout'), Object.assign(new Error('conflict'), {status:409}),
    Object.assign(rejectedDevice(), {detail:[{type:'uuid_parsing',loc:['body','device_id']}]}),
    Object.assign(rejectedDevice(), {detail:[{type:'extra_forbidden',loc:['body','replace_session_id']}]}),
    Object.assign(rejectedDevice(), {detail:[{type:'extra_forbidden',loc:['body','device_id']},{type:'extra_forbidden',loc:['body','network_room_id']}]})]) {
    const h = harness([error]);
    await assert.rejects(h.launch('game', payload));
    assert.equal(h.calls.length, 1);
  }
});
test('failed legacy retry propagates once rather than looping', async () => {
  const failure = Object.assign(new Error('conflict'), {status:409});
  const h = harness([rejectedDevice(), failure]);
  await assert.rejects(h.launch('game', payload), error => error === failure);
  assert.equal(h.calls.length, 2);
});
