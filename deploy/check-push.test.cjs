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
