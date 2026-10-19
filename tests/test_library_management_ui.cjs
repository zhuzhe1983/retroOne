const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = vm.createContext({});
context.window = context;
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/i18n.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/i18n-en.js'), 'utf8'), context);
vm.runInContext(`const I18n = window.RetroI18n; const t = (source, params = {}) => I18n.t(source, params); function localizedLabels(labels) { return new Proxy(labels, {get(target, key) { const value = Reflect.get(target,key); return typeof value === 'string' ? t(value) : value; }}); } function diagnostic(message) { return I18n.locale() === 'en' ? t('服务原始诊断：{message}', {message}) : message; }`, context);
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const functions = ['formatSize', 'managementJobText', 'managementProgressText'].map(name => source.match(new RegExp(`^  function ${name}\\([\\s\\S]*?^  }`, 'm'))[0]);
const number = source.match(/^  const number = .+;$/m)[0];
const ui = vm.runInContext([number, ...functions, '({managementJobText,managementProgressText})'].join('\n'), context);

test('management distinguishes paused work, failed work and completed checks', () => {
  const labels = {running:'正在核验内容', complete:'本轮内容核验完成', paused:'本批核验完成，可继续', failed:'内容核验未完成', interrupted:'核验已中断，可继续'};
  for (const [status, label] of Object.entries(labels)) assert.equal(ui.managementJobText({status}), label);
  assert.equal(ui.managementJobText(null), '尚未开始内容核验');
  assert.equal(ui.managementJobText({status:'toString'}), '核验状态待确认');
});

test('partial progress reports only known measured values without invented percentages', () => {
  assert.equal(ui.managementProgressText(null), '');
  assert.equal(ui.managementProgressText({phase:'hashing'}), '计算内容指纹');
  assert.equal(ui.managementProgressText({games_discovered:20,games_total:100,bytes_hashed:1048576}), '已识别 20 / 100 项 · 已核验 1 MB');
  assert.equal(ui.managementProgressText({games_total:100}), '');
  assert.equal(ui.managementProgressText({games_discovered:NaN,bytes_hashed:Infinity}), '');
});

test('arbitrary phase and backend implementation details are not displayed as progress text', () => {
  assert.equal(ui.managementProgressText({phase:'/private/location/<script>',current_file:'/private/a.rom'}), '');
  assert.equal(ui.managementProgressText({phase:'__proto__',games_discovered:0}), '已识别 0 项');
});
