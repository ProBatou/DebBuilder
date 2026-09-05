function lifecycleState(packageRow) {
  return packageRow.lifecycle_display_status || packageRow.lifecycle_state || packageRow.status || 'unknown';
}

function sourceLabel(packageRow) {
  const source = packageRow.source || {};
  return source.repository ? `${source.type || 'github'} · ${source.repository}` : source.type || 'local';
}

function sourceRefLabel(packageRow) {
  const source = packageRow.source || {};
  return source.release || source.tag || source.branch || source.commit || source.default_branch || '—';
}

function packageMatches(packageRow) {
  const query = ($('packageSearch')?.value || '').toLowerCase();
  const filter = $('packageFilter')?.value || 'all';
  const text = [packageRow.name, packageRow.apt_version, packageRow.upstream_version, packageRow.architecture, packageRow.recipe, (packageRow.source || {}).repository, lifecycleState(packageRow)].join(' ').toLowerCase();
  return (!query || text.includes(query)) && (filter === 'all' || packageRow.status === filter || lifecycleState(packageRow) === filter);
}

function packageByName(name) {
  return adminState.packages.find(packageRow => packageRow.name === name);
}

function packageVersionLabel(packageRow) {
  return packageRow.apt_version || (packageRow.version || {}).published || 'not published';
}

function recipeIdFromPackageName(name) {
  return name.replace(/[^a-zA-Z0-9_.+-]/g, '-');
}

function renderPackageOptions() {
  if ($('packageOptions')) $('packageOptions').innerHTML = adminState.packages.map(packageRow => `<option value="${esc(packageRow.name)}">${esc(packageVersionLabel(packageRow))}</option>`).join('');
  const select = $('newRecipePackage');
  if (!select) return;
  const previous = select.value;
  const available = adminState.packages.filter(packageRow => !packageRow.recipe);
  select.innerHTML = available.map(packageRow => `<option value="${esc(packageRow.name)}">${esc(packageRow.name)} · ${esc(packageVersionLabel(packageRow))}</option>`).join('') || '<option value="">No package available without a recipe</option>';
  select.disabled = available.length === 0;
  if (previous && available.some(packageRow => packageRow.name === previous)) select.value = previous;
  if (!select.value && available.length) select.selectedIndex = 0;
  syncNewRecipeFromPackage();
}

async function loadPackages() {
  const data = await getJson('/api/packages');
  adminState.packages = data.packages || [];
  renderPackageOptions();
  renderPackages();
}

function actionButtons(packageRow) {
  const name = esc(packageRow.name);
  const actions = packageRow.allowed_actions || {};
  const buttons = [];
  if (packageRow.recipe) buttons.push(`<button class="btn-primary" data-admin-action="open-recipe" data-recipe-id="${esc(packageRow.recipe)}">Open recipe</button>`);
  else buttons.push(`<button class="btn-primary" data-admin-action="create-recipe" data-package-name="${name}">Create recipe</button>`);
  if (actions.test) buttons.push(`<button class="btn-warning" data-admin-action="build-package" data-package-name="${name}" data-dry-run="true">Test</button>`);
  if (actions.build) buttons.push(`<button class="btn-success" data-admin-action="build-package" data-package-name="${name}" data-dry-run="false">Build</button>`);
  if (actions.validate) buttons.push(`<button class="btn-primary" data-admin-action="validate-package" data-package-name="${name}">Validate</button>`);
  if (actions.publish) buttons.push(`<button class="btn-danger" data-admin-action="publish-package" data-package-name="${name}">Publish</button>`);
  return buttons.join('');
}

function renderPackages() {
  const rows = adminState.packages.filter(packageMatches);
  if ($('packageCount')) $('packageCount').textContent = `${rows.length} shown`;
  $('packageList').innerHTML = rows.map(packageRow => {
    const version = packageRow.version || {};
    return `<article class="package-row package-table-row list-row lifecycle" role="button" tabindex="0" data-admin-action="open-package" data-package-name="${esc(packageRow.name)}"><div class="package-cell package-identity" data-label="Package"><div class="package-name">${esc(packageRow.name)}</div><div class="package-sub">${esc(sourceLabel(packageRow))}${packageRow.recipe ? ` · recipe ${esc(packageRow.recipe)}` : ''}</div></div><div class="package-cell package-status" data-label="Status">${badge(lifecycleState(packageRow))}</div><div class="package-cell" data-label="Published"><strong>${esc(version.published || packageRow.apt_version || 'Not published')}</strong></div><div class="package-cell" data-label="Available"><strong>${esc(version.source || packageRow.upstream_version || 'Unknown')}</strong><div class="package-sub">Ref ${esc(sourceRefLabel(packageRow))}</div></div><div class="package-cell" data-label="Built / arch"><strong>${esc(version.candidate || 'Never built')} · ${esc(packageRow.architecture || 'all')}</strong><div class="package-sub">Latest run: ${esc((packageRow.build || {}).latest_status || 'none')}</div></div></article>`;
  }).join('') || '<div class="empty-state">No package matches these filters.</div>';
}

function packageDetailSection(title, rows) {
  const normalizedLabels = {Lifecycle: 'Current lifecycle', 'Latest built': 'Latest built version'};
  const content = rows.map(([label, value, isHtml = false]) => `<div class="detail-pair"><dt>${esc(normalizedLabels[label] || label)}</dt><dd>${isHtml ? value : esc(value || '—')}</dd></div>`).join('');
  return `<section class="drawer-section section"><h3>${esc(title)}</h3><dl class="detail-grid">${content}</dl></section>`;
}

function closePackageDrawer() {
  $('packageDrawer')?.classList.remove('open');
  $('packageDrawer')?.setAttribute('aria-hidden', 'true');
}

async function openPackage(name) {
  adminState.selectedPackage = name;
  const packageRow = (await getJson('/api/packages/' + encodeURIComponent(name))).package;
  const index = adminState.packages.findIndex(row => row.name === name);
  if (index >= 0) adminState.packages[index] = packageRow;
  const source = packageRow.source || {};
  const version = packageRow.version || {};
  const build = packageRow.build || {};
  const repository = packageRow.repository || {};
  if ($('packageDrawerTitle')) $('packageDrawerTitle').textContent = packageRow.name;
  $('packageDrawer')?.classList.add('open');
  $('packageDrawer')?.setAttribute('aria-hidden', 'false');
  const validation = packageRow.validation || {};
  const publication = packageRow.publication || {};
  const artifactName = (build.last_artifact || '').split('/').pop();
  const artifact = [artifactName, build.artifact_source, build.artifact_sha256].filter(Boolean).join(' · ');
  $('packageDetail').innerHTML = `<div class="package-lifecycle-panel stack stack--sm">${packageDetailSection('General', [['Linked recipe', packageRow.recipe || 'None'], ['Description', packageRow.description || 'Not set'], ['Architecture', packageRow.architecture || 'all'], ['Dependencies', packageRow.depends || 'None declared']])}${packageDetailSection('Source', [['Type', source.type || 'Unknown'], ['Repository', source.repository || 'Not set'], ['Strategy', packageRow.tracking || packageRow.version_strategy || version.strategy || 'Not set'], ['Resolved ref', sourceRefLabel(packageRow)], ['Latest release', source.latest_release || 'Not fetched']])}${packageDetailSection('Versions', [['Available upstream', version.source || packageRow.upstream_version || 'Unknown'], ['Latest built', version.candidate || 'None'], ['Published', version.published || packageRow.apt_version || 'Not published'], ['Lifecycle', badge(lifecycleState(packageRow)), true]])}${packageDetailSection('Build', [['Method', build.method || 'Not set'], ['Latest run status', build.latest_status || 'No real run'], ['Latest run', build.latest_run_id || 'None'], ['Artifact run', build.last_build_id || 'None'], ['Latest artifact', artifact || 'None']])}${packageDetailSection('Validation & publication', [['Validation', validation.status ? `${validation.status} · ${validation.finished_at || validation.started_at || ''}` : 'Not run'], ['Publication', publication.status ? `${publication.status} · ${publication.finished_at || publication.requested_at || ''}` : 'Not run'], ['Repository version', `${version.published || packageRow.apt_version || 'None'} remains published`]])}${packageDetailSection('APT repository', [['Repository', repository.url || 'Not configured'], ['Distribution', repository.distribution || 'Not configured'], ['Component', repository.component || 'Not configured'], ['Architectures', (repository.architectures || [packageRow.architecture || 'all']).join(', ')], ['Publication', repository.published ? 'Published' : 'Not published']])}</div><div class="package-action-bar toolbar"><div class="toolbar-actions">${actionButtons(packageRow)}</div></div><section class="drawer-history"><div class="section-header"><h3>History</h3></div><div class="data-list">${(packageRow.history || []).map(execution => `<div class="item list-row" role="button" tabindex="0" data-admin-action="open-history-execution" data-execution-id="${esc(execution.id)}"><div class="item-title"><span>${esc(execution.id)} · ${esc(execution.action)}</span>${badge(execution.lifecycle_status || execution.status)}</div><div class="item-meta">${fmtTime(execution.updated)}</div></div>`).join('') || '<div class="empty-state">No linked history.</div>'}</div></section><div class="danger-zone"><div><strong>Remove package</strong><p class="muted">Does not delete the package from the APT repository.</p></div><button class="btn btn--danger" data-admin-action="delete-package" data-package-name="${esc(packageRow.name)}">Delete from DebBuilder</button></div>`;
}

async function createPackageUi() {
  const name = await showPrompt({
    title: 'New package',
    description: 'Create the managed package entity first; a Recipe can be attached afterward.',
    inputLabel: 'Debian package name',
    inputPlaceholder: 'example-package',
    inputRequired: true,
    confirmLabel: 'Continue',
  });
  if (!name) return;
  const repository = await showPrompt({
    title: 'Link a GitHub repository',
    description: 'Optional. Leave this empty to create a manually managed package.',
    inputLabel: 'GitHub repository',
    inputPlaceholder: 'owner/repository',
    inputValue: '',
    confirmLabel: 'Create package',
  });
  const body = {name, architecture: 'all', source: repository ? {type: 'github', repository} : {type: 'manual'}};
  await postJson('/api/packages', body);
  await loadPackages();
  await openPackage(name);
  showToast(`Package ${name} created.`, {type: 'success'});
}

async function openLinkedRecipe(recipe) {
  closePackageDrawer();
  switchView('recipes');
  await refreshWorkflows();
  $('workflowSelect').value = recipe;
  await loadSelectedWorkflow();
}

async function createRecipeForPackage(name) {
  closePackageDrawer();
  switchView('recipes');
  await loadPackages();
  newRecipeUi(name);
}

async function deletePackageUi(name) {
  const confirmed = await showConfirm({
    title: `Delete ${name} from DebBuilder?`,
    description: 'The package entity is removed from DebBuilder. The APT repository is not modified.',
    confirmLabel: 'Delete package',
    danger: true,
  });
  if (!confirmed) return;
  const response = await fetch('/api/packages/' + encodeURIComponent(name), {method: 'DELETE'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  closePackageDrawer();
  await loadPackages();
  showToast(`Package ${name} deleted from DebBuilder.`, {type: 'success'});
}

async function validatePackage(name) {
  const packageRow = adminState.packages.find(row => row.name === name);
  const runId = packageRow?.build?.latest_run_id;
  if (!runId) throw new Error('No successful Build Run is ready to validate');
  const response = await postLifecycleJson(`/api/executions/${encodeURIComponent(runId)}/validate`, {});
  showToast(`Validation: ${response.validation.status}${response.validation.error ? ` — ${response.validation.error.message}` : ''}`, {type: response.validation.error ? 'error' : 'success'});
  await loadPackages();
  await openPackage(name);
}

async function publishPackage(name) {
  const packageRow = adminState.packages.find(row => row.name === name);
  const build = packageRow?.build || {};
  const version = (packageRow?.version || {}).candidate || '';
  const runId = build.latest_run_id || build.last_build_id;
  if (!runId || !version) throw new Error('No validated Build Run is ready to publish');
  const confirmation = `publish:${name}:${version}`;
  const confirmed = await showConfirm({
    title: `Publish ${name} ${version}?`,
    description: `This publishes the validated artifact to APT.\nRequired confirmation: ${confirmation}`,
    confirmLabel: 'Publish to APT',
  });
  if (!confirmed) return;
  const response = await postLifecycleJson(`/api/executions/${encodeURIComponent(runId)}/publish`, {confirm: confirmation});
  showToast(`Publication: ${response.publication.status}${response.publication.error ? ` — ${response.publication.error.message}` : ''}`, {type: response.publication.error ? 'error' : 'success'});
  await loadPackages();
}

async function buildPackage(name, dryRun = true) {
  const packageRow = adminState.packages.find(row => row.name === name);
  if (!packageRow || !packageRow.recipe) {
    showToast('No linked Recipe.', {type: 'warning'});
    return;
  }
  if (!dryRun) {
    const confirmed = await showConfirm({
      title: `Build ${name}?`,
      description: 'This starts the real build pipeline and validates the resulting package.',
      confirmLabel: 'Start build',
    });
    if (!confirmed) return;
  }
  const workflow = await getJson('/api/workflows/' + encodeURIComponent(packageRow.recipe));
  try {
    const run = await postJson('/api/run', {workflow, dry_run: dryRun});
    showToast(dryRun ? `Test finished: ${run.run_id} · code ${run.returncode}` : `Build finished: ${run.run_id} · status ${run.status || run.returncode}`, {type: 'success'});
    await Promise.all([loadExecutions(), loadPackages()]);
    await openPackage(name);
  } catch (error) {
    showToast(`${dryRun ? 'Test' : 'Build'} failed: ${error.message}`, {type: 'error'});
  }
}

function syncNewRecipeFromPackage(packageName) {
  const select = $('newRecipePackage');
  const selectedName = packageName || select?.value || '';
  const packageRow = packageByName(selectedName);
  if (select && packageName && Array.from(select.options).some(option => option.value === packageName)) select.value = packageName;
  if ($('newRecipeName')) $('newRecipeName').value = selectedName;
  if ($('newRecipeGithub')) $('newRecipeGithub').value = packageRow?.source?.repository || '';
}

function newRecipeUi(packageName = '') {
  renderPackageOptions();
  syncNewRecipeFromPackage(packageName);
  $('newRecipeDialog')?.showModal();
  ($('newRecipePackage')?.disabled ? $('cancelNewRecipe') : $('newRecipeGithub'))?.focus();
}

async function createRecipeFromDialog() {
  const packageName = $('newRecipePackage').value.trim();
  if (!packageName) {
    showToast('Create a package first, then attach a Recipe to it.', {type: 'warning'});
    return;
  }
  const tracking = $('newRecipeTracking').value;
  const versionSource = $('newRecipeVersionSource').value;
  const workflow = {schema_version: 1, name: packageName, active: true, package: {name: packageName}, source: {provider: 'github', repository: $('newRecipeGithub').value.trim(), tracking, ref: tracking === 'latest_release' ? '' : $('newRecipeSourceRef').value.trim(), version: {source: versionSource, expression: versionSource === 'regex' ? $('newRecipeVersionExpression').value.trim() : ''}}};
  currentRecipeId = recipeIdFromPackageName(packageName);
  renderWorkflow(workflow);
  $('recipeTitle').textContent = workflow.name;
  $('newRecipeDialog').close();
  switchView('recipes');
  autosaveRevision += 1;
  await saveRecipeNow();
  await Promise.all([refreshWorkflows(), loadPackages()]);
  $('workflowSelect').value = currentRecipeId;
}
