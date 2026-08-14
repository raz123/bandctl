'use strict';
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'web', 'index.html'), 'utf8');

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

(async () => {
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

  console.log('\nTOTAL: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('HARNESS ERROR', e); process.exit(2); });
