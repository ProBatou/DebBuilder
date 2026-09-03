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
  const upstreamArtifact = $('recipeArtifactMode')?.value === 'upstream_deb';
  ['.recipe-build-card','.recipe-install-card','.recipe-service-card'].forEach(selector => document.querySelector(selector)?.classList.toggle('not-applicable', upstreamArtifact));
  if ($('recipeArtifactPatternField')) $('recipeArtifactPatternField').hidden = !upstreamArtifact;
  const configuredFiles = $('installContentSource')?.value === 'configured_files';
  if ($('installDestination')) { $('installDestination').disabled = configuredFiles; $('installDestination').closest('label').hidden = configuredFiles; }
  const configured = !!$('serviceConfigured')?.checked;
  document.querySelectorAll('.recipe-service-card input, .recipe-service-card select, .recipe-service-card textarea').forEach(field => { if (field.id !== 'serviceConfigured') field.disabled = !configured; });
  if (!configured && $('serviceEnabled')) $('serviceEnabled').checked = false;
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
  const data = await postJson('/api/run', {workflow:wf, dry_run:true});
  if (data.detection) {
    $('buildDetectedProject').textContent = data.detection.display_name;
    $('buildDetectedProject').dataset.value = data.detection.project_type;
    $('buildDetectedFiles').textContent = data.detection.detected_files.join(', ');
    $('buildDetectedFiles').dataset.value = data.detection.detected_files.join('\n');
    $('buildDetectedDependencies').textContent = data.detection.build_dependencies.join(', ');
    $('buildDetectedDependencies').dataset.value = data.detection.build_dependencies.join('\n');
    if (!(wf.build?.commands || []).length) renderBuildCommands(data.detection.proposed_commands);
  }
  if (data.dependencies) {
    $('buildAvailableDependencies').textContent = data.dependencies.available.join(', ') || 'None';
    $('buildMissingDependencies').textContent = data.dependencies.missing.join(', ') || 'None';
    $('buildDependencyState').classList.toggle('has-missing', data.dependencies.missing.length > 0);
  }
  await loadExecutions();
}

async function buildReal() {
  const wf = collectWorkflow();
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
$('serviceConfigured')?.addEventListener('change',refreshRecipeApplicability);
$('newRecipeVersionSource')?.addEventListener('change',toggleNewVersionExpression);
$('newRecipeTracking')?.addEventListener('change',toggleNewVersionExpression);
['recipeMetaName','recipeMetaPackage','recipeMetaGithub','recipeMetaSourceRef','recipeMetaVersionExpression'].forEach(id => $(id)?.addEventListener('input',scheduleRecipeAutosave));
['recipeMetaTracking','recipeMetaVersionSource','recipeMetaActive','recipeArtifactMode'].forEach(id => $(id)?.addEventListener('change',event=>{toggleVersionExpression();refreshRecipeApplicability();scheduleRecipeAutosave(event);}));
$('recipeArtifactPattern')?.addEventListener('input',scheduleRecipeAutosave);
document.querySelectorAll('.recipe-build-card input, .recipe-build-card textarea, .recipe-build-card select, .recipe-install-card input, .recipe-install-card textarea, .recipe-install-card select, .recipe-service-card input, .recipe-service-card textarea, .recipe-service-card select').forEach(element => {
  element.addEventListener(element.tagName === 'SELECT' ? 'change' : 'input', () => {
    if (element.id === 'buildCommands') renderBuildCommands(lines(element.value));
    scheduleRecipeAutosave();
  });
});
$('btnBuildReal')?.addEventListener('click',()=>buildReal().catch(error=>alert(error.message)));
$('btnDeleteRecipeTop')?.addEventListener('click',()=>deleteCurrentRecipe().catch(error=>alert(`Delete failed: ${error.message}`)));
