<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {loadRecipe} from '../features/recipes/persistence.js';
  import {t} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import RecipeDetail from '../features/recipes/RecipeDetail.svelte';
  import {isManaged} from '../features/recipes/model.js';
  const diagnosticNames = {
    en: {'application.runtime':'Application runtime','settings.documents':'Settings','repository.publication':'Repository publication','validation.oci':'Validation capability','execution.admission':'Build and Test admission','execution.containment':'Command containment','application.mutations':'Application changes','automation.scheduler':'Automation schedule','automation.orchestrator':'Automation service'},
    fr: {'application.runtime':'Exécution de l’application','settings.documents':'Paramètres','repository.publication':'Publication du dépôt','validation.oci':'Capacité de validation','execution.admission':'Admission construction et test','execution.containment':'Confinement des commandes','application.mutations':'Modifications de l’application','automation.scheduler':'Calendrier d’automatisation','automation.orchestrator':'Service d’automatisation'},
    de: {'application.runtime':'Anwendungslaufzeit','settings.documents':'Einstellungen','repository.publication':'Repository-Veröffentlichung','validation.oci':'Validierungsfähigkeit','execution.admission':'Build- und Testzulassung','execution.containment':'Befehlsisolation','application.mutations':'Anwendungsänderungen','automation.scheduler':'Automatisierungszeitplan','automation.orchestrator':'Automatisierungsdienst'},
    es: {'application.runtime':'Ejecución de la aplicación','settings.documents':'Ajustes','repository.publication':'Publicación del repositorio','validation.oci':'Capacidad de validación','execution.admission':'Admisión de compilación y prueba','execution.containment':'Contención de comandos','application.mutations':'Cambios de la aplicación','automation.scheduler':'Programación de automatización','automation.orchestrator':'Servicio de automatización'},
  };
  const checkName = check => diagnosticNames[language]?.[check.id] || diagnosticNames.en[check.id] || check.id;
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
<div class="segmented-tabs system-tabs" role="group" aria-label={t('system',language)}>{#each ['health','maintenance','developer','managed'] as key}<button class:active={tab===key} aria-current={tab===key?'page':undefined} onclick={() => selectTab(key)}>{key==='managed'?t('managedSelfBuild',language):t(key,language)}</button>{/each}</div>
<ErrorNotice {error} retry={load} {language}/>
{#if tab==='health'}
  <div class="system-grid"><section class="panel"><div class="section-head"><h2>{t('diagnosis',language)}</h2><Status value={snapshot?.status || 'unknown'} {language}/></div>
    <div class="diagnostic-list curated-diagnostics">{#each (snapshot?.checks || []).filter(check => check.id!=='repository.publication').slice(0,4) as check (check.id)}<div><span>{checkName(check)}</span><strong>{check.message}</strong><Status value={check.status} {language}/></div>{:else}<p>{t('loading',language)}</p>{/each}</div>
    {#if (snapshot?.checks || []).filter(check => check.id!=='repository.publication').length>4}<details class="system-extra"><summary>{t('moreChecks',language)} · {(snapshot?.checks || []).filter(check => check.id!=='repository.publication').length-4}</summary><div class="diagnostic-list curated-diagnostics">{#each (snapshot?.checks || []).filter(check => check.id!=='repository.publication').slice(4) as check (check.id)}<div><span>{checkName(check)}</span><strong>{check.message}</strong><Status value={check.status} {language}/></div>{/each}</div></details>{/if}
  </section><aside class="panel compact-panel"><h2>{t('managedSelfBuild',language)}</h2><p class="muted">{t('allowedOverrides',language)}</p><div class="repo-line">DebBuilder</div><button class="button secondary" onclick={() => selectTab('managed')}>{t('managedSelfBuild',language)} →</button></aside></div>
  <section class="panel health-repository"><div class="section-head"><h2>{t('repository',language)}</h2>{#each (snapshot?.checks || []).filter(check => check.id==='repository.publication') as check}<Status value={check.status} {language}/>{/each}</div>{#each (snapshot?.checks || []).filter(check => check.id==='repository.publication') as check}<p class="repo-line">{check.message}</p><div class="detail-facts">{#if check.details?.signed_release_present === true}<div><span>{t('signedMetadata',language)}</span><strong>{t('available',language)}</strong></div>{/if}{#if check.details?.last_publication_at}<div><span>{t('published',language)}</span><strong>{check.details.last_publication_at}</strong></div>{/if}</div>{/each}</section>
{:else if tab==='maintenance'}
  <div class="maintenance-layout"><section class="panel maintenance-card"><h2>{t('details',language)}</h2>{#if storage}<div class="detail-facts"><div><span>{t('status',language)}</span><Status value={storage.state} {language}/></div>{#if storage.bytes?.managed_total != null}<div><span>{t('managedStorage',language)}</span><strong>{new Intl.NumberFormat(language).format(storage.bytes.managed_total)} B</strong></div>{/if}{#if storage.runs?.count != null}<div><span>{t('runs',language)}</span><strong>{storage.runs.count}</strong></div>{/if}</div>{:else}<p>{t('loading',language)}</p>{/if}</section><section class="panel maintenance-card"><h2>{t('maintenance',language)}</h2><p class="muted">{t('readOnly',language)}</p></section><section class="panel maintenance-card"><h2>{t('recovery',language)}</h2><p class="muted">{t('availableLater',language)}</p></section></div>
{:else if tab==='developer'}
  <div class="system-grid"><section class="panel"><h2>{t('developer',language)}</h2><div class="developer-list"><a href="/api/openapi.json" target="_blank" rel="noopener noreferrer">{t('openapi',language)} <span>→</span></a></div><details class="developer-payload"><summary>{t('inspectDiagnosticPayload',language)}</summary><pre class="facts">{JSON.stringify(snapshot?.checks || [],null,2)}</pre></details></section><aside class="panel compact-panel"><h2>{t('details',language)}</h2><p class="muted">{t('availableLater',language)}: {t('inspectors',language)}, {t('supportBundle',language)}</p></aside></div>
{:else}
  <ErrorNotice error={managedError} retry={loadManaged} {language}/>
  {#if managed}<div class="managed-self-build"><section class="panel"><div class="section-head"><div><h2>{t('managedSelfBuild',language)}</h2><p class="muted">{t('allowedOverrides',language)}</p></div><Status value="managed" {language}/></div><div class="plan-rows"><div><span>{t('managedIdentity',language)}</span><strong>{managed.management.builtin_id}</strong></div><div><span>{t('definitionVersion',language)}</span><strong>{managed.management.definition_version}</strong></div><div><span>{t('allowedOverrides',language)}</span><strong>{allowedOverrides.join(', ') || '—'}</strong></div></div>{#if Object.keys(managed.management.operator_overrides || {}).length}<div class="detail-facts"><div><span>{t('currentOverrides',language)}</span><strong>{Object.keys(managed.management.operator_overrides).join(', ')}</strong></div></div>{/if}</section><div><RecipeDetail recipe={managed} revision={managedRevision} inspection={managedInspection} automation={managedAutomation} {inspectionError} {automationError} {language} editablePaths={allowedOverrides} saved={managedSaved}/></div></div>{:else if !managedError}<p>{t('loading',language)}</p>{/if}
{/if}
