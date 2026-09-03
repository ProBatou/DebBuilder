function $(id) {
  return document.getElementById(id);
}

const STATUS_LABELS = {
  ready: 'Up to date',
  up_to_date: 'Up to date',
  update_available: 'Update available',
  publication_available: 'Ready to publish',
  build_available: 'Build needed',
  build_required: 'Build needed',
  not_published: 'Build needed',
  recipe_missing: 'Recipe needed',
  success: 'Success',
  build_success: 'Validation needed',
  validation_needed: 'Validation needed',
  build_failed: 'Build failed',
  validating: 'Validating',
  validation_failed: 'Validation failed',
  publishing: 'Publishing',
  publication_failed: 'Publication failed',
  ready_to_publish: 'Ready to publish',
  published: 'Published',
  running: 'Running',
  failed: 'Error',
  cancelled: 'Cancelled',
  dry_run: 'Dry-run',
  prepared: 'Prepared',
  unknown: 'Unknown',
};
window.STATUS_LABELS = STATUS_LABELS;

const BUILDABLE_PACKAGE_STATES = new Set([
  'update_available',
  'build_available',
  'build_required',
  'not_published',
  'recipe_missing',
  'failed',
]);
window.BUILDABLE_PACKAGE_STATES = BUILDABLE_PACKAGE_STATES;

function executionLifecycleModel(execution, pendingAction = '') {
  const validation = (execution.validations || []).slice(-1)[0] || {};
  const publication = (execution.publications || []).slice(-1)[0] || {};
  const buildStatus = execution.status || 'pending';
  const validationStatus = pendingAction === 'validation' ? 'running' : (validation.status || 'not_run');
  const publicationStatus = pendingAction === 'publication' ? 'running' : (publication.status || 'not_run');
  const buildReady = execution.mode === 'build' && buildStatus === 'success' && !!execution.artifact?.path;
  const published = publicationStatus === 'success';
  return {
    buildStatus, validationStatus, publicationStatus, validation, publication,
    canValidate: buildReady && !published && validationStatus !== 'success',
    validationDisabled: validationStatus === 'running',
    canPublish: buildReady && validationStatus === 'success' && !published,
    publicationDisabled: publicationStatus === 'running',
  };
}
window.executionLifecycleModel = executionLifecycleModel;

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  }[character]));
}

function statusBadge(label, state = 'neutral') {
  return `<span class="settings-badge ${state}">${esc(label)}</span>`;
}

async function getJson(url) {
  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function fmtTime(timestamp) {
  if (!timestamp) return '—';
  try {
    return new Date(timestamp * 1000).toLocaleString('en-US');
  } catch (error) {
    return '—';
  }
}
