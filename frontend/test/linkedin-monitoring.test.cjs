const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const Module = require('node:module');
require.extensions['.ts'] = (mod, filename) => mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText, filename);
const {eligibleLinkedInSources, selectLinkedInSources, sourceFilterKey, feedScopes} = require('../src/features/intelligence/linkedin-monitoring.ts');
const source = (id, url, status='ACTIVE') => ({id, url, type:'LINKEDIN', name:id, status});
test('all eligible personal and company sources appear, including retained paused history', () => {
  const sources = [source('new-person','https://www.linkedin.com/in/new-person/'),
    source('new-company','https://linkedin.com/company/new-company/?trk=x', 'PAUSED'),
    source('bad','https://evil.test/company/example/'),
    {...source('rss','https://linkedin.com/in/rss/'),type:'RSS'}];
  assert.deepEqual(eligibleLinkedInSources(sources).map(s=>s.id), ['new-person','new-company']);
  assert.deepEqual(selectLinkedInSources(sources,'all').map(s=>s.id), ['new-person','new-company']);
  assert.equal(sourceFilterKey(sources[0]),'new-person');
});
test('legacy company bookmarks and arbitrary source selection remain available', () => {
  const sources=[source('id-1','https://linkedin.com/company/primexeu/'),source('id-2','https://linkedin.com/in/new/')];
  assert.deepEqual(selectLinkedInSources(sources,'primex').map(s=>s.id),['id-1']);
  assert.deepEqual(selectLinkedInSources(sources,'id-2').map(s=>s.id),['id-2']);
  assert.deepEqual(selectLinkedInSources(sources,'deleted'),[]);
});
test('large feed selections respect API source list bounds and include reposts for companies', () => {
  const sources=Array.from({length:51},(_,i)=>source(String(i),`https://linkedin.com/company/source-${i}/`));
  const scopes=feedScopes(sources,'REPOST');
  assert.deepEqual(scopes.map(s=>s.sourceIds.length),[20,20,11]);
  assert(scopes.every(s=>s.activityKind==='REPOST'));
  assert.deepEqual(feedScopes(sources.slice(0,1),'ALL'),[{sourceIds:['0'],activityKind:undefined}]);
});
