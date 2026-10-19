const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');

function catalog(languages = ['zh-CN'], document) {
  const context = vm.createContext({navigator: {languages}, document});
  context.window = context;
  vm.runInContext(read('web/i18n.js'), context);
  vm.runInContext(read('web/i18n-en.js'), context);
  return context.RetroI18n;
}

test('explicit and browser languages have a deterministic fallback', () => {
  const i18n = catalog(['fr-FR', 'en-GB', 'zh-CN']);
  assert.equal(i18n.locale(), 'zh-CN');
  assert.equal(i18n.setLocale('auto'), 'en');
  assert.equal(i18n.getLocale(), 'auto');
  assert.equal(i18n.setLocale('zh-CN'), 'zh-CN');
  assert.equal(i18n.setLocale('__proto__'), 'zh-CN');
  assert.equal(catalog(['zh-TW', 'en']).setLocale('auto'), 'zh-CN');
  assert.equal(catalog(['de-DE']).setLocale('auto'), 'zh-CN');
});

test('translation preserves unknown metadata and interpolates once without interpreting markup', () => {
  const i18n = catalog();
  i18n.add('en', {'你好，{name}': 'Hello, {name}', '__proto__': 'ignored object prototype'});
  i18n.setLocale('en');
  assert.equal(i18n.t('你好，{name}', {name: '<img src=x>{other}'}), 'Hello, <img src=x>{other}');
  assert.equal(i18n.t('原游戏名称'), '原游戏名称');
  assert.equal(i18n.t('constructor'), 'constructor');
  assert.equal(i18n.t('__proto__'), '__proto__');
  assert.throws(() => i18n.add('en', {invalid: {text: 'not a string'}}));
  assert.throws(() => i18n.add('fr', {}));
});

test('DOM translation only touches explicit UI markers and leaves game elements alone', () => {
  const element = attrs => ({attrs: {...attrs}, textContent: '', hasAttribute(name) { return Object.hasOwn(this.attrs, name); }, getAttribute(name) { return this.attrs[name] ?? null; }, setAttribute(name, value) { this.attrs[name] = value; }});
  const button = element({'data-i18n': '返回游戏库', 'data-i18n-title': '返回游戏库'});
  const game = element({}); game.textContent = '原游戏名称';
  const picker = {value: 'zh-CN'};
  const document = {documentElement: {}, querySelectorAll(selector) { return selector === '[data-language-picker]' ? [picker] : [button]; }};
  const i18n = catalog(['en'], document);
  i18n.setLocale('en');
  assert.equal(button.textContent, 'Back to library');
  assert.equal(button.attrs.title, 'Back to library');
  assert.equal(game.textContent, '原游戏名称');
  assert.equal(document.documentElement.lang, 'en');
  assert.equal(picker.value, 'en');
  i18n.setLocale('zh-CN');
  assert.equal(button.textContent, '返回游戏库');
});

test('all static Chinese UI markers have English translations', () => {
  const i18n = catalog(); i18n.setLocale('en');
  const markers = [...read('web/index.html').matchAll(/data-i18n(?:-(?:aria-label|aria-description|title|placeholder|content))?="([^"]+)"/g)];
  assert.ok(markers.length > 50, 'check actual interface markup');
  for (const [, encoded] of markers) {
    const key = encoded.replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&#x27;', "'").replaceAll('&lt;', '<').replaceAll('&gt;', '>');
    assert.ok(!/[\u3400-\u9fff]/u.test(i18n.t(key)), `Missing English translation: ${key}`);
  }
});

test('number and date formatting follow the effective locale', () => {
  const i18n = catalog(); i18n.setLocale('en');
  assert.equal(i18n.number(12345), '12,345');
  assert.equal(i18n.date('invalid'), '—');
  assert.match(i18n.date('2026-10-19T12:00:00Z', {month: 'long', day: 'numeric', timeZone: 'UTC'}), /October 19/);
});

test('English catalog keeps every interpolation parameter', () => {
  const catalogs = [];
  vm.runInNewContext(read('web/i18n-en.js'), {window: {RetroI18n: {add: (language, messages) => catalogs.push(messages)}}});
  const parameters = text => [...new Set([...text.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)].map(match => match[1]))].sort();
  for (const messages of catalogs) for (const [source, translation] of Object.entries(messages)) {
    assert.deepEqual(parameters(translation), parameters(source), `Translation changes parameters: ${source}`);
  }
});
