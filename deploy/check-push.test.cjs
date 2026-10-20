const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const script = path.join(__dirname, 'check-push.cjs');
test('private evidence is excluded while application and example configuration remain committable', () => {
  const {deniedFiles} = require('./check-push.cjs');
  assert.deepEqual(deniedFiles(['backend/app/main.py','.env.example','docs/report.md','tmp/private.json','audit/raw.csv','frontend/.env']),
    ['tmp/private.json','audit/raw.csv','frontend/.env']);
});

test('dotenv variants, exported keys and private operations reports cannot be committed', () => {
  const {deniedFiles} = require('./check-push.cjs');
  assert.deepEqual(deniedFiles(['.env.example','frontend/.env.example','frontend/.env.local',
    '.env.production','deploy/private/key.pem','deploy/token.key','docs/brightdata-admin-handoff.md',
    'docs/linkedin-provider-fallback-analysis.md','docs/release-readiness.md']),[
    'frontend/.env.local','.env.production','deploy/private/key.pem','deploy/token.key',
    'docs/brightdata-admin-handoff.md','docs/linkedin-provider-fallback-analysis.md']);
});
test('development push allowed; protected branches, deletion and tags blocked', () => {
  for (const [ref, code] of [['refs/heads/fix/linkedin-release-readiness',0],['refs/heads/main',1],
    ['refs/heads/master',1],['refs/heads/release/paused',1],['refs/tags/v1',1]]) {
    const result = spawnSync(process.execPath,[script],{input:`refs/heads/local ${'a'.repeat(40)} ${ref} ${'0'.repeat(40)}\n`});
    assert.equal(result.status,code,ref);
  }
});

test('explicit main approval is bound to both exact commit and remote base', () => {
  const {deniedRefs} = require('./check-push.cjs');
  const sha = 'a'.repeat(40), base = 'b'.repeat(40);
  const input = `refs/heads/reviewed ${sha} refs/heads/main ${base}\n`;
  assert.deepEqual(deniedRefs(input, {sha, base}), []);
  for (const approval of [{sha}, {base}, {sha: 'c'.repeat(40), base}, {sha, base: 'c'.repeat(40)}]) {
    assert.deepEqual(deniedRefs(input, approval), ['refs/heads/main']);
  }
  assert.deepEqual(deniedRefs(`(delete) ${'0'.repeat(40)} refs/heads/main ${base}\n`, {sha, base}), ['refs/heads/main']);
  assert.deepEqual(deniedRefs(`refs/heads/x ${sha} refs/tags/v1 ${base}\n`, {sha, base}), ['refs/tags/v1']);
});

test('matching main approval still refuses a non-fast-forward history', () => {
  const result = spawnSync(process.execPath, [script], {
    input: `refs/heads/reviewed ${'a'.repeat(40)} refs/heads/main ${'b'.repeat(40)}\n`,
    env: {...process.env, NEWS_APPROVED_MAIN_SHA: 'a'.repeat(40), NEWS_APPROVED_MAIN_BASE: 'b'.repeat(40)},
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr.toString(), /fast-forward/);
});
