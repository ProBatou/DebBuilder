<script>
  import {onMount, tick} from 'svelte';
  import {navigate, setNavigationGuard} from '../../navigation/location.js';
  import {hydrate, candidate, dirty, cancel, equal, changedPaths, validationErrors} from './draft.js';
  import {loadConflictVersion, saveExistingRecipe, createRecipe, validateRecipe} from './persistence.js';
  import {admitDraftRun} from './admission.js';
  import {fields} from './fields.js';
  import {presentationSection} from './presentation.js';
  import RecipeEditor from './RecipeEditor.svelte';
  import {recipePlan} from './model.js';
  import Provenance from '../../components/Provenance.svelte';
  import ErrorNotice from '../../components/ErrorNotice.svelte';
  import {t} from '../../i18n/i18n.js';
  export let recipe, revision = null, createMode = false, saved = () => {};
  export let inspection = null, automation = null, inspectionError = null, automationError = null, language = 'en', writable = true, editablePaths = [];
  let editor = null, loaded = null, received = null, editing = false, validating = false, validation = null, errors = null, section = 'plan';
  let saveState = 'clean', saving = false, currentRevision = null, conflict = null, conflictDialog, conflictHeading, saveStatus = '', replacementId = '';
  let admissionState = 'idle', admissionError = null, admittedRunId = '', admittedMode = '', admissionHeading;
  let admissionLocked = false;
  let discardDialog, resolveNavigation, pendingNavigation;
  $: if (recipe !== received) {received = recipe; loaded = recipe; editor = hydrate(recipe,{managed: Boolean(recipe.management),editablePaths}); editing = createMode; currentRevision = revision; saveState = createMode ? 'dirty' : 'clean'; conflict = null; validation = null; errors = null;}
  $: changed = editor && dirty(editor);
  $: admissionBusy = ['validating_test','starting_test','validating_build','starting_build'].includes(admissionState);
  $: canSave = editing && (changed || createMode) && writable && !saving && !validating && !admissionBusy && !conflict && (createMode || /^[0-9a-f]{64}$/.test(currentRevision || ''));
  $: runBlocked = createMode || editor?.draft.active === false;
  function update(next) {editor = next; validation = null; errors = null; if (!saving && !conflict) saveState = dirty(next) || createMode ? 'dirty' : 'clean';}
  function reset() {if (admissionLocked) return; editor = cancel(editor); editing = createMode || Boolean(conflict); validation = null; errors = null; saveState = conflict ? 'conflict' : createMode ? 'dirty' : 'clean';}
  async function showError(error) {
    errors = validationErrors(error); saveState = error.status === 422 ? 'validation_error' : 'save_error';
    const path = error.path || error.details?.path || '$';
    const target = fields.find(entry => path === `$.${entry.path}` || path.startsWith(`$.${entry.path}.`) || path.startsWith(`$.${entry.path}[`));
    if (target) section = presentationSection(target);
    await tick();
    const control = [...document.querySelectorAll('[data-recipe-path]')].find(node => node.dataset.recipePath === target?.path);
    control?.querySelector('input,textarea,select,button')?.focus();
  }
  async function validate() {
    if (admissionLocked) return;
    const snapshot = candidate(editor);
    const source = loaded;
    validating = true; saveState = 'validating'; validation = null; errors = null;
    try {const response = await validateRecipe(snapshot); if (editing && loaded === source && equal(editor.draft,snapshot)) validation = response;}
    catch (error) {
      if (!editing || loaded !== source || !equal(editor.draft,snapshot)) return;
      await showError(error);
    } finally {validating = false; if (saveState === 'validating') saveState = changed || createMode ? 'dirty' : 'clean';}
  }
  async function save() {
    if (!canSave || admissionLocked) return;
    const snapshot = candidate(editor), source = loaded, expected = currentRevision;
    saving = true; saveState = 'validating'; validation = null; errors = null; saveStatus = '';
    try {
      await validateRecipe(snapshot);
      saveState = 'saving';
      const result = createMode ? await createRecipe(snapshot.name, snapshot) : await saveExistingRecipe(snapshot.name, snapshot, expected);
      if (loaded !== source) return;
      const newer = !equal(editor.draft, snapshot);
      editor = {...editor, baseline: structuredClone(result.recipe), draft: newer ? editor.draft : structuredClone(result.recipe)};
      loaded = result.recipe; currentRevision = result.revision;
      saveState = newer ? 'dirty' : 'saved'; saveStatus = t('saved',language);
      const wasCreating = createMode;
      createMode = false;
      saving = false;
      await tick();
      saved({recipe:result.recipe, revision:result.revision, created:wasCreating, newer});
    } catch (error) {
      if (loaded !== source) return;
      if (error.status === 409 && (error.code === 'recipe_revision_conflict' || error.code === 'recipe_exists' || (createMode && error.code === 'builtin_recipe_reserved'))) {
        conflict = {error, latest:null, loadError:null}; replacementId = snapshot.name; saveState = 'conflict';
        await tick(); conflictDialog?.showModal(); conflictHeading?.focus();
        if (error.code === 'recipe_revision_conflict') {
          try {const latest = await loadConflictVersion(snapshot.name); if (conflict) conflict = {...conflict,latest};}
          catch (loadError) {if (conflict) conflict = {...conflict,loadError};}
        }
      } else await showError(error);
    } finally {saving = false;}
  }
  async function launch(dryRun) {
    if (admissionLocked || validating || saving || createMode || editor?.draft.active === false || !editor) return;
    admissionLocked = true;
    const action = dryRun ? 'test' : 'build';
    admissionState = `validating_${action}`;
    admissionError = null; admittedRunId = ''; errors = null; validation = null;
    try {
      admittedRunId = await admitDraftRun(editor, dryRun, {onStarting: () => admissionState = `starting_${action}`});
      admittedMode = action;
      admissionState = 'admitted';
    } catch (error) {
      admissionError = error;
      admissionState = 'admission_error';
      if (error.status === 422 || error.status === 400) {
        errors = validationErrors(error);
        const path = error.path || error.details?.path || '$';
        const target = fields.find(entry => path === `$.${entry.path}` || path.startsWith(`$.${entry.path}.`) || path.startsWith(`$.${entry.path}[`));
        if (target) section = presentationSection(target);
      }
      await tick(); admissionHeading?.focus();
    } finally {admissionLocked = false;}
  }
  function conflictPaths() {
    if (!conflict?.latest) return [];
    const local = new Set(changedPaths(editor, editor.baseline, editor.draft));
    const server = new Set(changedPaths(editor, editor.baseline, conflict.latest.recipe));
    return [...new Set([...local,...server])].sort().map(path => ({path, kind:local.has(path) && server.has(path) ? 'both' : local.has(path) ? 'local' : 'server'}));
  }
  function reloadConflict() {
    if (!conflict?.latest) return;
    editor = hydrate(conflict.latest.recipe,{managed:editor.managed,editablePaths:editor.editablePaths});
    loaded = conflict.latest.recipe; currentRevision = conflict.latest.revision; conflict = null;
    saveState = 'clean'; editing = false; conflictDialog.close();
  }
  function useNewIdentity() {
    if (!/^[A-Za-z0-9_.+-]+$/.test(replacementId) || replacementId === editor.draft.name) return;
    editor = {...editor, draft:{...editor.draft, name:replacementId}};
    conflict = null; saveState = 'dirty'; conflictDialog.close();
  }
  function navigationGuard() {
    if (saving || admissionLocked) return false;
    if (!editing || (!changed && !createMode)) return true;
    if (pendingNavigation) return pendingNavigation;
    pendingNavigation = new Promise(resolve => {resolveNavigation = resolve; discardDialog.showModal();});
    return pendingNavigation;
  }
  function decide(discard) {discardDialog.close(); const resolve = resolveNavigation; resolveNavigation = null; pendingNavigation = null; resolve?.(discard);}
  function beforeUnload(event) {if (saving || admissionLocked || (editing && (changed || createMode))) {event.preventDefault(); event.returnValue = '';}}
  onMount(() => {const release = setNavigationGuard(navigationGuard); window.addEventListener('beforeunload',beforeUnload); return () => {release(); window.removeEventListener('beforeunload',beforeUnload);};});
  $: plan = recipePlan(recipe);
  const planLabels = new Set(['Source','Tracking','Artifact mode','Build strategy','Output','Install destination','Service','Runtime Depends','Lifecycle hooks']);
  $: planRows = plan.summary.filter(([label]) => planLabels.has(label));
  $: canonicalJson = JSON.stringify(editing && editor ? candidate(editor) : recipe,null,2);
  let jsonCopied = false;
  async function copyJson() {try {await navigator.clipboard.writeText(canonicalJson); jsonCopied = true;} catch {jsonCopied = false;}}
  function brief(value,label) {
    if (label === 'Artifact mode') return value === 'source_build' ? t('sourceBuild',language) : String(value).startsWith('upstream_') ? t('prebuilt',language) : String(value);
    if (label === 'Service' && value === 'Not configured') return t('serviceNotConfigured',language);
    if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
    if (value && typeof value === 'object') {
      if (Array.isArray(value.paths)) return `${value.mode || 'paths'} · ${value.paths.length} paths`;
      if (value.path) return `${value.mode || 'path'} · ${value.path}`;
      return Object.keys(value).length ? `${Object.keys(value).length} ${t('details',language)}` : '—';
    }
    return String(value ?? '—');
  }
</script>
<dialog bind:this={discardDialog} oncancel={event => {event.preventDefault(); decide(false);}} aria-labelledby="discard-title"><div class="modal-head"><h2 id="discard-title">{t('discardChanges',language)}</h2><button type="button" class="icon-button" aria-label={t('close',language)} onclick={() => decide(false)}>×</button></div><p class="modal-copy">{t('unsavedChanges',language)}</p><div class="modal-actions"><button type="button" class="button secondary" onclick={() => decide(false)}>{t('keepEditing',language)}</button><button type="button" class="button primary" onclick={() => decide(true)}>{t('discard',language)}</button></div></dialog>
<dialog bind:this={conflictDialog} aria-labelledby="conflict-title" onclose={() => {}}>
  <div class="modal-head"><h2 id="conflict-title" bind:this={conflictHeading} tabindex="-1">{t('recipeConflict',language)}</h2><button type="button" class="icon-button" aria-label={t('close',language)} onclick={() => conflictDialog.close()}>×</button></div>
  <p class="modal-copy">{t('conflictPreserved',language)}</p>
  {#if conflict?.error?.code === 'recipe_exists' || conflict?.error?.code === 'builtin_recipe_reserved'}<p>{t('recipeAlreadyExists',language)}: <strong>{editor?.draft.name}</strong></p>{/if}
  {#if conflict?.loadError}<ErrorNotice error={conflict.loadError} language={language}/>{/if}
  {#if conflict?.latest}<div class="recipe-review"><details><summary>{t('reviewChanges',language)}</summary><ul>{#each conflictPaths() as row}<li><code>{row.path}</code> — {t(row.kind === 'both' ? 'changedBoth' : row.kind === 'local' ? 'changedLocally' : 'changedServer',language)}</li>{/each}</ul></details></div>{/if}
  {#if createMode}<label>{t('changeIdentity',language)} <input bind:value={replacementId} pattern="[A-Za-z0-9_.+-]+" aria-describedby="conflict-title"></label>{/if}
  <div class="modal-actions"><button type="button" class="button secondary" onclick={() => conflictDialog.close()}>{t('keepEditing',language)}</button>{#if conflict?.latest}<button type="button" class="button primary" onclick={reloadConflict}>{t('discardReload',language)}</button>{/if}{#if createMode}<button type="button" class="button secondary" onclick={useNewIdentity}>{t('changeIdentity',language)}</button>{/if}</div>
</dialog>
<div class="recipe-main" class:editing>
  <div class="editor-head recipe-identity"><div><p class="detail-breadcrumb">{t('recipes',language)} / <strong>{recipe.name}</strong></p><h2>{recipe.name}</h2><p class="muted">{recipe.active === false ? t('inactive',language) : t('active',language)} · Recipe v{recipe.schema_version}</p></div><div class="editor-actions identity-actions"><span class="chip">{editing ? (changed || createMode ? t('unsaved',language) : t('editing',language)) : writable ? t('view',language) : t('readOnly',language)}</span>{#if !editing && (writable || editor.managed)}<button class="button secondary" type="button" onclick={() => editing = true}>{t('edit',language)}</button>{/if}</div></div>
  <nav class="recipe-levels" aria-label="Recipe sections">
    <button class:active={section==='plan'} type="button" onclick={() => section='plan'}>{t('plan',language)}</button>
    <button class:active={section==='customize'} type="button" onclick={() => {editing=true;section='customize';}} disabled={!writable && !editor.managed}>{t('customize',language)}</button>
    <button class:active={section==='advanced'} type="button" onclick={() => {editing=true;section='advanced';}} disabled={!writable && !editor.managed}>{t('advancedTab',language)}</button>
    <button class:active={section==='expert'} type="button" onclick={() => {if (writable || editor.managed) editing=true;section='expert';}}>{t('expert',language)}</button>
  </nav>
  {#if editing}
    <div class="recipe-savebar" role="status"><strong>{t('reviewChanges',language)}</strong><div class="actions"><button type="button" class="button secondary" onclick={() => section='review'}>{t('reviewChanges',language)}</button><button class="button secondary" type="button" onclick={validate} disabled={validating || saving || admissionBusy}>{validating ? t('validating',language) : t('validate',language)}</button><button class="button primary" type="button" onclick={save} disabled={!canSave}>{saveState === 'validating' ? t('validating',language) : saveState === 'saving' ? t('saving',language) : t('save',language)}</button><button class="button secondary" type="button" onclick={reset} disabled={saving || admissionBusy}>{t('cancel',language)}</button></div></div>
    <p class="sr-only" role="status" aria-live="polite">{saveStatus}</p>
    {#if !createMode && !currentRevision}<p class="notice error">{t('revisionUnavailable',language)}</p>{/if}
    {#if conflict}<div class="notice error" role="alert"><p>{t('conflictPreserved',language)}</p><button type="button" onclick={() => {conflictDialog.showModal(); conflictHeading.focus();}}>{t('reviewChanges',language)}</button></div>{/if}
    {#if validation}<p class="notice" role="status">{t('validated',language)}</p>{/if}
    {#if errors}<div class="notice error" role="alert"><strong>{errors.sections ? Object.keys(errors.sections).join(', ') : ''}</strong><p>{errors.global[0]?.message || Object.values(errors.sections)[0]?.[0]?.message}</p><small>{errors.global[0]?.path || Object.values(errors.sections)[0]?.[0]?.path || '$'}</small></div>{/if}
    <fieldset class="recipe-save-fields" disabled={saving}><RecipeEditor {editor} {update} {language} {errors} bind:section/></fieldset>
  {:else if section === 'plan'}
  <section class="panel recipe-plan plan-card"><div class="section-head"><div><h3>{t('effectivePackagePlan',language)}</h3><p class="muted">{t('testRequired',language)}</p></div></div>
  <div class="recipe-pending">{t('noMatchingEvidence',language)} · {t('testRequired',language)}</div><div class="plan-rows">{#each planRows as [label,value,provenance]}<div><span>{t(label,language)}</span><strong>{brief(value,label)}</strong><Provenance kind={provenance} {language}/></div>{/each}</div></section>
  {/if}
  <section class="panel recipe-next recipe-run-actions" aria-busy={admissionBusy}>
    <h3>{t('nextStep',language)}</h3>
    <div class="actions">
      <button class="button primary" type="button" onclick={() => launch(true)} disabled={runBlocked || admissionBusy || validating || saving}>{admissionState === 'validating_test' ? t('validating',language) : admissionState === 'starting_test' ? t('startingTest',language) : t('test',language)}</button>
      <button class="button secondary" type="button" onclick={() => launch(false)} disabled={runBlocked || admissionBusy || validating || saving}>{admissionState === 'validating_build' ? t('validating',language) : admissionState === 'starting_build' ? t('startingBuild',language) : t('buildAction',language)}</button>
    </div>
    <p class="muted">{t('testBuildHelp',language)}</p>
    {#if createMode}<p class="muted">{t('saveBeforeRun',language)}</p>{:else if editor?.draft.active === false}<p class="muted">{t('enableBeforeRun',language)}</p>{/if}
    {#if admissionState === 'admitted'}<div class="notice" role="status" aria-live="polite"><strong>{t(admittedMode === 'test' ? 'testQueued' : 'buildQueued',language)}</strong> <button type="button" onclick={() => navigate('runs',admittedRunId)}>{t('viewRun',language)}</button></div>{/if}
    {#if admissionState === 'admission_error'}<div class="notice error" role="alert"><strong bind:this={admissionHeading} tabindex="-1">{t('runNotStarted',language)}</strong><p>{admissionError?.message}</p>{#if admissionError?.details?.path}<code>{admissionError.details.path}</code>{/if}{#if ['network','timeout','ambiguous'].includes(admissionError?.kind)}<p>{t('admissionUnknown',language)}</p><button type="button" onclick={() => navigate('runs')}>{t('viewRuns',language)}</button>{/if}</div>{/if}
  </section>
</div>
{#if !editing && section === 'plan'}
  {#if inspection || inspectionError}<details class="panel recipe-secondary recipe-inspection"><summary>{t('inspection',language)}</summary><ErrorNotice error={inspectionError} {language}/>{#if inspection}<div class="detail-facts">{#if inspection.identity?.recipe_schema_version}<div><span>{t('recipeSchema',language)}</span><strong>v{inspection.identity.recipe_schema_version}</strong></div>{/if}{#if inspection.source?.provider}<div><span>{t('source',language)}</span><strong>{inspection.source.provider}</strong></div>{/if}{#if inspection.build?.detected_project}<div><span>{t('detectedProject',language)}</span><strong>{inspection.build.detected_project}</strong></div>{/if}{#if inspection.artifact?.mode}<div><span>{t('artifactFact',language)}</span><strong>{brief(inspection.artifact.mode,'Artifact mode')}</strong></div>{/if}</div>{/if}</details>{/if}
  {#if automation || automationError}<details class="panel recipe-secondary recipe-automation"><summary>{t('automation',language)}</summary><ErrorNotice error={automationError} {language}/>{#if automation}<div class="detail-facts"><div><span>{t('status',language)}</span><strong>{t(automation.automation?.enabled ? 'enabledValue' : 'disabledValue',language)}</strong></div>{#if automation.automation?.policy}<div><span>{t('automationPolicy',language)}</span><strong>{automation.automation.policy}</strong></div>{/if}{#if automation.automation?.enabled && automation.state}<div><span>{t('currentState',language)}</span><strong>{automation.state}</strong></div>{/if}{#if automation.automation?.enabled && automation.last_check_at}<div><span>{t('lastCheck',language)}</span><strong>{automation.last_check_at}</strong></div>{/if}</div><p class="muted">{t('readOnly',language)}</p>{/if}</details>{/if}
{/if}
{#if section === 'expert'}<details class="panel recipe-json"><summary>{t('canonicalJson',language)}</summary><div class="recipe-json-actions"><button type="button" class="button secondary" onclick={copyJson}>{t('copy',language)}</button>{#if jsonCopied}<span role="status">{t('copied',language)}</span>{/if}</div><pre class="facts">{canonicalJson}</pre></details>{/if}
