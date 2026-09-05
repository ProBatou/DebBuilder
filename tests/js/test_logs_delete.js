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

function element() {
  return {
    classList: classList(),
    disabled: false,
    hidden: false,
    innerHTML: '',
    textContent: '',
    value: '',
    removeAttribute: () => {},
  };
}

const nodes = Object.fromEntries([
  'btnDeleteExecutionLog', 'btnLogLiveBadge', 'btnPublishExecution', 'btnRevalidateExecution',
  'executionDetail', 'executionList', 'executionMeta', 'executionMetaMore',
  'executionMoreDetails', 'executionSteps', 'logSearch', 'logStatus', 'view-logs',
].map(id => [id, element()]));
nodes['view-logs'].classList.add('active');
const logsLayout = element();
const body = {classList: classList(['mobile-log-open'])};
const toasts = [];
const requests = [];
let confirmationOptions = null;
let reopenCount = 0;
let resolveStaleList = null;
let executionListCalls = 0;

const context = vm.createContext({
  STATUS_LABELS: {success: 'Success'},
  adminState: {
    executionAction: null,
    executionListRevision: 0,
    executions: [{id: 'run-one', package: 'demo', action: 'build', lifecycle_status: 'success', updated: 1}],
    selectedExecution: {id: 'run-one', package: 'demo', action: 'build', lifecycle_status: 'success', updated: 1},
    logAutoScroll: true,
    logFollowing: false,
    logOffset: 42,
    logVerbosity: 'normal',
    logPollTimer: null,
  },
  badge: status => `<b>${status}</b>`,
  clearTimeout: () => {},
  closeLogDetail: () => body.classList.remove('mobile-log-open'),
  document: {
    body,
    querySelector: selector => selector === '.logs-layout' ? logsLayout : element(),
  },
  esc: value => String(value ?? ''),
  fetch: async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => ({ok: true, deletion: {id: 'run-one', deleted: 'log_history', history_deleted: true, visible: false, already_deleted: false}})};
  },
  fmtTime: value => String(value ?? ''),
  getJson: url => {
    assert.equal(url, '/api/executions');
    executionListCalls += 1;
    if (executionListCalls === 1) {
      return new Promise(resolve => { resolveStaleList = resolve; });
    }
    return Promise.resolve({executions: []});
  },
  packageLabelForExecution: execution => execution.package || 'Operation',
  setTimeout: () => 1,
  showConfirm: async options => {
    confirmationOptions = options;
    return true;
  },
  showToast: (message, options) => toasts.push({message, options}),
  $: id => nodes[id] || null,
});

vm.runInContext(fs.readFileSync('static/js/pages/logs.js', 'utf8'), context, {filename: 'logs.js'});
context.openExecution = async () => { reopenCount += 1; };

(async () => {
  const staleLoad = context.loadExecutions();
  await Promise.resolve();
  const deleted = await context.deleteExecutionLog('run-one');
  resolveStaleList({executions: [{id: 'run-one', package: 'demo', updated: 1}]});
  const staleApplied = await staleLoad;
  assert.equal(deleted, true);
  assert.equal(staleApplied, false);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/executions/run-one/logs');
  assert.equal(requests[0].options.method, 'DELETE');
  assert.equal(confirmationOptions.danger, true);
  assert.match(confirmationOptions.description, /Package: demo/);
  assert.equal(context.adminState.executions.length, 0);
  assert.equal(context.adminState.selectedExecution, null);
  assert.equal(context.adminState.logOffset, 0);
  assert.equal(nodes.executionDetail.textContent, 'No log selected.');
  assert.equal(nodes.executionMoreDetails.hidden, true);
  assert.equal(nodes.btnDeleteExecutionLog.disabled, true);
  assert.equal(body.classList.contains('mobile-log-open'), false);
  assert.equal(reopenCount, 0);
  assert.equal(toasts.some(toast => toast.options.type === 'success'), true);

  context.adminState.executions = [{id: 'run-two', package: 'demo', updated: 2}];
  context.adminState.selectedExecution = context.adminState.executions[0];
  context.fetch = async () => ({ok: true, json: async () => ({ok: true, deletion: {id: 'run-two', history_deleted: false, visible: false}})});
  const toastCount = toasts.length;
  await assert.rejects(() => context.deleteExecutionLog('run-two'), /did not confirm/);
  assert.equal(context.adminState.executions[0].id, 'run-two');
  assert.equal(toasts.length, toastCount);

  context.adminState.executions = [{id: 'run-three', package: 'demo', updated: 3}];
  context.adminState.selectedExecution = context.adminState.executions[0];
  context.getJson = async () => {
    const error = new Error('not found');
    error.status = 404;
    throw error;
  };
  await context.pollOpenExecution();
  assert.equal(context.adminState.executions.length, 0);
  assert.equal(context.adminState.selectedExecution, null);
  assert.equal(toasts.at(-1).options.type, 'info');

  const active = {id: 'active-run', lifecycle_active: true};
  context.adminState.executions = [active];
  context.adminState.selectedExecution = active;
  context.updateExecutionActionButtons(active);
  assert.equal(nodes.btnDeleteExecutionLog.disabled, true);
  const previousConfirmation = confirmationOptions;
  await assert.rejects(() => context.deleteExecutionLog(active.id), /active execution cannot be deleted/);
  assert.equal(confirmationOptions, previousConfirmation);
  assert.equal(context.adminState.selectedExecution.id, active.id);
  active.lifecycle_active = false;
  context.adminState.executionAction = {id: active.id, type: 'validation'};
  context.updateExecutionActionButtons(active);
  assert.equal(nodes.btnDeleteExecutionLog.disabled, true);
  await assert.rejects(() => context.deleteExecutionLog(active.id), /active execution cannot be deleted/);
  context.adminState.executionAction = null;
  context.fetch = async () => ({ok: false, status: 409, json: async () => ({error: 'Execution is active'})});
  const beforeRejectedDelete = toasts.length;
  await assert.rejects(() => context.deleteExecutionLog(active.id), /Execution is active/);
  assert.equal(context.adminState.selectedExecution.id, active.id);
  assert.equal(context.adminState.executions[0].id, active.id);
  assert.equal(toasts.length, beforeRejectedDelete);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
