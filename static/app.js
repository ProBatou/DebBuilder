/* global $, postJson, collectWorkflow, renderWorkflow, switchView, ArchiveTree */
let currentRecipeId = '';
let currentRecipeManaged = false;
let currentRecipeEditablePaths = [];
let currentRecipeDocument = null;
let renderingWorkflow = false;
let autosaveTimer = null;
let autosaveInFlight = false;
let autosaveRevision = 0;
let autosaveDirty = false;
let recipeMutationPaused = false;
let autosaveIdleWaiters = [];
let recipeRunSubmissionInFlight = false;

function toggleVersionExpression() {
  if ($('recipeVersionExpressionField')) $('recipeVersionExpressionField').hidden = $('recipeMetaVersionSource')?.value !== 'regex';
  if ($('recipeSourceRefField')) $('recipeSourceRefField').hidden = $('recipeMetaTracking')?.value === 'latest_release';
}

function toggleNewVersionExpression() {
  if ($('newRecipeVersionExpressionField')) $('newRecipeVersionExpressionField').hidden = $('newRecipeVersionSource')?.value !== 'regex';
  if ($('newRecipeSourceRefField')) $('newRecipeSourceRefField').hidden = $('newRecipeTracking')?.value === 'latest_release';
}

function refreshRecipeApplicability() {
  const mode = $('recipeArtifactMode')?.value || 'source_build';
  const upstreamDeb = mode === 'upstream_deb';
  const upstreamArchive = mode === 'upstream_archive';
  const archiveSource = $('recipeArchiveSource')?.value || 'auto';
  const assetSelection = $('recipeAssetSelection')?.value || 'exact';
  const releaseAsset = upstreamArchive && archiveSource === 'release_asset';
  ['.recipe-build-card','.recipe-install-card','.recipe-service-card'].forEach(selector => document.querySelector(selector)?.classList.toggle('not-applicable', upstreamDeb || (upstreamArchive && selector === '.recipe-build-card')));
  if ($('recipeArchiveSourceField')) $('recipeArchiveSourceField').hidden = !upstreamArchive;
  if ($('recipeArchiveFormatField')) $('recipeArchiveFormatField').hidden = !(upstreamArchive && archiveSource === 'github_source');
  if ($('recipeAssetSelectionField')) $('recipeAssetSelectionField').hidden = !releaseAsset;
  if ($('recipeArtifactPatternField')) $('recipeArtifactPatternField').hidden = upstreamArchive ? !(releaseAsset && assetSelection === 'pattern') : !upstreamDeb;
  if ($('recipeArtifactNameField')) $('recipeArtifactNameField').hidden = !(releaseAsset && assetSelection === 'exact');
  if ($('recipeArchivePayloadField')) $('recipeArchivePayloadField').hidden = !upstreamArchive;
  if ($('recipeArchiveInspectionField')) $('recipeArchiveInspectionField').hidden = !upstreamArchive;
  const configuredFiles = $('installContentSource')?.value === 'configured_files';
  if ($('installDestination')) { $('installDestination').disabled = configuredFiles; $('installDestination').closest('label').hidden = configuredFiles; }
  if ($('installAutomaticGroup')) $('installAutomaticGroup').hidden = configuredFiles;
  if ($('installMappingTitle')) $('installMappingTitle').textContent = configuredFiles ? 'Custom mappings' : 'Additional mappings';
  if ($('installMappingHelp')) $('installMappingHelp').textContent = configuredFiles ? 'These mappings are the complete installed content for this package.' : 'Optionally install extra files from the selected build output at specific absolute paths.';
  renderInstallContentSummary();
  const staticMappingsOnly = $('buildDetectedProject')?.dataset.value === 'static' && lines(value('buildCommands')).length === 0 && configuredFiles;
  if ($('staticSourceSummary')) $('staticSourceSummary').hidden = !staticMappingsOnly;
  if ($('buildCommandsSection')) $('buildCommandsSection').hidden = staticMappingsOnly;
  if ($('buildOutputSection')) $('buildOutputSection').hidden = staticMappingsOnly;
  if ($('serviceEmptyState')) $('serviceEmptyState').hidden = !!window.recipeServiceVisible;
  if ($('serviceConfiguration')) $('serviceConfiguration').hidden = !window.recipeServiceVisible;
  const recipeEnabled = $('recipeMetaActive')?.checked !== false;
  ['btnDryRun', 'btnBuildReal'].forEach(id => {
    const button = $(id);
    if (!button) return;
    button.disabled = !recipeEnabled || recipeRunSubmissionInFlight;
    button.title = recipeRunSubmissionInFlight ? 'A Build/Test submission is already in progress.' : recipeEnabled ? '' : 'Enable this Recipe to test or build it.';
  });
  if (typeof scheduleRecipeStepUpdate === 'function') scheduleRecipeStepUpdate();
  applyRecipeManagementUi();
}

const MANAGED_RECIPE_CONTROL_BY_PATH = Object.freeze({
  active: 'recipeMetaActive',
  'package.maintainer': 'packageMaintainer',
  'build.environment': 'buildEnvironment',
  'build.inactivity_timeout': 'buildInactivityTimeout',
  'build.maximum_runtime': 'buildMaximumRuntime',
});

function applyRecipeManagementUi() {
  document.querySelectorAll('.recipe-step-card input, .recipe-step-card textarea, .recipe-step-card select, .recipe-step-card button').forEach(control => {
    if (currentRecipeManaged) {
      if (!control.disabled) control.dataset.managedDisabled = 'true';
      control.disabled = true;
    } else if (control.dataset.managedDisabled === 'true') {
      control.disabled = false;
      delete control.dataset.managedDisabled;
    }
  });
  if (currentRecipeManaged) {
    currentRecipeEditablePaths.forEach(path => {
      const control = $(MANAGED_RECIPE_CONTROL_BY_PATH[path]);
      if (control) control.disabled = false;
    });
  }
  const badge = $('recipeManagedBadge');
  if (badge) badge.hidden = !currentRecipeManaged;
  const notice = $('recipeManagedNotice');
  if (notice) notice.hidden = !currentRecipeManaged;
  const deleteButton = $('btnDeleteRecipeTop');
  if (deleteButton) {
    deleteButton.hidden = currentRecipeManaged;
    deleteButton.disabled = currentRecipeManaged;
  }
}

function renderRecipeLoadErrors(errors = []) {
  const node = $('recipeLoadErrors');
  if (!node) return;
  node.replaceChildren();
  errors.forEach(row => {
    const item = document.createElement('p');
    const detail = row?.error || {};
    const location = detail.path && detail.path !== '$' ? ` (${detail.path})` : '';
    item.textContent = `Recipe “${row?.id || 'unknown'}” could not be loaded: ${detail.message || 'invalid Recipe'}${location}`;
    node.appendChild(item);
  });
  node.hidden = errors.length === 0;
}

function recipePathValue(recipe, path) {
  return path.split('.').reduce((value, segment) => value?.[segment], recipe);
}

function setRecipePathValue(recipe, path, value) {
  const segments = path.split('.');
  const key = segments.pop();
  const target = segments.reduce((container, segment) => {
    if (!container[segment] || typeof container[segment] !== 'object') container[segment] = {};
    return container[segment];
  }, recipe);
  target[key] = structuredClone(value);
}

function workflowForCurrentRecipe() {
  const formDocument = collectWorkflow();
  if (!currentRecipeManaged || !currentRecipeDocument) return formDocument;
  const effective = structuredClone(currentRecipeDocument);
  currentRecipeEditablePaths.forEach(path => setRecipePathValue(effective, path, recipePathValue(formDocument, path)));
  return effective;
}

function renderAccountProvisioning(owner = {}) {
  const user = value('installAccountUser');
  const group = value('installAccountGroup');
  const createUser = owner.create_user === true;
  const createGroup = owner.create_group === true;
  let mode = 'existing';
  if ((user !== 'root' || group !== 'root') && createUser === (user !== 'root') && createGroup === (group !== 'root')) mode = 'ensure';
  setValue('installAccountProvisioning', mode);
}

function refreshAccountProvisioning() {
  const user = value('installAccountUser');
  const group = value('installAccountGroup');
  let mode = $('installAccountProvisioning')?.value || 'existing';
  if (user === 'root' && group === 'root') { mode = 'existing'; setValue('installAccountProvisioning', mode); }
}

const SERVICE_FIELD_IDS = ['serviceName','serviceDescription','serviceUser','serviceGroup','serviceCommand','serviceWorkingDirectory','serviceEnvironmentFiles','serviceEnvironment','serviceAfter','serviceWants','serviceRequires','serviceConflicts','serviceRestartSec','serviceTimeoutStartSec','serviceTimeoutStopSec','serviceKillSignal','serviceKillMode','serviceLimitNOFILE','serviceSyslogIdentifier','serviceAmbientCapabilities','serviceExecStartPre','serviceExecStartPost','serviceExecStop','serviceStandardOutput','serviceStandardError'];

function configureService() {
  window.recipeServiceVisible = true;
  if (!value('serviceType')) setValue('serviceType', 'simple');
  if (!value('serviceRestart')) setValue('serviceRestart', 'on-failure');
  refreshRecipeApplicability();
  $('serviceName')?.focus();
}

async function removeService() {
  const confirmed = await showConfirm({
    title: 'Remove systemd service?',
    description: 'This clears the service configuration from the current Recipe. The change is saved automatically.',
    confirmLabel: 'Remove service',
    danger: true,
  });
  if (!confirmed) return;
  window.recipeServiceVisible = false;
  SERVICE_FIELD_IDS.forEach(id => setValue(id, ''));
  setValue('serviceType', ''); setValue('serviceRestart', '');
  if ($('serviceEnabled')) $('serviceEnabled').checked = false;
  refreshRecipeApplicability();
  scheduleRecipeAutosave();
}

function projectDisplayName(projectType) {
  return ({nodejs:'Node.js', python:'Python', rust:'Rust · Cargo', static:'Static files · no build', upstream_archive:'Upstream release artifact · no source build'})[projectType] || projectType || 'Not detected';
}

function buildEnvironmentState(detection) {
  if (!detection?.project_type) return {key:'not-detected', label:'Not detected'};
  if (detection.project_type === 'nodejs' && !detection.node_version) return {key:'partially-detected', label:'Partially detected'};
  return {key:'detected', label:'Detected'};
}

function archiveCount(label, count) {
  const plural = label === 'directory' ? 'directories' : `${label}s`;
  return `${count} ${count === 1 ? label : plural}`;
}

function archiveSelectorRows(paths, role) {
  if (!paths.length) return '<p class="muted">None</p>';
  return `<div class="archive-selector-list">${paths.map(path => {
    const directory = path.endsWith('/');
    return `<div class="archive-selector-row"><code>${esc(path)}</code><span>${directory ? 'Recursive' : 'File'}</span><button type="button" class="ghost compact-button danger-text" data-archive-action="remove-${role}" data-archive-path="${esc(path)}">Remove</button></div>`;
  }).join('')}</div>`;
}

function renderArchivePayloadSummary() {
  const node = $('recipeArchivePayloadSummary');
  if (!node) return;
  const state = window.recipeArchiveState;
  const summary = ArchiveTree.selectionSummary(state);
  const selectedLabel = summary.mode === 'entire_archive'
    ? `Entire archive${summary.resolvedFiles === null ? '' : ` · ${archiveCount('file', summary.resolvedFiles)}`}`
    : `${archiveCount('directory', summary.selectedDirectories)} · ${archiveCount('explicit file', summary.explicitFiles)}${summary.resolvedFiles === null ? '' : ` · ${archiveCount('resolved file', summary.resolvedFiles)}`}`;
  const exclusions = archiveCount('directory', summary.excludedDirectories) + ` · ${archiveCount('file', summary.excludedFiles)}`;
  const legacy = state.payload.legacy_file_layout ? '<p class="archive-legacy-note">Existing file placement is preserved until you change this selection.</p>' : '';
  const missing = summary.missing.length ? `<p class="archive-selector-warning">Missing from inspected archive: ${summary.missing.map(esc).join(', ')}</p>` : '';
  node.innerHTML = `<section class="archive-summary-card archive-summary-card--selected"><div class="archive-summary-title"><strong>Selected</strong><span>${esc(selectedLabel)}</span></div>${summary.mode === 'paths' ? archiveSelectorRows(state.payload.include, 'include') : ''}</section><section class="archive-summary-card archive-summary-card--excluded"><div class="archive-summary-title"><strong>Excluded</strong><span>${esc(exclusions)}</span></div>${archiveSelectorRows(state.payload.exclude, 'exclude')}</section>${legacy}${missing}`;
}

function archiveTreeRow(node) {
  const state = window.recipeArchiveState;
  const expanded = node.kind === 'directory' && state.expanded.has(node.path);
  const label = node.path.split('/').filter(Boolean).at(-1) + (node.kind === 'directory' ? '/' : '');
  const stateLabel = ArchiveTree.entryState(state, node.path);
  const exactInclude = state.payload.include.includes(node.path);
  const exactExclude = state.payload.exclude.includes(node.path);
  const includeAllowed = !state.stale && state.payload.mode === 'paths' && !exactInclude && !state.payload.include.some(path => ArchiveTree.selectorMatches(path, node.path));
  const excludeAllowed = ArchiveTree.canExclude(state, node.path);
  const stateTone = stateLabel.startsWith('Excluded') ? ' archive-tree-state--excluded' : stateLabel.startsWith('Included') ? ' archive-tree-state--selected' : '';
  const stateMarkup = stateLabel ? `<span class="archive-tree-state${stateTone}">${esc(stateLabel)}</span>` : '';
  const toggle = node.kind === 'directory' ? `<button type="button" class="archive-tree-toggle" data-archive-action="toggle" data-archive-path="${esc(node.path)}" aria-expanded="${expanded}"${state.stale ? ' disabled' : ''}><span aria-hidden="true">${expanded ? '▾' : '▸'}</span><span class="sr-only">${expanded ? 'Collapse' : 'Expand'} ${esc(node.path)}</span></button>` : '<span class="archive-tree-spacer" aria-hidden="true"></span>';
  const metadata = node.kind === 'directory' ? archiveCount('file', node.descendant_files || 0) : `${Number(node.size || 0).toLocaleString()} bytes`;
  const includeAction = includeAllowed ? `<button type="button" class="ghost compact-button" data-archive-action="include" data-archive-path="${esc(node.path)}">${node.kind === 'directory' ? 'Include recursively' : 'Include'}</button>` : '';
  const excludeAction = excludeAllowed ? `<button type="button" class="ghost compact-button" data-archive-action="exclude" data-archive-path="${esc(node.path)}">${node.kind === 'directory' ? 'Exclude recursively' : 'Exclude'}</button>` : '';
  return `<div class="archive-tree-row" role="treeitem" aria-level="${node.depth + 1}" style="--archive-depth:${node.depth}">${toggle}<code>${esc(label)}</code><span class="archive-tree-meta">${esc(metadata)}</span>${stateMarkup}<span class="archive-tree-actions">${includeAction}${excludeAction}</span></div>`;
}

function renderArchivePayload() {
  const state = window.recipeArchiveState;
  document.querySelectorAll('input[name="recipeArchivePayloadMode"]').forEach(input => { input.checked = input.value === state.payload.mode; });
  renderArchivePayloadSummary();
  const node = $('recipeArchiveInspection');
  if (!node) return;
  node.classList.remove('has-error');
  node.classList.remove('is-stale');
  if (!state.tree) {
    node.innerHTML = '<p>No archive inspected.</p>';
  } else {
    const rows = ArchiveTree.visibleNodes(state.tree, state.expanded);
    const source = window.recipeArchiveInspectionMeta?.source || {};
    const sourceLabel = [source.source || 'archive', source.name || ''].filter(Boolean).join(' · ');
    node.classList.toggle('is-stale', state.stale);
    node.innerHTML = `<div class="archive-inspection-head"><strong>${esc(sourceLabel)}</strong><span>${archiveCount('file', state.inventory.file_count)} · ${archiveCount('directory', state.inventory.directory_count)}</span></div><div class="archive-tree" role="tree" aria-label="Archive contents">${rows.map(archiveTreeRow).join('')}</div>`;
  }
  const status = $('recipeArchiveInspectionStatus');
  if (status) status.textContent = !state.tree ? 'Inspect to browse archive contents.' : state.stale ? 'Inspection is stale. Inspect again to enable tree actions.' : state.selectionError ? state.selectionError.message : 'Inspection is current.';
}

function renderArchiveInspection(inspection) {
  window.recipeArchiveInspectionMeta = {source:inspection.source || {}, release:inspection.release || {}, extraction:inspection.extraction || {}};
  ArchiveTree.setInventory(window.recipeArchiveState, inspection.inventory, inspection.selection_error || null);
  renderArchivePayload();
}

function renderArchiveInspectionError(error) {
  const node = $('recipeArchiveInspection');
  if (!node) return;
  const details = error?.details || {};
  const sources = details.sources || [];
  if (window.recipeArchiveState?.tree) ArchiveTree.markStale(window.recipeArchiveState);
  node.classList.add('has-error');
  node.innerHTML = `<p>${esc(error?.message || 'Archive inspection failed')}</p>` +
    (sources.length ? `<div class="archive-file-list">${sources.map(row => `<div class="archive-file-row"><code>${esc(row.name)}</code><span>${esc(row.source)} · ${esc(row.archive_format)}</span></div>`).join('')}</div>` : '');
  if ($('recipeArchiveInspectionStatus')) $('recipeArchiveInspectionStatus').textContent = 'Inspection failed. Configured selections are unchanged.';
  renderArchivePayloadSummary();
}

async function inspectArchive() {
  const wf = workflowForCurrentRecipe();
  const inspectionWorkflow = structuredClone(wf);
  if (inspectionWorkflow.artifact?.payload?.mode === 'paths' && !inspectionWorkflow.artifact.payload.include.length) {
    inspectionWorkflow.artifact.payload = {mode:'entire_archive', include:[], exclude:[]};
  }
  const node = $('recipeArchiveInspection');
  if (node) { node.classList.remove('has-error'); node.textContent = 'Inspecting archive…'; }
  const response = await fetch('/api/upstream-archive/inspect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({workflow:inspectionWorkflow})});
  const payload = await response.json();
  if (!response.ok) {
    renderArchiveInspectionError(payload.error || {message:payload.error || response.statusText});
    return;
  }
  renderArchiveInspection(payload.inspection);
}

function markArchiveInspectionStale() {
  if (!window.recipeArchiveState?.tree || window.recipeArchiveState.stale) return;
  ArchiveTree.markStale(window.recipeArchiveState);
  renderArchivePayload();
}

function mutateArchivePayload(action, path = '') {
  const state = window.recipeArchiveState;
  const changed = action === 'include' ? ArchiveTree.includePath(state, path)
    : action === 'exclude' ? ArchiveTree.excludePath(state, path)
      : action === 'remove-include' ? ArchiveTree.removeInclude(state, path)
        : action === 'remove-exclude' ? ArchiveTree.removeExclude(state, path)
          : action === 'mode' ? ArchiveTree.setMode(state, path) : false;
  if (action === 'toggle') {
    if (ArchiveTree.toggleExpanded(state, path)) renderArchivePayload();
    return;
  }
  if (!changed) return;
  renderArchivePayload();
  scheduleRecipeAutosave();
}

function renderBuildEnvironment(detection = {}) {
  const projectType = detection.project_type || '';
  const state = buildEnvironmentState(detection);
  const badge = $('buildDetectionBadge');
  if (badge) { badge.className = `detection-badge ${state.key}`; badge.querySelector('strong').textContent = state.label; }
  if ($('buildDetectedProject')) { $('buildDetectedProject').textContent = detection.display_name || projectDisplayName(projectType); $('buildDetectedProject').dataset.value = projectType; }
  const detectedFiles = detection.detected_files || [];
  const buildTools = detection.build_tools || [];
  const buildDependencies = detection.system_build_dependencies || detection.build_dependencies || [];
  if ($('buildDetectedFiles')) { $('buildDetectedFiles').textContent = detectedFiles.join(' · ') || 'No source inspected yet'; $('buildDetectedFiles').dataset.value = detectedFiles.join('\n'); }
  if ($('buildDetectedTools')) { $('buildDetectedTools').textContent = buildTools.join(', ') || 'None'; $('buildDetectedTools').dataset.value = buildTools.join('\n'); }
  if ($('buildDetectedDependencies')) { $('buildDetectedDependencies').textContent = buildDependencies.join(', ') || 'None'; $('buildDetectedDependencies').dataset.value = buildDependencies.join('\n'); }
  if ($('buildToolsSummary')) $('buildToolsSummary').hidden = buildTools.length === 0;
  if ($('buildDependenciesSummary')) $('buildDependenciesSummary').hidden = buildDependencies.length === 0;
}

function renderDependencyCheck(dependencies) {
  const checked = dependencies !== undefined && dependencies !== null;
  const available = dependencies?.available || [];
  const missing = dependencies?.missing || [];
  const availableTools = dependencies?.available_tools || [];
  const missingTools = dependencies?.missing_tools || [];
  if ($('buildDependencyPending')) $('buildDependencyPending').hidden = checked;
  if ($('buildDependencyResults')) $('buildDependencyResults').hidden = !checked;
  if ($('buildAvailableDependencies')) $('buildAvailableDependencies').textContent = available.join(', ') || 'None';
  if ($('buildMissingDependencies')) $('buildMissingDependencies').textContent = missing.join(', ') || 'None';
  if ($('buildAvailableTools')) $('buildAvailableTools').textContent = availableTools.join(', ') || 'None';
  if ($('buildMissingTools')) $('buildMissingTools').textContent = missingTools.join(', ') || 'None';
  if ($('buildToolDetails')) $('buildToolDetails').innerHTML = (dependencies?.tool_checks || []).map(row => `<small><strong>${esc(row.tool)}</strong> · ${esc(row.status)} · ${esc(row.path || 'not found')}${row.version ? ` · ${esc(row.version)}` : ''}</small>`).join('');
  $('buildDependencyState')?.classList.toggle('has-missing', checked && (missing.length > 0 || missingTools.length > 0));
}

function renderInstallContentSummary() {
  const summary = $('installContentSummary');
  if (!summary) return;
  const mode = $('installContentSource')?.value || 'build_output';
  if (mode === 'configured_files') {
    summary.innerHTML = '';
    return;
  }
  const destination = value('installDestination') || `/opt/${value('recipeMetaPackage') || value('recipeMetaName') || 'package'}`;
  const output = collectBuildOutput();
  let rows;
  if (output.mode === 'paths') {
    rows = output.paths.map(path => ({source:path, destination:`${destination}/${path.replace(/^\.\/+/, '')}`}));
  } else if (output.mode === 'path') {
    rows = [{source:output.path || 'Selected path', destination:`${destination}/…`}];
  } else {
    rows = [{source:'Entire source tree', destination:`${destination}/…`}];
  }
  summary.innerHTML = rows.map(row => installMappingRowHtml(row)).join('');
}

function assertRecipeVersionRevisionIsValid() {
  if ($('recipePackageVersionRevision')?.checkValidity()) return;
  throw new Error('Debian revision is required.');
}

async function dryRun() {
  if (recipeRunSubmissionInFlight) return;
  recipeRunSubmissionInFlight = true;
  refreshRecipeApplicability();
  try {
    assertRecipeVersionRevisionIsValid();
    const wf = workflowForCurrentRecipe();
    if (!buildOutputIsComplete(wf.build.output)) throw new Error('Build output requires at least one relative path.');
    const data = await postJson('/api/run', {workflow:wf, dry_run:true});
    showToast(`Test queued: ${data.run_id}`, {type:'info'});
    openTestRunModal({runId: data.run_id, workflow: wf});
  } finally {
    recipeRunSubmissionInFlight = false;
    refreshRecipeApplicability();
  }
}

async function buildReal() {
  if (recipeRunSubmissionInFlight) return;
  recipeRunSubmissionInFlight = true;
  refreshRecipeApplicability();
  try {
    assertRecipeVersionRevisionIsValid();
    const wf = workflowForCurrentRecipe();
    if (!buildOutputIsComplete(wf.build.output)) throw new Error('Build output requires at least one relative path.');
    const confirmed = await showConfirm({
      title: `Build ${wf.package?.name || wf.name}?`,
      description: 'This starts the real build pipeline for the selected Recipe.',
      confirmLabel: 'Start build',
    });
    if (!confirmed) return;
    const data = await postJson('/api/run', {workflow:wf, dry_run:false});
    showToast(`Build queued: ${data.run_id}`, {type:'info'});
    await loadExecutions({resumePolling:false});
    switchView('logs');
    await openExecution(data.run_id);
  } finally {
    recipeRunSubmissionInFlight = false;
    refreshRecipeApplicability();
  }
}

async function deleteCurrentRecipe() {
  const id = $('workflowSelect')?.value || currentRecipeId || '';
  if (!id) throw new Error('No selected recipe');
  if (currentRecipeManaged) throw new Error('The built-in Recipe cannot be deleted.');
  const name = $('recipeMetaName')?.value.trim() || id;
  const confirmed = await showConfirm({
    title: `Delete Recipe “${name}”?`,
    description: 'The Recipe is permanently removed. The published package and APT repository are not modified.',
    confirmLabel: 'Delete Recipe',
    danger: true,
  });
  if (!confirmed) return;
  recipeMutationPaused = true;
  clearTimeout(autosaveTimer);
  autosaveRevision += 1;
  autosaveDirty = false;
  try {
    await waitForAutosaveIdle();
    const deleteId = id;
    const response = await fetch('/api/workflows/' + encodeURIComponent(deleteId), {method:'DELETE'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    currentRecipeId = '';
    await refreshWorkflows();
    if ($('workflowSelect')?.value) await loadSelectedWorkflow();
    else {
      $('workflowName').value = '';
      $('recipeTitle').textContent = 'Recipe';
    }
    showToast(`Recipe “${name}” deleted.`, {type: 'success'});
  } finally {
    recipeMutationPaused = false;
  }
}

function reportAutosaveError(error) {
  console.error('Autosave recipe failed:', error);
  const location = error?.path && error.path !== '$' ? `${error.path}: ` : '';
  setRecipeAutosaveState('error', `Save failed: ${location}${error.message || error}`);
}

function setRecipeAutosaveState(state, message = '') {
  const status = $('recipeAutosaveStatus');
  if (!status) return;
  status.dataset.state = state;
  status.textContent = message;
}

function scheduleRecipeAutosave() {
  if (renderingWorkflow || recipeMutationPaused || !currentRecipeId) return;
  markPreflightStale();
  autosaveRevision += 1;
  autosaveDirty = true;
  setRecipeAutosaveState('pending');
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => saveRecipeNow().catch(reportAutosaveError), 600);
}

function waitForAutosaveIdle() {
  if (!autosaveInFlight) return Promise.resolve();
  return new Promise(resolve => autosaveIdleWaiters.push(resolve));
}

async function saveRecipeNow() {
  if (autosaveInFlight || recipeMutationPaused) return;
  const wf = workflowForCurrentRecipe();
  if (!$('recipeMetaName')?.checkValidity() || !$('recipeMetaPackage')?.checkValidity() || !$('recipeMetaGithub')?.checkValidity() || !$('recipePackageVersionRevision')?.checkValidity()) {
    setRecipeAutosaveState('error', 'Fix invalid fields to save');
    return;
  }
  if (!buildOutputIsComplete(wf.build.output)) {
    setRecipeAutosaveState('error', 'Complete build output to save');
    return;
  }
  if (wf.artifact?.mode === 'upstream_archive' && wf.artifact.payload?.mode === 'paths' && !wf.artifact.payload.include.length) {
    setRecipeAutosaveState('pending');
    return;
  }
  const revision = autosaveRevision;
  autosaveDirty = false;
  autosaveInFlight = true;
  setRecipeAutosaveState('saving');
  const id = wf.name.replace(/[^a-zA-Z0-9_.+-]/g, '-');
  const previousId = currentRecipeId;
  try {
    await postJson('/api/workflows/' + id, {workflow:wf, previous_id:previousId});
    if (autosaveRevision === revision && currentRecipeId === previousId) {
      currentRecipeId = id;
      $('workflowName').value = id;
      $('recipeTitle').textContent = wf.name;
      if (id !== previousId) {
        await refreshWorkflows();
        $('workflowSelect').value = id;
      }
      setRecipeAutosaveState('saved');
      if (currentRecipeManaged) currentRecipeDocument = await getJson('/api/workflows/' + encodeURIComponent(id));
    }
  } finally {
    autosaveInFlight = false;
    const waiters = autosaveIdleWaiters;
    autosaveIdleWaiters = [];
    waiters.forEach(resolve => resolve());
    if (autosaveDirty && !recipeMutationPaused) {
      setRecipeAutosaveState('pending');
      clearTimeout(autosaveTimer);
      autosaveTimer = setTimeout(() => saveRecipeNow().catch(reportAutosaveError), 0);
    }
  }
}

async function refreshWorkflows() {
  const res = await fetch('/api/workflows');
  const data = await res.json();
  const select = document.getElementById('workflowSelect');
  const previous = select.value;
  select.innerHTML = '';
  renderRecipeLoadErrors(data.errors || []);
  (data.workflows || []).forEach(w => {
    const opt = document.createElement('option');
    opt.value = w.id;
    opt.dataset.writable = String(w.writable !== false);
    opt.dataset.managed = String(w.managed === true);
    opt.dataset.editablePaths = JSON.stringify(w.editable_paths || []);
    opt.textContent = w.managed ? `${w.name} · Built-in` : `${w.name} · ${w.source}${w.writable ? '' : ' readonly'}`;
    opt.title = opt.textContent;
    select.appendChild(opt);
  });
  const current = document.getElementById('workflowName')?.value || previous || '';
  if (current && Array.from(select.options).some(o => o.value === current)) select.value = current;
  else if (select.options.length) select.selectedIndex = 0;
}

async function loadSelectedWorkflow() {
  const id = document.getElementById('workflowSelect').value;
  if (!id) return;
  clearTimeout(autosaveTimer);
  autosaveRevision += 1;
  autosaveDirty = false;
  const selected = document.getElementById('workflowSelect').selectedOptions[0];
  const wf = await getJson('/api/workflows/' + encodeURIComponent(id));
  currentRecipeManaged = selected?.dataset.managed === 'true';
  try {
    currentRecipeEditablePaths = JSON.parse(selected?.dataset.editablePaths || '[]');
  } catch (_error) {
    currentRecipeEditablePaths = [];
  }
  currentRecipeDocument = wf;
  currentRecipeId = id;
  clearPreflightReport();
  renderWorkflow(wf);
  document.getElementById('workflowName').value = wf.name || id;
  const title = document.getElementById('recipeTitle');
  if (title) title.textContent = wf.name || id;
  refreshRecipeApplicability();
  setRecipeAutosaveState('saved');
}

document.getElementById('btnDryRun').addEventListener('click', () => dryRun().catch(error => showToast(error.message, {type: 'error'})));
document.getElementById('btnLoad')?.addEventListener('click', () => loadSelectedWorkflow().catch(error => showToast(error.message, {type: 'error'})));
document.getElementById('btnRuns').addEventListener('click', () => switchView('logs'));
document.getElementById('workflowSelect').addEventListener('change', () => loadSelectedWorkflow().catch(error => showToast(error.message, {type: 'error'})));

fetch('/api/status').then(r=>r.json()).then(j=>{
  const repo = j.repo_default || '';
  document.getElementById('status').textContent = repo ? `curl -fsSL ${repo.replace(/\/$/, '')}/install.sh | sudo bash` : 'APT repository not configured';
});
refreshWorkflows().then(() => loadSelectedWorkflow()).catch(e => console.error('Error workflows:', e));


$('recipeMetaVersionSource')?.addEventListener('change',toggleVersionExpression);
$('installContentSource')?.addEventListener('change',refreshRecipeApplicability);
$('installDestination')?.addEventListener('input',renderInstallContentSummary);
$('installAccountProvisioning')?.addEventListener('change',refreshAccountProvisioning);
['installAccountUser','installAccountGroup'].forEach(id => $(id)?.addEventListener('input',refreshAccountProvisioning));
$('btnConfigureService')?.addEventListener('click',configureService);
$('btnRemoveService')?.addEventListener('click',()=>removeService().catch(error=>showToast(error.message, {type:'error'})));
$('newRecipeVersionSource')?.addEventListener('change',toggleNewVersionExpression);
$('newRecipeTracking')?.addEventListener('change',toggleNewVersionExpression);
['recipeMetaName','recipeMetaPackage','recipeMetaGithub','recipeMetaSourceRef','recipeMetaVersionExpression','recipePackageVersionRevision'].forEach(id => $(id)?.addEventListener('input',event=>{if (['recipeMetaGithub','recipeMetaSourceRef','recipeMetaVersionExpression'].includes(id)) markArchiveInspectionStale();scheduleRecipeAutosave(event);}));
['recipeMetaTracking','recipeMetaVersionSource','recipeMetaActive','recipeArtifactMode','recipeArchiveSource','recipeArchiveFormat','recipeAssetSelection'].forEach(id => $(id)?.addEventListener('change',event=>{if (['recipeMetaTracking','recipeMetaVersionSource','recipeArtifactMode','recipeArchiveSource','recipeArchiveFormat','recipeAssetSelection'].includes(id)) markArchiveInspectionStale();toggleVersionExpression();refreshRecipeApplicability();scheduleRecipeAutosave(event);}));
['recipeArtifactPattern','recipeArtifactName'].forEach(id => $(id)?.addEventListener('input',event=>{markArchiveInspectionStale();scheduleRecipeAutosave(event);}));
$('recipeArchivePayloadField')?.querySelectorAll('input[name="recipeArchivePayloadMode"]').forEach(input => input.addEventListener('change', () => mutateArchivePayload('mode', input.value)));
$('btnInspectArchive')?.addEventListener('click',()=>inspectArchive().catch(error=>renderArchiveInspectionError({message:error.message})));
document.addEventListener('click', event => {
  const control = event.target?.closest?.('[data-archive-action]');
  const action = control?.dataset.archiveAction;
  if (action) mutateArchivePayload(action, control.dataset.archivePath || '');
});
document.querySelectorAll('.recipe-build-card input, .recipe-build-card textarea, .recipe-build-card select, .recipe-install-card input, .recipe-install-card textarea, .recipe-install-card select, .recipe-service-card input, .recipe-service-card textarea, .recipe-service-card select').forEach(element => {
  element.addEventListener(element.tagName === 'SELECT' ? 'change' : 'input', () => {
    if (element.closest('.build-output-section')) return;
    if (element.id === 'buildCommands') renderBuildCommands(lines(element.value));
    scheduleRecipeAutosave();
  });
});
$('btnBuildReal')?.addEventListener('click',()=>buildReal().catch(error=>showToast(error.message, {type:'error'})));
$('btnDeleteRecipeTop')?.addEventListener('click',()=>deleteCurrentRecipe().catch(error=>showToast(`Delete failed: ${error.message}`, {type:'error'})));
