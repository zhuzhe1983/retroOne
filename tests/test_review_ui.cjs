const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const names = ['validReviewFilter', 'reviewStatus', 'duplicateCount', 'reviewCardNote', 'variantTags', 'reviewDetails', 'launchConfigDetails'];
const constants = ['escapeHTML', 'number', 'reviewFilters', 'reviewStatuses', 'modeLabels'].map(name => source.match(new RegExp(`^  const ${name} = .+;$`, 'm'))[0]);
const functions = names.map(name => (source.match(new RegExp(`^  function ${name}\\(.+}$`, 'm')) || source.match(new RegExp(`^  function ${name}\\([\\s\\S]*?^  }`, 'm')))[0]);
const ui = vm.runInNewContext([...constants, ...functions, `({${names.join(',')}})`].join('\n'));

test('unknown and absent audit states stay unverified', () => {
  for (const game of [{}, {review: null}, {review: {}}, {review: {verification: {status: 'verified_playable'}}}]) {
    assert.equal(ui.reviewStatus(game), 'unverified');
    assert.match(ui.reviewDetails(game), />未检测</);
  }
});

test('file and observed statuses do not collapse into playable claims', () => {
  const cases = {fingerprinted: '已计算内容指纹', archive_verified: '文件已校验', dependency_error: '缺依赖', load_failed: '启动失败', boot_observed: '已观察启动画面', input_observed: '已观察输入响应', gameplay_observed: '已观察进入游戏', runtime_unavailable: '运行环境未配置', stale: '检测记录已过期'};
  for (const [status, label] of Object.entries(cases)) {
    const html = ui.reviewDetails({review: {verification: {status}}});
    assert.match(html, new RegExp(`>${label}</p>`));
    assert.match(html, /不代表已验证可玩/);
  }
});

test('review filter accepts only API contract values', () => {
  for (const value of ['', 'needs_attention', 'pending', 'observed']) assert.equal(ui.validReviewFilter(value), value);
  for (const value of [undefined, null, 'all', 'playable', '<script>', 'toString', '__proto__']) assert.equal(ui.validReviewFilter(value), '');
});

test('untrusted tags, evidence, notes, errors and duplicate titles remain inert text', () => {
  const payload = '<img src=x onerror="alert(1)">';
  const game = {id: 'a', review: {variant_tags: [{raw: payload, label: payload, source: payload}], verification: {notes: [payload], errors: [payload], engine: payload}, duplicates: {total_count: 2, members: [{id: 'a', title: payload}, {id: 'b" onclick="alert(1)', title: payload, collection: payload}]}}};
  const html = ui.variantTags(game) + ui.reviewDetails(game);
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('data-open-copy="b&quot; onclick=&quot;alert(1)"'));
  assert.match(html, /当前这份记录/);
  assert.match(html, /打开这份记录/);
});

test('duplicate totals reject malformed counts and distinguish current-inclusive totals', () => {
  for (const count of [-1, 0, 1.5, '3', Number.MAX_SAFE_INTEGER + 1]) assert.equal(ui.duplicateCount({review: {duplicates: {total_count: count}}}), null);
  const game = {review: {duplicates: {total_count: 3}}};
  assert.equal(ui.duplicateCount(game), 3);
  assert.match(ui.reviewCardNote(game), /另 2 份相同副本/);
});

test('partial fingerprint scope stays explicit, invalid SHA is not shown', () => {
  const html = ui.reviewDetails({review: {fingerprint: {sha256: 'a'.repeat(64), complete: false, scope: '主文件'}}});
  assert.match(html, /范围不完整/);
  assert.match(html, /主文件/);
  assert.ok(!ui.reviewDetails({review: {fingerprint: {sha256: '<script>'}}}).includes('review-evidence-toggle'));
});

test('optional launch configuration neither fabricates readiness nor implies observation', () => {
  assert.equal(ui.launchConfigDetails({}), '');
  assert.match(ui.launchConfigDetails({launch_config: {status: 'configured', engine: 'test'}}), /配置已生成/);
  assert.match(ui.launchConfigDetails({launch_config: {status: 'unknown'}}), /配置待检查/);
  const html = ui.launchConfigDetails({launch_config: {status: 'blocked', issues: ['<script>bad</script>']}});
  assert.match(html, /配置受阻/);
  assert.ok(!html.includes('<script>'));
  assert.match(html, /配置记录与实测结果分别记录/);
});
