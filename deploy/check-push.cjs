// Invoked by the optional repository-local pre-push hook. No network or Git writes.
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
function deniedRefs(input, approval = {}) {
  return input.trim().split(/\r?\n/).filter(Boolean).map(line => line.split(/\s+/))
    .filter(([localRef, sha, ref, base]) => {
      const approvedMain = ref === 'refs/heads/main' && /^[a-f0-9]{40}$/.test(approval.sha || '') &&
        /^[a-f0-9]{40}$/.test(approval.base || '') && !/^0+$/.test(base) && !/^0+$/.test(sha) &&
        sha === approval.sha && base === approval.base;
      return !ref || localRef === '(delete)' || /^0+$/.test(sha) ||
        (ref === 'refs/heads/main' && !approvedMain) || ref === 'refs/heads/master' ||
        ref.startsWith('refs/heads/release/') || ref.startsWith('refs/tags/');
    }).map(parts => parts[2]);
}
function deniedFiles(files) {
  return files.filter(name => {
    const file = name.replaceAll('\\', '/').toLowerCase();
    const dotenv = /(^|\/)\.env(?:\..*)?$/.test(file) && !/(^|\/)\.env\.example$/.test(file);
    return file.startsWith('tmp/') || file.startsWith('audit/') || dotenv ||
      /(^|\/)private\//.test(file) || /\.(pem|key|pfx|p12|dump|backup)$/.test(file) ||
      /^docs\/brightdata-.*\.md$/.test(file) || file === 'docs/linkedin-provider-fallback-analysis.md';
  });
}
module.exports = {deniedRefs, deniedFiles};
if (require.main === module) {
  if (process.argv.includes('--staged')) {
    const files = execFileSync('git',['diff','--cached','--name-only','--diff-filter=ACMR','-z'],{encoding:'utf8'}).split('\0').filter(Boolean);
    if (deniedFiles(files).length) {
      console.error('Commit blocked: private evidence, environment, key exports or operational reports are staged. Preserve them locally and select only reviewed release files.');
      process.exitCode = 1;
    }
    return;
  }
  const input = fs.readFileSync(0, 'utf8');
  // Set only for an explicitly user-authorized main push, bound to both reviewed
  // local commit and freshly fetched remote base. No persistent blanket bypass.
  const approval = {sha: process.env.NEWS_APPROVED_MAIN_SHA, base: process.env.NEWS_APPROVED_MAIN_BASE};
  const denied = deniedRefs(input, approval);
  if (denied.length) {
    console.error('Push blocked: protected production refs/tags require a separately approved release. Use a development branch.');
    process.exitCode = 1;
  } else if (input.split(/\r?\n/).some(line => line.split(/\s+/)[2] === 'refs/heads/main')) {
    try {
      execFileSync('git', ['merge-base', '--is-ancestor', approval.base, approval.sha], {stdio: 'ignore'});
    } catch {
      console.error('Push blocked: approved main update must be a fast-forward of the exact remote base.');
      process.exitCode = 1;
    }
  }
}
