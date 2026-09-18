/* global $, STATUS_LABELS, currentRecipeId, currentRecipeManaged, switchView, openExecution, getJson, postJson, showToast */
let recipeAutomationStatus = null;
let recipeAutomationPollTimer = null;
let recipeAutomationRequestRevision = 0;
let recipeAutomationActionPending = false;
let recipeAutomationActionRevision = 0;

const AUTOMATION_POLICY_LABELS = Object.freeze({
  manual: 'Manual only',
  detect: 'Detect changes',
  test: 'Detect + Test',
  build: 'Detect + Build',
  build_validate: 'Detect + Build + Validate',
  full: 'Full automatic lifecycle',
});

function automationTimestamp(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function automationStateLabel(status) {
  // A failed automation attempt is a completed state, not a generic UI error.
  // Keep the execution-wide "Error" label while naming this lifecycle state
  // consistently with the operator-facing Retry action.
  if (status?.state === 'failed') return 'Failed';
  return STATUS_LABELS[status?.state] || String(status?.state || 'Unknown').replaceAll('_', ' ');
}

function refreshRecipeAutomationControls() {
  const check = $('btnAutomationCheckNow');
  const retry = $('btnAutomationRetry');
  const active = $('recipeMetaActive')?.checked !== false;
  const enabled = $('recipeAutomationEnabled')?.checked === true;
  const policy = $('recipeAutomationPolicy')?.value || 'manual';
  const locallyEligible = active && enabled && policy !== 'manual' && !currentRecipeManaged;
  if (check) {
    const allowed = recipeAutomationStatus?.can_check_now === true && locallyEligible;
    check.disabled = recipeAutomationActionPending || !allowed;
    check.title = !active ? 'Enable the Recipe before checking upstream.'
      : !enabled || policy === 'manual' ? 'Enable an automatic policy before checking upstream.'
        : currentRecipeManaged ? 'Built-in automation is application-managed.'
          : allowed ? '' : 'Automation scheduling is unavailable.';
  }
  if (retry) retry.disabled = recipeAutomationActionPending || !locallyEligible || recipeAutomationStatus?.can_retry !== true;
}

function renderRecipeAutomationStatus(status) {
  recipeAutomationStatus = status;
  const node = $('recipeAutomationStatus');
  if (!node) return;
  const facts = [];
  const configuredPolicy = AUTOMATION_POLICY_LABELS[status.automation?.policy] || status.automation?.policy || 'Manual only';
  facts.push(status.automation?.managed ? `${configuredPolicy} · managed` : configuredPolicy);
  if (status.attempt_policy && status.attempt_policy !== status.automation?.policy) {
    facts.push(`Active attempt: ${AUTOMATION_POLICY_LABELS[status.attempt_policy] || status.attempt_policy}`);
  }
  if (status.last_check_at) facts.push(`Last check ${automationTimestamp(status.last_check_at)}`);
  const detected = [status.detected?.version, status.detected?.ref].filter(Boolean).join(' · ');
  if (detected) facts.push(`Detected ${detected}`);
  if (status.retry?.not_before) facts.push(`Next retry ${automationTimestamp(status.retry.not_before)}`);
  if (status.blocked?.message) facts.push(status.blocked.message);
  if (status.scheduler?.state !== 'running') facts.push('Scheduler stopped');
  node.innerHTML = `<span class="badge ${String(status.state || 'unknown').replace(/[^a-z0-9_-]/g, '')}">${automationStateLabel(status)}</span><span>${facts.map(esc).join(' · ')}</span>`;
  const retry = $('btnAutomationRetry');
  if (retry) retry.hidden = status.can_retry !== true;
  const run = $('btnAutomationRun');
  if (run) {
    run.hidden = !status.run?.id;
    run.dataset.runId = status.run?.id || '';
  }
  refreshRecipeAutomationControls();
  scheduleRecipeAutomationPolling(status.state === 'retry_scheduled' ? 10000 : status.state_active ? 1500 : null);
}

function stopRecipeAutomationPolling() {
  if (recipeAutomationPollTimer) clearTimeout(recipeAutomationPollTimer);
  recipeAutomationPollTimer = null;
  recipeAutomationRequestRevision += 1;
  recipeAutomationActionRevision += 1;
  recipeAutomationActionPending = false;
}

function scheduleRecipeAutomationPolling(delay) {
  if (recipeAutomationPollTimer) clearTimeout(recipeAutomationPollTimer);
  recipeAutomationPollTimer = null;
  if (delay === null || !currentRecipeId || !$('view-recipes')?.classList.contains('active')) return;
  const recipeId = currentRecipeId;
  recipeAutomationPollTimer = setTimeout(() => {
    if (currentRecipeId !== recipeId) return;
    loadRecipeAutomationStatus(recipeId).catch(() => {
      if (currentRecipeId === recipeId) scheduleRecipeAutomationPolling(5000);
    });
  }, delay);
}

async function loadRecipeAutomationStatus(recipeId = currentRecipeId) {
  if (!recipeId) return false;
  const revision = ++recipeAutomationRequestRevision;
  const status = (await getJson(`/api/recipes/${encodeURIComponent(recipeId)}/automation`)).automation;
  if (revision !== recipeAutomationRequestRevision || currentRecipeId !== recipeId) return false;
  renderRecipeAutomationStatus(status);
  return true;
}

async function checkRecipeAutomationNow() {
  const recipeId = currentRecipeId;
  if (!recipeId || recipeAutomationActionPending) return;
  recipeAutomationActionPending = true;
  const actionRevision = ++recipeAutomationActionRevision;
  const revision = ++recipeAutomationRequestRevision;
  refreshRecipeAutomationControls();
  try {
    const result = await postJson(`/api/recipes/${encodeURIComponent(recipeId)}/automation/check`, {});
    if (revision !== recipeAutomationRequestRevision || currentRecipeId !== recipeId) return;
    renderRecipeAutomationStatus(result.automation.status);
    showToast(result.automation.created ? 'Upstream check accepted.' : 'An upstream check is already active.', {type: 'info'});
  } finally {
    if (actionRevision === recipeAutomationActionRevision && currentRecipeId === recipeId) {
      recipeAutomationActionPending = false;
      refreshRecipeAutomationControls();
    }
  }
}

async function retryRecipeAutomation() {
  const recipeId = currentRecipeId;
  const observed = recipeAutomationStatus;
  if (!recipeId || !observed?.can_retry || recipeAutomationActionPending) return;
  recipeAutomationActionPending = true;
  const actionRevision = ++recipeAutomationActionRevision;
  const revision = ++recipeAutomationRequestRevision;
  refreshRecipeAutomationControls();
  try {
    const result = await postJson(`/api/recipes/${encodeURIComponent(recipeId)}/automation/retry`, {
      generation: observed.generation,
      revision: observed.revision,
    });
    if (revision !== recipeAutomationRequestRevision || currentRecipeId !== recipeId) return;
    renderRecipeAutomationStatus(result.automation.status);
    showToast(result.automation.created ? 'Automation retry accepted.' : 'The retry was already accepted.', {type: 'info'});
  } finally {
    if (actionRevision === recipeAutomationActionRevision && currentRecipeId === recipeId) {
      recipeAutomationActionPending = false;
      refreshRecipeAutomationControls();
    }
  }
}

$('btnAutomationCheckNow')?.addEventListener('click', () => checkRecipeAutomationNow().catch(error => {
  showToast(`Check now failed: ${error.message}`, {type: 'error'});
}));
$('btnAutomationRetry')?.addEventListener('click', () => retryRecipeAutomation().catch(error => {
  showToast(`Retry failed: ${error.message}`, {type: 'error'});
}));
$('btnAutomationRun')?.addEventListener('click', event => {
  const runId = event.currentTarget.dataset.runId;
  if (!runId) return;
  stopRecipeAutomationPolling();
  switchView('logs');
  openExecution(runId).catch(error => showToast(error.message, {type: 'error'}));
});
