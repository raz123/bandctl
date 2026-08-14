'use strict';

/*
 * Merged dev-only jsdom regression harness for web/index.html.
 *
 * Two suites run in one process:
 *   1. Behavior suite (S1-S16): bootstrap/auth/ordering/race/state fixes from
 *      fix/webui-behavior (A-089 A-090 A-091 A-092 A-101 A-109 A-110 A-113
 *      A-117 A-123 A-124 A-125 A-130 A-132 A-147 A-190 A-193 A-202 A-203
 *      A-222 A-223 ...).
 *   2. a11y suite: CSS token/WCAG checks + focus/keyboard/live-region checks
 *      from fix/webui-a11y (A-003 A-005 A-006 A-018 A-027 A-029 A-031 A-039
 *      A-040 A-055 A-076 A-077 A-079 A-081 A-083 A-086 A-094 A-095 A-107
 *      A-111 A-121 A-159 A-162 A-172 A-191 A-192 ...).
 *
 * Not part of the shipped module (the module bundles Python; no Node runtime).
 * Run: npm test  (== node tests/js/run.js)
 */

const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'index.html'), 'utf8');

/* ============================ Suite 1: behavior ============================ */
// Both suites run as independent async IIFEs over live jsdom windows; their
// timers keep the event loop alive, so exit explicitly once both have settled.
const __suitesPending = { behavior: true, a11y: true };
function __suiteDone(name) {
  __suitesPending[name] = false;
  if (!__suitesPending.behavior && !__suitesPending.a11y) {
    process.exit(process.exitCode || 0);
  }
}
(async () => {
let pass = 0, fail = 0;
function assert(cond, msg) {
  if (cond) { pass++; console.log('  PASS ' + msg); }
  else { fail++; console.log('  FAIL ' + msg); }
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
function abortErr() { const e = new Error('Aborted'); e.name = 'AbortError'; return e; }
function jsonRes(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (h) => (h.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: () => Promise.resolve(body),
  };
}
// fetch stub: routes by pathname; handler returns a promise of a response.
function makeFetch(server) {
  return function (url, opts = {}) {
    let path;
    try { path = new URL(String(url)).pathname; } catch (e) { path = String(url); }
    const handler = server[path];
    return new Promise((resolve, reject) => {
      if (opts.signal) {
        if (opts.signal.aborted) return reject(abortErr());
        opts.signal.addEventListener('abort', () => reject(abortErr()));
      }
      Promise.resolve()
        .then(() => {
          if (!handler) throw new Error('no route for ' + path);
          return handler({ pathname: path, opts });
        })
        .then((res) => { if (opts.signal && opts.signal.aborted) return reject(abortErr()); resolve(res); })
        .catch(reject);
    });
  };
}
function makeDom(server) {
  return new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://localhost:8080/',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = makeFetch(server);
      window.scrollTo = () => {};
      Object.defineProperty(window.HTMLCanvasElement.prototype, 'clientWidth', { configurable: true, get: () => 300 });
      window.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, {
        get: (t, p) => (typeof p === 'string' || typeof p === 'symbol' ? () => {} : undefined),
        set: () => true,
      });
    },
  });
}
const okRoutes = (over = {}) => Object.assign({
  '/api/health': () => Promise.resolve(jsonRes({ status: 'ok' })),
  '/api/read': () => Promise.resolve(jsonRes({ lte: ['1', '2', '3'], nr: ['5'], source: 'qmi' })),
  '/api/signal': () => Promise.resolve(jsonRes({ rsrp_dbm: -90, rsrq_db: -11, tech: 'LTE', level: 3 })),
  '/api/registration': () => Promise.resolve(jsonRes({ service_state: 'IN_SERVICE', data_state: 'IN_SERVICE', network_type: 'LTE' })),
  '/api/band-camping': () => Promise.resolve(jsonRes({ samples: [] })),
  '/api/defaults': () => Promise.resolve(jsonRes({ carrier: 'rogers', operator: 'ROGERS', lte: ['1', '2', '3'], nr: ['5', '6'] })),
  '/api/settings': () => Promise.resolve(jsonRes({ ok: true, lan_enabled: false, token_required: false })),
  '/api/drop-log': () => Promise.resolve(jsonRes({ ok: true, enabled: false, dir: '/x', files: [] })),
  '/api/write': () => Promise.resolve(jsonRes({ ok: true, source: 'qmi' })),
}, over);

  // ============ S1: bootstrap failure -> no fallback, NET tag, recovery (A-117/A-089/A-090/A-190/A-069) ============
  console.log('S1 bootstrap failure/recovery');
  {
    const down = {};
    ['/api/health', '/api/read', '/api/signal', '/api/registration', '/api/band-camping', '/api/defaults', '/api/settings', '/api/drop-log', '/api/write']
      .forEach((p) => { down[p] = () => Promise.reject(new Error('ECONNREFUSED')); });
    const dom = makeDom(down);
    const w = dom.window;
    // pre-settle paint (A-089): nothing claims Connected/Applied/0-count
    assert(w.$('connection-state').textContent === 'Connecting…', 'S1 initial header is Connecting…');
    assert(w.$('save-btn').disabled === true, 'S1 save disabled before bootstrap');
    assert(w.$('staged-count').textContent === '—', 'S1 staged count unknown');
    assert(w.$('config-summary-meta').textContent === '—', 'S1 summary meta unknown');
    await wait(80);
    assert(w.serverDownReported === true, 'S1 serverDown reported after read failure');
    assert(w.$('server-banner').style.display === 'block', 'S1 banner shown');
    assert(w.lteEnabled.size === 0, 'S1 no fallback applied on failed read (A-117)');
    assert(w.bootstrapped === false, 'S1 not bootstrapped');
    assert(w.$('connection-state').textContent === 'Offline', 'S1 header Offline');
    const tags = Array.from(w.$('history-list').querySelectorAll('.tag')).map((t) => t.textContent);
    assert(tags.every((t) => t === 'NET'), 'S1 outage logged as NET, never DROP (A-090/A-190): ' + tags.join(','));
    // server comes back; user hits Retry
    Object.assign(down, okRoutes());
    w.retryConnect();
    await wait(120);
    assert(w.serverDownReported === false, 'S1 retry recovered');
    assert(w.bootstrapped === true, 'S1 bootstrapped after retry');
    assert(w.lteEnabled.has('1') && w.lteEnabled.has('3'), 'S1 bands loaded from modem');
    assert(w.$('connection-state').textContent === 'Connected', 'S1 header Connected');
    assert(w.$('save-btn').disabled === false, 'S1 save enabled after bootstrap');
  }

  // ============ S2: saveBands mid-flight edit + dirty flag (A-091/A-202/A-023/A-007) ============
  console.log('S2 saveBands races');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    assert(w.lteEnabled.size === 3 && w.userTouched === false, 'S2 baseline loaded, clean');
    assert(w.$('config-summary-meta').textContent === 'Applied', 'S2 meta Applied after load');
    let writeResolve;
    server['/api/write'] = () => new Promise((res) => { writeResolve = res; });
    const saveP = w.saveBands();
    await wait(20);
    w.toggleBand('7', 'lte'); // edit while write is in flight
    assert(w.userTouched === true, 'S2 touched during flight');
    writeResolve(jsonRes({ ok: true, source: 'qmi' }));
    await saveP;
    await wait(10);
    assert(w.lteEnabled.has('7'), 'S2 newer edit preserved, read-back skipped (A-091/A-202)');
    assert(w.userTouched === true, 'S2 still staged');
    assert(w.$('config-summary-meta').textContent.indexOf('Staged') === 0, 'S2 meta shows Staged (A-007)');
    // clean save now
    server['/api/write'] = () => Promise.resolve(jsonRes({ ok: true, source: 'qmi' }));
    server['/api/read'] = () => Promise.resolve(jsonRes({ lte: ['1', '2', '3', '7'], nr: ['5'], source: 'qmi' }));
    await w.saveBands();
    assert(w.userTouched === false, 'S2 dirty flag cleared after clean save (A-023)');
    assert(w.lteEnabled.has('7') && w.lteEnabled.size === 4, 'S2 read-back applied on clean save');
    assert(w.$('config-summary-meta').textContent === 'Applied', 'S2 meta Applied after save');
  }

  // ============ S3: 401 is not an outage (A-203) ============
  console.log('S3 401 handling');
  {
    const unauth = {};
    ['/api/health', '/api/read', '/api/signal', '/api/registration', '/api/band-camping', '/api/defaults', '/api/drop-log', '/api/write']
      .forEach((p) => { unauth[p] = () => Promise.resolve(jsonRes({ error: 'unauthorized' }, 401)); });
    unauth['/api/settings'] = () => Promise.resolve(jsonRes({ ok: true, lan_enabled: true, token_required: true }));
    const dom = makeDom(unauth);
    const w = dom.window;
    await wait(80);
    assert(w.serverDownReported === false, 'S3 no serverDown on 401');
    assert(w.$('server-banner').style.display !== 'block', 'S3 no false banner');
    assert(w.$('history-list').children.length === 0, 'S3 no fabricated DROP/NET entries');
    assert(w.$('lan-token-entry').style.display === 'flex', 'S3 re-auth entry shown');
    // paste a token and save it -> bootstrap re-runs (A-067)
    w.$('lan-token-input').value = 'sekret';
    Object.assign(unauth, okRoutes());
    w.saveLanToken();
    await wait(120);
    assert(w.serverDownReported === false, 'S3 still not down after token');
    assert(w.bootstrapped === true, 'S3 bootstrap re-ran after token (A-067)');
    assert(w.lteEnabled.size === 3, 'S3 bands loaded after token');
  }

  // ============ S4: out-of-order signal polls (A-124) ============
  console.log('S4 signal ordering');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    let resolvers = [];
    server['/api/signal'] = () => new Promise((res) => resolvers.push(res));
    w.pollSignal(); // older, will resolve last
    await wait(10);
    w.pollSignal(); // newer, resolves first
    await wait(10);
    resolvers[1](jsonRes({ rsrp_dbm: -90, rsrq_db: -11, tech: 'LTE', level: 3, timestamp: 200 }));
    await wait(10);
    resolvers[0](jsonRes({ rsrp_dbm: -110, rsrq_db: -14, tech: 'LTE', level: 1, timestamp: 100 }));
    await wait(10);
    assert(w.$('sig-rsrp').textContent === '-90', 'S4 newest RSRP displayed');
    assert(w.signalHistory[w.signalHistory.length - 1].rsrp === -90, 'S4 history tail is newest');
    assert(w.signalHistory[w.signalHistory.length - 1].t === 200, 'S4 history uses server timestamp');
  }

  // ============ S5: out-of-order camping polls (A-132) ============
  console.log('S5 camping ordering');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    let resolvers = [];
    server['/api/band-camping'] = () => new Promise((res) => resolvers.push(res));
    w.updateCamping();
    await wait(10);
    w.updateCamping();
    await wait(10);
    resolvers[1](jsonRes({ samples: [{ timestamp: 200, earfcn: 5060, band: 12 }] }));
    await wait(10);
    resolvers[0](jsonRes({ samples: [{ timestamp: 100, earfcn: 2050, band: 4 }] }));
    await wait(10);
    assert(w.$('band-live-camped').textContent === 'B12 · 5060', 'S5 newest camping row wins');
    assert(w.$('chip-camped').querySelector('.chipval').textContent === 'B12 · 5060', 'S5 camped chip newest');
  }

  // ============ S6: defaults validation + A-109 apply + A-008/A-113 reset ============
  console.log('S6 defaults + reset');
  {
    const server = okRoutes();
    server['/api/read'] = () => Promise.reject(new Error('ECONNREFUSED'));
    server['/api/defaults'] = () => Promise.resolve(jsonRes({ error: 'carrier detection failed' }, 200));
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    assert(w.defaultsLte.length === 25, 'S6 error-body defaults rejected, fallback kept (A-070)');
    assert(w.carrierInfo === null, 'S6 error object not stored as carrier');
    // now a valid carrier defaults response arrives; nothing loaded yet -> applied (A-109)
    server['/api/defaults'] = () => Promise.resolve(jsonRes({ carrier: 'rogers', operator: 'ROGERS', lte: ['1', '2', '3'], nr: ['5', '6'] }));
    const ok = await w.fetchDefaults();
    assert(ok === true, 'S6 valid defaults accepted');
    assert(w.lteEnabled.size === 3 && w.nrEnabled.size === 2, 'S6 carrier defaults staged after read failure (A-109)');
    assert(w.bootstrapped === true, 'S6 bootstrapped from carrier defaults');
    // resetDefaults marks touched (A-008) and locks the button (A-113)
    server['/api/read'] = () => Promise.resolve(jsonRes({ lte: ['1', '2', '3'], nr: ['5'], source: 'qmi' }));
    await w.loadBands();
    w.userTouched = false;
    await w.resetDefaults();
    assert(w.userTouched === true, 'S6 reset sets userTouched (A-008)');
    assert(w.$('reset-btn').disabled === false, 'S6 reset button re-enabled');
    assert(w.$('config-summary-meta').textContent.indexOf('Staged') === 0, 'S6 meta Staged after reset');
    // reset with an edit during the pending lookup keeps the edit (A-113)
    let defaultsResolve;
    server['/api/defaults'] = () => new Promise((res) => { defaultsResolve = res; });
    w.userTouched = false;
    const resetP = w.resetDefaults();
    await wait(20);
    w.toggleBand('41', 'lte');
    defaultsResolve(jsonRes({ carrier: 'rogers', operator: 'ROGERS', lte: ['1', '2', '3'], nr: ['5', '6'] }));
    await resetP;
    assert(w.lteEnabled.has('41'), 'S6 newer edit kept during reset lookup (A-113)');
    assert(w.$('status').textContent.indexOf('newer edits were kept') >= 0, 'S6 status explains kept edits');
  }

  // ============ S7: token reveal masking (A-101/A-130) ============
  console.log('S7 token panel');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    w.localStorage.setItem('bandController.token.v1', 'tok12345abc');
    w.renderSettings({ lan_enabled: true });
    assert(w.$('lan-token-entry').style.display === 'none', 'S7 paste form hidden when token exists (A-101)');
    assert(w.$('lan-token').textContent === 'tok123…', 'S7 token masked by default');
    w.toggleTokenShown();
    assert(w.$('lan-token').textContent === 'tok12345abc', 'S7 token revealed on Show');
    w.renderSettings({ lan_enabled: false });
    assert(w.lanTokenShown === false, 'S7 reveal flag reset on disable (A-130)');
    w.renderSettings({ lan_enabled: true });
    assert(w.$('lan-token').textContent === 'tok123…', 'S7 token re-masked after panel transition');
    w.toggleTokenShown();
    w.regenerateToken();
    await wait(30);
    assert(w.lanTokenShown === false, 'S7 regenerate re-masks (A-130)');
  }

  // ============ S8: malformed preset (A-147) ============
  console.log('S8 malformed preset');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    w.localStorage.setItem('bandController.presets.v1', JSON.stringify([{ name: 'Broken', lte: 1, nr: [] }]));
    w.renderPresets();
    assert(w.$('preset-list').querySelectorAll('.preset-item').length === 1, 'S8 broken preset still renders');
    assert(w.$('preset-list').querySelector('.preset-count').textContent === '0 LTE / 0 NR', 'S8 counts safe');
    const before = w.lteEnabled.size;
    w.loadPreset(0);
    assert(w.lteEnabled.size === before, 'S8 load does not crash or apply garbage');
    assert(w.$('toast').textContent.indexOf('corrupted') >= 0, 'S8 corrupted preset toast shown');
  }

  // ============ S9: empty preset name (A-222) ============
  console.log('S9 empty preset name');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    w.$('preset-name-input').value = '   ';
    w.openModal('preset-modal');
    w.confirmSavePreset();
    assert(w.$('preset-modal').classList.contains('open'), 'S9 modal stays open');
    assert(w.$('toast').textContent === 'Enter a name for the preset', 'S9 feedback toast shown');
    const stored = JSON.parse(w.localStorage.getItem('bandController.presets.v1') || '[]');
    assert(stored.length === 0, 'S9 nothing saved');
  }

  // ============ S10: tab/hash sync (A-223) ============
  console.log('S10 tab hash sync');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    w.localStorage.setItem('bandController.presets.v1', JSON.stringify([{ name: 'P', lte: ['1'], nr: [] }]));
    w.renderPresets();
    w.location.hash = '#settings';
    w.switchTab('settings');
    w.loadPreset(0);
    assert(w.location.hash === '#bands', 'S10 loadPreset syncs hash');
    w.location.hash = '#settings';
    w.switchTab('settings');
    const file = new w.File(['{"lte":["1","2"],"nr":[]}'], 'c.json', { type: 'application/json' });
    w.onImportFile(file);
    await wait(20);
    assert(w.location.hash === '#bands', 'S10 import syncs hash');
    assert(w.userTouched === true, 'S10 import marks touched (A-009)');
  }

  // ============ S11: fetch deadline (A-084) ============
  console.log('S11 fetch deadline');
  {
    const server = okRoutes();
    server['/api/read'] = () => new Promise(() => {}); // never settles
    const dom = makeDom(server);
    const w = dom.window;
    await wait(50);
    w.FETCH_TIMEOUT_MS = 150;
    const t0 = Date.now();
    try {
      await w.apiFetch('/api/read?action=read');
      assert(false, 'S11 hung fetch should reject');
    } catch (e) {
      assert(e.name === 'AbortError', 'S11 rejects with AbortError after deadline');
      assert(Date.now() - t0 < 2000, 'S11 rejects within deadline');
    }
  }

  // ============ S12: single-endpoint failure does not stop monitors (A-025) ============
  console.log('S12 poll failure isolation');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    w.POLL_FAIL_LIMIT = 2;
    server['/api/registration'] = () => Promise.reject(new Error('timeout'));
    await w.pollRegistration();
    assert(w.serverDownReported === false, 'S12 one failure does not stop everything');
    assert(w.regTimer !== null, 'S12 timers still alive');
    await w.pollRegistration();
    assert(w.serverDownReported === true, 'S12 sustained failure trips serverDown');
    assert(w.regTimer === null && w.signalTimer === null, 'S12 timers stopped after threshold');
  }

  // ============ S13: late poll cannot resurrect a dead server (A-092) ============
  console.log('S13 no resurrection');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    w.serverDown(new Error('boom'));
    assert(w.$('connection-state').textContent === 'Offline', 'S13 offline');
    await w.pollSignal(); // succeeds but must be dropped
    assert(w.$('connection-state').textContent === 'Offline', 'S13 late success does not resurrect');
    assert(w.$('server-banner').style.display === 'block', 'S13 banner remains');
  }

  // ============ S14: LAN origin resolution (A-004) ============
  console.log('S14 API base resolution');
  {
    // remote LAN page: page origin answers JSON -> use same origin
    const lanServer = okRoutes();
    const lanDom = new JSDOM(html, {
      runScripts: 'dangerously',
      url: 'http://192.168.1.50:8080/',
      pretendToBeVisual: true,
      beforeParse(window) {
        window.fetch = makeFetch(lanServer);
        window.scrollTo = () => {};
        Object.defineProperty(window.HTMLCanvasElement.prototype, 'clientWidth', { configurable: true, get: () => 300 });
        window.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {}, set: () => true });
      },
    });
    const base = await lanDom.window.resolveApiBase();
    assert(base === 'http://192.168.1.50:8080', 'S14 LAN page resolves to page origin');
    const seen = [];
    lanDom.window.fetch = (url) => { seen.push(String(url)); return Promise.resolve(jsonRes({ ok: true })); };
    await lanDom.window.apiFetch('/api/read?action=read');
    assert(seen.some((u) => u.indexOf('http://192.168.1.50:8080/api/read') === 0), 'S14 LAN apiFetch targets page origin');
    // container origin (KSU WebView): non-JSON health -> phone loopback
    const containerDom = new JSDOM(html, {
      runScripts: 'dangerously',
      url: 'https://mui.kernelsu.org/index.html',
      pretendToBeVisual: true,
      beforeParse(window) {
        window.fetch = (url, opts) => Promise.resolve({
          ok: false, status: 404,
          headers: { get: (h) => (h.toLowerCase() === 'content-type' ? 'text/html' : null) },
          json: () => Promise.resolve({}),
        });
        window.scrollTo = () => {};
        Object.defineProperty(window.HTMLCanvasElement.prototype, 'clientWidth', { configurable: true, get: () => 300 });
        window.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {}, set: () => true });
      },
    });
    const cbase = await containerDom.window.resolveApiBase();
    assert(cbase === 'http://127.0.0.1:8080', 'S14 container page stays on phone loopback');
  }

  // ============ S15: polling pauses when hidden (A-114) ============
  console.log('S15 visibility pause');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    assert(w.signalTimer !== null && w.regTimer !== null && w.campTimer !== null, 'S15 polling running when visible');
    Object.defineProperty(w.document, 'hidden', { configurable: true, get: () => true });
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert(w.signalTimer === null && w.regTimer === null && w.campTimer === null, 'S15 timers cleared when hidden');
    Object.defineProperty(w.document, 'hidden', { configurable: true, get: () => false });
    w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert(w.signalTimer !== null, 'S15 timers resumed when visible');
  }

  // ============ S16: stale settings GET cannot roll back a newer LAN change (A-125) ============
  console.log('S16 stale settings rollback guard');
  {
    const server = okRoutes();
    const dom = makeDom(server);
    const w = dom.window;
    await wait(80);
    assert(w.lanEnabled === false, 'S16 baseline LAN off');
    // Hold the next settings GET in flight; the POST returns LAN on.
    let staleResolve;
    server['/api/settings'] = (ctx) => {
      if ((ctx.opts.method || 'GET').toUpperCase() === 'GET') {
        return new Promise((res) => { staleResolve = res; });
      }
      return Promise.resolve(jsonRes({ ok: true, lan_enabled: true, token_required: false }));
    };
    w.fetchSettings(); // bootstrap-style GET, will resolve last
    await wait(10);
    w.$('lan-toggle').checked = true;
    w.$('lan-toggle').dispatchEvent(new w.Event('change')); // user flips LAN on
    await wait(30);
    assert(w.lanEnabled === true, 'S16 LAN enabled by the user action');
    assert(w.$('lan-toggle').checked === true, 'S16 toggle stays on after POST');
    // Now the stale GET (captured before the toggle) finally returns LAN off.
    staleResolve(jsonRes({ ok: true, lan_enabled: false, token_required: false }));
    await wait(30);
    assert(w.lanEnabled === true, 'S16 stale GET did not roll back LAN (A-125)');
    assert(w.$('lan-toggle').checked === true, 'S16 UI still shows LAN on');
  }

  
  console.log('\n[behavior] TOTAL: ' + pass + ' passed, ' + fail + ' failed');
  if (fail) process.exitCode = 1;
  __suiteDone('behavior');
})().catch((e) => { console.error('BEHAVIOR HARNESS ERROR', e); process.exitCode = 2; __suiteDone('behavior'); });

/* ============================ Suite 2: a11y ============================ */
(async () => {

/* ---------------- extraction ---------------- */

function extractCss() {
  const m = html.match(/<style>([\s\S]*?)<\/style>/);
  return m ? m[1] : '';
}
function extractScript() {
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('no inline <script> found in web/index.html');
  // The app script starts with 'use strict'; strip the directive so the
  // sloppy indirect eval below executes in global scope and its top-level
  // var/function declarations land on window (where the tests reach them).
  return m[1].replace(/^\s*'use strict';\s*/, '');
}

const css = extractCss();
const script = extractScript();

/* ---------------- tiny test runner ---------------- */

let passed = 0;
let failed = 0;
const failures = [];

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log('  ok   ' + name);
  } catch (e) {
    failed += 1;
    failures.push({ name, error: e });
    console.log('  FAIL ' + name);
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed');
}
function assertEq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error((msg || 'assertEq') + ': expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual));
  }
}
function assertIn(haystack, needle, msg) {
  if (typeof haystack !== 'string' || haystack.indexOf(needle) === -1) {
    throw new Error((msg || 'assertIn') + ': expected to find ' + JSON.stringify(needle));
  }
}

/* ---------------- page factory ---------------- */

function defaultRoute(url) {
  if (url.indexOf('/api/read') !== -1) return { body: { lte: ['1', '3'], nr: ['78'], source: 'manual' } };
  if (url.indexOf('/api/signal') !== -1) return { body: { rsrp_dbm: -105, rsrq_db: -9, tech: 'LTE', level: 2, timestamp: Date.now() } };
  if (url.indexOf('/api/registration') !== -1) return { body: { service_state: 'IN_SERVICE', data_state: 'IN_SERVICE', network_type: 'LTE', operator: 'TestNet', roaming: false } };
  if (url.indexOf('/api/band-camping') !== -1) return { body: { samples: [{ timestamp: Date.now(), band: 1, earfcn: 100 }] } };
  if (url.indexOf('/api/defaults') !== -1) return { body: { lte: ['1'], nr: ['78'], carrier: 'other' } };
  if (url.indexOf('/api/settings') !== -1) return { body: { lan_enabled: false, token: null } };
  if (url.indexOf('/api/drop-log') !== -1) return { body: { ok: true, enabled: false, files: [] } };
  return { status: 404, body: {} };
}

async function flush(w) {
  for (let i = 0; i < 12; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

async function createPage(opts) {
  opts = opts || {};
  const dom = new JSDOM(html, {
    url: 'https://mui.kernelsu.org/bandctl/index.html',
    runScripts: 'outside-only'
  });
  const w = dom.window;
  const d = w.document;

  w.scrollTo = () => {}; // jsdom logs not-implemented otherwise
  const ctxStub = new Proxy({}, { get: () => () => {} });
  Object.defineProperty(w.HTMLCanvasElement.prototype, 'getContext', {
    value: () => ctxStub,
    configurable: true,
    writable: true
  });

  const fetchCalls = {};
  w.fetch = async (url) => {
    fetchCalls[String(url)] = (fetchCalls[String(url)] || 0) + 1;
    const route = (opts.fetchRoutes && opts.fetchRoutes[String(url)]) || defaultRoute(String(url));
    return {
      ok: (route.status || 200) < 400,
      status: route.status || 200,
      json: async () => route.body
    };
  };

  if (opts.seed) {
    Object.keys(opts.seed).forEach((k) => w.localStorage.setItem(k, opts.seed[k]));
  }

  w.eval(script);
  await flush(w);

  const $ = (id) => d.getElementById(id);
  return { dom, w, d, $, fetchCalls, flush: () => flush(w) };
}

function teardown(page) {
  try { page.w.stopPolling(); } catch (e) { /* already stopped */ }
  try { clearTimeout(page.w.toastTimer); } catch (e) { /* none pending */ }
  // Defer the close by a tick: the merged page fires fire-and-forget bootstrap
  // reads on token save (A-067 re-bootstrap), and those promise chains must
  // settle on a live document — closing synchronously lets them resolve into a
  // dead window and crash the runner with closed-window rejections.
  setTimeout(function () {
    try { page.dom.window.close(); } catch (e) { /* already closed */ }
  }, 0);
}

function click(w, el) {
  el.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
}
function key(w, el, k, opts) {
  el.dispatchEvent(new w.KeyboardEvent('keydown', Object.assign({ bubbles: true, cancelable: true }, { key: k }, opts)));
}

/* ---------------- WCAG contrast math ---------------- */

function channelLum(v) {
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}
function luminance(hex) {
  const c = hex.replace('#', '');
  const r = parseInt(c.slice(0, 2), 16) / 255;
  const g = parseInt(c.slice(2, 4), 16) / 255;
  const b = parseInt(c.slice(4, 6), 16) / 255;
  return 0.2126 * channelLum(r) + 0.7152 * channelLum(g) + 0.0722 * channelLum(b);
}
function contrast(a, b) {
  const l1 = luminance(a);
  const l2 = luminance(b);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

/* ---------------- CSS / token checks ---------------- */

async function cssTests() {
  console.log('\n[CSS + design tokens]');

  await test('A-027: reduced-motion block keeps color/opacity feedback, drops spatial motion', () => {
    const start = css.indexOf('@media (prefers-reduced-motion: reduce)');
    const end = css.indexOf('WHITE MOBILE INSTRUMENT SKIN');
    assert(start !== -1, 'reduced-motion media query present');
    const rm = css.slice(start, end);

    assertIn(rm, 'animation-duration: 0.01ms !important', 'spatial animations collapsed');
    assertIn(rm, 'animation-iteration-count: 1 !important', 'animations play once');
    assertIn(rm, '.tab-panel.active { animation: none; }', 'panel slide-in disabled');

    const tp = rm.match(/transition-property:\s*([^;]+);/);
    assert(tp, 'transition-property declared');
    assert(/opacity/.test(tp[1]) && /background-color/.test(tp[1]) && /\bcolor\b/.test(tp[1]),
      'color/opacity feedback retained: ' + tp[1]);
    assert(!/transform/.test(tp[1]), 'transform (spatial) excluded from transitions: ' + tp[1]);
  });

  await test('A-029/A-162/A-079: white-skin contrast tokens (4.5:1 palette) are in place', () => {
    const skin = css.slice(css.indexOf('WHITE MOBILE INSTRUMENT SKIN'));
    assertIn(skin, '--accent: #067a8d;', 'darker teal accent (A-029)');
    assertIn(skin, '--accent-ink: #ffffff;', 'white text on accent');
    assertIn(skin, '--success: #16793a;', 'darkened success (A-079)');
    assertIn(skin, '--danger: #c22f28;', 'darkened danger (A-079)');
    assertIn(skin, '--warn: #96620a;', 'darkened warn (A-079)');
  });

  await test('A-018/A-077: --ease and --header-h tokens defined in the skin', () => {
    const skin = css.slice(css.indexOf('WHITE MOBILE INSTRUMENT SKIN'));
    assertIn(skin, '--header-h: 90px;', 'header height token (A-077)');
    assertIn(skin, '--ease: cubic-bezier(0.22, 1, 0.36, 1);', 'ease token (A-018)');
  });

  await test('A-005/A-083: sticky shell — app overflow:clip, header sticky + safe-area top', () => {
    const skin = css.slice(css.indexOf('WHITE MOBILE INSTRUMENT SKIN'));
    assertIn(skin, 'overflow: clip;', 'app does not become a scroll container (A-005)');
    assert(/\.app-header \{[^}]*position: sticky/.test(skin), '.app-header sticky (A-005)');
    assertIn(skin, 'env(safe-area-inset-top', 'header honors top safe area (A-083)');
    assert(/\.tabbar \{[^}]*position: sticky[^}]*top: var\(--header-h\)/.test(skin),
      'tabbar sticks below the header (A-005)');
  });

  await test('A-095: bottom-anchored chrome honors safe-area-inset-bottom', () => {
    const skin = css.slice(css.indexOf('WHITE MOBILE INSTRUMENT SKIN'));
    assertIn(skin, 'env(safe-area-inset-bottom', 'toast/modal bottom safe area present');
    assert(/\.modal-overlay \{[^}]*env\(safe-area-inset-bottom/.test(skin),
      'modal overlay bottom safe area (A-095)');
  });

  await test('A-076/A-172: 44px+ touch targets on interactive controls', () => {
    assertIn(css, '.btn { display: inline-flex', 'base .btn rule present');
    assert(/\.btn \{[^}]*min-height: 48px/.test(css), '.btn min-height 48px');
    assertIn(css, '.btn.small { min-height: 44px;', 'small buttons (A-076)');
    assertIn(css, '.icon-btn { display: inline-grid; width: 44px; min-height: 44px;', 'icon buttons (A-076)');
    assertIn(css, '#server-banner button { min-height: 44px;', 'banner Retry (A-076)');
    assertIn(css, '.switch { position: relative; flex: 0 0 auto; width: 52px; height: 44px; }', 'switches (A-076)');
    assertIn(css, '#lan-token-input { min-height: 44px; }', 'token input (A-172)');
    assertIn(css, '.modal input[type="text"] { min-height: 44px;', 'modal text input');
    assertIn(css, '.modem-reset { width: 100%; min-height: 52px;', 'modem reset (A-076)');
    assertIn(css, '.band { min-width: 0; min-height: 58px;', 'band tiles');
    const skin = css.slice(css.indexOf('WHITE MOBILE INSTRUMENT SKIN'));
    assertIn(skin, 'min-height: 54px;', 'skin tabbar buttons');
  });

  await test('A-107: sr-only utility exists for the signal graph text alternative', () => {
    assertIn(css, '.sr-only {', 'sr-only class defined');
  });

  await test('A-029/A-162/A-079: computed WCAG contrast >= 4.5:1 for the fixed pairs', () => {
    const pairs = [
      ['#ffffff', '#067a8d', 'button text on accent fill (A-029)'],
      ['#067a8d', '#ffffff', 'accent text on white (A-029)'],
      ['#067a8d', '#e4f8fb', 'active band text on its fill (A-162)'],
      ['#067a8d', '#f1fcfd', 'active state on hover fill (A-162)'],
      ['#16793a', '#e9f6e8', 'success text on status bg (A-079)'],
      ['#16793a', '#ffffff', 'success on white (A-079)'],
      ['#c22f28', '#ffffff', 'danger text on white (A-079)'],
      ['#c22f28', '#fff0ee', 'danger on danger-tint bg (A-079)'],
      ['#96620a', '#ffffff', 'warn text on white (A-079)'],
      ['#96620a', '#fdf3e2', 'warn on warn-tint bg (A-079)'],
      ['#111a24', '#ffffff', 'body ink on white (AA body text)']
    ];
    pairs.forEach(([fg, bg, label]) => {
      const r = contrast(fg, bg);
      assert(r >= 4.5, label + ': ' + r.toFixed(2) + ':1 < 4.5:1');
    });
  });
}

/* ---------------- behavior checks ---------------- */

const PRESETS_KEY = 'bandController.presets.v1';
const HISTORY_KEY = 'bandController.history.v1';

async function behaviorTests() {
  console.log('\n[Behavior]');

  await test('harness: script extracts, evaluates, and exposes the app functions', async () => {
    const p = await createPage({});
    try {
      ['switchTab', 'toggleBand', 'openModal', 'closeModal', 'logEntry', 'loadHistory',
        'saveLanToken', 'toggleTokenShown', 'renderPresets', 'checkRegChange'].forEach((fn) => {
        assert(typeof p.w[fn] === 'function', fn + ' exposed on window');
      });
    } finally { teardown(p); }
  });

  await test('A-031/A-107: live regions + canvas text alternative + summaries update', async () => {
    const p = await createPage({});
    try {
      assertEq(p.$('status').getAttribute('role'), 'status', '#status role');
      assertEq(p.$('toast').getAttribute('role'), 'status', '#toast role');
      const canvas = p.$('signal-canvas');
      assertEq(canvas.getAttribute('role'), 'img', 'canvas role=img');
      assert(canvas.getAttribute('aria-label') && canvas.getAttribute('aria-label').length > 0,
        'canvas aria-label present');
      const sum = p.$('signal-summary');
      assert(sum.classList.contains('sr-only'), 'summary is visually hidden');
      assertIn(sum.textContent, 'Signal graph:', 'summary prefixed');
      assertIn(sum.textContent, '-105', 'summary includes latest RSRP');
      assertIn(p.$('status').textContent, 'Loaded from modem (manual)', 'status reflects load');
      assertEq(p.$('sig-rsrp').textContent, '-105', 'readout shows RSRP');
    } finally { teardown(p); }
  });

  await test('A-040: modal moves focus in, traps Tab, restores focus on close', async () => {
    const p = await createPage({});
    try {
      const saveBtn = p.$('preset-save-btn');
      const overlay = p.$('preset-modal');
      const input = p.$('preset-name-input');

      saveBtn.focus();
      click(p.w, saveBtn);
      assert(overlay.classList.contains('open'), 'modal opened');
      assertEq(p.d.activeElement, input, 'focus moved to first focusable inside modal');

      key(p.w, input, 'Tab', { shiftKey: true });
      assertEq(p.d.activeElement, p.$('preset-modal-cancel'), 'Shift+Tab wraps first -> last');

      key(p.w, p.$('preset-modal-cancel'), 'Tab', {});
      assertEq(p.d.activeElement, input, 'Tab wraps last -> first');

      key(p.w, input, 'Escape', {});
      assert(!overlay.classList.contains('open'), 'Escape closed the modal');
      assertEq(p.d.activeElement, saveBtn, 'focus restored to the trigger');
    } finally { teardown(p); }
  });

  await test('A-086: tabset roles, roving tabindex, arrow/Home/End navigation', async () => {
    const p = await createPage({});
    try {
      const tabs = ['tab-bands', 'tab-diag', 'tab-settings'].map((id) => p.$(id));
      tabs.forEach((t) => assertEq(t.getAttribute('role'), 'tab', 'role=tab'));
      assertEq(p.$('panel-bands').getAttribute('role'), 'tabpanel', 'role=tabpanel');

      assertEq(tabs[0].getAttribute('aria-selected'), 'true', 'bands active at init');
      assertEq(tabs[0].getAttribute('tabindex'), '0', 'selected tab in tab order');
      assertEq(tabs[1].getAttribute('tabindex'), '-1', 'inactive tab removed from tab order');
      assertEq(tabs[2].getAttribute('tabindex'), '-1', 'inactive tab removed from tab order');

      key(p.w, tabs[0], 'ArrowRight', {});
      await p.flush();
      assertEq(tabs[1].getAttribute('aria-selected'), 'true', 'ArrowRight activates diag');
      assertEq(tabs[1].getAttribute('tabindex'), '0', 'roving tabindex follows to diag');
      assertEq(tabs[0].getAttribute('tabindex'), '-1', 'bands leaves the tab order');
      assert(p.$('panel-diag').classList.contains('active'), 'diag panel shown');
      assertEq(p.d.activeElement, tabs[1], 'focus moved to the tab');

      key(p.w, tabs[0], 'ArrowLeft', {});
      await p.flush();
      assertEq(tabs[2].getAttribute('aria-selected'), 'true', 'ArrowLeft wraps bands -> settings');
      assert(p.$('panel-settings').classList.contains('active'), 'settings panel shown');

      key(p.w, tabs[1], 'Home', {});
      await p.flush();
      assertEq(tabs[0].getAttribute('aria-selected'), 'true', 'Home returns to bands');

      key(p.w, tabs[0], 'End', {});
      await p.flush();
      assertEq(tabs[2].getAttribute('aria-selected'), 'true', 'End jumps to settings');
    } finally { teardown(p); }
  });

  await test('A-094: event history persists to localStorage and restores on reload', async () => {
    const p = await createPage({});
    try {
      assert(p.$('history-list').children.length >= 1, 'init logged an entry');

      p.w.logEntry('TEST', 'probe one');
      p.w.logEntry('DROP', 'probe two');

      const saved = JSON.parse(p.w.localStorage.getItem(HISTORY_KEY));
      assert(Array.isArray(saved) && saved.length >= 3, 'history written to localStorage');
      assertEq(saved[0].tag, 'DROP', 'newest first in storage');
      assertEq(saved[0].text, 'probe two');
      assertEq(saved[1].text, 'probe one');

      const top = p.$('history-list').children[0];
      assertEq(top.querySelector('.tag').textContent, 'DROP', 'DOM mirrors newest first');
      assertEq(top.querySelector('.hist-text').textContent, 'probe two');
    } finally { teardown(p); }
  });

  await test('A-094: persisted history is restored by init on the next load', async () => {
    const p1 = await createPage({});
    let saved;
    try {
      p1.w.logEntry('DROP', 'outage 1');
      saved = p1.w.localStorage.getItem(HISTORY_KEY);
    } finally { teardown(p1); }

    const p2 = await createPage({ seed: { [HISTORY_KEY]: saved } });
    try {
      const savedCount = JSON.parse(saved).length;
      const list = p2.$('history-list');
      // init prepends one fresh REG entry, then the restored history in order.
      assertEq(list.children.length, savedCount + 1, 'restored history + fresh entry');
      assertEq(list.children[1].querySelector('.tag').textContent, 'DROP', 'restored newest entry');
      assertEq(list.children[1].querySelector('.hist-text').textContent, 'outage 1');
    } finally { teardown(p2); }
  });

  await test('A-081: registration transitions (data/roaming/operator) are logged', async () => {
    const p = await createPage({});
    try {
      const before = p.$('history-list').children.length;
      p.w.lastReg = { service: 'IN_SERVICE', data: 'IN_SERVICE', net: 'LTE', roam: false, op: 'TestNet' };
      p.w.checkRegChange({ service_state: 'IN_SERVICE', data_state: 'OUT_OF_SERVICE', network_type: 'LTE', operator: 'TestNet', roaming: false });
      assertEq(p.$('history-list').children.length, before + 1, 'data transition logged');
      assertIn(p.$('history-list').children[0].querySelector('.hist-text').textContent, 'OUT_OF_SERVICE');

      p.w.checkRegChange({ service_state: 'IN_SERVICE', data_state: 'OUT_OF_SERVICE', network_type: 'LTE', operator: 'OtherNet', roaming: true });
      assertEq(p.$('history-list').children.length, before + 2, 'operator/roaming transition logged');
      assertIn(p.$('history-list').children[0].querySelector('.hist-text').textContent, 'roaming');
      assertIn(p.$('history-list').children[0].querySelector('.hist-text').textContent, 'OtherNet');
    } finally { teardown(p); }
  });

  await test('A-121: band selection keeps keyboard focus on the re-rendered tile', async () => {
    const p = await createPage({});
    try {
      const tile = p.d.querySelector('.band[data-band="1"][data-type="lte"]');
      assert(tile, 'band tile rendered');
      assertEq(tile.getAttribute('aria-pressed'), 'true', 'band 1 starts enabled (fake read)');

      tile.focus();
      click(p.w, tile);
      const afterClick = p.d.activeElement;
      assertEq(afterClick.getAttribute('data-band'), '1', 'focus retained on tile after click');
      assertEq(afterClick.getAttribute('data-type'), 'lte');
      assert(afterClick !== tile, 'tile was re-rendered (new node)');
      assertEq(afterClick.getAttribute('aria-pressed'), 'false', 'click toggled band off');

      key(p.w, afterClick, 'Enter', {});
      const afterKey = p.d.activeElement;
      assertEq(afterKey.getAttribute('data-band'), '1', 'focus retained on tile after Enter');
      assertEq(afterKey.getAttribute('aria-pressed'), 'true', 'Enter toggled band back on');
    } finally { teardown(p); }
  });

  await test('A-191: token input starts masked, show/hide toggles, save re-masks', async () => {
    const p = await createPage({
      fetchRoutes: { '/api/settings?action=settings': { body: { lan_enabled: true, token: null } } }
    });
    try {
      const input = p.$('lan-token-input');
      const toggle = p.$('lan-token-input-toggle');
      assertEq(input.type, 'password', 'token input starts masked');
      assertEq(toggle.textContent, 'Show');

      click(p.w, toggle);
      assertEq(input.type, 'text', 'Show reveals the token');
      assertEq(toggle.textContent, 'Hide');

      click(p.w, toggle);
      assertEq(input.type, 'password', 'Hide re-masks');
      assertEq(toggle.textContent, 'Show');

      input.value = 'abc123xyz';
      click(p.w, p.$('lan-token-save'));
      assertEq(p.w.localStorage.getItem('bandController.token.v1'), 'abc123xyz', 'token stored');
      assertEq(input.value, '', 'input cleared after save');
      assertEq(input.type, 'password', 'input re-masked after save (A-191)');
      assertEq(toggle.textContent, 'Show', 'toggle label reset after save');
      assertEq(p.$('lan-token').textContent, 'abc123\u2026', 'display shows masked token');
      assertEq(p.$('lan-token-show').textContent, 'Show');

      click(p.w, p.$('lan-token-show'));
      assertEq(p.$('lan-token').textContent, 'abc123xyz', 'Show reveals the full token');
      assertEq(p.$('lan-token-show').textContent, 'Hide');
    } finally { teardown(p); }
  });

  await test('A-192: preset delete requires an in-page confirm', async () => {
    const seedPresets = JSON.stringify([
      { name: 'Work', lte: ['1'], nr: ['78'], savedAt: 1700000000000 },
      { name: 'Play', lte: ['3'], nr: [], savedAt: 1700000000001 }
    ]);
    const p = await createPage({ seed: { [PRESETS_KEY]: seedPresets } });
    try {
      const rows = p.d.querySelectorAll('#preset-list .preset-item');
      assertEq(rows.length, 2, 'two presets rendered');

      const del = p.d.querySelector('[data-action="del"][data-index="0"]');
      del.focus();
      click(p.w, del);
      assert(p.$('delete-modal').classList.contains('open'), 'confirm modal opened before delete');
      assertIn(p.$('delete-modal-text').textContent, 'Work', 'modal names the preset');
      assertEq(p.d.activeElement, p.$('delete-modal-cancel'), 'focus moved into the modal');
      assertEq(JSON.parse(p.w.localStorage.getItem(PRESETS_KEY)).length, 2, 'nothing deleted before confirm');

      click(p.w, p.$('delete-modal-confirm'));
      assert(!p.$('delete-modal').classList.contains('open'), 'modal closed after confirm');
      const after = JSON.parse(p.w.localStorage.getItem(PRESETS_KEY));
      assertEq(after.length, 1, 'preset deleted only after confirm');
      assertEq(after[0].name, 'Play');
      assertIn(p.$('toast').textContent, 'deleted', 'toast confirms deletion');
      assertEq(p.d.activeElement.getAttribute('data-action'), 'load', 'focus parked on the replacement row');
    } finally { teardown(p); }
  });

  await test('A-192: cancelling the delete confirm keeps the preset', async () => {
    const seedPresets = JSON.stringify([
      { name: 'Work', lte: ['1'], nr: ['78'], savedAt: 1700000000000 }
    ]);
    const p = await createPage({ seed: { [PRESETS_KEY]: seedPresets } });
    try {
      const del = p.d.querySelector('[data-action="del"][data-index="0"]');
      del.focus();
      click(p.w, del);
      click(p.w, p.$('delete-modal-cancel'));
      assert(!p.$('delete-modal').classList.contains('open'), 'modal closed on cancel');
      assertEq(JSON.parse(p.w.localStorage.getItem(PRESETS_KEY)).length, 1, 'preset kept on cancel');
      assertEq(p.d.activeElement, del, 'focus restored to the delete trigger');
    } finally { teardown(p); }
  });
}



  if (!/function init\(\)/.test(script)) {
    console.error('harness error: inline script extraction failed');
    process.exitCode = 1;
    return;
  }
  await cssTests();
  await behaviorTests();
  console.log('\n[a11y] ' + passed + ' passed, ' + failed + ' failed');
  failures.forEach((f) => {
    console.log('\n--- ' + f.name);
    console.log(f.error && f.error.stack ? f.error.stack : String(f.error));
  });
  if (failed) process.exitCode = 1;
  __suiteDone('a11y');
})().catch((e) => {
  console.error('harness crashed:', e && e.stack ? e.stack : e);
  process.exitCode = 1;
  __suiteDone('a11y');
});
