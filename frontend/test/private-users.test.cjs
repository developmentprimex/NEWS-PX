/* eslint-disable @typescript-eslint/no-require-imports -- Node's test runner loads CommonJS fixtures. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {url: 'http://localhost/'});
global.self = dom.window;
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'Element', 'Node', 'Event', 'MouseEvent']) global[key] = dom.window[key];
global.IS_REACT_ACT_ENVIRONMENT = true;
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (request.startsWith('@/')) request = path.resolve(__dirname, '../src', request.slice(2));
  return originalResolve.call(this, request, parent, ...rest);
};
for (const extension of ['.ts', '.tsx']) Module._extensions[extension] = (mod, filename) => mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true}, fileName: filename,
}).outputText, filename);
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');

test('new private workspace stays empty instead of showing example news', async () => {
  const {intelligenceFeedService} = require('../src/features/intelligence/data/news.service.ts');
  const feed = await intelligenceFeedService.getFeed(async () => Response.json({items: [], hasLiveSources: false, demoAllowed: false, emailConfigured: true, emailRecipient: 'member@example.com', aiConfigured: false}));
  assert.deepEqual(feed.items, []);
  assert.equal(feed.isDemo, false);
  assert.equal(feed.emailRecipient, 'member@example.com');
});

test('administrator can create a member and sees the new account without storing password in the list', async () => {
  const {UsersWorkspace} = require('../src/features/users/users-page.tsx');
  const requests = [];
  let users = [{id: 'admin', email: 'admin@example.com', role: 'ADMIN', active: true}];
  const apiFetch = async (url, init) => {
    requests.push({url, init});
    if (init?.method === 'POST') {
      const payload = JSON.parse(init.body);
      users = [...users, {id: 'member', email: payload.email, role: 'MEMBER', active: true}];
      return Response.json(users[1], {status: 201});
    }
    return Response.json(users);
  };
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  await act(async () => { root.render(React.createElement(UsersWorkspace, {user: {role: 'ADMIN'}, apiFetch})); });
  for (const [label, value] of [['Email', 'member@example.com'], ['Password', 'member-password-123']]) {
    const input = host.querySelector(`[aria-label="${label}"]`);
    assert(input);
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
      input.dispatchEvent(new window.Event('input', {bubbles: true}));
    });
  }
  await act(async () => { host.querySelector('form').dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true})); });
  assert(host.textContent.includes('member@example.com'));
  assert.equal(requests.find(r => r.init?.method === 'POST').url, '/auth/users');
  assert.equal(host.querySelector('[aria-label="Password"]').value, '');
  assert(!host.textContent.includes('member-password-123'));
  await act(async () => root.unmount()); host.remove();
});

test('member can open personal Sources without requesting administrator billing data', async () => {
  const {SourcesAdminWorkspace} = require('../src/features/intelligence/components/sources-admin-page.tsx');
  const requests = [];
  const apiFetch = async url => {
    requests.push(url);
    return Response.json(url.endsWith('/sources') ? [] : {linkedinConfigured: true});
  };
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  await act(async () => { root.render(React.createElement(SourcesAdminWorkspace, {user: {role: 'MEMBER'}, apiFetch, confirm: async () => false})); });
  assert(host.textContent.includes('No sources added yet.'));
  assert(host.textContent.includes('Add Source'));
  assert.deepEqual(requests.sort(), ['/intelligence/sources', '/intelligence/status']);
  await act(async () => root.unmount()); host.remove();
});
