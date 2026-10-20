const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
require.extensions['.ts'] = (mod, filename) => mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText, filename);
const {sourceCollectionStatus} = require('../src/features/intelligence/source-status.ts');
const source = (overrides = {}) => ({type: 'LINKEDIN', status: 'ACTIVE', collection_supported: true,
  last_error: null, pending_snapshot_id: null, last_checked_at: '2026-10-19T12:00:00Z', ...overrides});

test('daily and monthly allowance deferrals are waiting states, not failed checks', () => {
  for (const error of ['Daily record target reached.', 'Operational monthly record budget reached.']) {
    const status = sourceCollectionStatus(source({last_error: error}));
    assert.equal(status.tone, 'waiting');
    assert.match(status.message, /allowance/i);
    assert.doesNotMatch(status.label, /failed/i);
  }
  assert.equal(sourceCollectionStatus(source({last_error: 'Daily record target reached.'})).label, 'Daily allowance reached');
});

test('website failures identify the action needed without dumping HTTP exception details', () => {
  for (const [code, label] of [[403, 'Access blocked'], [500, 'Website unavailable'], [526, 'Website certificate error']]) {
    const status = sourceCollectionStatus(source({type: 'WEBSITE', last_error:
      `Client error '${code} Error' for url 'https://example.org/' For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/${code}`}));
    assert.equal(status.label, label);
    assert.equal(status.tone, 'error');
    assert.doesNotMatch(status.message, /developer.mozilla.org|Client error/);
    assert.match(status.message, new RegExp(String(code)));
  }
});

test('an in-flight or manually paused source takes precedence over an old allowance error', () => {
  assert.equal(sourceCollectionStatus(source({pending_snapshot_id: 'snapshot', last_error: 'Daily record target reached.'})).label, 'Checking');
  assert.equal(sourceCollectionStatus(source({status: 'PAUSED', last_error: 'Daily record target reached.'})).label, 'Paused');
});

test('ordinary failures and connector readiness remain visible', () => {
  assert.equal(sourceCollectionStatus(source({last_error: 'Provider response was invalid.'})).label, 'Check failed');
  assert.equal(sourceCollectionStatus(source()).label, 'Monitoring');
  assert.equal(sourceCollectionStatus(source({last_checked_at: null})).label, 'Ready');
  assert.equal(sourceCollectionStatus(source(), false).label, 'Setup needed');
  assert.equal(sourceCollectionStatus(source({type: 'FACEBOOK', collection_supported: false})).label, 'No connector');
  assert.equal(sourceCollectionStatus(source({type: 'FACEBOOK', collection_note: 'Outside approved pilot'})).label, 'Setup needed');
});
