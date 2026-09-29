<script>
  import {onMount, tick} from 'svelte';
  import {navigate, setNavigationGuard} from '../../navigation/location.js';
  import {hydrate, candidate, dirty, cancel, equal, changedPaths, validationErrors} from './draft.js';
  import {loadConflictVersion, saveExistingRecipe, createRecipe, validateRecipe} from './persistence.js';
  import {admitDraftRun} from './admission.js';
  import {fields} from './fields.js';
  import {recipeReviewLabel} from './reviewLabels.js';
  import {presentationSection} from './presentation.js';
  import RecipeEditor from './RecipeEditor.svelte';
  import RecipeField from './RecipeField.svelte';
  import {recipePlan} from './model.js';
  import Provenance from '../../components/Provenance.svelte';
  import ErrorNotice from '../../components/ErrorNotice.svelte';
  import {t} from '../../i18n/i18n.js';
  export let recipe, revision = null, createMode = false, saved = () => {};
  export let language = 'en', writable = true, editablePaths = [], compactManaged = false;
  let editor = null, loaded = null, received = null, editing = false, validating = false, validation = null, errors = null, section = 'plan';
  let saveState = 'clean', saving = false, currentRevision = null, conflict = null, conflictDialog, conflictHeading, saveStatus = '', replacementId = '';
  let admissionState = 'idle', admissionError = null, admittedRunId = '', admittedMode = '', admissionHeading;
  let admissionLocked = false;
  let discardDialog, resolveNavigation, pendingNavigation;
  const jsonMaxBytes = 2_000_000;
  let jsonText = '', jsonBase = '', jsonEditing = false, jsonApplying = false, jsonError = '', jsonTextarea, jsonFileInput, jsonDetails;
  $: if (recipe !== received) {received = recipe; loaded = recipe; editor = hydrate(recipe,{managed: Boolean(recipe.management),editablePaths}); editing = createMode || compactManaged; section = 'plan'; currentRevision = revision; saveState = createMode ? 'dirty' : 'clean'; conflict = null; validation = null; errors = null; jsonEditing = false; jsonText = ''; jsonBase = ''; jsonError = '';}
  $: changed = editor && dirty(editor);
  $: jsonPending = jsonEditing && jsonText !== jsonBase;
  $: admissionBusy = ['validating_test','starting_test','validating_build','starting_build'].includes(admissionState);
  $: canSave = editing && (changed || createMode) && writable && !saving && !validating && !jsonPending && !jsonApplying && !admissionBusy && !conflict && (createMode || /^[0-9a-f]{64}$/.test(currentRevision || ''));
  $: runBlocked = createMode || editor?.draft.active === false;
  function update(next) {editor = next; validation = null; errors = null; if (!saving && !conflict) saveState = dirty(next) || createMode ? 'dirty' : 'clean';}
  function reset() {if (admissionLocked) return; editor = cancel(editor); editing = createMode || compactManaged || Boolean(conflict); if (!compactManaged) section = 'plan'; validation = null; errors = null; jsonEditing = false; jsonText = ''; jsonBase = ''; jsonError = ''; saveState = conflict ? 'conflict' : createMode ? 'dirty' : 'clean';}
  function actionableError(error) {
    const service = editor?.draft.service;
    const missingName = !String(service?.name || '').trim();
    const missingCommand = !String(service?.command || '').trim();
    if (error.status === 422 && error.code === 'invalid_recipe' && error.message === 'The Recipe is invalid' && (error.path || error.details?.path || '$') === '$') {
      if (service?.enabled && (missingName || missingCommand))
        return {...error, path: missingName ? '$.service.name' : '$.service.command', message:t('serviceRequiresDetails',language)};
      if (!missingName && !/^[A-Za-z0-9_.@-]+\.service$/.test(service.name))
        return {...error, path:'$.service.name', message:t('serviceNameInvalid',language)};
    }
    return error;
  }
  function firstError() {return errors?.global?.[0] || Object.values(errors?.sections || {})[0]?.[0];}
  function errorTitle() {const path = firstError()?.path; return path && path !== '$' ? recipeReviewLabel(path,language) : t('recipeValidationIssue',language);}
  async function showError(error) {
    error = actionableError(error);
    errors = validationErrors(error); saveState = error.status === 422 ? 'validation_error' : 'save_error';
    const path = error.path || error.details?.path || '$';
    const target = fields.find(entry => path === `$.${entry.path}` || path.startsWith(`$.${entry.path}.`) || path.startsWith(`$.${entry.path}[`));
    if (target) section = presentationSection(target) === 'plan' && !createMode ? 'customize' : presentationSection(target);
    await tick();
    const controlPath = target?.path === 'automation.enabled' ? 'automation.policy' : target?.path;
    const control = [...document.querySelectorAll('[data-recipe-path]')].find(node => node.dataset.recipePath === controlPath);
    control?.querySelector('input,textarea,select,button')?.focus();
  }
  async function validate() {
    if (admissionLocked || jsonPending || jsonApplying) return;
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
    if (admissionLocked || validating || saving || jsonPending || jsonApplying || createMode || editor?.draft.active === false || !editor) return;
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
        error = actionableError(error);
        errors = validationErrors(error);
        const path = error.path || error.details?.path || '$';
        const target = fields.find(entry => path === `$.${entry.path}` || path.startsWith(`$.${entry.path}.`) || path.startsWith(`$.${entry.path}[`));
        if (target) section = presentationSection(target) === 'plan' && !createMode ? 'customize' : presentationSection(target);
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
    saveState = 'clean'; editing = compactManaged; if (!compactManaged) section = 'plan'; conflictDialog.close();
  }
  function useNewIdentity() {
    if (!/^[A-Za-z0-9_.+-]+$/.test(replacementId) || replacementId === editor.draft.name) return;
    editor = {...editor, draft:{...editor.draft, name:replacementId}};
    conflict = null; saveState = 'dirty'; conflictDialog.close();
  }
  function navigationGuard() {
    if (saving || admissionLocked || jsonApplying) return false;
    if (!editing || (!changed && !createMode && !jsonPending)) return true;
    if (pendingNavigation) return pendingNavigation;
    pendingNavigation = new Promise(resolve => {resolveNavigation = resolve; discardDialog.showModal();});
    return pendingNavigation;
  }
  function decide(discard) {discardDialog.close(); const resolve = resolveNavigation; resolveNavigation = null; pendingNavigation = null; resolve?.(discard);}
  function beforeUnload(event) {if (saving || admissionLocked || jsonApplying || (editing && (changed || createMode || jsonPending))) {event.preventDefault(); event.returnValue = '';}}
  onMount(() => {const release = setNavigationGuard(navigationGuard); window.addEventListener('beforeunload',beforeUnload); return () => {release(); window.removeEventListener('beforeunload',beforeUnload);};});
  $: plan = recipePlan(editor?.draft || recipe);
  const planLabels = new Set(['Source','Tracking','Artifact mode','Build strategy','Package','Output','Install destination','Service','Runtime Depends','Lifecycle hooks']);
  $: planRows = plan.summary.filter(([label]) => planLabels.has(label));
  const managedText = {
    en:{description:'DebBuilder owns the Recipe definition. Only operational settings below can be changed.',source:'GitHub source',tracking:'Tracking',definition:'Definition version',settings:'Editable settings',advanced:'Build and resource limits',active:'Active',maintainer:'Package maintainer',environment:'Build environment',inactivity:'Inactivity timeout (seconds)',maximum:'Maximum runtime (seconds)',memory:'Memory limit (bytes)',tasks:'Task limit',cpu:'CPU quota (%)',read:'Read bandwidth (bytes/s)',write:'Write bandwidth (bytes/s)'},
    fr:{description:'DebBuilder gère la définition de cette recette. Seuls les réglages ci-dessous peuvent être modifiés.',source:'Source GitHub',tracking:'Suivi',definition:'Version de définition',settings:'Réglages modifiables',advanced:'Construction et limites de ressources',active:'Active',maintainer:'Mainteneur du paquet',environment:'Environnement de construction',inactivity:'Délai d’inactivité (secondes)',maximum:'Durée maximale (secondes)',memory:'Limite mémoire (octets)',tasks:'Limite de tâches',cpu:'Quota CPU (%)',read:'Débit de lecture (octets/s)',write:'Débit d’écriture (octets/s)'},
    de:{description:'DebBuilder verwaltet die Rezeptdefinition. Nur die folgenden Betriebseinstellungen sind änderbar.',source:'GitHub Quelle',tracking:'Verfolgung',definition:'Definitionsversion',settings:'Änderbare Einstellungen',advanced:'Build und Ressourcenlimits',active:'Aktiv',maintainer:'Paketbetreuer',environment:'Build Umgebung',inactivity:'Inaktivitätszeitlimit (Sekunden)',maximum:'Maximale Laufzeit (Sekunden)',memory:'Speicherlimit (Bytes)',tasks:'Aufgabenlimit',cpu:'CPU Quote (%)',read:'Lesebandbreite (Bytes/s)',write:'Schreibbandbreite (Bytes/s)'},
    es:{description:'DebBuilder gestiona la definición de la receta. Solo pueden cambiarse los siguientes ajustes operativos.',source:'Origen GitHub',tracking:'Seguimiento',definition:'Versión de definición',settings:'Ajustes editables',advanced:'Compilación y límites de recursos',active:'Activa',maintainer:'Responsable del paquete',environment:'Entorno de compilación',inactivity:'Tiempo de inactividad (segundos)',maximum:'Duración máxima (segundos)',memory:'Límite de memoria (bytes)',tasks:'Límite de tareas',cpu:'Cuota CPU (%)',read:'Ancho de banda de lectura (bytes/s)',write:'Ancho de banda de escritura (bytes/s)'},
  };
  const managedCopy = key => managedText[language]?.[key] || managedText.en[key];
  const managedLabels = {active:'active', 'package.maintainer':'maintainer','build.environment':'environment','build.inactivity_timeout':'inactivity','build.maximum_runtime':'maximum','resource_limits.memory_max_bytes':'memory','resource_limits.tasks_max':'tasks','resource_limits.cpu_quota_percent':'cpu','resource_limits.io_read_bandwidth_max_bytes_per_sec':'read','resource_limits.io_write_bandwidth_max_bytes_per_sec':'write'};
  const managedCore = ['active','package.maintainer'];
  const managedAdvanced = ['build.environment','build.inactivity_timeout','build.maximum_runtime','resource_limits.memory_max_bytes','resource_limits.tasks_max','resource_limits.cpu_quota_percent','resource_limits.io_read_bandwidth_max_bytes_per_sec','resource_limits.io_write_bandwidth_max_bytes_per_sec'];
  const managedAllowed = path => editablePaths.some(allowed => path === allowed || path.startsWith(`${allowed}.`));
  const managedEntry = path => ({...fields.find(entry => entry.path === path),label:{[language]:managedCopy(managedLabels[path])}});
  const managedFieldError = path => Object.entries(errors?.fields || {}).filter(([key]) => key === `$.${path}` || key.startsWith(`$.${path}.`) || key.startsWith(`$.${path}[`)).flatMap(([,items]) => items)[0];
  $: canonicalJson = JSON.stringify(editing && editor ? candidate(editor) : recipe,null,2) + '\n';
  let jsonCopied = false;
  function selectExpert() {if (jsonEditing && !jsonPending && jsonBase !== canonicalJson) {jsonText = canonicalJson; jsonBase = canonicalJson;} if (writable || editor.managed) editing = true; section = 'expert'; if (jsonEditing) tick().then(() => {if (jsonDetails) jsonDetails.open = true;});}
  function beginJsonEdit() {jsonBase = canonicalJson; jsonText = canonicalJson; jsonError = ''; jsonEditing = true; tick().then(() => jsonTextarea?.focus());}
  function discardJson() {jsonEditing = false; jsonText = ''; jsonBase = ''; jsonError = '';}
  async function copyJson() {try {await navigator.clipboard.writeText(jsonEditing ? jsonText : canonicalJson); jsonCopied = true;} catch {jsonCopied = false;}}
  function exportJson() {
    const url = URL.createObjectURL(new Blob([canonicalJson],{type:'application/json;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = `${editor.draft.name}.json`; link.click(); URL.revokeObjectURL(url);
  }
  async function importJson(event) {
    const file = event.currentTarget.files?.[0]; event.currentTarget.value = '';
    if (!file) return;
    if (file.size > jsonMaxBytes) {jsonError = t('jsonTooLarge',language); return;}
    try {jsonBase = canonicalJson; jsonText = await file.text(); jsonEditing = true; jsonError = ''; await tick(); jsonTextarea?.focus();}
    catch {jsonError = t('jsonImportError',language);}
  }
  async function applyJson() {
    if (!jsonPending || jsonApplying || !writable || editor.managed) return;
    jsonError = '';
    if (new TextEncoder().encode(jsonText).length > jsonMaxBytes) {jsonError = t('jsonTooLarge',language); return;}
    let parsed;
    try {parsed = JSON.parse(jsonText);} catch (error) {jsonError = `${t('jsonSyntaxError',language)}: ${error.message}`; return;}
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {jsonError = t('jsonObjectRequired',language); return;}
    if (parsed.name !== editor.draft.name) {jsonError = t('jsonIdentityError',language); return;}
    if (jsonBase !== canonicalJson) {jsonError = t('jsonDraftChanged',language); return;}
    const source = loaded, text = jsonText, draftBeforeValidation = candidate(editor);
    jsonApplying = true;
    try {
      const result = await validateRecipe(parsed);
      if (loaded !== source || jsonText !== text) return;
      if (!equal(editor.draft,draftBeforeValidation)) {jsonError = t('jsonDraftChanged',language); return;}
      update({...editor,draft:structuredClone(result.recipe),modeCache:{}});
      validation = result; discardJson();
    } catch (error) {if (loaded === source && jsonText === text) jsonError = error.path && error.path !== '$' ? `${recipeReviewLabel(error.path,language)}: ${error.message}` : error.message;}
    finally {jsonApplying = false;}
  }
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
  {#if compactManaged}
    <section class="panel managed-recipe-summary">
      <div class="section-head"><div><h2>{t('managedSelfBuild',language)}</h2><p class="muted">{managedCopy('description')}</p></div></div>
      <div class="detail-facts">
        <div><span>{managedCopy('source')}</span><strong>{recipe.source?.repository || '—'}</strong></div>
        <div><span>{managedCopy('tracking')}</span><strong>{recipe.source?.tracking || '—'}</strong></div>
        <div><span>{managedCopy('definition')}</span><strong>{recipe.management?.definition_version ?? '—'}</strong></div>
      </div>
      <h3 class="managed-recipe-group-title">{managedCopy('settings')}</h3>
      {#if !currentRevision}<p class="notice error">{t('revisionUnavailable',language)}</p>{/if}
      {#if conflict}<div class="notice error" role="alert"><p>{t('conflictPreserved',language)}</p><button type="button" onclick={() => {conflictDialog.showModal(); conflictHeading.focus();}}>{t('reviewChanges',language)}</button></div>{/if}
      {#if errors}<div class="notice error" role="alert"><strong>{errorTitle()}</strong><p>{firstError()?.message}</p></div>{/if}
      <fieldset class="recipe-save-fields" disabled={saving}>
        <div class="managed-recipe-fields">{#each managedCore.filter(managedAllowed) as path}<RecipeField entry={managedEntry(path)} {editor} {update} {language} fieldError={managedFieldError(path)}/>{/each}</div>
        {#if managedAdvanced.some(managedAllowed)}<h3 class="managed-recipe-group-title">{managedCopy('advanced')}</h3><div class="managed-recipe-fields">{#each managedAdvanced.filter(managedAllowed) as path}<RecipeField entry={managedEntry(path)} {editor} {update} {language} fieldError={managedFieldError(path)}/>{/each}</div>{/if}
      </fieldset>
      <div class="managed-recipe-actions"><button class="button primary" type="button" onclick={save} disabled={!canSave}>{saveState === 'validating' ? t('validating',language) : saveState === 'saving' ? t('saving',language) : t('save',language)}</button><button class="button secondary" type="button" onclick={reset} disabled={!changed || saving || admissionBusy}>{t('cancel',language)}</button>{#if changed}<span role="status">{t('unsaved',language)}</span>{/if}</div>
      <p class="sr-only" role="status" aria-live="polite">{saveStatus}</p>
    </section>
  {:else}
  <div class="editor-head recipe-identity"><div><p class="detail-breadcrumb">{t('recipes',language)} / <strong>{recipe.name}</strong></p><h2>{recipe.name}</h2><p class="muted">{recipe.active === false ? t('inactive',language) : t('active',language)} · Recipe v{recipe.schema_version}</p></div></div>
  <nav class="recipe-levels" aria-label={t('recipeSections',language)}>
    <button class:active={section==='plan'} type="button" onclick={() => {section='plan'; if (!changed && !createMode) editing=false;}}>{t('plan',language)}</button>
    <button class:active={section==='customize'} type="button" onclick={() => {editing=true;section='customize';}} disabled={!writable && !editor.managed}>{t('customize',language)}</button>
    <button class:active={section==='advanced'} type="button" onclick={() => {editing=true;section='advanced';}} disabled={!writable && !editor.managed}>{t('advancedTab',language)}</button>
    <button class:active={section==='expert'} type="button" onclick={selectExpert}>{t('expert',language)}</button>
  </nav>
  {#if editing && (changed || createMode || conflict || errors || jsonPending)}
    <div class="recipe-savebar" role="status"><strong>{#if jsonPending}{t('jsonPending',language)}{:else if validation}<span class="recipe-validation-success"><span aria-hidden="true">✓</span>{t('validated',language)}</span>{:else}{t('reviewChanges',language)}{/if}</strong><div class="actions"><button type="button" class="button secondary" onclick={() => section='review'} disabled={jsonPending || jsonApplying}>{t('reviewChanges',language)}</button><button class="button secondary" type="button" onclick={validate} disabled={validating || saving || jsonPending || jsonApplying || admissionBusy}>{validating ? t('validating',language) : t('validate',language)}</button><button class="button primary" type="button" onclick={save} disabled={!canSave}>{saveState === 'validating' ? t('validating',language) : saveState === 'saving' ? t('saving',language) : t('save',language)}</button><button class="button secondary" type="button" onclick={reset} disabled={saving || jsonApplying || admissionBusy}>{t('cancel',language)}</button></div></div>
  {/if}
  {#if editing}
    <p class="sr-only" role="status" aria-live="polite">{saveStatus}</p>
    {#if !createMode && !currentRevision}<p class="notice error">{t('revisionUnavailable',language)}</p>{/if}
    {#if conflict}<div class="notice error" role="alert"><p>{t('conflictPreserved',language)}</p><button type="button" onclick={() => {conflictDialog.showModal(); conflictHeading.focus();}}>{t('reviewChanges',language)}</button></div>{/if}
    {#if errors}<div class="notice error" role="alert"><strong>{errorTitle()}</strong><p>{firstError()?.message}</p></div>{/if}
  {/if}
  {#if section === 'plan' && !createMode}
  <section class="panel recipe-plan plan-card"><div class="section-head"><div><h3>{t('effectivePackagePlan',language)}</h3><p class="muted">{t('testRequired',language)}</p></div></div>
  <div class="recipe-pending">{t('noMatchingEvidence',language)} · {t('testRequired',language)}</div><div class="plan-rows">{#each planRows as [label,value,provenance]}<div><span>{t(label,language)}</span><strong>{brief(value,label)}</strong><Provenance kind={provenance} {language}/></div>{/each}</div></section>
  {:else if editing}<fieldset class="recipe-save-fields" disabled={saving}><RecipeEditor {editor} {update} {language} {errors} bind:section/></fieldset>
  {/if}
  {/if}
  {#if !compactManaged}<section class="panel recipe-next recipe-run-actions" aria-busy={admissionBusy}>
    <h3>{t('nextStep',language)}</h3>
    <div class="actions">
      <button class="button primary" type="button" onclick={() => launch(true)} disabled={runBlocked || admissionBusy || validating || saving || jsonPending || jsonApplying}>{admissionState === 'validating_test' ? t('validating',language) : admissionState === 'starting_test' ? t('startingTest',language) : t('test',language)}</button>
      <button class="button secondary" type="button" onclick={() => launch(false)} disabled={runBlocked || admissionBusy || validating || saving || jsonPending || jsonApplying}>{admissionState === 'validating_build' ? t('validating',language) : admissionState === 'starting_build' ? t('startingBuild',language) : t('buildAction',language)}</button>
    </div>
    <p class="muted">{t('testBuildHelp',language)}</p>
    {#if createMode}<p class="muted">{t('saveBeforeRun',language)}</p>{:else if editor?.draft.active === false}<p class="muted">{t('enableBeforeRun',language)}</p>{/if}
    {#if admissionState === 'admitted'}<div class="notice" role="status" aria-live="polite"><strong>{t(admittedMode === 'test' ? 'testQueued' : 'buildQueued',language)}</strong> <button type="button" onclick={() => navigate('runs',admittedRunId)}>{t('viewRun',language)}</button></div>{/if}
    {#if admissionState === 'admission_error'}<div class="notice error" role="alert"><strong bind:this={admissionHeading} tabindex="-1">{t('runNotStarted',language)}</strong><p>{admissionError?.message}</p>{#if admissionError?.details?.path}<code>{admissionError.details.path}</code>{/if}{#if ['network','timeout','ambiguous'].includes(admissionError?.kind)}<p>{t('admissionUnknown',language)}</p><button type="button" onclick={() => navigate('runs')}>{t('viewRuns',language)}</button>{/if}</div>{/if}
  </section>{/if}
</div>
{#if !compactManaged && section === 'expert'}
  <details class="panel recipe-json" bind:this={jsonDetails}>
    <summary>{t('canonicalJson',language)}</summary>
    <div class="recipe-json-actions">
      <button type="button" class="button secondary" onclick={copyJson}>{t('copy',language)}</button>
      <button type="button" class="button secondary" onclick={exportJson} disabled={jsonPending}>{t('jsonExport',language)}</button>
      {#if writable && !editor.managed}
        <button type="button" class="button secondary" onclick={() => jsonFileInput?.click()} disabled={jsonPending || jsonApplying}>{t('jsonImport',language)}</button>
        <input class="sr-only" type="file" accept=".json,application/json" bind:this={jsonFileInput} onchange={importJson} aria-label={t('jsonImport',language)} />
        {#if !jsonEditing}<button type="button" class="button secondary" onclick={beginJsonEdit}>{t('jsonEdit',language)}</button>{/if}
      {/if}
      {#if jsonCopied}<span role="status">{t('copied',language)}</span>{/if}
    </div>
    {#if jsonEditing}
      <label class="recipe-json-label" for="recipe-json-text">{t('jsonEditorLabel',language)}</label>
      <textarea id="recipe-json-text" class="recipe-json-text" bind:this={jsonTextarea} bind:value={jsonText} oninput={() => {jsonError = ''; jsonCopied = false;}} disabled={jsonApplying} spellcheck="false"></textarea>
    {:else}
      <pre class="facts">{canonicalJson}</pre>
    {/if}
    {#if jsonError}<p class="notice error" role="alert">{jsonError}</p>{/if}
    {#if jsonEditing}
      <div class="recipe-json-actions">
        <button type="button" class="button primary" onclick={applyJson} disabled={!jsonPending || jsonApplying}>{jsonApplying ? t('validating',language) : t('jsonApply',language)}</button>
        <button type="button" class="button secondary" onclick={discardJson} disabled={jsonApplying}>{t('jsonDiscard',language)}</button>
        <span class="muted">{t('jsonHelp',language)}</span>
      </div>
    {/if}
  </details>
{/if}
