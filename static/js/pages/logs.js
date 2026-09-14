async function loadExecutions({resumePolling = true} = {}) {
  const requestRevision = ++adminState.executionListRevision;
  const data = await getJson('/api/executions');
  if (requestRevision !== adminState.executionListRevision) return false;
  adminState.executions = data.executions || [];
  renderExecutions();
  if (resumePolling && adminState.selectedExecution && $('view-logs')?.classList.contains('active')) scheduleExecutionPoll(0);
  return true;
}

function validationAttemptIsActive(validation) {
  return ['queued', 'running', 'cancelling'].includes(validation?.status);
}

function executionIsSelected(id) {
  return adminState.selectedExecution?.id === id;
}

function shortExecutionId(id) {
  const value = String(id || '');
  return value.length > 18 ? `${value.slice(0, 13)}...${value.slice(-4)}` : value;
}

function executionMatchesStatus(execution, status) {
  if (status === 'all') return true;
  if (status === 'running') return execution.lifecycle_active === true;
  if (status === 'failed') return ['failed', 'build_failed', 'validation_failed', 'publication_failed'].includes(execution.lifecycle_status || execution.status);
  return execution.status === status || execution.lifecycle_status === status;
}

function renderExecutions() {
  const query = ($('logSearch')?.value || '').toLowerCase();
  const status = $('logStatus')?.value || 'all';
  const rows = adminState.executions.filter(execution =>
    executionMatchesStatus(execution, status)
      && (!query || JSON.stringify(execution).toLowerCase().includes(query))
  );
  $('executionList').innerHTML = rows.map(execution => `<div class="item list-row execution-item ${executionIsSelected(execution.id) ? 'active' : ''}" role="option" tabindex="0" aria-selected="${executionIsSelected(execution.id) ? 'true' : 'false'}" data-admin-action="open-execution" data-execution-id="${esc(execution.id)}"><div class="execution-item-body"><div class="item-title"><span>${esc(packageLabelForExecution(execution))} · ${esc(execution.action || 'run')}</span>${badge(execution.lifecycle_status || execution.status)}</div><div class="item-meta">${fmtTime(execution.updated)} · ${esc(shortExecutionId(execution.id))}</div></div></div>`).join('') || '<div class="empty-state logs-empty-message">No logs available.</div>';
  document.querySelector('.logs-layout')?.classList.toggle('logs-empty', adminState.executions.length === 0);
}

function executionIsLive(execution) {
  return execution?.lifecycle_active === true
    || (typeof executionStatusIsActive === 'function' && executionStatusIsActive(execution?.status));
}

function executionCancellationState(id) {
  return adminState.executionCancellation && adminState.executionCancellation.id === id
    ? adminState.executionCancellation.state
    : '';
}

function validationCancellationState(id) {
  return adminState.validationCancellation && adminState.validationCancellation.id === id
    ? adminState.validationCancellation.state
    : '';
}

function executionWithAcceptedCancellation(execution) {
  if (executionCancellationState(execution?.id) !== 'accepted' || !['queued', 'running'].includes(execution?.status)) return execution;
  return {
    ...execution,
    status: 'cancelling',
    build_status: 'cancelling',
    lifecycle_status: 'cancelling',
    lifecycle_active: true,
  };
}

function syncExecutionListEntry(execution) {
  const index = adminState.executions.findIndex(row => row.id === execution.id);
  if (index < 0) adminState.executions.unshift(execution);
  else adminState.executions[index] = {...adminState.executions[index], ...execution};
}

function applyCanonicalExecution(execution, {preserveLog = false} = {}) {
  if (adminState.executionCancellation?.id && adminState.executionCancellation.id !== execution?.id) {
    adminState.executionCancellation = null;
  }
  if (executionCancellationState(execution?.id) && !['queued', 'running', 'cancelling'].includes(execution?.status)) {
    adminState.executionCancellation = null;
  }
  const validation = (execution?.validations || []).slice(-1)[0] || {};
  if (adminState.validationCancellation?.id && adminState.validationCancellation.id !== execution?.id) {
    adminState.validationCancellation = null;
  }
  if (validationCancellationState(execution?.id) && !validationAttemptIsActive(validation)) {
    adminState.validationCancellation = null;
  }
  execution = executionWithAcceptedCancellation(execution);
  if (adminState.selectedExecution?.id !== execution.id) adminState.diagnosticExpandedRunId = '';
  adminState.selectedExecution = execution;
  syncExecutionListEntry(execution);
  renderExecutions();
  renderOpenExecution(execution, {preserveLog});
}

function scheduleExecutionPoll(delay) {
  if (adminState.logPollTimer) clearTimeout(adminState.logPollTimer);
  adminState.logPollTimer = null;
  if (!adminState.selectedExecution || !$('view-logs')?.classList.contains('active')) return;
  const actionPending = adminState.executionAction?.id === adminState.selectedExecution.id;
  const cancellationPending = executionCancellationState(adminState.selectedExecution.id);
  const validationCancellationPending = validationCancellationState(adminState.selectedExecution.id);
  if (!executionIsLive(adminState.selectedExecution) && !actionPending && !cancellationPending && !validationCancellationPending) return;
  adminState.logPollTimer = setTimeout(pollOpenExecution, delay);
}

function executionPollDelay(execution) {
  const actionPending = adminState.executionAction?.id === execution?.id || executionCancellationState(execution?.id) === 'pending';
  return actionPending ? 500 : 1500;
}

function stopLogPolling() {
  if (adminState.logPollTimer) clearTimeout(adminState.logPollTimer);
  adminState.logPollTimer = null;
  adminState.executionPollRevision = (Number(adminState.executionPollRevision) || 0) + 1;
  adminState.logFollowing = false;
  updateLogLiveBadge();
}

function logIsNearBottom() {
  const node = $('executionDetail');
  return !node || node.scrollHeight - node.scrollTop - node.clientHeight < 24;
}

function middleTruncate(value, limit = 28) {
  const text = String(value ?? '');
  if (text.length <= limit) return text;
  const head = Math.max(8, Math.ceil((limit - 1) / 2));
  const tail = Math.max(6, limit - 1 - head);
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

function metaValueHtml(key, value) {
  const text = String(value ?? '—');
  const longKeys = new Set(['Run ID', 'Source', 'Resolved ref', 'Artifact', 'SHA-256']);
  if (!longKeys.has(key) && text.length <= 32) return `<strong class="meta-value">${esc(text)}</strong>`;
  return `<button type="button" class="meta-value meta-copy-value" title="${esc(text)}" data-copy-value="${esc(text)}">${esc(middleTruncate(text, key === 'SHA-256' ? 22 : 30))}</button>`;
}

function executionMetaHtml(rows) {
  return rows.map(([key, value]) => `<div class="meta-cell"><span>${esc(key)}</span>${metaValueHtml(key, value)}</div>`).join('');
}

async function copyTextValue(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  document.execCommand('copy');
  input.remove();
}

function updateLogLiveBadge() {
  const node = $('btnLogLiveBadge');
  if (!node) return;
  const active = adminState.selectedExecution && adminState.logFollowing && executionIsLive(adminState.selectedExecution);
  node.hidden = !active;
  if (!active) return;
  node.textContent = adminState.logAutoScroll ? '● Live' : '↓ Jump to latest';
  node.classList.toggle('paused', !adminState.logAutoScroll);
}

function setLogAutoScroll(enabled) {
  adminState.logAutoScroll = !!enabled;
  updateLogLiveBadge();
}

function updateExecutionActionButtons(execution) {
  const actions = execution?.allowed_actions || {};
  const validationPending = adminState.executionAction?.id === execution?.id && adminState.executionAction?.type === 'validation';
  const publicationPending = adminState.executionAction?.id === execution?.id && adminState.executionAction?.type === 'publication';
  const validation = (execution?.validations || []).slice(-1)[0] || {};
  const publication = (execution?.publications || []).slice(-1)[0] || {};
  const validateButton = $('btnRevalidateExecution');
  const publishButton = $('btnPublishExecution');
  const deleteButton = $('btnDeleteExecutionLog');
  const cancelButton = $('btnCancelExecution');
  const cancellationState = executionCancellationState(execution?.id);
  const validationCancellation = validationCancellationState(execution?.id);
  const validationActive = validationAttemptIsActive(validation);
  const cancelling = execution?.status === 'cancelling' || cancellationState === 'pending' || cancellationState === 'accepted';
  const cancellable = ['queued', 'running'].includes(execution?.status);
  if (validateButton) {
    validateButton.hidden = !actions.validate && !validationPending && !validationActive;
    validateButton.disabled = !!validationPending || validationActive;
    validateButton.textContent = validationPending ? 'Starting validation…' : validationActive ? (STATUS_LABELS[validation.status] || validation.status) : validation.status ? 'Revalidate' : 'Validate';
  }
  if (publishButton) {
    publishButton.hidden = !actions.publish && !publicationPending;
    publishButton.disabled = !!publicationPending;
    publishButton.textContent = publicationPending ? 'Publishing…' : publication.status === 'failed' ? 'Retry publish' : 'Publish';
  }
  if (cancelButton) {
    const validationCancelling = validation.status === 'cancelling' || !!validationCancellation;
    cancelButton.hidden = !execution || cancellationState === 'refreshing' || (!cancellable && !cancelling && !validationActive);
    cancelButton.disabled = !execution || !!cancellationState || cancelling || validationCancelling || (!cancellable && !validation.cancellable);
    cancelButton.textContent = validationActive ? (validationCancelling ? 'Cancelling validation…' : 'Cancel validation') : cancelling ? 'Cancelling…' : 'Cancel';
  }
  if (deleteButton) deleteButton.disabled = !execution || executionIsLive(execution) || validationPending || publicationPending;
}

function renderExecutionCancellation(execution) {
  const node = $('executionCancellationSummary');
  if (!node) return;
  const validation = (execution?.validations || []).slice(-1)[0] || {};
  if (validation.status === 'cancelling' || validation.status === 'cancelled') {
    const cancelled = validation.status === 'cancelled';
    node.innerHTML = `<strong>${cancelled ? 'Validation cancelled' : 'Validation cancellation requested'}</strong><span>${cancelled ? 'The Build Run remains successful and can be revalidated.' : 'Cleanup is completing before the attempt becomes terminal.'}</span>`;
    node.hidden = false;
    return;
  }
  const cancellation = execution?.cancellation;
  if (!cancellation || !['cancelling', 'cancelled'].includes(cancellation.kind || execution?.status)) {
    node.hidden = true;
    node.innerHTML = '';
    return;
  }
  const cancelled = (cancellation.kind || execution.status) === 'cancelled';
  const stage = String(cancellation.stage || cancellation.phase || 'run').replaceAll('_', ' ');
  const stageLabel = stage.charAt(0).toUpperCase() + stage.slice(1);
  const requestLabel = cancellation.reason === 'server_shutdown' ? 'Server shutdown requested cancellation' : 'Requested by user';
  node.innerHTML = `<strong>${cancelled ? 'Cancelled' : 'Cancellation requested'}</strong><span>${requestLabel} during ${esc(stageLabel)}</span>`;
  node.hidden = false;
}

async function loadExecutionLog(id, {reset = false} = {}) {
  const shouldStick = adminState.logAutoScroll || logIsNearBottom();
  if (reset) {
    adminState.logOffset = 0;
    if ($('executionDetail')) $('executionDetail').textContent = '';
  }
  const response = await fetch(`/api/executions/${encodeURIComponent(id)}/logs?verbosity=${encodeURIComponent(adminState.logVerbosity)}&after=${adminState.logOffset}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  const log = payload.log || {};
  if (log.text) {
    const displayedText = adminState.logVerbosity === 'raw'
      ? log.text.replace(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))(?=\s|$)/gm, timestamp => {
          const date = new Date(timestamp);
          return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString(undefined, {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
          });
        })
      : log.text;
    $('executionDetail').textContent += displayedText;
    if (shouldStick) {
      setLogAutoScroll(true);
      $('executionDetail').scrollTop = $('executionDetail').scrollHeight;
    } else {
      setLogAutoScroll(false);
    }
  }
  adminState.logOffset = log.offset || 0;
  return log;
}

async function pollOpenExecution() {
  const selected = adminState.selectedExecution;
  if (!selected) return;
  const revision = (Number(adminState.executionPollRevision) || 0) + 1;
  adminState.executionPollRevision = revision;
  try {
    const detail = (await getJson('/api/executions/' + encodeURIComponent(selected.id))).execution;
    if (adminState.selectedExecution?.id !== selected.id || revision !== adminState.executionPollRevision) return;
    applyCanonicalExecution(detail, {preserveLog: true});
    await loadExecutionLog(detail.id);
    adminState.logFollowing = executionIsLive(detail);
    updateLogLiveBadge();
    scheduleExecutionPoll(executionPollDelay(detail));
  } catch (error) {
    if (adminState.selectedExecution?.id !== selected.id || revision !== adminState.executionPollRevision) return;
    if (error.status === 404) {
      adminState.executions = adminState.executions.filter(execution => execution.id !== selected.id);
      clearOpenExecution();
      renderExecutions();
      showToast('This execution history is no longer available.', {type: 'info'});
      return;
    }
    adminState.logFollowing = false;
    updateLogLiveBadge();
    scheduleExecutionPoll(5000);
  }
}

async function deleteExecutionLog(id) {
  const row = adminState.executions.find(item => item.id === id) || adminState.selectedExecution || {id};
  if (executionIsLive(row) || adminState.executionAction?.id === id) throw new Error('An active execution cannot be deleted. Wait for it to finish.');
  const confirmed = await showConfirm({
    title: 'Delete log/history for this execution?',
    description: `Package: ${packageLabelForExecution(row)}\nRun ID: ${row.id}\nDate: ${fmtTime(row.updated || row.created_at_epoch)}\n\nThis removes the execution history, detailed logs and disposable workspace files. It does not delete any Recipe, package, published APT entry, or build artifact.`,
    confirmLabel: 'Delete log/history',
    danger: true,
  });
  if (!confirmed) return false;
  const response = await fetch(`/api/executions/${encodeURIComponent(id)}/logs`, {method: 'DELETE'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  if (payload.deletion?.history_deleted !== true || payload.deletion?.visible !== false) {
    throw new Error('The backend did not confirm execution-history deletion');
  }
  adminState.executionListRevision += 1;
  adminState.executions = adminState.executions.filter(execution => execution.id !== id);
  if (adminState.selectedExecution?.id === id) clearOpenExecution();
  renderExecutions();
  await loadExecutions({resumePolling: false});
  if (adminState.executions.some(execution => execution.id === id)) {
    throw new Error('The deleted execution is still present in canonical Logs history');
  }
  showToast('Execution log/history deleted.', {type: 'success'});
  return true;
}

function clearOpenExecution() {
  stopLogPolling();
  adminState.executionCancellation = null;
  adminState.selectedExecution = null;
  adminState.logOffset = 0;
  adminState.logFollowing = false;
  adminState.diagnosticExpandedRunId = '';
  setLogAutoScroll(true);
  if ($('executionMeta')) $('executionMeta').textContent = 'Select an execution.';
  if ($('executionMetaMore')) $('executionMetaMore').textContent = '';
  if ($('executionMoreDetails')) {
    $('executionMoreDetails').hidden = true;
    $('executionMoreDetails').removeAttribute('open');
  }
  if ($('executionSteps')) $('executionSteps').textContent = '';
  renderExecutionCancellation(null);
  renderExecutionDiagnostic(null);
  if ($('executionDetail')) $('executionDetail').textContent = 'No log selected.';
  updateExecutionActionButtons(null);
  closeLogDetail();
}

function changeLogVerbosity(value) {
  const shouldStick = adminState.logAutoScroll || logIsNearBottom();
  adminState.logVerbosity = ['compact', 'normal', 'verbose', 'raw'].includes(value) ? value : 'normal';
  setLogAutoScroll(shouldStick);
  if (adminState.selectedExecution) loadExecutionLog(adminState.selectedExecution.id, {reset: true}).catch(error => showToast(error.message, {type: 'error'}));
}

function handleLogScroll() {
  if (!adminState.selectedExecution || !executionIsLive(adminState.selectedExecution)) return;
  setLogAutoScroll(logIsNearBottom());
}

function resumeLiveLog() {
  setLogAutoScroll(true);
  const node = $('executionDetail');
  if (node) node.scrollTop = node.scrollHeight;
}

async function postLifecycleJson(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const payload = await response.json();
  const recordedLifecycleFailure = response.status === 422 && (payload.validation || payload.publication);
  if (!response.ok && !recordedLifecycleFailure) {
    const error = payload.error;
    throw new Error(error?.message || error || response.statusText);
  }
  return payload;
}

async function refreshExecutionLifecycle(id) {
  await Promise.all([loadExecutions({resumePolling: false}), loadPackages()]);
  if (adminState.selectedExecution?.id !== id) return;
  const execution = (await getJson('/api/executions/' + encodeURIComponent(id))).execution;
  if (adminState.selectedExecution?.id !== id) return;
  applyCanonicalExecution(execution, {preserveLog: true});
  await loadExecutionLog(id);
  adminState.logFollowing = executionIsLive(execution);
  updateLogLiveBadge();
  scheduleExecutionPoll(executionPollDelay(execution));
}

async function cancelOpenExecution(id) {
  const execution = adminState.selectedExecution?.id === id ? adminState.selectedExecution : null;
  const validation = (execution?.validations || []).slice(-1)[0] || {};
  if (execution && validationAttemptIsActive(validation)) {
    if (adminState.validationCancellation) return;
    adminState.validationCancellation = {id, state: 'pending', attemptId: validation.attempt_id || validation.id};
    updateExecutionActionButtons(execution);
    scheduleExecutionPoll(0);
    try {
      const current = await cancelValidationRequest(id, validation.attempt_id || validation.id);
      if (adminState.selectedExecution?.id !== id) return;
      const validations = [...(adminState.selectedExecution.validations || [])];
      validations[validations.length - 1] = {...validations[validations.length - 1], ...current};
      adminState.validationCancellation = current.status === 'cancelling' ? {id, state: 'accepted', attemptId: current.attempt_id} : null;
      applyCanonicalExecution({...adminState.selectedExecution, validations, validation_status: current.status, lifecycle_active: validationAttemptIsActive(current)}, {preserveLog: true});
      scheduleExecutionPoll(0);
    } catch (error) {
      adminState.validationCancellation = null;
      if (adminState.selectedExecution?.id === id) updateExecutionActionButtons(adminState.selectedExecution);
      showToast(`Validation cancellation failed: ${error.message}`, {type: 'error'});
      scheduleExecutionPoll(5000);
    }
    return;
  }
  if (!execution || adminState.executionCancellation || !['queued', 'running'].includes(execution.status)) return;
  adminState.executionCancellation = {id, state: 'pending'};
  updateExecutionActionButtons(execution);
  scheduleExecutionPoll(0);
  let result;
  try {
    result = await cancelExecutionRequest(id);
  } catch (error) {
    adminState.executionCancellation = null;
    if (adminState.selectedExecution?.id === id) updateExecutionActionButtons(adminState.selectedExecution);
    showToast(`Cancellation failed: ${error.message}`, {type: 'error'});
    scheduleExecutionPoll(executionPollDelay(execution));
    return;
  }
  if (result.outcome === 'cancelling') {
    adminState.executionCancellation = {id, state: 'accepted'};
    if (adminState.selectedExecution?.id === id) {
      applyCanonicalExecution(executionWithAcceptedCancellation(adminState.selectedExecution), {preserveLog: true});
      adminState.logFollowing = true;
      updateLogLiveBadge();
      scheduleExecutionPoll(0);
    }
    return;
  }
  adminState.executionCancellation = result.outcome === 'not_cancellable' ? {id, state: 'refreshing'} : null;
  if (result.outcome === 'cancelled' && adminState.selectedExecution?.id === id) {
    applyCanonicalExecution({
      ...adminState.selectedExecution,
      status: 'cancelled', build_status: 'cancelled', lifecycle_status: 'cancelled', lifecycle_active: false,
      cancellation: {...result.payload, kind: 'cancelled'},
    }, {preserveLog: true});
  } else if (adminState.selectedExecution?.id === id) {
    updateExecutionActionButtons(adminState.selectedExecution);
  }
  if (result.outcome === 'not_cancellable') showToast('Run already finished.', {type: 'info'});
  try {
    await refreshExecutionLifecycle(id);
  } catch (error) {
    if (adminState.selectedExecution?.id === id) scheduleExecutionPoll(executionPollDelay(adminState.selectedExecution));
  }
}

async function validateExecution(id) {
  if (adminState.executionAction) return;
  adminState.executionAction = {id, type: 'validation'};
  if (adminState.selectedExecution?.id === id) updateExecutionActionButtons(adminState.selectedExecution);
  scheduleExecutionPoll(0);
  try {
    const response = await postLifecycleJson(`/api/executions/${encodeURIComponent(id)}/validate`, {});
    if (adminState.selectedExecution?.id === id) {
      adminState.executionPollRevision = (Number(adminState.executionPollRevision) || 0) + 1;
      const validations = [...(adminState.selectedExecution.validations || [])];
      const existing = validations.findIndex(row => (row.attempt_id || row.id) === response.validation.attempt_id);
      if (existing >= 0) validations[existing] = {...validations[existing], ...response.validation};
      else validations.push(response.validation);
      applyCanonicalExecution({
        ...adminState.selectedExecution,
        validations,
        validation_status: response.validation.status,
        lifecycle_status: 'validating',
        lifecycle_active: true,
      }, {preserveLog: true});
    }
    showToast(`Validation accepted: ${response.validation.status}`, {type: 'info'});
  } catch (error) {
    showToast(`Validation could not start: ${error.message}`, {type: 'error'});
  } finally {
    adminState.executionAction = null;
    if (adminState.selectedExecution?.id === id) {
      updateExecutionActionButtons(adminState.selectedExecution);
      scheduleExecutionPoll(0);
    }
  }
}

async function publishExecution(id) {
  if (adminState.executionAction) return;
  const execution = adminState.selectedExecution?.id === id ? adminState.selectedExecution : null;
  const artifact = execution?.artifact || {};
  const inspection = artifact.inspection || {};
  const runVersion = typeof execution?.version === 'object' ? execution.version.debian : execution?.version;
  const packageName = inspection.package || execution?.package || execution?.recipe_id || '';
  const version = inspection.version || runVersion || '';
  if (!packageName || !version) throw new Error('The selected execution has no publishable package identity');
  const confirmation = `publish:${packageName}:${version}`;
  const confirmed = await showConfirm({
    title: `Publish ${packageName} ${version}?`,
    description: `This publishes the validated artifact to APT.\nRequired confirmation: ${confirmation}`,
    confirmLabel: 'Publish to APT',
  });
  if (!confirmed) return;
  adminState.executionAction = {id, type: 'publication'};
  updateExecutionActionButtons(execution);
  scheduleExecutionPoll(0);
  try {
    await postLifecycleJson(`/api/executions/${encodeURIComponent(id)}/publish`, {confirm: confirmation});
  } catch (error) {
    showToast(`Publication could not start: ${error.message}`, {type: 'error'});
  } finally {
    adminState.executionAction = null;
    await refreshExecutionLifecycle(id);
  }
}

function renderOpenExecution(execution, {preserveLog = false} = {}) {
  const artifact = execution.artifact || {};
  const validation = (execution.validations || []).slice(-1)[0] || {};
  const publication = (execution.publications || []).slice(-1)[0] || {};
  const source = (execution.steps || []).find(step => step.name === 'source')?.details || {};
  const sourceAsset = source.asset || {};
  const sourceFileCount = Number(source.file_count ?? sourceAsset.file_count ?? 0);
  const sourcePayload = source.payload_kind === 'raw_file'
    ? `Raw file · ${sourceFileCount} ${sourceFileCount === 1 ? 'file' : 'files'}`
    : source.payload_kind === 'archive'
      ? `Archive payload · ${sourceFileCount} ${sourceFileCount === 1 ? 'file' : 'files'}`
      : '—';
  const version = typeof execution.version === 'object' ? execution.version : {debian: execution.version};
  const lifecycle = STATUS_LABELS[execution.lifecycle_status] || execution.lifecycle_status || execution.status || 'Unknown';
  const recovery = validation.recovery_blocker;
  const meta = [['Run ID', '#' + execution.id], ['Package', execution.package || execution.recipe_id || '—'], ['Lifecycle', lifecycle], ['Mode', execution.mode || execution.action || '—'], ['Build status', execution.build_status || execution.status], ['Date', fmtTime(execution.updated || execution.created_at_epoch)], ['Validation', execution.validation_status || validation.status || 'Not run'], ['Validation recovery', recovery?.message || '—'], ['Publication', execution.publication_status || publication.status || 'Not run']];
  const moreMeta = [['Recipe', execution.recipe_id || '—'], ['Source', source.repository || '—'], ['Resolved ref', source.ref || source.tag || '—'], ['Source payload', sourcePayload], ['Source asset', sourceAsset.name || '—'], ['Source SHA-256', sourceAsset.sha256 || '—'], ['Upstream', version.upstream || '—'], ['Debian version', version.debian || '—'], ['Artifact', (artifact.path || '').split('/').pop() || '—'], ['Size', artifact.size || '—'], ['SHA-256', artifact.sha256 || '—']];
  const symbols = {pending: '○', running: '◌', success: '✓', failed: '✕', cancelled: '⊘', skipped: '–'};
  if ($('executionMeta')) $('executionMeta').innerHTML = executionMetaHtml(meta);
  if ($('executionMetaMore')) $('executionMetaMore').innerHTML = executionMetaHtml(moreMeta);
  if ($('executionMoreDetails')) $('executionMoreDetails').hidden = false;
  if (validation.profile && $('executionMetaMore')) $('executionMetaMore').insertAdjacentHTML('beforeend', executionMetaHtml([['Validation profile', validation.profile.name || '—']]));
  if ($('executionSteps')) $('executionSteps').innerHTML = (execution.steps || []).map(step => `<span class="step-chip ${esc(step.status || 'pending')}">${symbols[step.status] || '○'} ${esc(step.name)} · ${esc(step.status || 'pending')}</span>`).join('');
  renderExecutionCancellation(execution);
  renderExecutionDiagnostic(execution);
  updateExecutionActionButtons(execution);
  if (!preserveLog && $('executionDetail')) $('executionDetail').textContent = 'Loading log…';
}

async function openDiagnosticRecipe(recipeId, step) {
  await openLinkedRecipe(recipeId);
  const target = $(`recipe-step-${step}`);
  target?.scrollIntoView({behavior:'smooth', block:'start'});
}

async function openExecution(id) {
  stopLogPolling();
  setLogAutoScroll(true);
  const execution = (await getJson('/api/executions/' + encodeURIComponent(id))).execution;
  adminState.logOffset = 0;
  applyCanonicalExecution(execution);
  await loadExecutionLog(id, {reset: true});
  adminState.logFollowing = executionIsLive(execution);
  updateLogLiveBadge();
  scheduleExecutionPoll(executionPollDelay(execution));
  if (isMobileViewport()) document.body.classList.add('mobile-log-open');
}
