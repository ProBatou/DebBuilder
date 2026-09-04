/* global $, postJson, collectWorkflow, renderWorkflow, switchView */
let currentRecipeId = '';
let renderingWorkflow = false;
let autosaveTimer = null;
let autosaveInFlight = false;
let autosaveRevision = 0;
let autosaveDirty = false;
let recipeMutationPaused = false;
let autosaveIdleWaiters = [];

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
  if ($('recipeArtifactFilesField')) $('recipeArtifactFilesField').hidden = !upstreamArchive;
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
}

function renderAccountProvisioning(owner = {}) {
  const user = value('installAccountUser');
  const group = value('installAccountGroup');
  const createUser = owner.create_user === true;
  const createGroup = owner.create_group === true;
  let mode = 'existing';
  if ((user !== 'root' || group !== 'root') && createUser === (user !== 'root') && createGroup === (group !== 'root')) mode = 'ensure';
  else if (createUser || createGroup) mode = 'custom';
  setValue('installAccountProvisioning', mode);
  const customOption = $('installAccountProvisioning')?.querySelector('option[value="custom"]');
  if (customOption) customOption.hidden = mode !== 'custom';
  if ($('installAccountProvisioningAdvanced')) $('installAccountProvisioningAdvanced').hidden = mode !== 'custom';
}

function refreshAccountProvisioning() {
  const user = value('installAccountUser');
  const group = value('installAccountGroup');
  let mode = $('installAccountProvisioning')?.value || 'existing';
  if (user === 'root' && group === 'root') { mode = 'existing'; setValue('installAccountProvisioning', mode); }
  if (mode === 'ensure') { $('installCreateUser').checked = user !== 'root'; $('installCreateGroup').checked = group !== 'root'; }
  if (mode === 'existing') { $('installCreateUser').checked = false; $('installCreateGroup').checked = false; }
  const customOption = $('installAccountProvisioning')?.querySelector('option[value="custom"]');
  if (customOption) customOption.hidden = mode !== 'custom';
  if ($('installAccountProvisioningAdvanced')) $('installAccountProvisioningAdvanced').hidden = mode !== 'custom';
}

const SERVICE_FIELD_IDS = ['serviceName','serviceUser','serviceGroup','serviceCommand','serviceEnvironmentFiles','serviceEnvironment','serviceAfter','serviceWants','serviceRequires','serviceConflicts','serviceRestartSec','serviceTimeoutStartSec','serviceTimeoutStopSec','serviceKillSignal','serviceKillMode','serviceLimitNOFILE','serviceSyslogIdentifier','serviceAmbientCapabilities','serviceExecStartPre','serviceExecStartPost','serviceExecStop','serviceStandardOutput','serviceStandardError'];

function configureService() {
  window.recipeServiceVisible = true;
  if (!value('serviceType')) setValue('serviceType', 'simple');
  if (!value('serviceRestart')) setValue('serviceRestart', 'on-failure');
  refreshRecipeApplicability();
  $('serviceName')?.focus();
}

function removeService() {
  if (!confirm('Remove this systemd service configuration?')) return;
  window.recipeServiceVisible = false;
  SERVICE_FIELD_IDS.forEach(id => setValue(id, ''));
  setValue('serviceType', ''); setValue('serviceRestart', '');
  if ($('serviceEnabled')) $('serviceEnabled').checked = false;
  window.recipeAdvancedFields.service_description = '';
  window.recipeAdvancedFields.service_working_directory = '';
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

function addArchiveSelectedFile(path) {
  const current = lines(value('recipeArtifactFiles'));
  if (!current.includes(path)) current.push(path);
  setValue('recipeArtifactFiles', current.join('\n'));
  scheduleRecipeAutosave();
}

function renderArchiveInspection(inspection) {
  const node = $('recipeArchiveInspection');
  if (!node) return;
  const files = inspection?.files || [];
  const source = inspection?.source || {};
  node.classList.remove('has-error');
  node.innerHTML = `<div class="archive-inspection-head"><strong>${esc(source.source || 'archive')} · ${esc(source.name || '')}</strong><span>${files.length} files shown</span></div>` +
    (files.length ? `<div class="archive-file-list">${files.slice(0, 80).map(row => `<div class="archive-file-row"><code>${esc(row.relative_path)}</code><span>${esc(String(row.size || 0))} bytes</span><button type="button" class="ghost compact-button" data-add-archive-file="${esc(row.relative_path)}">Add</button></div>`).join('')}</div>` : '<p>No regular file found in this archive.</p>');
}

function renderArchiveInspectionError(error) {
  const node = $('recipeArchiveInspection');
  if (!node) return;
  const details = error?.details || {};
  const sources = details.sources || [];
  node.classList.add('has-error');
  node.innerHTML = `<p>${esc(error?.message || 'Archive inspection failed')}</p>` +
    (sources.length ? `<div class="archive-file-list">${sources.map(row => `<div class="archive-file-row"><code>${esc(row.name)}</code><span>${esc(row.source)} · ${esc(row.archive_format)}</span></div>`).join('')}</div>` : '');
}

async function inspectArchive() {
  const wf = collectWorkflow();
  wf.artifact.selected_files = wf.artifact.selected_files || [];
  const node = $('recipeArchiveInspection');
  if (node) { node.classList.remove('has-error'); node.textContent = 'Inspecting archive…'; }
  const response = await fetch('/api/upstream-archive/inspect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({workflow:wf})});
  const payload = await response.json();
  if (!response.ok) {
    renderArchiveInspectionError(payload.error || {message:payload.error || response.statusText});
    return;
  }
  renderArchiveInspection(payload.inspection);
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

function formatBuildAudit(build) {
  if (!build) return '';
  const plan = build.plan || {};
  const results = build.commands || [];
  const lines = ['Build command audit', `Working directory: ${plan.working_directory || '—'}`];
  if (results.length) results.forEach(row => lines.push(`[${row.index}] ${row.command}\n  cwd: ${row.working_directory}\n  status: ${row.status} · exit: ${row.exit_code ?? '—'} · duration: ${row.duration}s\n  stdout: ${row.stdout || ''}\n  stderr: ${row.stderr || ''}`));
  else (plan.commands || []).forEach((row,index) => lines.push(`[${index+1}] ${row.command}\n  cwd: ${plan.working_directory}\n  status: validated, not executed`));
  return lines.join('\n');
}

async function dryRun() {
  const wf = collectWorkflow();
  if (!buildOutputIsComplete(wf.build.output)) throw new Error('Build output requires at least one relative path.');
  const data = await postJson('/api/run', {workflow:wf, dry_run:true});
  if (data.detection) {
    renderBuildEnvironment(data.detection);
    if (typeof setBuildOutputSuggestions === 'function') setBuildOutputSuggestions(data.detection.suggested_output_paths || []);
    if (!(wf.build?.commands || []).length) renderBuildCommands(data.detection.proposed_commands);
  }
  if (data.dependencies) {
    renderDependencyCheck(data.dependencies);
  }
  await loadExecutions();
}

async function buildReal() {
  const wf = collectWorkflow();
  if (!buildOutputIsComplete(wf.build.output)) throw new Error('Build output requires at least one relative path.');
  if (!confirm(`Build ${wf.package?.name || wf.package_name || wf.name} with the real pipeline?`)) return;
  const data = await postJson('/api/run', {workflow:wf, dry_run:false});
  await loadExecutions();
  switchView('logs');
}

async function deleteCurrentRecipe() {
  const id = $('workflowSelect')?.value || currentRecipeId || '';
  if (!id) throw new Error('No selected recipe');
  const name = $('recipeMetaName')?.value.trim() || id;
  if (!confirm(`Permanently delete recipe "${name}"?\nThe published package and APT repository will not be modified.`)) return;
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
  } finally {
    recipeMutationPaused = false;
  }
}

function reportAutosaveError(error) {
  console.error('Autosave recipe failed:', error);
}

function scheduleRecipeAutosave() {
  if (renderingWorkflow || recipeMutationPaused || !currentRecipeId) return;
  autosaveRevision += 1;
  autosaveDirty = true;
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => saveRecipeNow().catch(reportAutosaveError), 600);
}

function waitForAutosaveIdle() {
  if (!autosaveInFlight) return Promise.resolve();
  return new Promise(resolve => autosaveIdleWaiters.push(resolve));
}

async function saveRecipeNow() {
  if (autosaveInFlight || recipeMutationPaused) return;
  const wf = collectWorkflow();
  if (!$('recipeMetaName')?.checkValidity() || !$('recipeMetaPackage')?.checkValidity() || !$('recipeMetaGithub')?.checkValidity()) {
    return;
  }
  if (!buildOutputIsComplete(wf.build.output)) return;
  const revision = autosaveRevision;
  autosaveDirty = false;
  autosaveInFlight = true;
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
    }
  } finally {
    autosaveInFlight = false;
    const waiters = autosaveIdleWaiters;
    autosaveIdleWaiters = [];
    waiters.forEach(resolve => resolve());
    if (autosaveDirty && !recipeMutationPaused) {
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
  (data.workflows || []).forEach(w => {
    const opt = document.createElement('option');
    opt.value = w.id;
    opt.textContent = `${w.name} · ${w.source}${w.writable ? '' : ' readonly'}`;
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
  const res = await fetch('/api/workflows/' + encodeURIComponent(id));
  const wf = await res.json();
  if (!res.ok) throw new Error(wf.error || res.statusText);
  currentRecipeId = id;
  renderWorkflow(wf);
  document.getElementById('workflowName').value = wf.name || id;
  const title = document.getElementById('recipeTitle');
  if (title) title.textContent = wf.name || id;
  refreshRecipeApplicability();
}

async function showRuns() {
  const res = await fetch('/api/runs');
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  switchView('logs');
  await loadExecutions();
}

document.getElementById('btnDryRun').onclick = () => dryRun().catch(e => alert(e.message));
if (document.getElementById('btnLoad')) document.getElementById('btnLoad').onclick = () => loadSelectedWorkflow().catch(e => alert(e.message));
document.getElementById('btnRuns').onclick = () => showRuns().catch(e => alert(e.message));
document.getElementById('workflowSelect').onchange = () => loadSelectedWorkflow().catch(e => alert(e.message));

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
$('btnRemoveService')?.addEventListener('click',removeService);
$('newRecipeVersionSource')?.addEventListener('change',toggleNewVersionExpression);
$('newRecipeTracking')?.addEventListener('change',toggleNewVersionExpression);
['recipeMetaName','recipeMetaPackage','recipeMetaGithub','recipeMetaSourceRef','recipeMetaVersionExpression'].forEach(id => $(id)?.addEventListener('input',scheduleRecipeAutosave));
['recipeMetaTracking','recipeMetaVersionSource','recipeMetaActive','recipeArtifactMode','recipeArchiveSource','recipeArchiveFormat','recipeAssetSelection'].forEach(id => $(id)?.addEventListener('change',event=>{toggleVersionExpression();refreshRecipeApplicability();scheduleRecipeAutosave(event);}));
['recipeArtifactPattern','recipeArtifactName','recipeArtifactFiles'].forEach(id => $(id)?.addEventListener('input',scheduleRecipeAutosave));
$('btnInspectArchive')?.addEventListener('click',()=>inspectArchive().catch(error=>renderArchiveInspectionError({message:error.message})));
document.addEventListener('click', event => {
  const path = event.target?.dataset?.addArchiveFile;
  if (path) addArchiveSelectedFile(path);
});
document.querySelectorAll('.recipe-build-card input, .recipe-build-card textarea, .recipe-build-card select, .recipe-install-card input, .recipe-install-card textarea, .recipe-install-card select, .recipe-service-card input, .recipe-service-card textarea, .recipe-service-card select').forEach(element => {
  element.addEventListener(element.tagName === 'SELECT' ? 'change' : 'input', () => {
    if (element.closest('.build-output-section')) return;
    if (element.id === 'buildCommands') renderBuildCommands(lines(element.value));
    scheduleRecipeAutosave();
  });
});
$('btnBuildReal')?.addEventListener('click',()=>buildReal().catch(error=>alert(error.message)));
$('btnDeleteRecipeTop')?.addEventListener('click',()=>deleteCurrentRecipe().catch(error=>alert(`Delete failed: ${error.message}`)));
