const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const {JSDOM} = require('jsdom');

const dom = new JSDOM('<!doctype html><html><body></body></html>', {url: 'http://localhost/'});
global.self = dom.window;
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'Element', 'Node', 'NodeFilter',
  'DocumentFragment', 'MutationObserver', 'CustomEvent', 'Event', 'MouseEvent', 'KeyboardEvent']) {
  global[key] = dom.window[key];
}
global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
global.requestAnimationFrame = (callback) => setTimeout(callback, 0);
global.cancelAnimationFrame = clearTimeout;
dom.window.matchMedia = () => ({matches: false, addEventListener() {}, removeEventListener() {}});
global.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLElement.prototype.scrollIntoView = function() {};
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (request.startsWith('@/')) request = path.resolve(__dirname, '../src', request.slice(2));
  return originalResolve.call(this, request, parent, ...rest);
};
for (const extension of ['.ts', '.tsx']) {
  Module._extensions[extension] = (mod, filename) => mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true}, fileName: filename,
  }).outputText, filename);
}
const React = require('react');
const {act} = React;
const {createRoot} = require('react-dom/client');
const {Toaster} = require('sonner');
const {ConfirmDialogProvider, useConfirm} = require('../src/components/providers/confirm-dialog-provider.tsx');
const {SourcesAdminWorkspace} = require('../src/features/intelligence/components/sources-admin-page.tsx');

function button(label) {
  const result = [...document.querySelectorAll('button')].find(el => el.textContent === label || el.getAttribute('aria-label') === label);
  assert(result, `Button ${label} is visible`);
  return result;
}
async function click(element) {
  await act(async () => { element.click(); await new Promise(resolve => setTimeout(resolve, 20)); });
}
async function visibleMessage(message) {
  for (let attempt = 0; attempt < 20 && !document.body.textContent.includes(message); attempt++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  }
  assert(document.body.textContent.includes(message), `Message is visible: ${message}`);
}
async function setup(kind = 'WEBSITE', deleteResponse = () => new Response(null, {status: 204})) {
  let sources = [{id: 'source-1', name: 'Example source', type: kind, url: 'https://example.org/',
    status: 'ACTIVE', priority: 'NORMAL', categories: [], email_enabled: false, collection_supported: false}];
  const deletes = [];
  const apiFetch = async (url, init) => {
    if (init?.method === 'DELETE') {
      deletes.push(url);
      const response = await deleteResponse();
      if (response.ok) sources = [];
      return response;
    }
    return Response.json(url === '/intelligence/sources' ? sources : {linkedinConfigured: true});
  };
  function Workspace() {
    const confirm = useConfirm();
    return React.createElement(SourcesAdminWorkspace, {user: {role: 'ADMIN'}, apiFetch, confirm});
  }
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(React.createElement(ConfirmDialogProvider, null,
    React.createElement(Workspace), React.createElement(Toaster))));
  await act(async () => {
    button('Actions for Example source').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  });
  const item = [...document.querySelectorAll('[role="menuitem"]')].find(el => el.textContent === 'Delete');
  assert(item, 'Delete menu item is available');
  await click(item);
  assert(document.querySelector('[role="dialog"]'), 'Delete opens a confirmation dialog');
  return {deletes, async close() {
    await act(async () => root.unmount());
    container.remove();
    document.body.innerHTML = '';
  }};
}

for (const kind of ['WEBSITE', 'LINKEDIN', 'FACEBOOK']) {
  test(`confirming Delete removes a ${kind} source and updates the list`, async () => {
    const view = await setup(kind);
    try {
      assert.match(document.querySelector('[role="dialog"]').textContent, /permanently remove the source/i);
      await click(button('Delete source'));
      assert.deepEqual(view.deletes, ['/intelligence/sources/source-1']);
      assert(!document.body.textContent.includes('Example source'));
      assert(document.body.textContent.includes('No sources added yet.'));
    } finally { await view.close(); }
  });
}

test('cancelling deletion keeps the source without sending DELETE', async () => {
  const view = await setup();
  try {
    await click(button('Cancel'));
    assert.deepEqual(view.deletes, []);
    assert(document.body.textContent.includes('Example source'));
    assert(!document.querySelector('[role="dialog"]'));
    assert.notEqual(document.body.style.pointerEvents, 'none');
  } finally { await view.close(); }
});

test('server rejection is visible and keeps the source', async () => {
  const view = await setup('LINKEDIN', () => Response.json({detail: 'Wait for the pending collection.'}, {status: 409}));
  try {
    await click(button('Delete source'));
    assert(document.body.textContent.includes('Example source'));
    await visibleMessage('Wait for the pending collection.');
  } finally { await view.close(); }
});

test('connection failure is visible and keeps the source for retry', async () => {
  const view = await setup('WEBSITE', () => { throw new TypeError('Failed to fetch'); });
  try {
    await click(button('Delete source'));
    assert(document.body.textContent.includes('Example source'));
    await visibleMessage('Could not delete source. Please try again.');
    assert(!button('Actions for Example source').disabled);
  } finally { await view.close(); }
});
