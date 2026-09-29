<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {admitDraftRun} from '../features/recipes/admission.js';
  import {createRecipe, loadRecipe, newRecipeDraft, validateRecipe} from '../features/recipes/persistence.js';
  import {navigate} from '../navigation/location.js';
  import {openRecipe} from '../navigation/recipe.js';
  import {t,number} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import {operatorStatusSemantics} from '../components/statusSemantics.js';
  export let id = '', language = 'en';
  let rows = [], selected = null, error = null, detailError = null, query = '', filter = 'all', controller, detailController, revision = 0;
  let inventoryOpen = false, inventory = null, inventoryError = null, inventoryLoading = false, inventoryQuery = '', inventoryController, inventoryRevision = 0, navigationError = null;
  let actionDialog, actionCloseButton, actionTrigger, actionContext = null, actionError = null, actionResult = null;
  let actionLoading = false, actionPending = false, publishConfirmed = false, admittedRunId = '', actionController, actionRevision = 0;
  let createDialog, createNameInput, createTrigger, createName = '', createRepository = '', createError = null, createPending = false;
  const validationStates = new Set(['validation_needed','ready_to_validate']);
  const publicationStates = new Set(['ready_to_publish','publication_available']);
  const activeStates = new Set(['pending','queued','running','building','validating','publishing','cancelling']);
  const failureStates = new Set(['failed','error','build_failed','validation_failed','publication_failed']);
  const buildStates = new Set(['update_available','build_available','build_required','not_published']);
  const packageState = pkg => pkg?.lifecycle_display_status || pkg?.lifecycle_state || pkg?.status || '';
  function nextStep(pkg) {
    const state = packageState(pkg);
    const hasRun = Boolean(pkg.build?.latest_run_id);
    if (failureStates.has(state)) return {label:hasRun?'packageViewLogs':'reviewRecipe',kind:hasRun?'run':pkg.recipe?'recipe':null};
    if (activeStates.has(state)) return {label:hasRun?'overviewFollowRun':'reviewRecipe',kind:hasRun?'run':pkg.recipe?'recipe':null};
    if (validationStates.has(state)) return pkg.allowed_actions?.validate === true && hasRun
      ? {label:'overviewStartValidation',kind:'validation'}
      : {label:hasRun?'viewRun':'reviewRecipe',kind:hasRun?'run':pkg.recipe?'recipe':null};
    if (publicationStates.has(state)) return pkg.allowed_actions?.publish === true && hasRun
      ? {label:'overviewPublishToApt',kind:'publication'}
      : {label:hasRun?'viewRun':'reviewRecipe',kind:hasRun?'run':pkg.recipe?'recipe':null};
    if (state === 'update_available' && pkg.allowed_actions?.build === true && pkg.recipe) return {label:'overviewUpdateAction',kind:'update'};
    if (buildStates.has(state) && pkg.allowed_actions?.build === true && pkg.recipe) return {label:'packageOpenRecipeToBuild',kind:'recipe'};
    if (state === 'recipe_missing' || !pkg.recipe && !hasRun) return {label:null,kind:null};
    if (state === 'published' || state === 'up_to_date') return {label:pkg.recipe?'reviewRecipe':'viewRun',kind:pkg.recipe?'recipe':hasRun?'run':null};
    if (hasRun) return {label:'viewRun',kind:'run'};
    return {label:pkg.recipe?'reviewRecipe':null,kind:pkg.recipe?'recipe':null};
  }
  const publicationIdentity = run => {
    const name = run.artifact?.inspection?.package || run.package || run.recipe_id;
    const version = run.artifact?.inspection?.version || run.version?.debian;
    return name && version ? {name,version,confirmation:`publish:${name}:${version}`} : null;
  };
  async function currentActionTarget(pkg, kind, signal) {
    const current = (await api.package(pkg.name,{signal})).package;
    const runId = current?.build?.latest_run_id;
    if (kind === 'update') {
      if (packageState(current) !== 'update_available' || current.allowed_actions?.build !== true || !current.recipe || (pkg.version?.source && pkg.version.source !== current.version?.source)) throw new Error(t('overviewUpdateUnavailable',language));
      const loaded = await loadRecipe(current.recipe,{signal});
      if (loaded.recipe?.active === false) throw new Error(t('overviewUpdateUnavailable',language));
      return {pkg:current,recipe:loaded.recipe,revision:loaded.revision};
    }
    const states = kind === 'validation' ? validationStates : publicationStates;
    const permission = kind === 'validation' ? 'validate' : 'publish';
    if (!runId || runId !== pkg.build?.latest_run_id || !states.has(packageState(current)) || current.allowed_actions?.[permission] !== true) throw new Error(t('overviewActionUnavailable',language));
    const run = (await api.run(runId,{signal})).execution;
    if (run?.id !== runId || run.allowed_actions?.[permission] !== true) throw new Error(t('overviewActionUnavailable',language));
    const identity = kind === 'publication' ? publicationIdentity(run) : null;
    if (kind === 'publication' && (!identity || identity.name !== pkg.name)) throw new Error(t('overviewActionUnavailable',language));
    return {run,identity};
  }
  async function openAction(kind, trigger) {
    actionTrigger = trigger;
    actionController?.abort(); actionController = new AbortController();
    const token = ++actionRevision;
    actionContext = {pkg:selected,kind,target:null};
    actionError = null; actionResult = null; actionLoading = true; actionPending = false; publishConfirmed = false; admittedRunId = '';
    await tick();
    actionDialog.showModal(); actionCloseButton.focus();
    try {
      const target = await currentActionTarget(actionContext.pkg,kind,actionController.signal);
      if (token === actionRevision) actionContext = {...actionContext,target};
    } catch (caught) {
      if (caught.name !== 'AbortError' && token === actionRevision) actionError = caught;
    } finally {if (token === actionRevision) actionLoading = false;}
  }
  function closeAction() {if (!actionPending) actionDialog.close();}
  function resetAction() {
    ++actionRevision; actionController?.abort(); actionContext = null;
    const trigger = actionTrigger; actionTrigger = null;
    if (trigger?.isConnected) trigger.focus();
    else document.querySelector('.package-detail .package-detail-action')?.focus();
  }
  function visitBuildRun() {
    const runId = admittedRunId;
    const ambiguous = ['network','timeout','ambiguous'].includes(actionError?.kind);
    closeAction();
    if (ambiguous) navigate('runs');
    else if (runId) navigate('runs',runId);
  }
  async function submitAction() {
    const context = actionContext;
    if (!context?.target || actionPending || actionError || actionResult || (context.kind === 'publication' && !publishConfirmed)) return;
    actionPending = true;
    const token = actionRevision;
    try {
      const current = await currentActionTarget(context.pkg,context.kind,actionController.signal);
      if (context.kind === 'update') {
        if (current.pkg.recipe !== context.target.pkg.recipe || current.revision !== context.target.revision || current.pkg.version?.source !== context.target.pkg.version?.source) throw new Error(t('overviewUpdateUnavailable',language));
        admittedRunId = await admitDraftRun({draft:current.recipe},false);
      } else {
        if (current.run.id !== context.target.run.id || current.identity?.confirmation !== context.target.identity?.confirmation) throw new Error(t('overviewActionUnavailable',language));
        if (context.kind === 'validation') await api.startValidation(current.run.id);
        else await api.publishArtifact(current.run.id,current.identity.confirmation);
      }
      if (token === actionRevision) {
        actionResult = context.kind;
        await load();
        if (token === actionRevision) await detail(context.pkg.name);
      }
    } catch (caught) {
      if (caught.name !== 'AbortError' && token === actionRevision) actionError = caught;
    } finally {if (token === actionRevision) actionPending = false;}
  }
  function followNextStep(step, trigger) {
    if (step.kind === 'validation' || step.kind === 'publication' || step.kind === 'update') {openAction(step.kind,trigger); return;}
    if (step.kind === 'run' && selected?.build?.latest_run_id) navigate('runs',selected.build.latest_run_id);
    if (step.kind === 'recipe' && selected?.recipe) review(selected.recipe);
  }
  async function openCreate(trigger) {
    createTrigger = trigger; createName = ''; createRepository = ''; createError = null;
    await tick(); createDialog.showModal(); createNameInput.focus();
  }
  function closeCreate() {if (!createPending) createDialog.close();}
  function resetCreate() {if (createTrigger?.isConnected) createTrigger.focus(); createTrigger = null;}
  async function submitCreate(event) {
    event.preventDefault();
    if (createPending) return;
    createPending = true; createError = null;
    const name = createName.trim(), repository = createRepository.trim();
    try {
      const existing = (await api.packages()).packages || [];
      if (existing.some(row => row.name?.toLowerCase() === name.toLowerCase())) throw new Error(t('packageAlreadyExists',language));
      const draft = await newRecipeDraft(name,repository);
      const checked = await validateRecipe(draft);
      if (checked.collision?.exists) throw new Error(t('packageAlreadyExists',language));
      const created = await createRecipe(name,checked.recipe || draft);
      createDialog.close();
      await load();
      navigate('packages',created.recipe.package.name);
    } catch (caught) {createError = caught;}
    finally {createPending = false;}
  }
  async function loadInventory() {inventoryController?.abort(); inventoryController = new AbortController(); const token = ++inventoryRevision; inventoryLoading = true; inventoryError = null; inventory = null; try {const result = await api.inventory({signal:inventoryController.signal}); if(token === inventoryRevision) inventory = result;} catch(caught) {if(caught.name !== 'AbortError' && token === inventoryRevision) inventoryError = caught;} finally {if(token === inventoryRevision) inventoryLoading = false;}}
  function toggleInventory() {inventoryOpen = !inventoryOpen; if(inventoryOpen && !inventory) loadInventory();}
  async function review(recipeId) {navigationError = null; try {await openRecipe(recipeId);} catch(caught) {navigationError = caught;}}
  async function load() {controller?.abort(); controller = new AbortController(); error = null; try {rows = (await api.packages({signal:controller.signal})).packages || [];} catch (caught) {if(caught.name !== 'AbortError') error = caught;}}
  async function detail(name) {detailController?.abort(); detailController = new AbortController(); const token = ++revision; detailError = null; selected = null; if (!name) return; try {const value = (await api.package(name,{signal:detailController.signal})).package; if(token === revision) selected = value;} catch(caught) {if(caught.name !== 'AbortError' && token === revision) detailError = caught;}}
  onMount(() => {load(); return () => {++actionRevision; controller?.abort(); detailController?.abort(); inventoryController?.abort(); actionController?.abort();};});
  $: detail(id || rows[0]?.name);
  $: filtered = rows.filter(row => row.name?.toLowerCase().includes(query.toLowerCase()) && (filter === 'all' || (row.lifecycle_display_status || row.lifecycle_state || row.status) === filter));
  $: statuses = [...new Set(rows.map(row => row.lifecycle_display_status || row.lifecycle_state || row.status).filter(Boolean))];
  $: inventoryRows = (inventory?.packages || []).filter(row => `${row.name} ${row.version} ${row.architecture} ${row.component}`.toLowerCase().includes(inventoryQuery.toLowerCase()));
</script>
<ErrorNotice {error} retry={load} {language}/>
{#if inventoryOpen}
<div class="inventory-view"><button class="back-button" onclick={toggleInventory}>{t('back',language)}</button><section class="panel"><p class="detail-breadcrumb">{t('packages',language)} / <strong>{t('repositoryInventory',language)}</strong></p><div class="section-head"><h2>{t('repositoryInventory',language)}</h2>{#if inventory?.repository}<Status value="ok" {language}/>{/if}</div><p class="muted repository-note">{t('inventoryDistinction',language)}</p><ErrorNotice error={inventoryError} retry={loadInventory} {language}/>{#if inventoryLoading}<p>{t('loading',language)}</p>{:else if inventory}<div class="repo-facts"><div><span>{t('suite',language)}</span><strong>{inventory.repository?.suite || '—'}</strong></div><div><span>{t('components',language)}</span><strong>{inventory.repository?.components?.join(', ') || '—'}</strong></div><div><span>{t('architectures',language)}</span><strong>{inventory.repository?.architectures?.join(', ') || '—'}</strong></div><div><span>{t('publishedEntries',language)}</span><strong>{number(inventory.packages?.length,language)}</strong></div></div>{/if}</section>{#if inventory}<section class="panel"><div class="section-head"><h2>{t('publishedEntries',language)}</h2><span class="count-label">{number(inventoryRows.length,language)} / {number(inventory.packages?.length,language)}</span></div><label class="search-label"><span>{t('search',language)}</span><input type="search" bind:value={inventoryQuery}/></label>{#if inventoryRows.length}<div class="repo-packages inventory-rows">{#each inventoryRows as item}<div><strong>{item.name}</strong><code>{item.version}</code><code>{item.architecture}</code><code>{item.component}</code></div>{/each}</div>{:else}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{/if}</section>{/if}</div>
{:else}
<div class="package-layout" class:mobile-detail={Boolean(id)}><section class="panel package-list"><div class="section-head"><h2>{t('packages',language)}</h2><div class="package-create-controls"><span class="count-label">{number(filtered.length,language)} / {number(rows.length,language)}</span><button type="button" class="button primary" onclick={event => openCreate(event.currentTarget)}>{t('addPackage',language)}</button></div></div><div class="filters"><label><span>{t('search',language)}</span><input type="search" bind:value={query}/></label><label><span>{t('status',language)}</span><select bind:value={filter}><option value="all">{t('all',language)}</option>{#each statuses as state}<option value={state}>{state}</option>{/each}</select></label></div>{#if rows.length===0}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{:else if filtered.length===0}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{:else}<div class="list-head package-columns"><span>{t('packages',language)}</span><span>{t('status',language)}</span><span>{t('built',language)}</span><span>{t('published',language)}</span></div><div class="package-rows">{#each filtered as row (row.name)}<button class="package-row package-columns" class:selected={(id || rows[0]?.name)===row.name} aria-current={(id || rows[0]?.name)===row.name?'true':undefined} onclick={() => navigate('packages',row.name)}><span class="row-identity"><strong>{row.name}</strong><small>{row.source?.repository || row.source?.type || ''}</small></span><span><Status value={row.lifecycle_display_status || row.lifecycle_state || row.status} {language} semantic={operatorStatusSemantics}/></span><span class="version-cell" data-label={t('built',language)}>{row.version?.candidate || '—'}</span><span class="version-cell" data-label={t('published',language)}>{row.version?.published || row.apt_version || '—'}</span></button>{/each}</div>{/if}</section>
<div class="package-detail-wrap">
  {#if id}<button class="back-button mobile-only package-detail-back" onclick={() => navigate('packages')}>{t('back',language)}</button>{/if}
  <ErrorNotice error={detailError} retry={() => detail(id || rows[0]?.name)} {language}/>
  {#if selected}
    {@const step = nextStep(selected)}
    <aside class="panel package-detail">
      <p class="detail-breadcrumb">{t('packages',language)} / <strong>{selected.name}</strong></p>
      <h2>{selected.name}</h2>
      <p class="muted">{selected.source?.type || selected.source?.repository || ''}</p>
      <div class="detail-facts">
        <div><span>{t('source',language)}</span><strong>{selected.source?.repository || selected.source?.type || '—'}</strong></div>
        <div><span>{t('built',language)}</span><strong>{selected.version?.candidate || '—'}</strong></div>
        <div><span>{t('published',language)}</span><strong>{selected.version?.published || selected.apt_version || '—'}</strong></div>
        <div><span>{t('status',language)}</span><Status value={packageState(selected)} {language} semantic={operatorStatusSemantics}/></div>
      </div>
      {#if step.kind}<button class="button primary package-detail-action" onclick={event => followNextStep(step,event.currentTarget)}>{t(step.label,language)}</button>{/if}
      <ErrorNotice error={navigationError} {language}/>
    </aside>
  {:else if (id || rows.length) && !detailError}<p>{t('loading',language)}</p>{/if}
</div></div>
<dialog class="package-action-dialog" bind:this={actionDialog} aria-labelledby="package-action-title" oncancel={event => {if (actionPending) event.preventDefault();}} onclose={resetAction}>
  {#if actionContext}
    <div class="modal-head"><h2 id="package-action-title">{t(actionContext.kind === 'update' ? 'overviewStartBuild' : actionContext.kind === 'validation' ? 'overviewStartValidation' : 'overviewPublishToApt',language)}</h2><button type="button" class="icon-button" bind:this={actionCloseButton} aria-label={t('close',language)} onclick={closeAction}>×</button></div>
    <p class="modal-copy"><strong>{actionContext.pkg.name}</strong> — {t(actionContext.kind === 'update' ? 'overviewUpdateInfo' : actionContext.kind === 'validation' ? 'overviewValidationInfo' : 'overviewPublicationInfo',language)}</p>
    {#if actionLoading}<p class="muted">{t('loading',language)}</p>{/if}
    <ErrorNotice error={actionError} {language}/>
    {#if actionContext.kind === 'update' && ['network','timeout','ambiguous'].includes(actionError?.kind)}<p class="muted">{t('admissionUnknown',language)}</p>{/if}
    {#if actionContext.kind === 'update' && actionContext.target && !actionError && !actionResult}<p class="overview-publication-identity"><strong>{t('recipeFact',language)}:</strong> {actionContext.target.pkg.recipe}<br><strong>{t('version',language)}:</strong> {actionContext.target.pkg.version?.source || '—'}</p>{/if}
    {#if actionContext.target && !actionError && !actionResult && actionContext.kind === 'publication'}
      <p class="overview-publication-identity"><strong>{actionContext.target.identity.name}</strong> · {actionContext.target.identity.version}</p>
      <label class="overview-publication-confirm"><input type="checkbox" bind:checked={publishConfirmed} disabled={actionPending}> {t('overviewConfirmPublication',language)}</label>
    {/if}
    {#if actionResult}<p class="overview-action-result" role="status">{t(actionResult === 'update' ? 'overviewBuildQueued' : actionResult === 'validation' ? 'packageValidationStarted' : 'packagePublicationDone',language)}</p>{/if}
    <div class="modal-actions"><button type="button" class="button secondary" disabled={actionPending} onclick={closeAction}>{t('close',language)}</button>{#if actionContext.kind === 'update' && (admittedRunId || ['network','timeout','ambiguous'].includes(actionError?.kind))}<button type="button" class="button secondary" disabled={actionPending} onclick={visitBuildRun}>{t(admittedRunId ? 'viewRun' : 'viewRuns',language)}</button>{/if}{#if actionContext.target && !actionError && !actionResult}<button type="button" class="button primary" disabled={actionPending || (actionContext.kind === 'publication' && !publishConfirmed)} onclick={submitAction}>{actionPending ? t('overviewActionWorking',language) : t(actionContext.kind === 'update' ? 'overviewStartBuild' : actionContext.kind === 'validation' ? 'overviewStartValidation' : 'overviewPublishToApt',language)}</button>{/if}</div>
  {/if}
</dialog>
<dialog class="package-create-dialog" bind:this={createDialog} aria-labelledby="package-create-title" oncancel={event => {if (createPending) event.preventDefault();}} onclose={resetCreate}>
  <form onsubmit={submitCreate} aria-busy={createPending}>
    <div class="modal-head"><h2 id="package-create-title">{t('addPackage',language)}</h2><button type="button" class="icon-button" aria-label={t('close',language)} disabled={createPending} onclick={closeCreate}>×</button></div>
    <p class="modal-copy">{t('createPackageHelp',language)}</p>
    <div class="package-create-fields">
      <label><span>{t('packageName',language)}</span><input bind:this={createNameInput} bind:value={createName} required pattern="[A-Za-z0-9][A-Za-z0-9_.+-]*" disabled={createPending}/></label>
      <label><span>{t('githubRepository',language)}</span><input bind:value={createRepository} required placeholder="owner/name" pattern="[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+" disabled={createPending}/></label>
    </div>
    <ErrorNotice error={createError} {language}/>
    <div class="modal-actions"><button type="button" class="button secondary" disabled={createPending} onclick={closeCreate}>{t('cancel',language)}</button><button type="submit" class="button primary" disabled={createPending}>{t(createPending ? 'packageCreating' : 'packageCreateAction',language)}</button></div>
  </form>
</dialog>
<section class="panel repository-summary"><div><div class="section-head"><h2>{t('repository',language)}</h2></div><p class="muted">{t('inventoryDistinction',language)}</p></div><button class="button secondary" onclick={toggleInventory}>{t('viewInventory',language)}</button></section>
{/if}
