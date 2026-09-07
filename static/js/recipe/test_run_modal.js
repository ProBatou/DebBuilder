/* global $, STATUS_LABELS, esc, getJson, executionDiagnosticHtml, preflightReportPresentation */

const testRunModalState = {
  runId: '', workflow: null, execution: null, pollTimer: null, buildAction: null,
  cancellationState: '',
};

function testRunIsActive(execution) {
  if (typeof executionIsLive === 'function') return executionIsLive(execution);
  if (typeof executionStatusIsActive === 'function') return execution?.lifecycle_active === true || executionStatusIsActive(execution?.status);
  return execution?.lifecycle_active === true || ['pending', 'queued', 'running', 'cancelling'].includes(execution?.status);
}

function testRunWithAcceptedCancellation(execution) {
  if (testRunModalState.cancellationState !== 'accepted' || !['queued', 'running'].includes(execution?.status)) return execution;
  return {...execution, status: 'cancelling', lifecycle_status: 'cancelling', lifecycle_active: true};
}

function testRunLabel(execution) {
  const status = execution?.status || 'queued';
  if (status === 'failed') return 'Test failed';
  return STATUS_LABELS[status] || status;
}

function testRunPollDelay(execution) {
  return typeof executionPollDelay === 'function' ? executionPollDelay(execution) : 1500;
}

function stopTestRunModalPolling() {
  if (testRunModalState.pollTimer) clearTimeout(testRunModalState.pollTimer);
  testRunModalState.pollTimer = null;
}

function closeTestRunModal() {
  stopTestRunModalPolling();
  $('testRunDialog')?.close();
}

function renderTestRunLiveLog(text) {
  const node = $('testRunLiveLog');
  if (!node) return;
  const lines = String(text || '').split('\n').filter(Boolean).slice(-10);
  node.textContent = lines.join('\n') || 'Waiting for progress…';
  node.scrollTop = node.scrollHeight;
}

function renderTestRunModal(execution, logText = '') {
  if (testRunModalState.cancellationState && !['queued', 'running', 'cancelling'].includes(execution?.status)) {
    testRunModalState.cancellationState = '';
  }
  execution = testRunWithAcceptedCancellation(execution);
  testRunModalState.execution = execution;
  const active = testRunIsActive(execution);
  const status = execution?.status || 'queued';
  const label = testRunLabel(execution);
  const progress = $('testRunState');
  const statusBadge = $('testRunStatus');
  const help = $('testRunStateHelp');
  if (progress) progress.textContent = label;
  if (statusBadge) {
    statusBadge.className = `badge ${esc(status)}`;
    statusBadge.textContent = label;
    statusBadge.hidden = true;
  }
  if (help) help.textContent = status === 'cancelling'
    ? 'Cancellation was requested. This Test is stopping in the background.'
    : active
      ? 'This Test continues in the background. You can close this window at any time.'
      : status === 'cancelled'
        ? 'The Test was cancelled before completion.'
        : status === 'prepared' ? 'Preflight completed without executing build commands.' : 'The Test finished. Review the diagnostic or full logs.';

  const liveLog = $('testRunLiveLog');
  if (liveLog) liveLog.hidden = !active;
  if (active) renderTestRunLiveLog(logText);

  const prepared = $('testRunPrepared');
  const failed = $('testRunFailed');
  const build = $('btnTestRunBuild');
  const cancel = $('btnTestRunCancel');
  if (prepared) prepared.hidden = status !== 'prepared';
  if (failed) failed.hidden = status !== 'failed';
  if (build) build.hidden = execution?.ready_for_build !== true;
  if (cancel) {
    const pending = testRunModalState.cancellationState === 'pending';
    const cancelling = status === 'cancelling' || pending || testRunModalState.cancellationState === 'accepted';
    cancel.hidden = testRunModalState.cancellationState === 'refreshing' || (!['queued', 'running', 'cancelling'].includes(status) && !pending);
    cancel.disabled = !!testRunModalState.cancellationState || cancelling;
    cancel.textContent = cancelling ? 'Cancelling…' : 'Cancel';
  }

  if (status === 'prepared') {
    const presentation = preflightReportPresentation(execution, testRunModalState.workflow || {});
    $('testRunPreflightStatus').className = `settings-badge ${presentation.statusClass}`;
    $('testRunPreflightStatus').textContent = presentation.status;
    $('testRunPreflightSummary').textContent = presentation.summary;
    $('testRunPreflightContent').innerHTML = presentation.content;
  } else if (status === 'failed' && failed) {
    const fallback = execution?.error || {};
    failed.innerHTML = executionDiagnosticHtml(execution, {includeDetails: false}) ||
      `<div class="diagnostic-head"><div><span class="eyebrow">Test failed</span><h4>${esc(fallback.code || 'Test failed')}</h4></div><span class="badge failed">${esc(fallback.code || 'failed')}</span></div><p class="diagnostic-reason">${esc(fallback.message || 'The Test could not complete. View full logs for details.')}</p>`;
  }
}

async function loadTestRunModalLog(runId) {
  const response = await fetch(`/api/executions/${encodeURIComponent(runId)}/logs?verbosity=normal&after=0`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload.log?.text || '';
}

function scheduleTestRunModalPoll(execution, delay) {
  stopTestRunModalPolling();
  if (!$('testRunDialog')?.open || !testRunIsActive(execution)) return;
  testRunModalState.pollTimer = setTimeout(() => pollTestRunModal().catch(() => {}), delay);
}

async function pollTestRunModal() {
  const runId = testRunModalState.runId;
  if (!runId || !$('testRunDialog')?.open) return;
  try {
    const execution = (await getJson('/api/executions/' + encodeURIComponent(runId))).execution;
    if (testRunModalState.runId !== runId || !$('testRunDialog')?.open) return;
    const logText = testRunIsActive(execution) ? await loadTestRunModalLog(runId) : '';
    if (testRunModalState.runId !== runId || !$('testRunDialog')?.open) return;
    renderTestRunModal(execution, logText);
    scheduleTestRunModalPoll(execution, testRunIsActive(execution) ? testRunPollDelay(execution) : 5000);
  } catch (error) {
    if (testRunModalState.runId !== runId || !$('testRunDialog')?.open) return;
    const help = $('testRunStateHelp');
    if (help) help.textContent = 'Unable to refresh this Test right now. It continues in the background.';
    testRunModalState.pollTimer = setTimeout(() => pollTestRunModal().catch(() => {}), 5000);
  }
}

async function cancelTestRun() {
  const runId = testRunModalState.runId;
  const execution = testRunModalState.execution;
  if (!runId || testRunModalState.cancellationState || !['queued', 'running'].includes(execution?.status)) return;
  testRunModalState.cancellationState = 'pending';
  renderTestRunModal(execution);
  let result;
  try {
    result = await cancelExecutionRequest(runId);
  } catch (error) {
    testRunModalState.cancellationState = '';
    renderTestRunModal(execution);
    showToast(`Test cancellation failed: ${error.message}`, {type: 'error'});
    scheduleTestRunModalPoll(execution, testRunPollDelay(execution));
    return;
  }
  if (result.outcome === 'cancelling') {
    testRunModalState.cancellationState = 'accepted';
    renderTestRunModal(testRunWithAcceptedCancellation(testRunModalState.execution));
    scheduleTestRunModalPoll(testRunModalState.execution, 0);
    return;
  }
  testRunModalState.cancellationState = result.outcome === 'not_cancellable' ? 'refreshing' : '';
  if (result.outcome === 'cancelled') {
    renderTestRunModal({
      ...execution, status: 'cancelled', lifecycle_status: 'cancelled', lifecycle_active: false,
      ready_for_build: false, cancellation: {...result.payload, kind: 'cancelled'},
    });
  } else {
    renderTestRunModal(execution);
  }
  if (result.outcome === 'not_cancellable') showToast('Run already finished.', {type: 'info'});
  try {
    await pollTestRunModal();
  } catch (error) {
    // pollTestRunModal owns refresh retries and keeps the response-backed state visible.
  }
}

function openTestRunModal({runId, workflow, buildAction = null, subject = 'recipe'}) {
  stopTestRunModalPolling();
  testRunModalState.runId = runId;
  testRunModalState.workflow = workflow;
  testRunModalState.execution = null;
  testRunModalState.cancellationState = '';
  const packageName = workflow?.package?.name || workflow?.name || 'Recipe';
  $('testRunKind').textContent = subject === 'package' ? 'Test package' : 'Test recipe';
  $('testRunTitle').textContent = packageName;
  $('testRunDescription').textContent = `Run ${runId} · this Test continues in the background.`;
  $('testRunPrepared').hidden = true;
  $('testRunFailed').hidden = true;
  $('btnTestRunBuild').hidden = true;
  testRunModalState.buildAction = typeof buildAction === 'function' ? buildAction : null;
  renderTestRunModal({status: 'queued', lifecycle_active: true});
  const dialog = $('testRunDialog');
  if (!dialog.open) dialog.showModal();
  pollTestRunModal().catch(() => {});
}

async function viewTestRunLogs() {
  const runId = testRunModalState.runId;
  if (!runId) return;
  closeTestRunModal();
  switchView('logs');
  await openExecution(runId);
}

function wireTestRunModal() {
  const dialog = $('testRunDialog');
  if (!dialog) return;
  ['btnCloseTestRun', 'btnTestRunClose'].forEach(id => $(id)?.addEventListener('click', closeTestRunModal));
  $('btnTestRunLogs')?.addEventListener('click', () => viewTestRunLogs().catch(error => showToast(error.message, {type: 'error'})));
  $('btnTestRunCancel')?.addEventListener('click', () => cancelTestRun().catch(error => showToast(error.message, {type: 'error'})));
  $('btnTestRunBuild')?.addEventListener('click', () => {
    closeTestRunModal();
    const buildAction = testRunModalState.buildAction || (() => buildReal());
    buildAction().catch(error => showToast(error.message, {type: 'error'}));
  });
  dialog.addEventListener('close', stopTestRunModalPolling);
  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeTestRunModal();
  });
}

wireTestRunModal();
