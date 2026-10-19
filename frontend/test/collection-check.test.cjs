const test = require('node:test');
const assert = require('node:assert/strict');
require.extensions['.ts'] = (mod, filename) => mod._compile(require('typescript').transpileModule(
  require('node:fs').readFileSync(filename, 'utf8'), {compilerOptions: {module: 1, target: 7}}).outputText, filename);
const {startCollectionCheck, loadCollectionStatus} = require('../src/features/intelligence/collection-check.ts');

test('check starts one durable phase and repeated acceptance keeps its identity', async () => {
  const requests = [];
  const apiFetch = async (path, init) => {
    requests.push({path, method: init?.method || 'GET'});
    return new Response(JSON.stringify({state: 'running', cycle_id: 'existing-phase'}));
  };
  const result = await startCollectionCheck(apiFetch);
  assert.deepEqual(requests, [{path: '/intelligence/sources/check-all', method: 'POST'}]);
  assert.equal(result.cycle_id, 'existing-phase');
  assert.equal((await loadCollectionStatus(apiFetch)).cycle_id, 'existing-phase');
  assert.equal(requests[1].method, 'GET');
});

test('unauthorized and paused responses surface the server explanation', async () => {
  for (const status of [403, 409]) {
    await assert.rejects(startCollectionCheck(async () => new Response(
      JSON.stringify({detail: 'Source collection is paused.'}), {status})), /Source collection is paused/);
  }
});
