const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const requests = [];
let response;
const context = vm.createContext({
  document: {getElementById: () => null},
  fetch: async (url, options) => {
    requests.push({url, options});
    return response;
  },
  setTimeout: () => 1,
  window: {},
});
vm.runInContext(fs.readFileSync('static/ui_core.js', 'utf8'), context, {filename: 'ui_core.js'});

function jsonResponse(status, payload, statusText = '') {
  return {ok: status >= 200 && status < 300, status, statusText, json: async () => payload};
}

(async () => {
  assert.equal(context.window.STATUS_LABELS.cancelling, 'Cancelling…');
  assert.equal(context.window.STATUS_LABELS.cancelled, 'Cancelled');
  for (const status of ['pending', 'queued', 'running', 'cancelling']) {
    assert.equal(context.executionStatusIsActive(status), true, `${status} should remain active`);
  }
  for (const status of ['prepared', 'success', 'failed', 'cancelled']) {
    assert.equal(context.executionStatusIsActive(status), false, `${status} should be terminal`);
  }

  response = jsonResponse(200, {run_id: 'queued run', status: 'cancelled'});
  let result = await context.cancelExecutionRequest('queued run');
  assert.equal(result.outcome, 'cancelled');
  assert.equal(result.httpStatus, 200);
  assert.equal(requests.at(-1).url, '/api/executions/queued%20run/cancel');
  assert.equal(requests.at(-1).options.method, 'POST');
  assert.equal(requests.at(-1).options.body, '{}');

  response = jsonResponse(202, {run_id: 'active', status: 'cancelling', requested_at: 'now'});
  result = await context.cancelExecutionRequest('active');
  assert.equal(result.outcome, 'cancelling');
  assert.equal(result.payload.requested_at, 'now');

  response = jsonResponse(409, {error: {code: 'execution_not_cancellable', message: 'already terminal'}});
  result = await context.cancelExecutionRequest('race');
  assert.equal(result.outcome, 'not_cancellable');
  assert.equal(result.httpStatus, 409);

  response = jsonResponse(503, {error: {code: 'execution_manager_unavailable', message: 'manager down'}});
  await assert.rejects(
    () => context.cancelExecutionRequest('active'),
    error => error.message === 'manager down' && error.status === 503 && error.code === 'execution_manager_unavailable',
  );

  response = jsonResponse(409, {error: {code: 'different_conflict', message: 'real conflict'}});
  await assert.rejects(
    () => context.cancelExecutionRequest('active'),
    error => error.message === 'real conflict' && error.status === 409 && error.code === 'different_conflict',
  );

  console.log('cancellation API JS tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
