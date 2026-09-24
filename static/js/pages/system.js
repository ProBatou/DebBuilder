/* Read-only consumers of the versioned diagnostics and inspector contracts. */
const SYSTEM_CHECKS = {
  'application.runtime': 'Application runtime',
  'settings.documents': 'Settings documents',
  'repository.publication': 'Repository publication',
  'validation.oci': 'Validation OCI',
  'execution.admission': 'Execution admission',
  'execution.containment': 'Execution containment',
  'application.mutations': 'Mutation gate',
  'automation.scheduler': 'Automation scheduler',
  'automation.orchestrator': 'Automation orchestrator',
};
const SYSTEM_STATES = new Set(['ok', 'warning', 'failed', 'unknown']);
const SYSTEM_STATUS_LABELS = {ok: 'OK', warning: 'Warning', failed: 'Failed', unknown: 'Unknown'};
const SYSTEM_DETAIL_FIELDS = {
  'settings.documents': [['auth_mode', 'Authentication mode']],
  'repository.publication': [['listener_active', 'Repository listener', 'Active', 'Inactive'], ['configuration_valid', 'Configuration', 'Valid', 'Not confirmed'], ['signed_release_present', 'Signed release', 'Present', 'Missing'], ['public_key_present', 'Public signing key', 'Available', 'Missing'], ['signing_fingerprint', 'Signing fingerprint']],
  'validation.oci': [['podman_installed', 'Podman', 'Installed', 'Not installed'], ['runtime_verified', 'Runtime', 'Verified', 'Not verified'], ['admission_blocked', 'Validation admission', 'Blocked', 'Not blocked']],
  'execution.admission': [['open', 'Build/Test admission', 'Open', 'Closed'], ['recovery_blocked', 'Recovery', 'Blocked', 'Not blocked']],
  'execution.containment': [['backend', 'Containment backend'], ['available', 'Containment', 'Available', 'Unavailable or unverified']],
  'application.mutations': [['open', 'Mutation gate', 'Open', 'Closed']],
  'automation.scheduler': [['running', 'Scheduler', 'Running', 'Not accepting work'], ['checks_enabled', 'Checks', 'Enabled', 'Disabled']],
  'automation.orchestrator': [['available', 'Orchestrator', 'Available', 'Unavailable']],
};
const SYSTEM_RECIPE_SECTIONS = [
  ['identity', 'Identity', [['recipe_id', 'Recipe ID'], ['active', 'Recipe', 'Enabled', 'Disabled'], ['recipe_schema_version', 'Recipe schema'], ['source', 'Origin'], ['managed', 'Built-in management', 'Managed', 'Not managed'], ['package', 'Package'], ['architecture', 'Architecture'], ['revision', 'Debian revision']]],
  ['source', 'Source summary', [['provider', 'Provider'], ['repository_configured', 'Repository', 'Configured', 'Not configured'], ['tracking', 'Tracking'], ['ref_configured', 'Source ref', 'Configured', 'Not configured'], ['version_source', 'Version source']]],
  ['build', 'Build summary', [['detected_project', 'Detected project'], ['command_count', 'Build command count'], ['output_mode', 'Output mode'], ['output_path_count', 'Output path count'], ['source_change_count', 'Source change count'], ['inactivity_timeout_configured', 'Inactivity timeout', 'Configured', 'Not configured'], ['maximum_runtime_configured', 'Maximum runtime', 'Configured', 'Not configured']]],
  ['artifact', 'Artifact summary', [['mode', 'Mode'], ['type', 'Type'], ['architecture', 'Architecture'], ['archive_source', 'Archive source'], ['asset_selection', 'Asset selection'], ['archive_format', 'Archive format'], ['payload_mode', 'Payload mode'], ['include_count', 'Include count'], ['exclude_count', 'Exclude count']]],
  ['installation', 'Installation summary', [['content_source', 'Content source'], ['destination_configured', 'Destination', 'Configured', 'Not configured'], ['account_provisioning', 'Account provisioning', 'Enabled', 'Disabled'], ['directory_count', 'Directory count'], ['config_mapping_count', 'Config mapping count'], ['maintainer_scripts_present', 'Maintainer scripts', 'Present', 'Absent']]],
  ['service', 'Service summary', [['configured', 'Service', 'Configured', 'Not configured'], ['enabled', 'Service state', 'Enabled', 'Disabled'], ['type', 'Type'], ['restart', 'Restart policy']]],
  ['automation', 'Automation', [['enabled', 'Automation', 'Enabled', 'Disabled'], ['policy', 'Policy'], ['eligible', 'Eligibility', 'Eligible', 'Not eligible']]],
  ['observation', 'Local observation', [['classification', 'Classification']]],
];
const SYSTEM_RUN_SECTIONS = [
  ['identity', 'Identity and status', [['run_id', 'Run ID'], ['recipe_id', 'Recipe ID'], ['run_schema_version', 'Run schema'], ['mode', 'Mode'], ['status', 'Status'], ['terminal', 'Lifecycle', 'Terminal', 'Active'], ['recipe_sha256', 'Recipe fingerprint SHA-256']]],
  ['lifecycle', 'Lifecycle', [['created_at', 'Created'], ['started_at', 'Started'], ['finished_at', 'Finished'], ['current_stage', 'Current stage'], ['step_count', 'Step count'], ['steps_truncated', 'Steps', 'Truncated', 'Complete']]],
  ['artifact', 'Artifact', [['available', 'Artifact', 'Available', 'Unavailable'], ['package', 'Package'], ['architecture', 'Architecture'], ['size', 'Size (bytes)'], ['sha256', 'SHA-256']]],
  ['validation', 'Validation', [['attempt_count', 'Attempt count'], ['inventory_truncated', 'Inventory', 'Truncated', 'Complete'], ['status', 'Status'], ['attempt_id', 'Selected attempt'], ['recovery_blocked', 'Recovery', 'Blocked', 'Not blocked']]],
  ['publication', 'Publication', [['attempt_count', 'Attempt count'], ['history_truncated', 'History', 'Truncated', 'Complete'], ['status', 'Status'], ['published', 'Publication proof', 'Available', 'Unavailable'], ['proof_available', 'Proof', 'Available', 'Unavailable'], ['suite', 'Suite'], ['component', 'Component']]],
  ['execution', 'Recovery and containment', [['cancellable', 'Cancellation', 'Available', 'Unavailable'], ['recovery_status', 'Recovery status'], ['recovery_blocked', 'Recovery', 'Blocked', 'Not blocked'], ['containment_backend', 'Containment backend']]],
  ['error', 'Structured error', [['code', 'Code'], ['stage', 'Stage']]],
];

function systemFactList(node, source, fields) {
  node.replaceChildren();
  for (const [key, label, yes, no] of fields) {
    if (!Object.prototype.hasOwnProperty.call(source || {}, key)) continue;
    const value = source[key];
    if (value === null || value === undefined) continue;
    const row = document.createElement('div');
    row.className = 'system-fact';
    const term = document.createElement('dt');
    term.textContent = label;
    const description = document.createElement('dd');
    description.textContent = typeof value === 'boolean' ? (value ? yes : no) : String(value);
    row.append(term, description);
    node.appendChild(row);
  }
}

function systemStatus(status) {
  const safe = SYSTEM_STATES.has(status) ? status : 'unknown';
  const span = document.createElement('span');
  span.className = `system-status system-status--${safe}`;
  span.textContent = SYSTEM_STATUS_LABELS[safe];
  return span;
}

function renderSystemDiagnostics(snapshot) {
  const checks = Array.isArray(snapshot?.checks) ? snapshot.checks : [];
  const runtime = checks.find(check => check.id === 'application.runtime')?.details || {};
  const settings = checks.find(check => check.id === 'settings.documents')?.details || {};
  const runtimeFacts = $('systemRuntime');
  systemFactList(runtimeFacts, runtime, [['version', 'DebBuilder version'], ['recipe_schema_version', 'Recipe schema'], ['run_schema_version', 'Run schema'], ['python_version', 'Python version'], ['debian_architecture', 'Debian architecture']]);
  const auth = document.createElement('dl');
  systemFactList(auth, settings, [['auth_mode', 'Authentication mode']]);
  runtimeFacts.append(...auth.childNodes);
  $('systemOverallStatus').replaceChildren(systemStatus(snapshot.status));
  const container = $('systemChecks');
  container.replaceChildren();
  for (const check of checks) {
    if (!Object.prototype.hasOwnProperty.call(SYSTEM_CHECKS, check.id)) continue;
    const article = document.createElement('article');
    article.className = 'section system-check';
    const header = document.createElement('div');
    header.className = 'system-check-head';
    const heading = document.createElement('h4');
    heading.textContent = SYSTEM_CHECKS[check.id];
    header.append(heading, systemStatus(check.status));
    const message = document.createElement('p');
    message.textContent = check.message || '';
    article.append(header, message);
    const details = document.createElement('dl');
    details.className = 'system-facts';
    systemFactList(details, check.details, SYSTEM_DETAIL_FIELDS[check.id] || []);
    if (details.childElementCount) article.appendChild(details);
    container.appendChild(article);
  }
  $('systemContent').hidden = false;
}

let systemDiagnosticsRequest = 0;
async function loadSystemDiagnostics() {
  const revision = ++systemDiagnosticsRequest;
  const refresh = $('btnRefreshDiagnostics');
  refresh.disabled = true;
  $('systemFeedback').textContent = 'Loading diagnostics…';
  try {
    const snapshot = await getJson('/api/system/diagnostics');
    if (revision !== systemDiagnosticsRequest) return;
    renderSystemDiagnostics(snapshot);
    $('systemFeedback').textContent = 'Diagnostics updated. Refresh to check again.';
  } catch (error) {
    if (revision !== systemDiagnosticsRequest) return;
    $('systemFeedback').textContent = error.message || 'Diagnostics are unavailable.';
  } finally {
    if (revision === systemDiagnosticsRequest) refresh.disabled = false;
  }
}

const systemPendingDownloads = new Set();
async function downloadSupportBundle(button, selection = {}) {
  if (!button || systemPendingDownloads.has(button)) return;
  const params = new URLSearchParams(selection);
  const url = `/api/support-bundle${params.size ? `?${params}` : ''}`;
  systemPendingDownloads.add(button);
  button.disabled = true;
  try {
    const response = await fetch(url);
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error?.message || 'Support bundle is unavailable.');
    }
    if (response.headers.get('Content-Type')?.split(';')[0] !== 'application/zip') throw new Error('Support bundle is unavailable.');
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = 'debbuilder-support.zip';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }
  } catch (error) {
    showToast(error.message || 'Support bundle is unavailable.', {type: 'error'});
  } finally {
    systemPendingDownloads.delete(button);
    button.disabled = false;
  }
}

let systemInspectionRequest = 0;
let systemInspectionPreviousFocus = null;
async function openSystemInspection(kind, id) {
  if (!id) return;
  const revision = ++systemInspectionRequest;
  const dialog = $('systemInspectorDialog');
  systemInspectionPreviousFocus = document.activeElement;
  $('systemInspectorTitle').textContent = kind === 'recipe' ? 'Recipe inspection' : 'Run inspection';
  $('systemInspectorFeedback').textContent = 'Loading inspection…';
  $('systemInspectorContent').replaceChildren();
  if (!dialog.open) dialog.showModal();
  $('btnCloseSystemInspector').focus();
  try {
    const path = kind === 'recipe' ? `/api/recipes/${encodeURIComponent(id)}/inspect` : `/api/executions/${encodeURIComponent(id)}/inspect`;
    const payload = await getJson(path);
    if (revision !== systemInspectionRequest || !dialog.open) return;
    const inspection = payload.inspection;
    const sections = kind === 'recipe' ? SYSTEM_RECIPE_SECTIONS : SYSTEM_RUN_SECTIONS;
    for (const [key, title, fields] of sections) {
      const section = document.createElement('section');
      section.className = 'section system-inspection-section';
      const heading = document.createElement('h3');
      heading.textContent = title;
      const facts = document.createElement('dl');
      facts.className = 'system-facts';
      systemFactList(facts, inspection?.[key], fields);
      section.append(heading, facts);
      if (kind === 'run' && key === 'lifecycle' && Array.isArray(inspection?.lifecycle?.steps)) {
        const steps = document.createElement('ol');
        steps.className = 'system-steps';
        for (const step of inspection.lifecycle.steps) {
          const item = document.createElement('li');
          item.textContent = `${step.id}: ${step.status}`;
          steps.appendChild(item);
        }
        section.appendChild(steps);
      }
      $('systemInspectorContent').appendChild(section);
    }
    $('systemInspectorFeedback').textContent = 'Read-only summary; raw records and logs are not included.';
  } catch (error) {
    if (revision === systemInspectionRequest && dialog.open) $('systemInspectorFeedback').textContent = error.message || 'Inspection is unavailable.';
  }
}

function wireSystemUi() {
  $('btnRefreshDiagnostics')?.addEventListener('click', loadSystemDiagnostics);
  $('btnSystemSupportBundle')?.addEventListener('click', event => downloadSupportBundle(event.currentTarget));
  $('btnInspectRecipe')?.addEventListener('click', () => openSystemInspection('recipe', $('workflowSelect')?.value));
  $('btnRecipeSupportBundle')?.addEventListener('click', event => {
    const id = $('workflowSelect')?.value;
    if (id) downloadSupportBundle(event.currentTarget, {recipe_id: id});
  });
  $('btnInspectRun')?.addEventListener('click', () => openSystemInspection('run', adminState.selectedExecution?.id));
  $('btnRunSupportBundle')?.addEventListener('click', event => {
    const id = adminState.selectedExecution?.id;
    if (id) downloadSupportBundle(event.currentTarget, {run_id: id});
  });
  $('btnCloseSystemInspector')?.addEventListener('click', () => $('systemInspectorDialog').close());
  $('systemInspectorDialog')?.addEventListener('close', () => {
    systemInspectionRequest += 1;
    if (systemInspectionPreviousFocus?.isConnected) systemInspectionPreviousFocus.focus();
    systemInspectionPreviousFocus = null;
  });
}
wireSystemUi();
