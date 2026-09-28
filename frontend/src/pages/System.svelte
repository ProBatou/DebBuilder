<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {loadRecipe} from '../features/recipes/persistence.js';
  import {t} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import RecipeDetail from '../features/recipes/RecipeDetail.svelte';
  import StructuredValue from '../features/recipes/StructuredValue.svelte';
  import {isManaged} from '../features/recipes/model.js';
  export let id = '', language = 'en';
  let snapshot = null, storage = null, error = null, controller, tab = 'health';
  let managed = null, managedRevision = null, managedError = null, managedInspection = null, managedAutomation = null, inspectionError = null, automationError = null, managedController, managedGeneration = 0, allowedOverrides = [];
  async function loadManaged() {
    managedController?.abort(); managedController = new AbortController(); const token = ++managedGeneration; managedError = null; managed = null;
    managedInspection = null; managedAutomation = null; inspectionError = null; automationError = null; allowedOverrides = [];
    try {
      const listing = await api.workflows({signal:managedController.signal});
      const entry = (listing.workflows || []).find(row => row.managed === true);
      if (!entry) throw new Error('Managed Recipe metadata unavailable');
      const canonical = await loadRecipe(entry.id,{signal:managedController.signal});
      if (!isManaged(canonical.recipe)) throw new Error('Managed Recipe metadata unavailable');
      if (token !== managedGeneration) return;
      allowedOverrides = entry.editable_paths || []; managed = canonical.recipe; managedRevision = canonical.revision;
      const [inspected, automated] = await Promise.allSettled([api.recipeInspection(entry.id,{signal:managedController.signal}),api.automation(entry.id,{signal:managedController.signal})]);
      if (token !== managedGeneration) return;
      if (inspected.status === 'fulfilled') managedInspection = inspected.value.inspection; else if (inspected.reason.name !== 'AbortError') inspectionError = inspected.reason;
      if (automated.status === 'fulfilled') managedAutomation = automated.value.automation; else if (automated.reason.name !== 'AbortError') automationError = automated.reason;
    } catch(caught) {if(caught.name !== 'AbortError' && token === managedGeneration) managedError = caught;}
  }
  function managedSaved({recipe,revision}) {managed = recipe; managedRevision = revision;}
  async function selectTab(next) {tab = next; if (next === 'managed') loadManaged(); await tick(); document.querySelector('.system-tabs button.active')?.scrollIntoView({block:'nearest',inline:'nearest'});}
  async function load() {controller?.abort(); controller = new AbortController(); error = null; try {const [a,b] = await Promise.all([api.diagnostics({signal:controller.signal}),api.storage({signal:controller.signal})]); snapshot = a; storage = b.storage;} catch(caught) {if(caught.name !== 'AbortError') error = caught;}}
  onMount(() => {load(); return () => {controller?.abort(); managedController?.abort();};});
  $: if (id === 'managed') {tab = 'managed'; loadManaged(); tick().then(() => document.querySelector('.system-tabs button.active')?.scrollIntoView({block:'nearest',inline:'nearest'}));}
</script>
<div class="tabs system-tabs" role="group" aria-label={t('system',language)}><button class:active={tab==='health'} onclick={() => selectTab('health')}>{t('health',language)}</button><button class:active={tab==='maintenance'} onclick={() => selectTab('maintenance')}>{t('maintenance',language)}</button><button class:active={tab==='developer'} onclick={() => selectTab('developer')}>{t('developer',language)}</button><button class:active={tab==='managed'} onclick={() => selectTab('managed')}>{t('managedSelfBuild',language)}</button></div><ErrorNotice {error} retry={load} {language}/>
{#if tab === 'health'}<div class="grid system-health"><section class="panel"><div class="section-head"><h2>{t('diagnosis',language)}</h2><span class="chip">{snapshot?.checks?.length || 0}</span></div>{#each (snapshot?.checks || []).filter(check => check.id !== 'repository.publication') as check (check.id)}<div class="system-check"><span><strong>{check.id}</strong><small>{check.message}</small></span><Status value={check.status} {language}/><details><summary>{t('details',language)}</summary><StructuredValue value={check.details || {}}/></details></div>{:else}<p>{t('loading',language)}</p>{/each}</section><aside class="panel managed-summary"><h2>{t('managedSelfBuild',language)}</h2><p class="muted">{t('allowedOverrides',language)}</p><button onclick={() => selectTab('managed')}>{t('managedSelfBuild',language)} →</button></aside></div><section class="panel system-repository"><h2>{t('repository',language)}</h2>{#each (snapshot?.checks || []).filter(check => check.id === 'repository.publication') as check}<div class="system-check"><span><strong>{check.id}</strong><small>{check.message}</small></span><Status value={check.status} {language}/><details><summary>{t('details',language)}</summary><StructuredValue value={check.details || {}}/></details></div>{/each}</section>
{:else if tab === 'maintenance'}<div class="maintenance-layout"><section class="panel"><h2>{t('maintenance',language)}</h2><p class="muted">{t('readOnly',language)} · {t('availableLater',language)}</p>{#if storage}<div class="system-check"><strong>{t('details',language)}</strong><details><summary>{t('open',language)}</summary><StructuredValue value={storage}/></details></div>{/if}</section></div>
{:else if tab === 'managed'}<ErrorNotice error={managedError} retry={loadManaged} {language}/>{#if managed}<section class="panel"><h2>{t('managedSelfBuild',language)}</h2><dl><dt>{t('managedIdentity',language)}</dt><dd>{managed.management.builtin_id}</dd><dt>{t('definitionVersion',language)}</dt><dd>{managed.management.definition_version}</dd><dt>{t('allowedOverrides',language)}</dt><dd><StructuredValue value={allowedOverrides}/></dd><dt>{t('currentOverrides',language)}</dt><dd><StructuredValue value={managed.management.operator_overrides}/></dd></dl></section><RecipeDetail recipe={managed} revision={managedRevision} inspection={managedInspection} automation={managedAutomation} {inspectionError} {automationError} {language} editablePaths={allowedOverrides} saved={managedSaved}/>{:else if !managedError}<p>{t('loading',language)}</p>{/if}
{:else}<section class="panel developer-panel"><h2>{t('developer',language)}</h2><p><a href="/api/openapi.json" target="_blank" rel="noopener noreferrer">{t('openapi',language)}</a></p><p class="muted">{t('availableLater',language)}: {t('inspectors',language)}, {t('supportBundle',language)}</p></section>{/if}
