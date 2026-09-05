async function loadExecutions({resumePolling = true} = {}) {
  const requestRevision = ++adminState.executionListRevision;
  const data = await getJson('/api/executions');
  if (requestRevision !== adminState.executionListRevision) return false;
  adminState.executions = data.executions || [];
  renderExecutions();
  if (resumePolling && adminState.selectedExecution && $('view-logs')?.classList.contains('active')) scheduleExecutionPoll(0);
  return true;
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
  return execution?.lifecycle_active === true;
}

function syncExecutionListEntry(execution) {
  const index = adminState.executions.findIndex(row => row.id === execution.id);
  if (index < 0) adminState.executions.unshift(execution);
  else adminState.executions[index] = {...adminState.executions[index], ...execution};
}

function applyCanonicalExecution(execution, {preserveLog = false} = {}) {
  adminState.selectedExecution = execution;
  syncExecutionListEntry(execution);
  renderExecutions();
  renderOpenExecution(execution, {preserveLog});
}

function scheduleExecutionPoll(delay) {
  if (adminState.logPollTimer) clearTimeout(adminState.logPollTimer);
  adminState.logPollTimer = null;
  if (!adminState.selectedExecution || !$('view-logs')?.classList.contains('active')) return;
  adminState.logPollTimer = setTimeout(pollOpenExecution, delay);
}

function executionPollDelay(execution) {
  const actionPending = adminState.executionAction?.id === execution?.id;
  return actionPending ? 500 : executionIsLive(execution) ? 1500 : 5000;
}

function stopLogPolling() {
  if (adminState.logPollTimer) clearTimeout(adminState.logPollTimer);
  adminState.logPollTimer = null;
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
  if (validateButton) {
    validateButton.hidden = !actions.validate && !validationPending;
    validateButton.disabled = !!validationPending;
    validateButton.textContent = validationPending ? 'Validating…' : validation.status ? 'Revalidate' : 'Validate';
  }
  if (publishButton) {
    publishButton.hidden = !actions.publish && !publicationPending;
    publishButton.disabled = !!publicationPending;
    publishButton.textContent = publicationPending ? 'Publishing…' : publication.status === 'failed' ? 'Retry publish' : 'Publish';
  }
  if (deleteButton) deleteButton.disabled = !execution || executionIsLive(execution) || validationPending || publicationPending;
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
  try {
    const detail = (await getJson('/api/executions/' + encodeURIComponent(selected.id))).execution;
    if (adminState.selectedExecution?.id !== selected.id) return;
    applyCanonicalExecution(detail, {preserveLog: true});
    await loadExecutionLog(detail.id);
    adminState.logFollowing = executionIsLive(detail);
    updateLogLiveBadge();
    scheduleExecutionPoll(executionPollDelay(detail));
  } catch (error) {
    if (adminState.selectedExecution?.id !== selected.id) return;
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
  adminState.selectedExecution = null;
  adminState.logOffset = 0;
  adminState.logFollowing = false;
  setLogAutoScroll(true);
  if ($('executionMeta')) $('executionMeta').textContent = 'Select an execution.';
  if ($('executionMetaMore')) $('executionMetaMore').textContent = '';
  if ($('executionMoreDetails')) {
    $('executionMoreDetails').hidden = true;
    $('executionMoreDetails').removeAttribute('open');
  }
  if ($('executionSteps')) $('executionSteps').textContent = '';
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

async function validateExecution(id) {
  if (adminState.executionAction) return;
  adminState.executionAction = {id, type: 'validation'};
  if (adminState.selectedExecution?.id === id) updateExecutionActionButtons(adminState.selectedExecution);
  scheduleExecutionPoll(0);
  try {
    await postLifecycleJson(`/api/executions/${encodeURIComponent(id)}/validate`, {});
  } catch (error) {
    showToast(`Validation could not start: ${error.message}`, {type: 'error'});
  } finally {
    adminState.executionAction = null;
    await refreshExecutionLifecycle(id);
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
  const version = typeof execution.version === 'object' ? execution.version : {debian: execution.version};
  const lifecycle = STATUS_LABELS[execution.lifecycle_status] || execution.lifecycle_status || execution.status || 'Unknown';
  const meta = [['Run ID', '#' + execution.id], ['Package', execution.package || execution.recipe_id || '—'], ['Lifecycle', lifecycle], ['Mode', execution.mode || execution.action || '—'], ['Build status', execution.build_status || execution.status], ['Date', fmtTime(execution.updated || execution.created_at_epoch)], ['Validation', execution.validation_status || validation.status || 'Not run'], ['Publication', execution.publication_status || publication.status || 'Not run']];
  const moreMeta = [['Recipe', execution.recipe_id || '—'], ['Source', source.repository || '—'], ['Resolved ref', source.ref || source.tag || '—'], ['Upstream', version.upstream || '—'], ['Debian version', version.debian || '—'], ['Artifact', (artifact.path || '').split('/').pop() || '—'], ['Size', artifact.size || '—'], ['SHA-256', artifact.sha256 || '—']];
  const symbols = {pending: '○', running: '◌', success: '✓', failed: '✕', skipped: '–'};
  if ($('executionMeta')) $('executionMeta').innerHTML = executionMetaHtml(meta);
  if ($('executionMetaMore')) $('executionMetaMore').innerHTML = executionMetaHtml(moreMeta);
  if ($('executionMoreDetails')) $('executionMoreDetails').hidden = false;
  if (validation.profile && $('executionMetaMore')) {
    const node = (validation.checks || []).find(check => check.name === 'toolchain_node');
    $('executionMetaMore').insertAdjacentHTML('beforeend', executionMetaHtml([['Validation backend', validation.backend?.runtime || '—'], ['Profile', validation.profile.name || '—'], ['Node', node?.details?.actual || 'Not required'], ['Network', validation.backend?.network || 'disabled']]));
  }
  if ($('executionSteps')) $('executionSteps').innerHTML = (execution.steps || []).map(step => `<span class="step-chip ${esc(step.status || 'pending')}">${symbols[step.status] || '○'} ${esc(step.name)} · ${esc(step.status || 'pending')}</span>`).join('');
  updateExecutionActionButtons(execution);
  if (!preserveLog && $('executionDetail')) $('executionDetail').textContent = 'Loading log…';
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
