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
    return `<article class="package-row lifecycle" role="button" tabindex="0" data-admin-action="open-package" data-package-name="${esc(packageRow.name)}"><div><div class="package-name">${esc(packageRow.name)}</div><div class="package-sub">${esc(sourceLabel(packageRow))}${packageRow.recipe ? ` · recipe ${esc(packageRow.recipe)}` : ''}</div></div><div>${badge(lifecycleState(packageRow))}</div><div><div class="package-sub">Published version</div><strong>${esc(version.published || packageRow.apt_version || 'Not published')}</strong></div><div><div class="package-sub">Available version</div><strong>${esc(version.source || packageRow.upstream_version || 'Unknown')}</strong><div class="package-sub">Ref ${esc(sourceRefLabel(packageRow))}</div></div><div><div class="package-sub">Built / arch</div><strong>${esc(version.candidate || 'Never built')} · ${esc(packageRow.architecture || 'all')}</strong><div class="package-sub">Latest run: ${esc((packageRow.build || {}).latest_status || 'none')}</div></div></article>`;
  }).join('') || '<p class="muted">No package.</p>';
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
  $('packageDetail').innerHTML = `<div class="package-lifecycle-panel"><section><h3>General</h3><dl><dt>Linked recipe</dt><dd>${esc(packageRow.recipe || 'None')}</dd><dt>Description</dt><dd>${esc(packageRow.description || 'Not set')}</dd><dt>Architecture</dt><dd>${esc(packageRow.architecture || 'all')}</dd><dt>Dependencies</dt><dd>${esc(packageRow.depends || 'None declared')}</dd></dl></section><section><h3>Source</h3><dl><dt>Type</dt><dd>${esc(source.type || 'Unknown')}</dd><dt>Repository</dt><dd>${esc(source.repository || 'Not set')}</dd><dt>Strategy</dt><dd>${esc(packageRow.tracking || packageRow.version_strategy || version.strategy || 'Not set')}</dd><dt>Resolved ref</dt><dd>${esc(sourceRefLabel(packageRow))}</dd><dt>Latest resolved release</dt><dd>${esc(source.latest_release || 'Not fetched')}</dd></dl></section><section><h3>Versions</h3><dl><dt>Available upstream version</dt><dd>${esc(version.source || packageRow.upstream_version || 'Unknown')}</dd><dt>Latest built version</dt><dd>${esc(version.candidate || 'None')}</dd><dt>Published version</dt><dd>${esc(version.published || packageRow.apt_version || 'Not published')}</dd><dt>Current lifecycle</dt><dd>${badge(lifecycleState(packageRow))}</dd></dl></section><section><h3>Build</h3><dl><dt>Method</dt><dd>${esc(build.method || 'Not set')}</dd><dt>Latest run status</dt><dd>${esc(build.latest_status || 'No real run')}</dd><dt>Latest run</dt><dd>${esc(build.latest_run_id || 'None')}</dd><dt>Artifact run</dt><dd>${esc(build.last_build_id || 'None')}</dd><dt>Latest artifact</dt><dd>${esc(artifact || 'None')}</dd></dl></section><section><h3>Validation & publication</h3><dl><dt>Current run validation</dt><dd>${validation.status ? `${esc(validation.status)} · ${esc(validation.finished_at || validation.started_at || '')}` : 'Not run'}</dd><dt>Current run publication</dt><dd>${publication.status ? `${esc(publication.status)} · ${esc(publication.finished_at || publication.requested_at || '')}` : 'Not run'}</dd><dt>Repository version</dt><dd>${esc(version.published || packageRow.apt_version || 'None')} remains published</dd></dl></section><section><h3>APT repository</h3><dl><dt>Repository</dt><dd>${esc(repository.url || 'Not configured')}</dd><dt>Distribution</dt><dd>${esc(repository.distribution || 'Not configured')}</dd><dt>Component</dt><dd>${esc(repository.component || 'Not configured')}</dd><dt>Architectures</dt><dd>${esc((repository.architectures || [packageRow.architecture || 'all']).join(', '))}</dd><dt>Publication</dt><dd>${repository.published ? 'Published' : 'Not published'}</dd></dl></section></div><div class="actions package-actions">${actionButtons(packageRow)}</div><h3>History</h3>${(packageRow.history || []).map(execution => `<div class="item" role="button" tabindex="0" data-admin-action="open-history-execution" data-execution-id="${esc(execution.id)}"><div class="item-title"><span>${esc(execution.id)} · ${esc(execution.action)}</span>${badge(execution.lifecycle_status || execution.status)}</div><div class="item-meta">${fmtTime(execution.updated)}</div></div>`).join('') || '<p class="muted">No linked history.</p>'}<div class="danger-zone"><button class="danger" data-admin-action="delete-package" data-package-name="${esc(packageRow.name)}">Delete from DebBuilder</button><p class="muted">Does not delete the package from the APT repository.</p></div>`;
}

async function createPackageUi() {
  const name = prompt('Debian package name?');
  if (!name) return;
  const repository = prompt('Optional GitHub repository (owner/repo)?', '');
  const body = {name, architecture: 'all', source: repository ? {type: 'github', repository} : {type: 'manual'}};
  await postJson('/api/packages', body);
  await loadPackages();
  await openPackage(name);
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
  if (!confirm(`Delete ${name} from DebBuilder?\nThe package will NOT be removed from the APT repository.`)) return;
  const response = await fetch('/api/packages/' + encodeURIComponent(name), {method: 'DELETE'});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  closePackageDrawer();
  await loadPackages();
}

async function validatePackage(name) {
  const packageRow = adminState.packages.find(row => row.name === name);
  const runId = packageRow?.build?.latest_run_id;
  if (!runId) throw new Error('No successful Build Run is ready to validate');
  const response = await postLifecycleJson(`/api/executions/${encodeURIComponent(runId)}/validate`, {});
  alert(`Validation: ${response.validation.status}${response.validation.error ? `\n${response.validation.error.message}` : ''}`);
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
  if (!confirm(`Publish the validated artifact ${name} ${version} to APT?\n\nRequired confirmation: ${confirmation}`)) return;
  const response = await postLifecycleJson(`/api/executions/${encodeURIComponent(runId)}/publish`, {confirm: confirmation});
  alert(`Publication: ${response.publication.status}${response.publication.error ? `\n${response.publication.error.message}` : ''}`);
  await loadPackages();
}

async function buildPackage(name, dryRun = true) {
  const packageRow = adminState.packages.find(row => row.name === name);
  if (!packageRow || !packageRow.recipe) {
    alert('No linked recipe.');
    return;
  }
  if (!dryRun && !confirm(`Really build ${name} and validate the resulting package?`)) return;
  const workflow = await getJson('/api/workflows/' + encodeURIComponent(packageRow.recipe));
  try {
    const run = await postJson('/api/run', {workflow, dry_run: dryRun});
    alert(dryRun ? `Test finished: ${run.run_id} · code ${run.returncode}` : `Build finished: ${run.run_id} · status ${run.status || run.returncode}`);
    await Promise.all([loadExecutions(), loadPackages()]);
    await openPackage(name);
  } catch (error) {
    alert(`${dryRun ? 'Test' : 'Build'} failed: ${error.message}`);
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
    alert('Create a package first, then attach a recipe to it.');
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
