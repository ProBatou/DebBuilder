/* global $, collectWorkflow, renderWorkflow, refreshWorkflows, loadSelectedWorkflow, showConfirm, showToast, waitForAutosaveIdle, setRecipeAutosaveState */

const RECIPE_JSON_MAX_BYTES = 2_000_000;
let recipeJsonState = {context: 'current', baseline: null, validated: null, collision: null};

function canonicalRecipeJson(recipe) {
  return JSON.stringify(recipe, null, 2) + '\n';
}

function parseRecipeJsonText(text) {
  if (!String(text || '').trim()) throw new Error('The Recipe JSON file is empty.');
  try {
    return JSON.parse(text);
  } catch (error) {
    const position = /position\s+(\d+)/i.exec(error.message || '');
    if (!position) throw new Error(`JSON syntax error: ${error.message}`);
    const offset = Number(position[1]);
    const prefix = String(text).slice(0, offset);
    const line = prefix.split('\n').length;
    const column = offset - prefix.lastIndexOf('\n');
    throw new Error(`JSON syntax error at line ${line}, column ${column}: ${error.message}`);
  }
}

function recipeJsonChangedPaths(before, after, path = '$') {
  if (Object.is(before, after)) return [];
  if (Array.isArray(before) || Array.isArray(after)) {
    return JSON.stringify(before) === JSON.stringify(after) ? [] : [path];
  }
  if (before && after && typeof before === 'object' && typeof after === 'object') {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    return keys.flatMap(key => recipeJsonChangedPaths(before[key], after[key], `${path}.${key}`));
  }
  return [path];
}

async function recipeJsonRequest(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error(`Recipe request failed (${response.status}).`);
  }
  if (!response.ok) {
    const detail = payload.error;
    const error = new Error(typeof detail === 'object' ? detail.message : (detail || response.statusText));
    error.code = detail?.code || 'request_failed';
    error.path = detail?.path || '$';
    error.status = response.status;
    throw error;
  }
  return payload;
}

function currentRecipeIsWritable() {
  const option = $('workflowSelect')?.selectedOptions?.[0];
  return !option || option.dataset.writable !== 'false';
}

function setRecipeJsonError(error) {
  const node = $('recipeJsonError');
  const message = error ? `${error.path && error.path !== '$' ? `${error.path}: ` : ''}${error.message || error}` : '';
  node.textContent = message;
  node.hidden = !message;
}

function renderRecipeJsonPreview() {
  const preview = $('recipeJsonPreview');
  if (!recipeJsonState.validated || !recipeJsonState.baseline) {
    preview.hidden = true;
    $('recipeJsonDialog').classList.toggle('has-preview', false);
    return;
  }
  const paths = recipeJsonChangedPaths(recipeJsonState.baseline, recipeJsonState.validated);
  $('recipeJsonChangeSummary').textContent = paths.length
    ? `${paths.length} changed field${paths.length === 1 ? '' : 's'}: ${paths.slice(0, 8).join(', ')}${paths.length > 8 ? `, +${paths.length - 8} more` : ''}`
    : 'No canonical changes.';
  $('recipeJsonBefore').textContent = canonicalRecipeJson(recipeJsonState.baseline);
  $('recipeJsonAfter').textContent = canonicalRecipeJson(recipeJsonState.validated);
  preview.hidden = paths.length === 0;
  $('recipeJsonDialog').classList.toggle('has-preview', paths.length > 0);
}

function setRecipeJsonMode(editing) {
  $('recipeJsonEditor').readOnly = !editing;
  $('recipeJsonMode').textContent = editing ? (recipeJsonState.context === 'import' ? 'Import' : 'Edit') : 'View';
  $('btnValidateRecipeJson').hidden = !editing;
  $('btnApplyRecipeJson').hidden = !editing;
  $('btnEditRecipeJson').hidden = editing;
  if (editing) $('recipeJsonEditor').focus();
}

function openRecipeJsonDialog({recipe, baseline, context = 'current', collision = null, editing = false}) {
  recipeJsonState = {context, baseline: baseline || recipe, validated: recipe, collision};
  $('recipeJsonEditor').value = canonicalRecipeJson(recipe);
  $('recipeJsonTitle').textContent = context === 'import' ? `Import ${recipe.name}` : `${recipe.name} JSON`;
  $('recipeJsonDescription').textContent = context === 'import'
    ? (collision ? `This will replace the existing ${collision.source} Recipe after confirmation.` : 'This will create a new Recipe after confirmation.')
    : 'Canonical persistable representation of the current form.';
  setRecipeJsonError(collision && !collision.replaceable ? new Error('A shipped Recipe has this ID and cannot be replaced.') : null);
  setRecipeJsonMode(editing);
  $('btnApplyRecipeJson').disabled = !editing || !recipeJsonState.validated || (collision && !collision.replaceable);
  $('btnExportRecipeJson').disabled = false;
  renderRecipeJsonPreview();
  $('recipeJsonDialog').showModal();
}

async function validateRecipeObject(recipe) {
  return recipeJsonRequest('/api/recipes/validate', {recipe});
}

async function openCurrentRecipeJson() {
  if (!currentRecipeId) throw new Error('No selected Recipe.');
  const result = await validateRecipeObject(collectWorkflow());
  openRecipeJsonDialog({recipe: result.recipe, baseline: result.recipe});
  $('btnEditRecipeJson').disabled = !currentRecipeIsWritable();
  if (!currentRecipeIsWritable()) $('recipeJsonDescription').textContent += ' This shipped Recipe is read-only.';
}

async function validateRecipeJsonEditor() {
  setRecipeJsonError(null);
  let parsed;
  try {
    parsed = parseRecipeJsonText($('recipeJsonEditor').value);
  } catch (error) {
    setRecipeJsonError(error);
    return null;
  }
  try {
    const result = await validateRecipeObject(parsed);
    if (recipeJsonState.context === 'current' && result.id !== currentRecipeId) {
      const error = new Error(`Recipe ID cannot change from “${currentRecipeId}” in the JSON editor. Use Import to create another Recipe.`);
      error.path = '$.name';
      throw error;
    }
    recipeJsonState.validated = result.recipe;
    recipeJsonState.collision = result.collision;
    $('recipeJsonEditor').value = canonicalRecipeJson(result.recipe);
    $('recipeJsonEditor').scrollTop = 0;
    $('recipeJsonEditor').scrollLeft = 0;
    $('btnApplyRecipeJson').disabled = !!(result.collision && !result.collision.replaceable) || recipeJsonChangedPaths(recipeJsonState.baseline, result.recipe).length === 0;
    $('btnExportRecipeJson').disabled = false;
    renderRecipeJsonPreview();
    if (result.collision && !result.collision.replaceable) {
      setRecipeJsonError(new Error('A shipped Recipe has this ID and cannot be replaced.'));
    } else {
      showToast('Recipe JSON is valid.', {type: 'success'});
    }
    return result;
  } catch (error) {
    recipeJsonState.validated = null;
    $('btnApplyRecipeJson').disabled = true;
    $('btnExportRecipeJson').disabled = true;
    renderRecipeJsonPreview();
    setRecipeJsonError(error);
    return null;
  }
}

function closeRecipeJsonDialog() {
  if ($('recipeJsonDialog').open) $('recipeJsonDialog').close();
}

async function applyCurrentRecipeJson() {
  const recipe = recipeJsonState.validated;
  const changed = recipeJsonChangedPaths(recipeJsonState.baseline, recipe);
  if (!recipe || !changed.length) return;
  const confirmed = await showConfirm({
    title: `Apply JSON changes to “${currentRecipeId}”?`,
    description: `${changed.length} canonical field${changed.length === 1 ? '' : 's'} will change. The form will be refreshed and the Recipe saved.`,
    confirmLabel: 'Apply and save',
  });
  if (!confirmed) return;
  recipeMutationPaused = true;
  clearTimeout(autosaveTimer);
  autosaveRevision += 1;
  autosaveDirty = false;
  try {
    await waitForAutosaveIdle();
    await recipeJsonRequest('/api/workflows/' + encodeURIComponent(currentRecipeId), {workflow: recipe, previous_id: currentRecipeId});
    renderWorkflow(recipe);
    $('workflowName').value = currentRecipeId;
    $('recipeTitle').textContent = recipe.name;
    setRecipeAutosaveState('saved');
    closeRecipeJsonDialog();
    showToast('Recipe JSON applied and saved.', {type: 'success'});
  } catch (error) {
    autosaveDirty = true;
    setRecipeAutosaveState('error', 'JSON changes were not saved. Retry Apply.');
    throw error;
  } finally {
    recipeMutationPaused = false;
  }
}

async function applyImportedRecipeJson() {
  const recipe = recipeJsonState.validated;
  if (!recipe) return;
  const replacing = !!recipeJsonState.collision;
  const confirmed = await showConfirm({
    title: replacing ? `Replace Recipe “${recipe.name}”?` : `Create Recipe “${recipe.name}”?`,
    description: replacing
      ? 'A user Recipe with this ID already exists. Its canonical JSON will be replaced explicitly.'
      : 'The validated canonical JSON will be saved as a new Recipe.',
    confirmLabel: replacing ? 'Replace Recipe' : 'Create Recipe',
    danger: replacing,
  });
  if (!confirmed) return;
  const result = await recipeJsonRequest('/api/recipes/import', {recipe, replace: replacing});
  await refreshWorkflows();
  $('workflowSelect').value = result.id;
  await loadSelectedWorkflow();
  closeRecipeJsonDialog();
  showToast(result.replaced ? `Recipe “${result.id}” replaced.` : `Recipe “${result.id}” imported.`, {type: 'success'});
}

async function applyRecipeJson() {
  if (recipeJsonState.context === 'import') await applyImportedRecipeJson();
  else await applyCurrentRecipeJson();
}

async function importRecipeFile(file) {
  if (!file) return;
  if (file.size > RECIPE_JSON_MAX_BYTES) throw new Error('Recipe JSON is too large (maximum 2 MB).');
  const parsed = parseRecipeJsonText(await file.text());
  const imported = await validateRecipeObject(parsed);
  let baseline = null;
  if (currentRecipeId) {
    try {
      baseline = (await validateRecipeObject(collectWorkflow())).recipe;
    } catch (_error) {
      baseline = null;
    }
  }
  openRecipeJsonDialog({recipe: imported.recipe, baseline: baseline || imported.recipe, context: 'import', collision: imported.collision, editing: true});
  $('btnApplyRecipeJson').disabled = !!(imported.collision && !imported.collision.replaceable);
}

async function copyRecipeJson() {
  const text = $('recipeJsonEditor').value;
  if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
  else {
    $('recipeJsonEditor').select();
    if (!document.execCommand('copy')) throw new Error('Clipboard access is unavailable.');
  }
  showToast('Recipe JSON copied.', {type: 'success'});
}

function exportRecipeJson() {
  if (!recipeJsonState.validated) return;
  const blob = new Blob([canonicalRecipeJson(recipeJsonState.validated)], {type: 'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${recipeJsonState.validated.name}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

$('btnRecipeJson')?.addEventListener('click', () => openCurrentRecipeJson().catch(error => showToast(error.message, {type: 'error'})));
$('btnImportRecipe')?.addEventListener('click', () => $('recipeImportFile').click());
$('recipeImportFile')?.addEventListener('change', event => {
  importRecipeFile(event.target.files?.[0]).catch(error => showToast(error.message, {type: 'error'}));
  event.target.value = '';
});
$('btnEditRecipeJson')?.addEventListener('click', () => setRecipeJsonMode(true));
$('recipeJsonEditor')?.addEventListener('input', () => {
  recipeJsonState.validated = null;
  $('btnApplyRecipeJson').disabled = true;
  $('btnExportRecipeJson').disabled = true;
  setRecipeJsonError(null);
  renderRecipeJsonPreview();
});
$('btnValidateRecipeJson')?.addEventListener('click', () => validateRecipeJsonEditor());
$('btnApplyRecipeJson')?.addEventListener('click', () => applyRecipeJson().catch(error => setRecipeJsonError(error)));
$('btnCopyRecipeJson')?.addEventListener('click', () => copyRecipeJson().catch(error => showToast(error.message, {type: 'error'})));
$('btnExportRecipeJson')?.addEventListener('click', exportRecipeJson);
['btnCloseRecipeJson', 'btnCancelRecipeJson'].forEach(id => $(id)?.addEventListener('click', closeRecipeJsonDialog));
$('recipeJsonDialog')?.addEventListener('click', event => { if (event.target === $('recipeJsonDialog')) closeRecipeJsonDialog(); });

window.recipeJsonTools = {canonicalRecipeJson, parseRecipeJsonText, recipeJsonChangedPaths};
