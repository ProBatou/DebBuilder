const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function classList() {
  const values = new Set();
  return {
    add: value => values.add(value),
    remove: value => values.delete(value),
    toggle: (value, enabled) => enabled ? values.add(value) : values.delete(value),
    contains: value => values.has(value),
  };
}

function element() {
  return {
    classList: classList(),
    disabled: false,
    hidden: false,
    innerHTML: '',
    scrollHeight: 0,
    scrollTop: 0,
    clientHeight: 0,
    textContent: '',
    value: '',
  };
}

const nodes = Object.fromEntries([
  'btnPublishExecution', 'btnRevalidateExecution', 'executionDetail', 'executionList',
  'executionMeta', 'executionMetaMore', 'executionMoreDetails', 'executionSteps',
  'logSearch', 'logStatus', 'view-logs',
].map(id => [id, element()]));
nodes['view-logs'].classList.add('active');
nodes.executionMoreDetails.hidden = true;
const localizedDates = [];

class BrowserDate {
  constructor(value) { this.value = value; }
  getTime() { return this.value.includes('99:99') ? NaN : 1; }
  toLocaleString(locale, options) {
    localizedDates.push({value: this.value, locale, options});
    return '05/09/2026 13:21:35';
  }
}

const context = vm.createContext({
  STATUS_LABELS: {
    building: 'Building',
    validating: 'Validating',
    validation_failed: 'Validation failed',
    ready_to_publish: 'Ready to publish',
    publishing: 'Publishing',
    published: 'Published',
  },
  Date: BrowserDate,
  adminState: {
    executionAction: null,
    executionListRevision: 0,
    executions: [{id: 'run-one', lifecycle_status: 'building', status: 'running'}],
    logAutoScroll: true,
    logOffset: 0,
    logVerbosity: 'raw',
    selectedExecution: null,
  },
  badge: status => `<b>${status}</b>`,
  document: {
    querySelector: () => element(),
  },
  esc: value => String(value ?? ''),
  fetch: async () => ({
    ok: true,
    json: async () => ({log: {
      text: '2026-09-05T11:21:35.481846+00:00 Build started  with spacing\nplain message unchanged\n2026-09-05T99:99:35+00:00 invalid date unchanged\n',
      offset: 152,
    }}),
  }),
  fmtTime: value => String(value ?? ''),
  packageLabelForExecution: execution => execution.package || execution.recipe_id || 'Operation',
  setTimeout: () => 1,
  clearTimeout: () => {},
  $: id => nodes[id] || null,
});

vm.runInContext(fs.readFileSync('static/js/pages/logs.js', 'utf8'), context, {filename: 'logs.js'});

function canonical(lifecycle, allowedActions, validationStatus, publicationStatus) {
  return {
    id: 'run-one',
    package: 'demo',
    recipe_id: 'demo-recipe',
    action: 'build',
    mode: 'build',
    status: 'success',
    build_status: 'success',
    validation_status: validationStatus,
    publication_status: publicationStatus,
    lifecycle_status: lifecycle,
    lifecycle_active: ['validating', 'publishing'].includes(lifecycle),
    allowed_actions: allowedActions,
    updated: 1,
    version: {upstream: '2.0', debian: '2.0-1'},
    artifact: {
      path: '/builds/run-one/artifacts/demo_2.0-1_all.deb',
      size: 3,
      sha256: 'abc',
      inspection: {package: 'demo', version: '2.0-1'},
    },
    validations: validationStatus === 'not_run' ? [] : [{status: validationStatus}],
    publications: publicationStatus === 'not_run' ? [] : [{status: publicationStatus}],
    steps: [{name: 'build', status: 'success'}],
  };
}

context.canonical = canonical('ready_to_publish', {validate: true, publish: true}, 'success', 'not_run');
vm.runInContext('applyCanonicalExecution(canonical)', context);
assert.equal(context.adminState.selectedExecution.lifecycle_status, 'ready_to_publish');
assert.equal(context.adminState.executions[0].lifecycle_status, 'ready_to_publish');
assert.match(nodes.executionList.innerHTML, /ready_to_publish/);
assert.match(nodes.executionMeta.innerHTML, /Ready to publish/);
assert.match(nodes.executionMetaMore.innerHTML, /demo_2\.0-1_all\.deb/);
assert.match(nodes.executionSteps.innerHTML, /build · success/);
assert.equal(nodes.btnRevalidateExecution.hidden, false);
assert.equal(nodes.btnPublishExecution.hidden, false);

context.canonical = canonical('publishing', {validate: false, publish: false}, 'success', 'running');
vm.runInContext('applyCanonicalExecution(canonical, {preserveLog: true})', context);
assert.match(nodes.executionList.innerHTML, /publishing/);
assert.equal(nodes.btnRevalidateExecution.hidden, true);
assert.equal(nodes.btnPublishExecution.hidden, true);

context.canonical = canonical('published', {validate: true, publish: false}, 'success', 'success');
vm.runInContext('applyCanonicalExecution(canonical, {preserveLog: true})', context);
assert.match(nodes.executionMeta.innerHTML, /Published/);
assert.equal(nodes.btnRevalidateExecution.hidden, false);
assert.equal(nodes.btnPublishExecution.hidden, true);

context.canonical = canonical('validation_failed', {validate: true, publish: false}, 'failed', 'not_run');
vm.runInContext('applyCanonicalExecution(canonical, {preserveLog: true})', context);
assert.match(nodes.executionList.innerHTML, /validation_failed/);
assert.equal(nodes.btnRevalidateExecution.hidden, false);
assert.equal(nodes.btnPublishExecution.hidden, true);

(async () => {
  await context.loadExecutionLog('run-one', {reset: true});
  assert.equal(nodes.executionDetail.textContent, '05/09/2026 13:21:35 Build started  with spacing\nplain message unchanged\n2026-09-05T99:99:35+00:00 invalid date unchanged\n');
  assert.equal(localizedDates.length, 1);
  assert.equal(localizedDates[0].value, '2026-09-05T11:21:35.481846+00:00');
  assert.equal(localizedDates[0].locale, undefined);
  assert.equal(localizedDates[0].options.second, '2-digit');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
