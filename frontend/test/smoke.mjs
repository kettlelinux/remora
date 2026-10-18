// Smoke test: boots the built frontend (backend/remora/static) in Node against a fake Steam
// (React from npm as Steam's webpack modules, jsdom, a fake backend socket). Steam's own UI
// modules are absent, so every fragile lookup must degrade without breaking the rest.
// Run: pnpm run build && pnpm test
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const require = createRequire(import.meta.url);
const React = require('react');
const ReactDOM = require('react-dom');
const JSXR = require('react/jsx-runtime');

const g = globalThis;
g.window = g;
const els = {};
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><head><title>SP</title></head><body><div id="mount"></div></body></html>');
g.document = dom.window.document;
g.HTMLElement = dom.window.HTMLElement;
g.Node = dom.window.Node;
g.Element = dom.window.Element;
g.Event = dom.window.Event;
g.MouseEvent = dom.window.MouseEvent;
g.getComputedStyle = dom.window.getComputedStyle;
g.requestAnimationFrame = (cb) => setTimeout(cb, 0);
g.IS_REACT_ACT_ENVIRONMENT = true;
const mods = { 1: React, 2: ReactDOM, 3: JSXR, 4: { NotificationStoreX: 1 } };
const factories = {};
const req = (id) => { if (id in mods) return mods[id]; const m = { exports: {} }; factories[id](m, m.exports, req); return (mods[id] = m.exports); };
req.m = { 1: 0, 2: 0, 3: 0, 4: 0 };
req.c = { 1: { exports: React }, 2: { exports: ReactDOM }, 3: { exports: JSXR } };
g.webpackChunksteamui = { push: ([, modules, cb]) => { for (const k of Object.keys(modules ?? {})) { factories[k] = modules[k]; req.m[k] = modules[k]; } cb?.(req); } };
g.App = { BFinishedInitBeforeLogin: () => true };
let uiCb;
g.SteamClient = { UI: { RegisterForUIModeChanged: (cb) => { uiCb = cb; setTimeout(() => cb(4), 10); return { unregister() {} }; } }, User: {} };
const posted = [];
g.NotificationStore = { m_nNextTestNotificationID: 5, ProcessNotification: (...a) => posted.push(a) };
g.deckyAuthToken = 'tok';

const sent = [];
let sock;
g.WebSocket = class {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 0; sock = this; setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 5); }
  send(s) {
    const m = JSON.parse(s); sent.push(m);
    if (m.type !== 0) return;
    const reply = (result) => setTimeout(() => this.onmessage?.({ data: JSON.stringify({ type: 1, id: m.id, result }) }), 1);
    switch (m.route) {
      case 'loader/get_plugins': return reply([{ name: 'Legacy', version: '1.0.0', load_type: 0, disabled: false }, { name: 'Esm Plug', version: '2.0.0', load_type: 1, disabled: false }, { name: 'Off', version: '1', load_type: 1, disabled: true }]);
      case 'utilities/settings/get': return reply(m.args[1]);
      case 'remora/status': return reply({ version: '0.1', deckyVersion: '3.2', plugins: [] });
      case 'loader/call_legacy_plugin_method': return reply({ success: true, result: 42 });
      case 'loader/call_plugin_method': return setTimeout(() => this.onmessage?.({ data: JSON.stringify({ type: -1, id: m.id, error: { name: 'ValueError', message: 'bad', traceback: 'tb' } }) }), 1);
      case 'remora/never': return;
      default: return reply(null);
    }
  }
  close() {}
};
g.__slow = { n: 0, dismounted: [] };
g.fetch = async (url, opts) => {
  if (String(url).includes('/plugins/Slow/frontend_bundle')) {
    await new Promise((r) => setTimeout(r, 80));
    return { ok: true, text: async () => '(function(){ return function(){ const n = ++globalThis.__slow.n; return { content: "S" + n, onDismount: () => globalThis.__slow.dismounted.push(n) }; }; })()' };
  }
  if (String(url).includes('/plugins/Hang/frontend_bundle')) {
    return new Promise((_, rej) => opts.signal.addEventListener('abort', () => rej(new Error('aborted'))));
  }
  if (String(url).includes('/plugins/Legacy/frontend_bundle')) {
    if (opts.headers['X-Decky-Auth'] !== 'tok') throw new Error('no auth header');
    return { ok: true, text: async () => '(function(){ return function(serverAPI){ globalThis.__legacyApi = serverAPI; return { name: "wrong", title: "T", content: "C", icon: "I" }; }; })()' };
  }
  throw new Error('unexpected fetch ' + url);
};

const errors = [];
const origErr = console.error;
console.error = (...a) => { errors.push(a.map(String).join(' ')); origErr(...a); };

const staticDir = process.argv[2] ?? new URL('../../backend/remora/static', import.meta.url).pathname;
await import(pathToFileURL(staticDir + '/index.js').href);
await new Promise((r) => setTimeout(r, 1500));

const L = g.DeckyPluginLoader;
const check = (cond, msg) => { console.log(cond ? 'PASS' : 'FAIL', msg); if (!cond) process.exitCode = 1; };
check(g.DFL && typeof g.DFL.findModuleExport === 'function', 'window.DFL is @decky/ui');
check(g.SP_REACT === React && g.SP_JSX === JSXR && g.SP_REACTDOM === ReactDOM, 'SP_* globals are the webpack modules');
check(L && Array.isArray(L.pluginReloadQueue) && L.routerHook.routerState._routePatches instanceof Map, 'DeckyPluginLoader shape');
check(typeof L.deckyState.setActivePlugin === 'function', 'deckyState.setActivePlugin');
const names = L.plugins.get().map((p) => p.name).sort();
check(names.join(',') === 'Esm Plug,Legacy', 'plugins loaded (ESM fails in Node -> placeholder): ' + names.join(','));
const legacy = L.plugins.get().find((p) => p.name === 'Legacy');
check(legacy.version === '1.0.0' && legacy.loadType === 0 && legacy.title === 'T', 'backend name/version/loadType win');
check(L.plugins.get().find((p) => p.name === 'Esm Plug').loadError, 'ESM failure gives an error placeholder');
const r = await g.__legacyApi.callPluginMethod('foo', { a: 1 });
check(r.success && r.result === 42, 'legacy callPluginMethod');
const fr = await g.__legacyApi.fetchNoCors('https://x', {});
const httpCall = sent.find((m) => m.route === 'utilities/http_request');
check(fr.success && httpCall.args[0] === 'POST', 'legacy fetchNoCors defaults to POST');
const api = g.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit.connect(2, 'Esm Plug');
check(api._version === 2 && typeof api.useQuickAccessVisible === 'function', 'connect(2) gives v2');
const api1 = g.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit.connect(1, 'Esm Plug');
check(api1._version === 1 && !api1.useQuickAccessVisible, 'connect(1) gives v1');
const got = [];
api1.addEventListener('ev', (...a) => got.push(a));
api.addEventListener('ev', () => got.push('stale'));
sock.onmessage({ data: JSON.stringify({ type: 5, event: 'loader/plugin_event', args: [{ plugin: 'Esm Plug', event: 'ev', args: [1, 2] }] }) });
check(got.length === 1 && got[0][1] === 2, 'plugin_event fan-out to the newest connect only');
try { await api1.call('m', 1); check(false, 'call rejects'); } catch (e) { check(e.name === 'Python ValueError' && e.message === 'bad', 'call rejects with PyError'); }
check(sent.some((m) => m.type === 3), 'replies are acked');
check(sent[0].type === 4, 'FULL_SYNC first on connect');
check(api1.getExternalResourceURL('https://a b').includes('fetch_url=https%3A%2F%2Fa%20b') && api1.getExternalResourceURL('x').includes('auth=tok'), 'getExternalResourceURL');
const td = { title: 'a', body: 'b' };
L.toaster.toast(td);
check(td.duration === 5000 && td.eType === 31 && td.sound === 6, 'toast defaults written back');
// import_plugin event with queue
sock.onmessage({ data: JSON.stringify({ type: 5, event: 'loader/import_plugin', args: ['Legacy', '1.0.1', 0] }) });
await new Promise((r) => setTimeout(r, 200));
check(L.plugins.get().find((p) => p.name === 'Legacy').version === '1.0.1', 'reload via loader/import_plugin');
sock.onmessage({ data: JSON.stringify({ type: 5, event: 'loader/unload_plugin', args: ['Legacy'] }) });
check(!L.plugins.get().find((p) => p.name === 'Legacy'), 'unload_plugin');
const ev = (event, ...args) => sock.onmessage({ data: JSON.stringify({ type: 5, event, args }) });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
check(sent.find((m) => m.type === 0).id > 1000, 'call ids start at a random base');
// an unload that arrives while the import is in flight wins; the late result is dismounted
ev('loader/import_plugin', 'Slow', '1', 0, false);
await wait(10);
ev('loader/unload_plugin', 'Slow');
await wait(200);
check(!L.plugins.get().find((p) => p.name === 'Slow') && g.__slow.dismounted.join() === '1', 'unload during an import wins: ' + g.__slow.dismounted.join());
// two imports of one name: the newest wins, the first one's result is dismounted
ev('loader/import_plugin', 'Slow', '2', 0, false);
await wait(10); // the first one is fetching
ev('loader/import_plugin', 'Slow', '3', 0, false);
await wait(400);
const slow = L.plugins.get().filter((p) => p.name === 'Slow');
check(slow.length === 1 && slow[0].version === '3' && g.__slow.dismounted.join() === '1,2', 'newest of two imports wins: ' + slow.map((p) => p.version) + ' / ' + g.__slow.dismounted.join());
// a timed-out import leaves an error entry (with Retry) instead of vanishing
ev('loader/import_plugin', 'Hang', '1', 0, false, 50);
await wait(150);
check(L.plugins.get().find((p) => p.name === 'Hang')?.loadError, 'timed-out import gives an error placeholder');
// Remora's own settings go through remora/settings/set
await L.settings.set('remora.storeTelemetry', true);
await L.settings.set('sortPlugins', false);
check(sent.some((m) => m.route === 'remora/settings/set' && m.args[0] === 'remora.storeTelemetry') && sent.some((m) => m.route === 'utilities/settings/set' && m.args[0] === 'sortPlugins') && !sent.some((m) => m.route === 'utilities/settings/set' && String(m.args[0]).startsWith('remora.')), 'remora.* settings use remora/settings/set');
// root confirm: confirm_waiting shows the wait, confirm_done closes it
ev('remora/confirm_waiting', 'r1', 'Root Plug', 'keys', 'Hold Volume Up and Volume Down together for a second', Date.now() / 1000 + 30);
check(L.installs.confirm.get()?.name === 'Root Plug' && L.installs.confirm.get().until > Date.now(), 'confirm_waiting opens the confirm UI');
ev('remora/confirm_done', 'r1', 'Root Plug', true);
check(L.installs.confirm.get() === null, 'confirm_done closes it');
ev('remora/root_prompt', 'r2', 'Root Plug', '1.0', 'terminal', 'Run `sudo remora approve Root Plug` in a terminal on this device');
const rp = L.installs.prompts.get().find((p) => p.requestId === 'r2');
check(rp && rp.hint.includes('sudo remora approve'), 'root_prompt carries mode and hint');
await L.installs.answer(rp, false);
check(!L.installs.prompts.get().length && sent.some((m) => m.route === 'remora/store/answer_root' && m.args[0] === 'r2' && m.args[1] === false), 'root prompt answered');
// reconnect sends FULL_SYNC with pending calls
const pend = L.backend.call('remora/never');
sent.length = 0;
sock.readyState = 3; sock.onclose();
// Render Remora's own UI with react-dom (jsdom), to catch render-time errors.
const { createRoot } = require('react-dom/client');
const act = React.act ?? (async (f) => { await f(); await new Promise((r) => setTimeout(r, 20)); });
const renderErrors = [];
const tryRender = async (label, el) => {
  const div = document.createElement('div');
  document.body.appendChild(div);
  const root = createRoot(div, { onUncaughtError: (e) => renderErrors.push(label + ': ' + e), onCaughtError: (e) => renderErrors.push(label + ' (caught): ' + e) });
  await act(async () => { root.render(el); });
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
  const html = div.innerHTML;
  check(html.length > 0 && !/crashed/.test(html), `render ${label} (${html.length} chars)`);
  return { div, root };
};
const panel = L.tabsHook.panel();
const q = await tryRender('QAM panel', panel);
check(q.div.textContent.includes('Esm Plug'), 'QAM lists plugins');
await act(async () => { L.deckyState.setActivePlugin('Esm Plug'); await new Promise((r) => setTimeout(r, 20)); });
check(!q.div.textContent.includes('Legacy') && q.div.textContent.includes('Esm Plug'), 'setActivePlugin opens the plugin view: ' + q.div.textContent.slice(0, 80));
for (const path of ['/decky/settings', '/decky/store', '/remora/plugin/:name']) {
  const C = L.routerHook.routerState.routes.get(path).component;
  await tryRender(path, React.createElement(C));
}
// Router transform on a fake root output.
{
  const h = React.createElement;
  const Route = (p) => h('route', null, p.children);
  const Child = () => null;
  const tree = h('div', null, h('div', null, [h(Route, { key: 1, path: '/library' }), h(Route, { key: 2, path: '/library/app/:appid' }, h(Child, { x: 1 }))]), h('div', null, [h(Route, { key: 3, path: '/appoverlay' }), h(Route, { key: 4, path: '/apprunning' }, h(Child))]));
  const RH = L.routerHook;
  const seen = [];
  const patch = (props) => { seen.push(props.children.props.x); props.children.props.patched = true; return props; };
  RH.addPatch('/library/app/:appid', patch);
  RH.addPatch('/apprunning', (p) => { seen.push('game'); return p; });
  RH.addRoute('/test-route', () => null);
  const out = RH.transform(tree);
  const main = out.props.children[0].props.children;
  check(main.length === 3 && Array.isArray(main[2]) && main[2].some((r) => r.props.path === '/test-route'), 'route appended to main list');
  check(main[1].props.children.props.patched === true && tree.props.children[0].props.children[1].props.children.props.patched === undefined, 'route patch applied on a copy');
  check(seen.sort().join(',') === '1,game', 'patches run on main and in-game lists: ' + seen.join(','));
  check([...RH.routerState._routePatches.get('/library/app/:appid')][0] === patch, '_routePatches is Map<path, Set>');
  RH.removePatch('/library/app/:appid', patch);
  check(!RH.routerState._routePatches.has('/library/app/:appid'), 'removePatch');
  // structural fallback layout
  const tree2 = h('section', null, h('main', null, h('div', null, [h(Route, { key: 1, path: '/a' }), h(Route, { key: 2, path: '/b' })])));
  const out2 = RH.transform(tree2);
  const list2 = out2.props.children.props.children.props.children;
  check(list2.length === 3 && Array.isArray(list2[2]), 'structural search finds the route list');
}
check(renderErrors.length === 0, 'no render errors: ' + renderErrors.join(' | '));
console.log('statuses:', JSON.stringify(g.remora.hooks().map((h) => [h.id, h.state, h.strategy ?? h.detail])));
console.log('console errors:', errors.length);
setTimeout(() => {
  const fs = sent.find((m) => m.type === 4);
  check(fs && fs.messages.some((m) => m.route === 'remora/never'), 'reconnect resends pending in FULL_SYNC');
  // the resync after the reconnect: Legacy (unloaded above) is back, Slow/Hang (not listed) are gone
  const now = L.plugins.get().map((p) => p.name).sort().join(',');
  check(now === 'Esm Plug,Legacy', 'reconnect resync imports missing and unloads unlisted plugins: ' + now);
  process.exit();
}, 6200);
void pend;
