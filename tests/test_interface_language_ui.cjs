const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function harness() {
  const calls = [], messages = [], stored = new Map(), pending = [];
  const context = vm.createContext({console}); context.window = context;
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/i18n.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/i18n-en.js'), 'utf8'), context);
  context.I18n = context.RetroI18n; context.t = context.I18n.t;
  context.state = {profile: {id:'player1'}, profileEpoch:1, interfaceRequest:0, interfaceSaving:false};
  context.document = {body:{},activeElement:{matches:()=>false},addEventListener(){},removeEventListener(){}};
  context.$ = () => ({open:false}); context.syncLanguagePickers = () => {};
  context.remember = (key,value) => stored.set(key,value);
  context.applyInterfaceLanguage = value => context.I18n.setLocale(value);
  context.notify = context.profileMessage = message => messages.push(message);
  context.api = (url, options={}) => { calls.push({url,options}); return new Promise((resolve,reject)=>pending.push({resolve,reject})); };
  context.diagnostic = message => context.I18n.locale()==='en' ? context.t('服务原始诊断：{message}',{message}) : message;
  for (const name of ['interfaceError','errorMessage','loadInterfaceSettings','changeInterfaceLanguage']) {
    const match = source.match(new RegExp(`^  (?:async )?function ${name}\\([^\\n]+}$`,'m')) || source.match(new RegExp(`^  (?:async )?function ${name}\\([\\s\\S]*?^  }`,'m'));
    assert.ok(match, `source function ${name}`); vm.runInContext(match[0],context);
  }
  return {context,calls,messages,stored,pending};
}
const response = locale => ({settings:{locale},supported_locales:['zh-CN','en']});

test('all literal dynamic interface messages and known label maps have English entries', () => {
  const {context} = harness(); context.I18n.setLocale('en');
  const messages = [...source.matchAll(/\b(?:t|interfaceError)\(("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g)].map(match=>vm.runInNewContext(match[1]));
  for(const match of source.matchAll(/localizedLabels\((\{[^\n]+\})\)/g)) messages.push(...Object.values(vm.runInNewContext(`(${match[1]})`)));
  const platformMap = source.match(/function platformName[\s\S]*?const labels = (\{[^\n]+\});/)[1];
  messages.push(...Object.values(vm.runInNewContext(`(${platformMap})`)));
  for (const message of new Set(messages.filter(value=>/\p{Script=Han}/u.test(value)))) assert.notEqual(context.I18n.t(message), message, message);
});

test('before selecting a player, language preference stays on this device', async () => {
  const {context,calls,stored} = harness(); context.state.profile=null;
  await context.changeInterfaceLanguage('en');
  assert.equal(context.I18n.getLocale(),'en'); assert.equal(stored.get('retroone:interface-locale'),'en'); assert.equal(calls.length,0);
});

test('a user selection wins over a slower initial settings GET', async () => {
  const {context,pending,calls} = harness();
  const read=context.loadInterfaceSettings(); const write=context.changeInterfaceLanguage('en');
  pending[1].resolve(response('en')); await write;
  pending[0].resolve(response('zh-CN')); await read;
  assert.equal(context.I18n.getLocale(),'en'); assert.equal(context.state.interfaceSaving,false);
  assert.deepEqual(calls.map(call=>call.url),['/api/settings/interface','/api/settings/interface']);
  assert.equal(calls[1].options.cancelOnProfileChange,false);
});

test('failed save rolls back with a visible message; application errors follow the restored language', async () => {
  const {context,pending,messages} = harness(); context.I18n.setLocale('en');
  const write=context.changeInterfaceLanguage('zh-CN');
  pending[0].reject(context.interfaceError('连接超时，请检查服务器后重试。')); await write;
  assert.equal(context.I18n.locale(),'en'); assert.equal(context.state.interfaceSaving,false);
  assert.match(messages[0],/timed out/i); assert.ok(!/\p{Script=Han}/u.test(messages[0]));
});

test('a previous player response cannot alter the current language or unlock a newer write', async () => {
  const {context,pending} = harness();
  const old=context.changeInterfaceLanguage('en');
  Object.assign(context.state,{profile:{id:'player2'},profileEpoch:2,interfaceRequest:context.state.interfaceRequest+1,interfaceSaving:false});
  context.I18n.setLocale('zh-CN'); const current=context.changeInterfaceLanguage('auto');
  pending[0].resolve(response('en')); await old;
  assert.equal(context.I18n.getLocale(),'auto'); assert.equal(context.state.interfaceSaving,true);
  pending[1].resolve(response('auto')); await current;
  assert.equal(context.state.interfaceSaving,false);
});

test('a previous player GET and repeated read during save cannot overwrite a new player', async () => {
  const {context,pending,calls} = harness(); const old=context.loadInterfaceSettings();
  context.state.profileEpoch++; context.state.interfaceRequest++; context.I18n.setLocale('en');
  pending[0].resolve(response('zh-CN')); await old; assert.equal(context.I18n.locale(),'en');
  const save=context.changeInterfaceLanguage('auto'); await context.loadInterfaceSettings();
  assert.equal(calls.length,2); pending[1].resolve(response('auto')); await save;
});
