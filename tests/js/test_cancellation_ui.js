const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function classList(initial = []) {
  const values = new Set(initial);
  return {
    add: value => values.add(value),
    remove: value => values.delete(value),
    toggle: (value, enabled) => enabled ? values.add(value) : values.delete(value),
    contains: value => values.has(value),
  };
}

function element(overrides = {}) {
  return {
    addEventListener: () => {},
    classList: classList(),
    className: '',
    close() { this.open = false; },
    disabled: false,
    hidden: false,
    innerHTML: '',
    insertAdjacentHTML(_where, value) { this.innerHTML += value; },
    open: false,
    removeAttribute: () => {},
    scrollHeight: 0,
    scrollTop: 0,
    showModal() { this.open = true; },
    textContent: '',
    value: '',
    ...overrides,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function run(id, status, extra = {}) {
  return {
    id,
    package: 'demo',
    recipe_id: 'demo-recipe',
    action: 'build',
    mode: 'dry_run',
    status,
    build_status: status,
    lifecycle_status: status,
    lifecycle_active: ['queued', 'running', 'cancelling'].includes(status),
    allowed_actions: {validate: false, publish: false},
    ready_for_build: status === 'prepared',
    updated: 1,
    steps: [],
    ...extra,
  };
}

function logsHarness() {
  const ids = [
    'btnCancelExecution', 'btnDeleteExecutionLog', 'btnLogLiveBadge', 'btnPublishExecution',
    'btnRevalidateExecution', 'executionCancellationSummary', 'executionDetail', 'executionDiagnostic',
    'executionList', 'executionMeta', 'executionMetaMore', 'executionMoreDetails', 'executionSteps',
    'logSearch', 'logStatus', 'view-logs',
  ];
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  nodes['view-logs'].classList.add('active');
  const toasts = [];
  const timers = [];
  const context = vm.createContext({
    STATUS_LABELS: {queued: 'Queued', running: 'Running', cancelling: 'Cancelling…', cancelled: 'Cancelled', success: 'Success'},
    adminState: {
      executionAction: null, executionCancellation: null, executionListRevision: 0, executions: [],
      logAutoScroll: true, logFollowing: false, logOffset: 0, logPollTimer: null,
      logVerbosity: 'normal', selectedExecution: null,
    },
    badge: status => `<b>${status}</b>`,
    clearTimeout: () => {},
    closeLogDetail: () => {},
    document: {body: element(), querySelector: () => element()},
    esc: value => String(value ?? ''),
    executionStatusIsActive: status => ['pending', 'queued', 'running', 'cancelling'].includes(status),
    fetch: async () => ({ok: true, json: async () => ({log: {text: '', offset: 0}})}),
    fmtTime: value => String(value ?? ''),
    getJson: async () => ({execution: run('logs-run', 'cancelled')}),
    loadPackages: async () => {},
    packageLabelForExecution: execution => execution.package || 'Operation',
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
    showToast: (message, options) => toasts.push({message, options}),
    $: id => nodes[id] || null,
  });
  vm.runInContext(fs.readFileSync('static/js/build_insight.js', 'utf8'), context, {filename: 'build_insight.js'});
  vm.runInContext(fs.readFileSync('static/js/pages/logs.js', 'utf8'), context, {filename: 'logs.js'});
  return {context, nodes, timers, toasts};
}

function modalHarness() {
  const ids = [
    'btnCloseTestRun', 'btnTestRunBuild', 'btnTestRunCancel', 'btnTestRunClose', 'btnTestRunLogs',
    'testRunDescription', 'testRunDialog', 'testRunFailed', 'testRunKind', 'testRunLiveLog',
    'testRunPreflightContent', 'testRunPreflightStatus', 'testRunPreflightSummary', 'testRunPrepared',
    'testRunState', 'testRunStateHelp', 'testRunStatus', 'testRunTitle',
  ];
  const nodes = Object.fromEntries(ids.map(id => [id, element()]));
  nodes.testRunDialog.open = true;
  const timers = [];
  const toasts = [];
  let canonical = run('modal-run', 'running');
  const context = vm.createContext({
    STATUS_LABELS: {queued: 'Queued', running: 'Running', cancelling: 'Cancelling…', cancelled: 'Cancelled', prepared: 'Prepared'},
    clearTimeout: () => {},
    esc: value => String(value ?? ''),
    executionDiagnosticHtml: () => '',
    executionStatusIsActive: status => ['pending', 'queued', 'running', 'cancelling'].includes(status),
    fetch: async () => ({ok: true, json: async () => ({log: {text: 'progress'}})}),
    getJson: async () => ({execution: canonical}),
    preflightReportPresentation: () => ({statusClass: 'neutral', status: 'Prepared', summary: 'Ready', content: '<p>report</p>'}),
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
    showToast: (message, options) => toasts.push({message, options}),
    $: id => nodes[id] || null,
  });
  vm.runInContext(fs.readFileSync('static/js/recipe/test_run_modal.js', 'utf8'), context, {filename: 'test_run_modal.js'});
  context.testRunModalState = vm.runInContext('testRunModalState', context);
  context.testRunModalState.runId = 'modal-run';
  context.setCanonical = value => { canonical = value; };
  return {context, nodes, timers, toasts};
}

function packageActionHarness() {
  const context = vm.createContext({
    adminState: {packages: []},
    badge: status => status,
    esc: value => String(value ?? ''),
    $: () => null,
  });
  vm.runInContext(fs.readFileSync('static/js/pages/packages.js', 'utf8'), context, {filename: 'packages.js'});
  return context;
}

(async () => {
  const logs = logsHarness();
  for (const status of ['queued', 'running']) {
    logs.context.applyCanonicalExecution(run('logs-run', status));
    assert.equal(logs.nodes.btnCancelExecution.hidden, false);
    assert.equal(logs.nodes.btnCancelExecution.disabled, false);
    assert.equal(logs.nodes.btnCancelExecution.textContent, 'Cancel');
  }
  logs.context.applyCanonicalExecution(run('logs-run', 'cancelling'));
  assert.equal(logs.nodes.btnCancelExecution.hidden, false);
  assert.equal(logs.nodes.btnCancelExecution.disabled, true);
  assert.equal(logs.nodes.btnCancelExecution.textContent, 'Cancelling…');
  logs.context.applyCanonicalExecution(run('logs-run', 'cancelled', {
    cancellation: {kind: 'cancelled', stage: 'build'},
    steps: [{name: 'build', status: 'cancelled'}],
  }));
  assert.equal(logs.nodes.btnCancelExecution.hidden, true);
  assert.match(logs.nodes.executionCancellationSummary.innerHTML, /Requested by user during Build/);
  assert.match(logs.nodes.executionSteps.innerHTML, /step-chip cancelled/);
  logs.context.applyCanonicalExecution(run('logs-shutdown', 'cancelled', {
    cancellation: {kind: 'cancelled', reason: 'server_shutdown', stage: 'build'},
  }));
  assert.match(logs.nodes.executionCancellationSummary.innerHTML, /Server shutdown requested cancellation during Build/);

  logs.context.applyCanonicalExecution(run('logs-run', 'running'));
  const pendingLogs = deferred();
  let logsRequests = 0;
  logs.context.cancelExecutionRequest = async () => { logsRequests += 1; return pendingLogs.promise; };
  const firstCancel = logs.context.cancelOpenExecution('logs-run');
  const duplicateCancel = logs.context.cancelOpenExecution('logs-run');
  assert.equal(logsRequests, 1);
  assert.equal(logs.nodes.btnCancelExecution.disabled, true);
  assert.equal(logs.nodes.btnCancelExecution.textContent, 'Cancelling…');
  logs.context.adminState.executionCancellation = {id: 'logs-run', state: 'refreshing'};
  logs.context.updateExecutionActionButtons(run('logs-run', 'running'));
  assert.equal(logs.nodes.btnCancelExecution.hidden, true);
  logs.context.adminState.executionCancellation = null;
  pendingLogs.resolve({outcome: 'cancelling', httpStatus: 202, payload: {status: 'cancelling'}});
  await Promise.all([firstCancel, duplicateCancel]);
  assert.equal(logs.context.adminState.selectedExecution.status, 'cancelling');
  assert.equal(logs.context.adminState.executionCancellation.state, 'accepted');
  assert.equal(logs.nodes.btnCancelExecution.disabled, true);
  assert.equal(logs.timers.some(timer => timer.delay === 0), true);

  logs.context.adminState.executionCancellation = null;
  logs.context.applyCanonicalExecution(run('logs-run', 'queued'));
  let refreshes = 0;
  logs.context.refreshExecutionLifecycle = async () => { refreshes += 1; };
  logs.context.cancelExecutionRequest = async () => ({outcome: 'cancelled', httpStatus: 200, payload: {status: 'cancelled', stage: 'queue'}});
  await logs.context.cancelOpenExecution('logs-run');
  assert.equal(logs.context.adminState.selectedExecution.status, 'cancelled');
  assert.equal(logs.nodes.btnCancelExecution.hidden, true);
  assert.equal(refreshes, 1);

  logs.context.applyCanonicalExecution(run('logs-run', 'running'));
  logs.context.refreshExecutionLifecycle = async () => {
    refreshes += 1;
    logs.context.applyCanonicalExecution(run('logs-run', 'success'));
  };
  logs.context.cancelExecutionRequest = async () => ({outcome: 'not_cancellable', httpStatus: 409, payload: {}});
  await logs.context.cancelOpenExecution('logs-run');
  assert.equal(logs.context.adminState.selectedExecution.status, 'success');
  assert.equal(logs.toasts.at(-1).message, 'Run already finished.');
  assert.equal(logs.toasts.at(-1).options.type, 'info');

  logs.context.applyCanonicalExecution(run('logs-run', 'running'));
  logs.context.cancelExecutionRequest = async () => { throw new Error('manager down'); };
  await logs.context.cancelOpenExecution('logs-run');
  assert.equal(logs.nodes.btnCancelExecution.hidden, false);
  assert.equal(logs.nodes.btnCancelExecution.disabled, false);
  assert.equal(logs.toasts.at(-1).options.type, 'error');

  const modal = modalHarness();
  modal.context.renderTestRunModal(run('modal-run', 'queued'));
  assert.equal(modal.nodes.btnTestRunCancel.hidden, false);
  assert.equal(modal.nodes.btnTestRunCancel.disabled, false);
  modal.context.renderTestRunModal(run('modal-run', 'running'));
  assert.equal(modal.nodes.btnTestRunCancel.hidden, false);
  modal.context.renderTestRunModal(run('modal-run', 'cancelling'));
  assert.equal(modal.nodes.testRunState.textContent, 'Cancelling…');
  assert.equal(modal.nodes.btnTestRunCancel.disabled, true);
  assert.equal(modal.context.testRunIsActive(run('modal-run', 'cancelling')), true);
  modal.context.testRunModalState.cancellationState = 'refreshing';
  modal.context.renderTestRunModal(run('modal-run', 'running'));
  assert.equal(modal.nodes.btnTestRunCancel.hidden, true);
  modal.context.testRunModalState.cancellationState = '';

  modal.context.renderTestRunModal(run('modal-run', 'cancelled'));
  assert.equal(modal.nodes.testRunState.textContent, 'Cancelled');
  assert.equal(modal.nodes.testRunStateHelp.textContent, 'The Test was cancelled before completion.');
  assert.equal(modal.nodes.testRunFailed.hidden, true);
  assert.equal(modal.nodes.btnTestRunBuild.hidden, true);
  assert.equal(modal.nodes.btnTestRunCancel.hidden, true);
  const timersBeforeTerminal = modal.timers.length;
  modal.context.scheduleTestRunModalPoll(run('modal-run', 'cancelled'), 1500);
  assert.equal(modal.timers.length, timersBeforeTerminal);

  modal.context.renderTestRunModal(run('modal-run', 'prepared', {ready_for_build: true}));
  assert.equal(modal.nodes.btnTestRunBuild.hidden, false);
  modal.context.renderTestRunModal(run('modal-run', 'prepared', {ready_for_build: false}));
  assert.equal(modal.nodes.btnTestRunBuild.hidden, true);

  modal.context.renderTestRunModal(run('modal-run', 'running'));
  const pendingModal = deferred();
  let modalRequests = 0;
  modal.context.cancelExecutionRequest = async () => { modalRequests += 1; return pendingModal.promise; };
  const firstModalCancel = modal.context.cancelTestRun();
  const duplicateModalCancel = modal.context.cancelTestRun();
  assert.equal(modalRequests, 1);
  assert.equal(modal.nodes.btnTestRunCancel.disabled, true);
  pendingModal.resolve({outcome: 'cancelling', httpStatus: 202, payload: {status: 'cancelling'}});
  await Promise.all([firstModalCancel, duplicateModalCancel]);
  assert.equal(modal.nodes.testRunState.textContent, 'Cancelling…');
  assert.equal(modal.context.testRunModalState.cancellationState, 'accepted');
  assert.equal(modal.timers.some(timer => timer.delay === 0), true);

  modal.context.testRunModalState.cancellationState = '';
  modal.nodes.testRunDialog.open = true;
  modal.context.renderTestRunModal(run('modal-run', 'queued'));
  modal.context.setCanonical(run('modal-run', 'cancelled'));
  modal.context.cancelExecutionRequest = async () => ({outcome: 'cancelled', httpStatus: 200, payload: {status: 'cancelled'}});
  await modal.context.cancelTestRun();
  assert.equal(modal.nodes.testRunState.textContent, 'Cancelled');
  assert.equal(modal.nodes.btnTestRunBuild.hidden, true);

  modal.context.renderTestRunModal(run('modal-run', 'running'));
  modal.context.setCanonical(run('modal-run', 'prepared', {ready_for_build: true}));
  modal.context.cancelExecutionRequest = async () => ({outcome: 'not_cancellable', httpStatus: 409, payload: {}});
  await modal.context.cancelTestRun();
  assert.equal(modal.nodes.testRunState.textContent, 'Prepared');
  assert.equal(modal.nodes.btnTestRunBuild.hidden, false);
  assert.equal(modal.toasts.at(-1).message, 'Run already finished.');

  modal.context.renderTestRunModal(run('modal-run', 'running'));
  modal.context.cancelExecutionRequest = async () => { throw new Error('manager down'); };
  await modal.context.cancelTestRun();
  assert.equal(modal.nodes.btnTestRunCancel.hidden, false);
  assert.equal(modal.nodes.btnTestRunCancel.disabled, false);
  assert.equal(modal.toasts.at(-1).options.type, 'error');

  modalRequests = 0;
  modal.context.cancelExecutionRequest = async () => { modalRequests += 1; };
  modal.context.closeTestRunModal();
  assert.equal(modal.nodes.testRunDialog.open, false);
  assert.equal(modalRequests, 0);

  const packages = packageActionHarness();
  const retryActions = packages.actionButtons({
    name: 'demo', recipe: 'demo-recipe',
    allowed_actions: {test: true, build: true, validate: false, publish: false},
  });
  assert.match(retryActions, />Test</);
  assert.match(retryActions, />Build</);
  assert.doesNotMatch(retryActions, />Validate</);
  assert.doesNotMatch(retryActions, />Publish</);

  console.log('cancellation UI JS tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
