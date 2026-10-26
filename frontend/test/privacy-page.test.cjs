const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

test('privacy notice is a public page with LinkedIn disclosure and deletion instructions', () => {
  const filename = path.resolve(__dirname, '../src/app/privacy/page.tsx');
  assert(fs.existsSync(filename), 'The public /privacy route must exist outside the authenticated app layout');
  const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    fileName: filename,
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true},
  }).outputText;
  const loaded = {exports: {}};
  new Function('require', 'module', 'exports', output)(require, loaded, loaded.exports);
  const html = renderToStaticMarkup(React.createElement(loaded.exports.default));
  assert.match(html, /<h1[^>]*>Privacy Policy<\/h1>/);
  assert.match(html, /PrimEx/);
  assert.match(html, /LinkedIn/);
  assert.match(html, /encrypted/i);
  assert.match(html, /explicit.*approv/i);
  assert.match(html, /deletion/i);
  assert.match(html, /Disconnect/);
  assert.match(html, /OpenAI/);
  assert.match(html, /Bright Data/);
  assert.match(html, /session cookie/i);
  assert.equal(loaded.exports.metadata.alternates.canonical, 'https://news.primexeu.com/privacy');
});
