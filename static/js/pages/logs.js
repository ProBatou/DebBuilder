async function loadExecutions() {
  const data = await getJson('/api/executions');
  adminState.executions = data.executions || [];
  renderExecutions();
}

function executionIsSelected(id) {
  return adminState.selectedExecution?.id === id;
}

function shortExecutionId(id) {
  const value = String(id || '');
  return value.length > 18 ? `${value.slice(0, 13)}...${value.slice(-4)}` : value;
}

function renderExecutions() {
  const query = ($('logSearch')?.value || '').toLowerCase();
  const status = $('logStatus')?.value || 'all';
  const rows = adminState.executions.filter(execution =>
    (status === 'all' || execution.status === status || execution.lifecycle_status === status)
      && (!query || JSON.stringify(execution).toLowerCase().includes(query))
  );
  $('executionList').innerHTML = rows.map(execution => `<div class="item execution-item ${executionIsSelected(execution.id) ? 'active' : ''}" role="option" tabindex="0" aria-selected="${executionIsSelected(execution.id) ? 'true' : 'false'}" data-admin-action="open-execution" data-execution-id="${esc(execution.id)}"><div class="execution-item-body"><div class="item-title"><span>${esc(packageLabelForExecution(execution))} · ${esc(execution.action || 'run')}</span>${badge(execution.lifecycle_status || execution.status)}</div><div class="item-meta">${fmtTime(execution.updated)} · ${esc(shortExecutionId(execution.id))}</div></div></div>`).join('') || '<p class="muted logs-empty-message">No logs available.</p>';
  document.querySelector('.logs-layout')?.classList.toggle('logs-empty', adminState.executions.length === 0);
}

function executionIsLive(execution) {
  return ['pending', 'running'].includes(execution?.status)
    || ['running'].includes((execution?.validations || []).slice(-1)[0]?.status)
    || ['running'].includes((execution?.publications || []).slice(-1)[0]?.status);
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

function executionCanValidateAgain(execution) {
  return execution?.mode === 'build' && execution.status === 'success' && !!execution.artifact?.path;
}

function updateExecutionValidationButton(execution) {
  const button = $('btnRevalidateExecution');
  if (!button) return;
  const pending = adminState.executionAction?.id === execution?.id && adminState.executionAction.type === 'validation';
  const validation = (execution?.validations || []).slice(-1)[0] || {};
  button.hidden = !executionCanValidateAgain(execution);
  button.disabled = !!pending;
  button.textContent = pending ? 'Validating…' : validation.status ? 'Revalidate' : 'Validate';
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
    $('executionDetail').textContent += log.text;
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
    adminState.selectedExecution = detail;
    renderExecutions();
    renderOpenExecution(detail, {preserveLog: true});
    const log = await loadExecutionLog(detail.id);
    if (executionIsLive(detail) && !log.complete) {
      adminState.logFollowing = true;
      updateLogLiveBadge();
      adminState.logPollTimer = setTimeout(pollOpenExecution, 1500);
    } else {
      stopLogPolling();
    }
  } catch (_error) {
    stopLogPolling();
  }
}

async function deleteExecutionLog(id) {
  const row = adminState.executions.find(item => item.id === id) || adminState.selectedExecution || {id};
  if (!confirm(`Delete log/history for this execution?\n\nPackage: ${packageLabelForExecution(row)}\nRun ID: ${row.id}\nDate: ${fmtTime(row.updated || row.created_at_epoch)}\n\nThis does not delete any Recipe, package, published APT entry, or build artifact.`)) return;
  const response = await fetch(`/api/executions/${encodeURIComponent(id)}/logs`, {method: 'DELETE'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  stopLogPolling();
  await loadExecutions();
  if (adminState.selectedExecution?.id === id) await openExecution(id);
}

function changeLogVerbosity(value) {
  const shouldStick = adminState.logAutoScroll || logIsNearBottom();
  adminState.logVerbosity = ['compact', 'normal', 'verbose', 'raw'].includes(value) ? value : 'normal';
  setLogAutoScroll(shouldStick);
  if (adminState.selectedExecution) loadExecutionLog(adminState.selectedExecution.id, {reset: true}).catch(error => alert(error.message));
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
  await Promise.all([loadExecutions(), loadPackages()]);
  await openExecution(id);
}

async function validateExecution(id) {
  if (adminState.executionAction) return;
  adminState.executionAction = {id, type: 'validation'};
  if (adminState.selectedExecution?.id === id) updateExecutionValidationButton(adminState.selectedExecution);
  try {
    await postLifecycleJson(`/api/executions/${encodeURIComponent(id)}/validate`, {});
  } catch (error) {
    alert(`Validation could not start: ${error.message}`);
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
  const meta = [['Run ID', '#' + execution.id], ['Package', execution.package || execution.recipe_id || '—'], ['Recipe', execution.recipe_id || '—'], ['Mode', execution.mode || execution.action || '—'], ['Source', source.repository || '—'], ['Resolved ref', source.ref || source.tag || '—'], ['Upstream', version.upstream || '—'], ['Debian version', version.debian || '—'], ['Build status', execution.status], ['Date', fmtTime(execution.updated || execution.created_at_epoch)], ['Artifact', (artifact.path || '').split('/').pop() || '—'], ['Size', artifact.size || '—'], ['SHA-256', artifact.sha256 || '—'], ['Validation', validation.status || 'Not run'], ['Publication', publication.status || 'Not run']];
  const symbols = {pending: '○', running: '◌', success: '✓', failed: '✕', skipped: '–'};
  if ($('executionMeta')) $('executionMeta').innerHTML = meta.map(([key, value]) => `<div class="meta-cell"><span>${esc(key)}</span>${metaValueHtml(key, value)}</div>`).join('');
  if (validation.profile && $('executionMeta')) {
    const node = (validation.checks || []).find(check => check.name === 'toolchain_node');
    $('executionMeta').insertAdjacentHTML('beforeend', `<div class="meta-cell"><span>Validation backend</span>${metaValueHtml('Validation backend', validation.backend?.runtime || '—')}</div><div class="meta-cell"><span>Profile</span>${metaValueHtml('Profile', validation.profile.name || '—')}</div><div class="meta-cell"><span>Node</span>${metaValueHtml('Node', node?.details?.actual || 'Not required')}</div><div class="meta-cell"><span>Network</span>${metaValueHtml('Network', validation.backend?.network || 'disabled')}</div>`);
  }
  if ($('executionSteps')) $('executionSteps').innerHTML = (execution.steps || []).map(step => `<span class="step-chip ${esc(step.status || 'pending')}">${symbols[step.status] || '○'} ${esc(step.name)} · ${esc(step.status || 'pending')}</span>`).join('');
  updateExecutionValidationButton(execution);
  if (!preserveLog && $('executionDetail')) $('executionDetail').textContent = 'Loading log…';
}

async function openExecution(id) {
  stopLogPolling();
  setLogAutoScroll(true);
  const execution = (await getJson('/api/executions/' + encodeURIComponent(id))).execution;
  adminState.selectedExecution = execution;
  adminState.logOffset = 0;
  renderExecutions();
  renderOpenExecution(execution);
  const log = await loadExecutionLog(id, {reset: true});
  if (executionIsLive(execution) && !log.complete) {
    adminState.logFollowing = true;
    updateLogLiveBadge();
    adminState.logPollTimer = setTimeout(pollOpenExecution, 1500);
  } else {
    stopLogPolling();
  }
  if (isMobileViewport()) document.body.classList.add('mobile-log-open');
}
