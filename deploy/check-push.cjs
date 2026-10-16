// Invoked by the optional repository-local pre-push hook. No network or Git writes.
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
function deniedRefs(input) {
  return input.trim().split(/\r?\n/).filter(Boolean).map(line => line.split(/\s+/)[2])
    .filter(ref => !ref || ref === 'refs/heads/main' || ref === 'refs/heads/master' ||
      ref.startsWith('refs/heads/release/') || ref.startsWith('refs/tags/'));
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
  const denied = deniedRefs(fs.readFileSync(0, 'utf8'));
  if (denied.length) {
    console.error('Push blocked: protected production refs/tags require a separately approved release. Use a development branch.');
    process.exitCode = 1;
  }
}
