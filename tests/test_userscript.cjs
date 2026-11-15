const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'wallpaper-engine-video-deduplication.js'), 'utf8');

function element() {
  return {
    style: {}, textContent: '', children: [], handlers: {},
    appendChild(child) { this.children.push(child); },
    setAttribute() {},
    addEventListener(name, callback) { this.handlers[name] = callback; },
  };
}

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert(first >= 0 && last > first);
  return source.slice(first, last);
}

function context(extra = {}) {
  return vm.createContext({
    console: { log() {}, warn() {}, error() {} },
    setTimeout: (callback, milliseconds) => setTimeout(callback, Math.max(1, milliseconds / 100)),
    Date: { now: () => Date.now() * 100 },
    PROCESSING: false,
    FAST_MODE: true,
    DEFAULT_APPID: '431960',
    ACTIVE_JOB_ID: 'job',
    DURABLE_BACKEND: true,
    COMPLETED_PROGRESS: null,
    LEASE_TOKENS: new Map(),
    parseHashParam: () => '',
    isUnsubSessionDead: () => false,
    showUnsubSessionExpired: () => { throw new Error('Unexpected expired session'); },
    pendingReports: () => ({}),
    document: {
      createElement: element,
      body: { appendChild() {} },
    },
    ...extra,
  });
}

async function testSuccessResponses() {
  const sandbox = context();
  vm.runInContext(section('function unsubscribeResponseSucceeded', '// 直接通过API'), sandbox);
  for (const value of ['1', 'true', '{"success":1}', '{"success":true}', '{"success":"1"}']) {
    assert.equal(sandbox.unsubscribeResponseSucceeded(value), true);
  }
  for (const value of ['', '0', 'false', '{"success":0}', '<html>login</html>']) {
    assert.equal(sandbox.unsubscribeResponseSucceeded(value), false);
  }
}

async function testParallelRunner() {
  let next = 0;
  let active = 0;
  let peak = 0;
  const reports = [];
  const requested = [];
  const sandbox = context({
    fetchBatchIdsPromise: async count => {
      requested.push(count);
      if (next === 24) return { complete: true, tasks: [], progress: { total: 24, ok: 24 } };
      const tasks = [];
      while (tasks.length < count && next < 24) tasks.push({ id: String(++next), token: String(next) });
      return { job: 'job', tasks, progress: { total: 24, ok: reports.length } };
    },
    unsubscribeByAPI: async id => {
      active++;
      peak = Math.max(peak, active);
      await new Promise(resolve => setTimeout(resolve, 20));
      active--;
      return { success: true, reason: id };
    },
    reportUnsubProgress: async (id, success, detail) => {
      reports.push({ id, success, token: detail.token });
      return true;
    },
  });
  vm.runInContext(section('async function processDurableQueue', '// 单页面模式：批量处理工作项'), sandbox);
  await sandbox.processDurableQueue();
  assert.equal(requested[0], 8);
  assert(peak > 2 && peak <= 8);
  assert.equal(reports.length, 24);
  assert(reports.every(report => report.success && report.token === report.id));
  assert.equal(sandbox.PROCESSING, false);
}

async function testRateLimitCooldown() {
  let state = 'ready';
  let attempts = 0;
  let limitedAt = 0;
  let retryAt = 0;
  const sandbox = context({
    fetchBatchIdsPromise: async () => {
      if (state === 'done') return { complete: true, tasks: [], progress: { total: 1, ok: 1 } };
      if (state === 'inflight') return { tasks: [], retry_after_ms: 100, progress: { total: 1 } };
      state = 'inflight';
      return { job: 'job', tasks: [{ id: '1', token: String(attempts + 1) }], progress: { total: 1 } };
    },
    unsubscribeByAPI: async () => {
      if (++attempts === 1) {
        limitedAt = Date.now() * 100;
        return { success: false, reason: 'http_429' };
      }
      retryAt = Date.now() * 100;
      return { success: true };
    },
    reportUnsubProgress: async (id, success) => {
      state = success ? 'done' : 'ready';
      return true;
    },
  });
  vm.runInContext(section('async function processDurableQueue', '// 单页面模式：批量处理工作项'), sandbox);
  await sandbox.processDurableQueue();
  assert(retryAt - limitedAt >= 10000);
}

async function testFailedReportSurvivesUntilAcknowledged() {
  let online = false;
  const saved = {};
  const payloads = [];
  const sandbox = context({
    API_NEXT: 'http://127.0.0.1:8787/next',
    LEASE_TOKENS: new Map([['123', 'lease']]),
    storeReport: (key, value) => { if (value) saved[key] = value; else delete saved[key]; },
    buildReportUrl: () => 'http://127.0.0.1:8787/report',
    GM_xmlhttpRequest: options => {
      payloads.push(JSON.parse(options.data));
      if (online) options.onload({ status: 200 });
      else options.onerror();
    },
  });
  vm.runInContext(section('async function reportUnsubProgress', '  const D = document;'), sandbox);
  assert.equal(await sandbox.reportUnsubProgress('123', false, { reason: 'http_429' }), false);
  assert.equal(Object.keys(saved).length, 1);
  assert.equal(saved['job:123:lease'].reason, 'http_429');
  online = true;
  assert.equal(await sandbox.reportUnsubProgress('123', false, saved['job:123:lease']), true);
  assert.equal(Object.keys(saved).length, 0);
  assert(payloads.every(value => value.token === 'lease' && value.job === 'job'));
}

async function testStillSubscribedIsReportedAsFailure() {
  const sandbox = context({
    FAST_MODE: false,
    get_sessionid: () => 'session',
    normalizeAppId: value => value,
    checkSubscribeState: async () => 'subscribed',
    xhrText: async () => ({ status: 200, text: '{"success":1}' }),
    FormData: class { append() {} },
  });
  vm.runInContext(section('function unsubscribeResponseSucceeded', '// 直接通过API'), sandbox);
  vm.runInContext(section('async function unsubscribeByAPI', '// ========================='), sandbox);
  const result = await sandbox.unsubscribeByAPI('123', '431960', 1);
  assert.equal(result.success, false);
  assert.equal(result.reason, 'still_subscribed');
}

async function testFinalReportCanFinishWithoutAnotherPoll() {
  let polls = 0;
  const sandbox = context({
    parseHashParam: name => name === 'workers' ? '1' : '',
    fetchBatchIdsPromise: async () => {
      assert.equal(++polls, 1);
      return { job: 'job', tasks: [{ id: '1', token: 'lease' }], progress: { total: 1 } };
    },
    unsubscribeByAPI: async () => ({ success: true }),
    reportUnsubProgress: async () => {
      sandbox.COMPLETED_PROGRESS = { total: 1, ok: 1, fail: 0 };
      return true;
    },
  });
  vm.runInContext(section('async function processDurableQueue', '// 单页面模式：批量处理工作项'), sandbox);
  await sandbox.processDurableQueue();
  assert.equal(polls, 1);
}

async function testIdleSlotIsFilledBeforeSlowTaskFinishes() {
  const queue = ['1', '2', '3'];
  const finished = new Set();
  let releaseSlow;
  let slowFinished = false;
  let refillObserved = false;
  const sandbox = context({
    parseHashParam: name => name === 'workers' ? '2' : '',
    fetchBatchIdsPromise: async count => ({
      job: 'job', tasks: queue.splice(0, count).map(id => ({ id, token: id })),
      complete: !queue.length && finished.size === 3, retry_after_ms: 100, progress: { total: 3 },
    }),
    unsubscribeByAPI: async id => {
      if (id === '1') {
        await new Promise(resolve => { releaseSlow = resolve; });
        slowFinished = true;
      } else if (id === '2') {
        await new Promise(resolve => setTimeout(resolve, 4));
      } else {
        refillObserved = !slowFinished;
        releaseSlow();
      }
      return { success: true };
    },
    reportUnsubProgress: async id => { finished.add(id); return true; },
  });
  vm.runInContext(section('async function processDurableQueue', '// 单页面模式：批量处理工作项'), sandbox);
  let deadline;
  try {
    await Promise.race([
      sandbox.processDurableQueue(),
      new Promise((resolve, reject) => { deadline = setTimeout(() => reject(new Error('Slow task blocked the free slot')), 1000); }),
    ]);
  } finally {
    clearTimeout(deadline);
  }
  assert(refillObserved);
  assert.equal(finished.size, 3);
}

async function testConcurrencyCanBeAdjustedLive() {
  const inputs = [];
  const requested = [];
  const sandbox = context({
    document: {
      createElement: name => {
        const node = element();
        if (name === 'input') inputs.push(node);
        return node;
      },
      body: { appendChild() {} },
    },
    fetchBatchIdsPromise: async count => {
      requested.push(count);
      if (requested.length === 1) {
        inputs[0].value = '99';
        inputs[0].handlers.change();
        return { tasks: [], retry_after_ms: 100, progress: {} };
      }
      return { tasks: [], complete: true, progress: {} };
    },
  });
  vm.runInContext(section('async function processDurableQueue', '// 单页面模式：批量处理工作项'), sandbox);
  await sandbox.processDurableQueue();
  assert.deepEqual(requested, [8, 16]);
  assert.equal(inputs[0].value, '16');
}

async function testConcurrentRateLimitReportsShareSlowdown() {
  const initial = Array.from({ length: 8 }, (_, index) => String(index + 1));
  const retries = [];
  const firstResponses = [];
  const retryStarts = [];
  const waitsAfterFirstRetry = [];
  const seen = new Set();
  let failures = 0;
  let completed = 0;
  const sandbox = context({
    setTimeout: (callback, milliseconds) => {
      if (retryStarts.length === 1) waitsAfterFirstRetry.push(milliseconds);
      return setTimeout(callback, Math.max(1, milliseconds / 100));
    },
    fetchBatchIdsPromise: async count => {
      const ids = initial.length ? initial.splice(0, count) : (failures === 8 ? retries.splice(0, count) : []);
      return { job: 'job', tasks: ids.map(id => ({ id, token: id })), complete: completed === 8,
        retry_after_ms: 100, progress: { total: 8, ok: completed } };
    },
    unsubscribeByAPI: async id => {
      if (!seen.has(id)) {
        seen.add(id);
        return new Promise(resolve => {
          firstResponses.push(resolve);
          if (firstResponses.length === 8) {
            for (const response of firstResponses) response({ success: false, reason: 'http_429' });
          }
        });
      }
      retryStarts.push(Date.now() * 100);
      return { success: true };
    },
    reportUnsubProgress: async (id, success) => {
      if (success) completed++;
      else { failures++; retries.push(id); }
      return true;
    },
  });
  vm.runInContext(section('async function processDurableQueue', '// 单页面模式：批量处理工作项'), sandbox);
  await sandbox.processDurableQueue();
  assert.equal(completed, 8);
  assert(waitsAfterFirstRetry.every(milliseconds => milliseconds < 1500));
}

async function main() {
  await testSuccessResponses();
  await testParallelRunner();
  await testRateLimitCooldown();
  await testFailedReportSurvivesUntilAcknowledged();
  await testStillSubscribedIsReportedAsFailure();
  await testFinalReportCanFinishWithoutAnotherPoll();
  await testIdleSlotIsFilledBeforeSlowTaskFinishes();
  await testConcurrencyCanBeAdjustedLive();
  await testConcurrentRateLimitReportsShareSlowdown();
  console.log('9 userscript tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
