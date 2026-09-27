<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {t} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import RecipeDetail from '../features/recipes/RecipeDetail.svelte';
  import StructuredValue from '../features/recipes/StructuredValue.svelte';
  import {isManaged} from '../features/recipes/model.js';
  export let id = '', language = 'en';
  let snapshot = null, storage = null, error = null, controller, tab = 'health';
  let managed = null, managedError = null, managedInspection = null, managedAutomation = null, inspectionError = null, automationError = null, managedController, managedGeneration = 0, allowedOverrides = [];
  async function loadManaged() {
    managedController?.abort(); managedController = new AbortController(); const token = ++managedGeneration; managedError = null; managed = null;
    managedInspection = null; managedAutomation = null; inspectionError = null; automationError = null; allowedOverrides = [];
    try {
      const listing = await api.workflows({signal:managedController.signal});
      const entry = (listing.workflows || []).find(row => row.managed === true);
      if (!entry) throw new Error('Managed Recipe metadata unavailable');
      const canonical = await api.workflow(entry.id,{signal:managedController.signal});
      if (!isManaged(canonical)) throw new Error('Managed Recipe metadata unavailable');
      if (token !== managedGeneration) return;
      allowedOverrides = entry.editable_paths || []; managed = canonical;
      const [inspected, automated] = await Promise.allSettled([api.recipeInspection(entry.id,{signal:managedController.signal}),api.automation(entry.id,{signal:managedController.signal})]);
      if (token !== managedGeneration) return;
      if (inspected.status === 'fulfilled') managedInspection = inspected.value.inspection; else if (inspected.reason.name !== 'AbortError') inspectionError = inspected.reason;
      if (automated.status === 'fulfilled') managedAutomation = automated.value.automation; else if (automated.reason.name !== 'AbortError') automationError = automated.reason;
    } catch(caught) {if(caught.name !== 'AbortError' && token === managedGeneration) managedError = caught;}
  }
  async function load() {controller?.abort(); controller = new AbortController(); error = null; try {const [a,b] = await Promise.all([api.diagnostics({signal:controller.signal}),api.storage({signal:controller.signal})]); snapshot = a; storage = b.storage;} catch(caught) {if(caught.name !== 'AbortError') error = caught;}}
  onMount(() => {load(); return () => {controller?.abort(); managedController?.abort();};});
  $: if (id === 'managed') {tab = 'managed'; loadManaged();}
</script>
<h1>{t('system',language)}</h1><div class="tabs" role="group" aria-label={t('system',language)}><button class:active={tab==='health'} onclick={() => tab='health'}>{t('health',language)}</button><button class:active={tab==='maintenance'} onclick={() => tab='maintenance'}>{t('maintenance',language)}</button><button class:active={tab==='developer'} onclick={() => tab='developer'}>{t('developer',language)}</button><button class:active={tab==='managed'} onclick={() => {tab='managed'; loadManaged();}}>{t('managedSelfBuild',language)}</button></div><ErrorNotice {error} retry={load} {language}/>
{#if tab === 'health'}<div class="grid">{#each snapshot?.checks || [] as check (check.id)}<section class="panel"><div class="section-head"><h2>{check.id}</h2><Status value={check.status} {language}/></div><p>{check.message}</p><dl>{#each Object.entries(check.details || {}) as [key,value]}<dt>{key}</dt><dd>{typeof value === 'boolean' ? (value ? '✓' : '—') : value}</dd>{/each}</dl></section>{/each}</div>
{:else if tab === 'maintenance'}<section class="panel"><h2>{t('maintenance',language)}</h2><p>{t('availableLater',language)}</p>{#if storage}<pre class="facts">{JSON.stringify(storage,null,2)}</pre>{/if}</section>
{:else if tab === 'managed'}<ErrorNotice error={managedError} retry={loadManaged} {language}/>{#if managed}<section class="panel"><h2>{t('managedSelfBuild',language)}</h2><dl><dt>{t('managedIdentity',language)}</dt><dd>{managed.management.builtin_id}</dd><dt>{t('definitionVersion',language)}</dt><dd>{managed.management.definition_version}</dd><dt>{t('allowedOverrides',language)}</dt><dd><StructuredValue value={allowedOverrides}/></dd><dt>{t('currentOverrides',language)}</dt><dd><StructuredValue value={managed.management.operator_overrides}/></dd></dl></section><RecipeDetail recipe={managed} inspection={managedInspection} automation={managedAutomation} {inspectionError} {automationError} {language} editablePaths={allowedOverrides}/>{:else if !managedError}<p>{t('loading',language)}</p>{/if}
{:else}<section class="panel"><h2>{t('developer',language)}</h2><p><a href="/api/openapi.json" target="_blank" rel="noopener noreferrer">{t('openapi',language)}</a></p><p>{t('availableLater',language)}: {t('inspectors',language)}, {t('supportBundle',language)}</p></section>{/if}
