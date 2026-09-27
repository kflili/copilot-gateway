// Run: node --test tests/test_demo_account_ui.cjs
// Executes the entire inline demo.html script against a small fake DOM and a
// controllable fetch, so async ordering (A18) and gating are deterministic.
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

const html = readFileSync(join(__dirname, '..', 'demo.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];

class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach(x => this.set.add(x)); }
  remove(...c) { c.forEach(x => this.set.delete(x)); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : Boolean(force);
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
  contains(c) { return this.set.has(c); }
}

function makeDom() {
  const doc = { activeElement: null, listeners: {}, visibilityState: 'visible' };
  class El {
    constructor(tag = 'div', id = null) {
      Object.assign(this, { tagName: tag, id, children: [], _text: '', style: {}, dataset: {},
        attrs: {}, classList: new ClassList(), hidden: false, disabled: false, value: '',
        listeners: {}, parentElement: null, scrollTop: 0, scrollHeight: 0, _q: {} });
    }
    set className(v) { this.classList = new ClassList(); String(v).split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); }
    get className() { return [...this.classList.set].join(' '); }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set textContent(v) { this._text = String(v); this.children = []; }
    set innerHTML(v) { this._text = ''; this.children = []; this._q = {}; }
    get innerHTML() { return ''; }
    appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
    append(...c) { c.forEach(x => this.appendChild(x)); }
    replaceChildren(...c) { this.children = []; c.forEach(x => this.appendChild(x)); }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    removeAttribute(k) { delete this.attrs[k]; }
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
    querySelector(sel) {
      if (sel === '.meta') return this.children.find(c => c.classList.contains('meta')) || null;
      if (!this._q[sel]) { this._q[sel] = new El('div'); this.appendChild(this._q[sel]); }
      return this._q[sel];
    }
    querySelectorAll() { return []; }
    focus() { doc.activeElement = this; }
    contains() { return true; }
    getClientRects() { return this.hidden ? [] : [1]; }
  }
  const els = new Map();
  Object.assign(doc, {
    getElementById: id => { if (!els.has(id)) els.set(id, new El('div', id)); return els.get(id); },
    createElement: tag => new El(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: (t, f) => { (doc.listeners[t] ||= []).push(f); },
    body: new El('body'),
    documentElement: new El('html'),
  });
  return { doc, El };
}

function jsonResponse(status, data, headers = {}) {
  const h = { 'content-type': 'application/json', ...headers };
  const resp = {
    ok: status >= 200 && status < 300, status,
    headers: { get: k => h[k.toLowerCase()] || null },
    json: async () => data,
    clone() { return resp; },
  };
  return resp;
}

function setup() {
  const { doc } = makeDom();
  const requests = [];
  const timers = [];
  const eventSources = [];
  const fetch = (url, opts = {}) => new Promise((resolve, reject) => {
    const req = { url, opts, resolve, reject, honorAbort: true };
    requests.push(req);
    opts.signal?.addEventListener('abort', () => {
      if (req.honorAbort) reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    });
    if (url === '/api/session') resolve(jsonResponse(200, { csrf: 'csrf-1' }));
    else if (url.startsWith('/api/gateway/')) resolve(jsonResponse(502, { error: 'down' }));
  });
  class FakeEventSource {
    constructor(url) { this.url = url; this.listeners = {}; eventSources.push(this); }
    addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
    emit(t, data) { (this.listeners[t] || []).forEach(f => f({ data: JSON.stringify(data) })); }
  }
  const window = { matchMedia: () => ({ matches: true, addEventListener() {} }), addEventListener() {} };
  const ctx = vm.createContext({
    window, document: doc, fetch, EventSource: FakeEventSource,
    localStorage: { getItem: () => null, setItem() {} },
    navigator: { clipboard: { writeText: async () => {} } },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    performance, AbortController, TextDecoder, console, Date, URL,
  });
  vm.runInContext(script, ctx);
  const run = code => vm.runInContext(code, ctx);
  return { ctx, doc, requests, timers, eventSources, run, $: id => doc.getElementById(id) };
}

const tick = () => new Promise(r => setImmediate(r));
const model = id => ({ id, vendor: 'Anthropic', endpoints: ['/v1/messages'], billing: null });
function modelsPayload(mode, generation, ids, status = 'ready', login = 'alice') {
  return {
    mode, generation, api_base: 'https://api.githubcopilot.com',
    integration_id: mode === 'vscode' ? 'vscode-chat' : 'copilot-developer-cli',
    catalog_status: status !== 'ready' ? 'unavailable' : (ids.length ? 'ready' : 'empty'),
    auth: {
      mode, generation, status, source: 'gh_account', source_label: 'gh account (selected in demo)',
      account: status === 'signed_out' ? null : { host: 'github.com', login, user_id: 1 },
      integration_id: mode === 'vscode' ? 'vscode-chat' : 'copilot-developer-cli',
      error: status === 'ready' ? null : { category: status, message: `${status} reason` },
    },
    models: ids.map(model), listed_only: [],
  };
}
function streamResponse() {
  const queued = [];
  let waiter = null;
  const reader = { read: () => new Promise(r => { if (queued.length) r(queued.shift()); else waiter = r; }) };
  const push = v => {
    const item = v === null ? { done: true } : { done: false, value: new TextEncoder().encode(v) };
    if (waiter) { const w = waiter; waiter = null; w(item); } else queued.push(item);
  };
  const resp = { ok: true, status: 200, body: { getReader: () => reader }, clone() { return resp; },
    headers: { get: k => (k.toLowerCase() === 'content-type' ? 'text/event-stream' : null) } };
  return { resp, push };
}
async function ticks(n = 6) { for (let i = 0; i < n; i++) await tick(); }
const modelRequests = t => t.requests.filter(r => r.url.startsWith('/api/models'));
const optionIds = t => t.$('model-select').children.flatMap(g => g.children.map(o => o.value));

test('A18: only the latest mode/generation request updates the UI (aborted or not)', async () => {
  for (const honorAbort of [true, false]) {
    const t = setup();
    t.run("setMode('cli')");
    t.run("setMode('vscode')");
    const [r1, r2, r3] = modelRequests(t);
    r1.honorAbort = honorAbort;
    r2.honorAbort = honorAbort;
    assert.equal(r2.url, '/api/models?mode=cli');
    r3.resolve(jsonResponse(200, modelsPayload('vscode', 'g3', ['claude-new'])));
    await tick(); await tick();
    r2.resolve(jsonResponse(200, modelsPayload('cli', 'g2', ['cli-model'], 'ready', 'bob')));
    r1.reject(new Error('late failure'));
    await tick(); await tick();
    assert.deepEqual(optionIds(t), ['claude-new']);
    assert.equal(t.run('currentGeneration'), 'g3');
    assert.equal(t.$('mib-account').textContent, '@alice (github.com)');
    assert.equal(t.$('auth-banner').hidden, true, 'stale failure must not show an error');
    assert.equal(t.run('authStates.cli'), null, 'stale cli response discarded');
  }
});

test('A01/A14: signed-out and empty catalogs are explicit and disable send', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', [], 'signed_out')));
  await tick(); await tick();
  assert.equal(t.$('model-select').children[0].textContent, 'Sign in to load models');
  assert.equal(t.$('auth-banner').hidden, false);
  assert.match(t.$('auth-banner-text').textContent, /No account for VS Code mode/);
  assert.equal(t.$('account-button-text').textContent, 'Sign in');
  assert.equal(t.$('send-btn').disabled, true);

  t.run('loadModels()');
  modelRequests(t)[1].resolve(jsonResponse(200, modelsPayload('vscode', 'g2', [])));
  await tick(); await tick();
  assert.equal(t.$('model-select').children[0].textContent, 'No callable models');
  assert.match(t.$('auth-banner-text').textContent, /no callable models/);
  assert.equal(t.$('send-btn').disabled, true);

  t.run('loadModels()');
  modelRequests(t)[2].resolve(jsonResponse(200, modelsPayload('vscode', 'g3', ['m1'])));
  await tick(); await tick();
  assert.equal(t.$('send-btn').disabled, false);
  assert.equal(t.$('auth-banner').hidden, true);
  assert.equal(t.$('account-dot').className, 'account-dot ready');
});

test('A13: account error state shows the reason, not a silent empty list', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', [], 'error')));
  await tick(); await tick();
  assert.match(t.$('auth-banner-text').textContent, /models unavailable: error reason/);
  assert.equal(t.$('auth-banner').classList.contains('error'), true);
  assert.equal(t.$('send-btn').disabled, true);
  t.run('loadModels()');
  modelRequests(t)[1].resolve(jsonResponse(403, { error: { category: 'loopback_only', message: 'only on this computer' } }));
  await tick(); await tick();
  assert.equal(t.$('auth-banner-text').textContent, 'only on this computer');
});

test('A20/A21: chat carries the generation; stale rejection is shown and not replayed', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', ['m1'])));
  await tick(); await tick();
  t.$('chat-input').value = 'hello';
  const sending = t.run('sendMessage()');
  await tick(); await tick();
  const chatReq = t.requests.find(r => r.url === '/api/chat');
  const body = JSON.parse(chatReq.opts.body);
  assert.deepEqual({ mode: body.mode, generation: body.generation, model: body.model },
    { mode: 'vscode', generation: 'g1', model: 'm1' });
  assert.equal(chatReq.opts.headers['X-Demo-CSRF'], 'csrf-1');
  chatReq.resolve(jsonResponse(409, { error: { category: 'stale_generation', message: 'changed', generation: 'g2' } }));
  await sending;
  const chat = t.$('chat-messages');
  const assistant = chat.children[chat.children.length - 1];
  assert.match(assistant.textContent, /account for this mode changed/);
  assert.equal(assistant.classList.contains('errored'), true);
  assert.equal(t.run('messages.length'), 0, 'failed turn not kept for replay');
  assert.equal(modelRequests(t).length, 2, 'stale rejection refreshes models');
});

test('A21: switching account resets the conversation and discards in-flight output', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', ['m1'])));
  await tick(); await tick();
  t.run("messages = [{ role: 'user', content: 'old' }, { role: 'assistant', content: 'reply' }]");
  const controller = new AbortController();
  t.ctx.fakeChat = { controller, mode: 'vscode', generation: 'g1', discarded: false };
  t.run('activeChat = fakeChat');
  t.run('loadModels()');
  modelRequests(t)[1].resolve(jsonResponse(200, modelsPayload('vscode', 'g9', ['m2'], 'ready', 'bob')));
  await tick(); await tick();
  assert.equal(t.run('messages.length'), 0);
  assert.equal(t.ctx.fakeChat.discarded, true);
  assert.equal(controller.signal.aborted, true);
  const chat = t.$('chat-messages');
  assert.match(chat.children[chat.children.length - 1].textContent, /New conversation/);
});

test('stale flow events are filtered by mode and generation', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', ['m1'])));
  await tick(); await tick();
  const es = t.eventSources[0];
  const flow = t.$('flow-log');
  es.emit('chat_start', { id: 'a', model: 'm1', format: 'anthropic', message_count: 1, mode: 'vscode', generation: 'old' });
  es.emit('chat_start', { id: 'b', model: 'm1', format: 'anthropic', message_count: 1, mode: 'cli', generation: 'g1' });
  assert.equal(flow.children.length, 0);
  es.emit('chat_start', { id: 'c', model: 'm1', format: 'anthropic', message_count: 1, mode: 'vscode', generation: 'g1' });
  assert.equal(flow.children.length, 1);
});

test('A06/A16: device link uses only a validated github.com URI; polling follows retry_after', async () => {
  const t = setup();
  t.run("deviceTxn = null");
  t.run(`applyDeviceTxn({ transaction_id: 't1', mode: 'cli', status: 'pending', user_code: 'ABCD-1234',
    verification_uri: 'https://evil.example/login/device', expires_in: 900, retry_after: 7 })`);
  assert.equal(t.$('account-device-link').href, undefined);
  assert.equal(t.$('account-device-code').textContent, 'ABCD-1234');
  assert.equal(t.timers[t.timers.length - 1].ms, 7000);
  t.run(`applyDeviceTxn({ transaction_id: 't1', mode: 'cli', status: 'pending', user_code: 'ABCD-1234',
    verification_uri: 'https://github.com/login/device', expires_in: 890, retry_after: 10 })`);
  assert.equal(t.$('account-device-link').href, 'https://github.com/login/device');
  assert.equal(t.timers[t.timers.length - 1].ms, 10000);
  t.run(`applyDeviceTxn({ transaction_id: 't1', mode: 'cli', status: 'awaiting_confirmation',
    candidate: { login: 'devuser', user_id: 5, host: 'github.com', callable_count: 3, model_count: 4 } })`);
  assert.equal(t.$('account-device-confirm').hidden, false);
  assert.match(t.$('account-device-candidate').textContent, /Verified as @devuser/);
  t.run(`applyDeviceTxn({ transaction_id: 't1', mode: 'cli', status: 'denied',
    error: { category: 'device_denied', message: 'Authorization was denied on GitHub.' } })`);
  assert.equal(t.$('account-device-result').hidden, false);
  assert.match(t.$('account-device-result-text').textContent, /denied.*previous account selection is unchanged/);
});

test('A31: account dialog opens, Esc closes, cancels pending sign-in and restores focus', async () => {
  const t = setup();
  const opener = t.$('account-button');
  opener.focus();
  t.run('openAccountDialog()');
  assert.equal(t.$('account-modal').classList.contains('open'), true);
  assert.equal(t.$('account-modal').getAttribute('aria-hidden'), 'false');
  assert.equal(t.doc.activeElement, t.$('account-close'));
  t.run(`deviceTxn = { transaction_id: 't9', mode: 'vscode', status: 'pending' }`);
  const keydown = t.doc.listeners.keydown[0];
  keydown({ key: 'Escape', preventDefault() {} });
  await tick(); await tick();
  assert.equal(t.$('account-modal').classList.contains('open'), false);
  assert.equal(t.doc.activeElement, opener);
  const cancel = t.requests.find(r => r.url === '/api/auth/device/cancel');
  assert.ok(cancel, 'closing cancels the pending sign-in');
  assert.deepEqual(JSON.parse(cancel.opts.body), { transaction_id: 't9' });
});

test('account dialog shows source separately from mode and hides non-applicable actions', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', [], 'signed_out')));
  await tick(); await tick();
  t.run('openAccountDialog()');
  assert.equal(t.$('account-signout').hidden, true);
  assert.equal(t.$('account-title').textContent, 'Account — VS Code mode');
  t.run(`authStates.vscode = ${JSON.stringify(modelsPayload('vscode', 'g2', ['m']).auth)}`);
  t.run('renderAccountDialog()');
  const facts = t.$('account-facts').children.map(c => c.textContent);
  assert.ok(facts.includes('gh account (selected in demo)'));
  assert.ok(facts.includes('vscode-chat'));
  assert.equal(t.$('account-signout').hidden, false);
});

async function startPendingChat(t) {
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', ['m1'])));
  await ticks();
  t.$('chat-input').value = 'hi';
  const sending = t.run('sendMessage()');
  await ticks();
  const chatReq = t.requests.find(r => r.url === '/api/chat');
  chatReq.honorAbort = false; // model a response that still arrives after abort
  return { sending, chatReq };
}

test('round-2 #3: late error from the previous mode never renders in the new mode', async () => {
  const t = setup();
  const { sending, chatReq } = await startPendingChat(t);
  t.run('globalThis.oldChat = activeChat');
  t.run("setMode('cli')");
  assert.equal(t.run('oldChat.discarded'), true, 'discarded immediately on mode change');
  assert.equal(chatReq.opts.signal.aborted, true);
  modelRequests(t)[1].resolve(jsonResponse(200, modelsPayload('cli', 'g2', ['c1'], 'ready', 'bob')));
  await ticks();
  chatReq.resolve(jsonResponse(500, { error: { category: 'upstream_error', message: 'OLD MODE ERROR' } }));
  await sending;
  const text = t.$('chat-messages').textContent;
  assert.doesNotMatch(text, /OLD MODE ERROR/);
  assert.match(text, /Response discarded/);
  assert.equal(t.run('messages.length'), 0);
  assert.equal(t.run('currentMode'), 'cli');
  assert.equal(t.run('activeChat'), null);
  assert.equal(modelRequests(t).length, 2, 'discarded chat does not trigger extra loads');
});

test('round-2 #3: late stream chunks after a mode switch are not written', async () => {
  const t = setup();
  const { sending, chatReq } = await startPendingChat(t);
  const stream = streamResponse();
  chatReq.resolve(stream.resp);
  await ticks();
  stream.push('data: {"type":"content_block_delta","delta":{"text":"early "}}\n');
  await ticks();
  assert.match(t.$('chat-messages').textContent, /early/);
  t.run("setMode('cli')");
  stream.push('data: {"type":"content_block_delta","delta":{"text":"LATE-CHUNK"}}\n');
  stream.push(null);
  await sending;
  const text = t.$('chat-messages').textContent;
  assert.doesNotMatch(text, /LATE-CHUNK/);
  assert.match(text, /Response discarded/);
  assert.equal(t.run('messages.length'), 0);
});

test('round-2 #3: returning to the original mode does not revive the old chat', async () => {
  const t = setup();
  const { sending, chatReq } = await startPendingChat(t);
  t.run("setMode('cli')");
  t.run("setMode('vscode')");
  modelRequests(t)[2].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', ['m1'])));
  await ticks();
  chatReq.resolve(jsonResponse(200, { output_text: 'REVIVED' }, {}));
  await sending;
  assert.doesNotMatch(t.$('chat-messages').textContent, /REVIVED/);
  assert.equal(t.run('messages.length'), 0, 'old history not revived into the reset conversation');
});

for (const reopen of [false, true]) {
  test(`round-2 #5: device start completing after dialog ${reopen ? 'close+reopen' : 'close'} is cancelled`, async () => {
    const t = setup();
    t.run('openAccountDialog()');
    const starting = t.run('startDeviceSignIn()');
    await ticks();
    const startReq = t.requests.find(r => r.url === '/api/auth/device/start');
    assert.ok(startReq);
    t.run('closeAccountDialog()');
    if (reopen) t.run('openAccountDialog()');
    const timersBefore = t.timers.length;
    startReq.resolve(jsonResponse(200, { transaction_id: 't-late', mode: 'vscode', status: 'pending',
      user_code: 'ABCD-1234', verification_uri: 'https://github.com/login/device', expires_in: 900, retry_after: 5 }));
    await starting;
    await ticks();
    const cancels = t.requests.filter(r => r.url === '/api/auth/device/cancel');
    assert.equal(cancels.length, 1);
    assert.deepEqual(JSON.parse(cancels[0].opts.body), { transaction_id: 't-late' });
    assert.equal(t.run('deviceTxn'), null);
    assert.equal(t.timers.slice(timersBefore).some(x => x.ms === 5000), false, 'no polling scheduled');
    assert.equal(t.$('account-device-pending').hidden, true);
    assert.equal(t.run('accountBusy'), false);
  });
}

const contents = t => JSON.parse(t.run('JSON.stringify(messages.map(m => m.content))'));

test('round-3 #2: discarding a non-aborting old chat releases send ownership', async () => {
  const t = setup();
  const { sending: oldSending, chatReq: oldReq } = await startPendingChat(t);
  t.run("setMode('cli')");
  assert.equal(t.run('sending'), false);
  assert.equal(t.run('activeChat'), null);
  modelRequests(t)[1].resolve(jsonResponse(200, modelsPayload('cli', 'g2', ['c1'], 'ready', 'bob')));
  await ticks();
  assert.equal(t.$('send-btn').disabled, false, 'new mode can send while old request is unsettled');
  t.$('chat-input').value = 'new question';
  const newSending = t.run('sendMessage()');
  await ticks();
  const chats = t.requests.filter(r => r.url === '/api/chat');
  assert.equal(chats.length, 2);
  const body = JSON.parse(chats[1].opts.body);
  assert.deepEqual([body.mode, body.generation, body.messages.length], ['cli', 'g2', 1]);

  oldReq.resolve(jsonResponse(200, { output_text: 'OLD LATE' }));
  await oldSending;
  await ticks();
  assert.equal(t.run('sending'), true, "old finally must not clear the newer chat's ownership");
  assert.equal(t.run('activeChat && activeChat.mode'), 'cli');
  assert.equal(t.$('send-btn').disabled, true);
  assert.deepEqual(contents(t), ['new question']);
  assert.doesNotMatch(t.$('chat-messages').textContent, /OLD LATE/);

  chats[1].resolve(jsonResponse(200, { output_text: 'NEW ANSWER' }));
  await newSending;
  assert.equal(t.run('sending'), false);
  assert.equal(t.run('activeChat'), null);
  assert.deepEqual(contents(t), ['new question', 'NEW ANSWER']);
});

test('round-3 #2: account-change discard (same mode) also releases send ownership', async () => {
  const t = setup();
  const { sending: oldSending, chatReq: oldReq } = await startPendingChat(t);
  t.run('loadModels()');
  modelRequests(t)[1].resolve(jsonResponse(200, modelsPayload('vscode', 'g5', ['m1'], 'ready', 'bob')));
  await ticks();
  assert.equal(t.run('sending'), false);
  assert.equal(t.$('send-btn').disabled, false);
  oldReq.resolve(jsonResponse(500, { error: { message: 'OLD FAILURE' } }));
  await oldSending;
  assert.doesNotMatch(t.$('chat-messages').textContent, /OLD FAILURE/);
  assert.equal(t.$('send-btn').disabled, false);
});

test('round-4: a committed-but-expired confirmation is shown as not usable', async () => {
  const t = setup();
  modelRequests(t)[0].resolve(jsonResponse(200, modelsPayload('vscode', 'g1', ['m1'])));
  await ticks();
  t.run('openAccountDialog()');
  t.run(`deviceTxn = { transaction_id: 'tx', mode: 'vscode', status: 'awaiting_confirmation',
    candidate: { login: 'devuser', user_id: 5, host: 'github.com', callable_count: 3, model_count: 4 } }`);
  const confirming = t.run('confirmDeviceSignIn()');
  await ticks();
  const req = t.requests.find(r => r.url === '/api/auth/device/confirm');
  const state = { ...modelsPayload('vscode', 'g7', [], 'error', 'devuser').auth,
    error: { category: 'expired_credentials', message: 'The demo sign-in for @devuser expired. Sign in again.' } };
  req.resolve(jsonResponse(200, { transaction: { transaction_id: 'tx', mode: 'vscode', status: 'committed',
    candidate: { login: 'devuser' } }, state }));
  await confirming;
  await ticks();
  const text = t.$('account-device-result-text').textContent;
  assert.match(text, /@devuser was selected, but it is not usable: .*expired/);
  assert.doesNotMatch(text, /now uses/);
  assert.equal(t.run('authStates.vscode.status'), 'error');
});
