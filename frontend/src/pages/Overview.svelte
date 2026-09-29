<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {admitDraftRun} from '../features/recipes/admission.js';
  import {loadRecipe} from '../features/recipes/persistence.js';
  import {navigate} from '../navigation/location.js';
  import {t,when,number,statusLabel} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import StatusChip from '../components/StatusChip.svelte';
  import {operatorStatusSemantics as overviewStatus} from '../components/statusSemantics.js';
  export let language = 'en';
  let dashboard = null, diagnostics = null, error = null, loading = true, controller;
  let actionDialog, dialogTitle, dialogTrigger, actionContext = null;
  let actionLoading = false, actionPending = false, actionError = null, actionResult = null, publishConfirmed = false, admittedRunId = '';
  let actionRevision = 0, actionController;
  const actionableStates = {
    validation: new Set(['validation_needed','ready_to_validate']),
    publication: new Set(['ready_to_publish','publication_available']),
  };
  const actionKey = kind => kind === 'validation' ? 'validate' : 'publish';
  const publicationIdentity = run => {
    const name = run.artifact?.inspection?.package || run.package || run.recipe_id;
    const version = run.artifact?.inspection?.version || run.version?.debian;
    return name && version ? {name,version,confirmation:`publish:${name}:${version}`} : null;
  };
  async function currentActionTarget(row, kind, signal) {
    const pkg = (await api.package(row.name,{signal})).package;
    const runId = pkg?.build?.latest_run_id;
    const state = pkg?.lifecycle_display_status || pkg?.lifecycle_state;
    if (kind === 'update') {
      if (state !== 'update_available' || pkg.allowed_actions?.build !== true || !pkg.recipe || (row.version?.source && row.version.source !== pkg.version?.source)) throw new Error(t('overviewUpdateUnavailable',language));
      const loaded = await loadRecipe(pkg.recipe,{signal});
      if (loaded.recipe?.active === false) throw new Error(t('overviewUpdateUnavailable',language));
      return {pkg,recipe:loaded.recipe,revision:loaded.revision};
    }
    if (!runId || runId !== row.build?.latest_run_id || !actionableStates[kind].has(state) || pkg.allowed_actions?.[actionKey(kind)] !== true) throw new Error(t('overviewActionUnavailable',language));
    const run = (await api.run(runId,{signal})).execution;
    if (run?.id !== runId || run.allowed_actions?.[actionKey(kind)] !== true) throw new Error(t('overviewActionUnavailable',language));
    const identity = kind === 'publication' ? publicationIdentity(run) : null;
    if (kind === 'publication' && !identity) throw new Error(t('overviewActionUnavailable',language));
    return {run,identity};
  }
  function packageAction(row) {
    const state = row.lifecycle_display_status || row.lifecycle_state || '';
    const runId = row.build?.latest_run_id;
    if (/fail|error/.test(state)) return {label:runId ? 'overviewFailureAction' : 'overviewViewPackage',page:runId ? 'runs' : 'packages',id:runId || row.name};
    if (state === 'validation_needed' || state === 'ready_to_validate') return {label:'overviewValidationAction',dialog:'validation'};
    if (state === 'ready_to_publish' || state === 'publication_available') return {label:'overviewPublicationAction',dialog:'publication'};
    if (state === 'update_available') return {label:'overviewUpdateAction',dialog:'update'};
    return {label:'overviewViewPackage',page:'packages',id:row.name};
  }
  async function openPackageAction(row, action, trigger) {
    if (!action.dialog) {navigate(action.page, action.id); return;}
    if (actionLoading) return;
    dialogTrigger = trigger;
    actionController?.abort(); actionController = new AbortController();
    const token = ++actionRevision;
    actionContext = {row,kind:action.dialog,target:null};
    actionLoading = true; actionPending = false; actionError = null; actionResult = null; publishConfirmed = false; admittedRunId = '';
    try {
      const target = await currentActionTarget(row,action.dialog,actionController.signal);
      if (token === actionRevision) actionContext = {...actionContext,target};
    } catch (caught) {
      if (caught.name !== 'AbortError' && token === actionRevision) actionError = caught;
    } finally {
      if (token === actionRevision) {
        actionLoading = false;
        await tick();
        if (token === actionRevision) {actionDialog.showModal(); dialogTitle.focus();}
      }
    }
  }
  function closeActionDialog() {if (!actionPending) actionDialog.close();}
  function closeOnBackdrop(event) {
    if (event.target !== actionDialog) return;
    const bounds = actionDialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeActionDialog();
  }
  function resetActionDialog() {
    ++actionRevision; actionController?.abort(); actionContext = null;
    dialogTrigger?.focus(); dialogTrigger = null;
  }
  async function submitAction() {
    const context = actionContext;
    if (!context?.target || actionPending || actionResult || actionError || (context.kind === 'publication' && !publishConfirmed)) return;
    actionPending = true;
    const token = actionRevision;
    try {
      const current = await currentActionTarget(context.row,context.kind,actionController.signal);
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
        load();
      }
    } catch (caught) {
      if (caught.name !== 'AbortError' && token === actionRevision) actionError = caught;
    } finally {if (token === actionRevision) actionPending = false;}
  }
  function visitActionDetail() {
    const context = actionContext;
    const row = context?.row;
    const runId = admittedRunId;
    const ambiguous = context?.kind === 'update' && ['network','timeout','ambiguous'].includes(actionError?.kind);
    closeActionDialog();
    if (ambiguous) navigate('runs');
    else if (runId) navigate('runs',runId);
    else if (context?.kind === 'update' && context?.target?.pkg?.recipe) navigate('recipes',context.target.pkg.recipe);
    else if (row?.build?.latest_run_id) navigate('runs',row.build.latest_run_id);
    else if (row) navigate('packages',row.name);
  }
  async function load() {
    controller?.abort(); const current = new AbortController(); controller = current; loading = true; error = null;
    const [summary,health] = await Promise.allSettled([api.dashboard({signal:current.signal}),api.diagnostics({signal:current.signal})]);
    if (current.signal.aborted) return;
    if (summary.status === 'fulfilled') dashboard = summary.value.dashboard;
    if (health.status === 'fulfilled') diagnostics = health.value;
    error = [summary,health].find(result => result.status === 'rejected' && result.reason?.name !== 'AbortError')?.reason || null;
    loading = false;
  }
  onMount(() => {load(); return () => {++actionRevision; controller?.abort(); actionController?.abort();};});
  $: attention = (dashboard?.package_rows || []).filter(row => /fail|error|update_available|ready_to_publish|publication_available|validation_needed|ready_to_validate/.test(row.lifecycle_display_status || row.lifecycle_state || ''));
  $: active = (dashboard?.latest_operations || []).filter(row => row.lifecycle_active);
  $: readyToValidate = (dashboard?.package_rows || []).filter(row => ['validation_needed','ready_to_validate'].includes(row.lifecycle_display_status || row.lifecycle_state)).length;
  $: repository = diagnostics?.checks?.find(row => row.id === 'repository.publication');
  $: hasFailure = diagnostics?.checks?.some(row => row.status === 'error' || row.status === 'failed') || attention.some(row => overviewStatus(row.lifecycle_display_status || row.lifecycle_state).tone === 'danger');
  $: hasAction = diagnostics?.checks?.some(row => row.status === 'warning' || row.status === 'unknown') || attention.some(row => overviewStatus(row.lifecycle_display_status || row.lifecycle_state).tone === 'warning');
  $: hasUpdate = attention.some(row => overviewStatus(row.lifecycle_display_status || row.lifecycle_state).tone === 'update');
  $: healthy = diagnostics?.checks?.every(row => row.status === 'ok' || row.status === 'healthy') && !attention.length;
</script>
{#if loading}<p>{t('loading',language)}</p>{/if}<ErrorNotice {error} retry={load} {language}/>
{#if dashboard}
{#if diagnostics?.checks?.some(check => check.status === 'error' || check.status === 'failed')}<div class="attention-notice critical" role="status"><span class="notice-mark" aria-hidden="true">!</span><div><strong>{t('needsAttention',language)}</strong><p>{diagnostics.checks.find(check => check.status === 'error' || check.status === 'failed')?.message}</p></div><button class="button secondary" onclick={() => navigate('system')}>{t('overviewViewSystem',language)}</button></div>{/if}
<div class="overview-headline"><StatusChip label={healthy ? t('health',language) : t('needsAttention',language)} tone={healthy ? 'success' : hasFailure ? 'danger' : hasAction ? 'warning' : hasUpdate ? 'update' : 'warning'} icon={healthy ? '●' : '!'} /><div class="compact-stats"><span>{t('packages',language)} <strong>{number(dashboard.packages,language)}</strong></span><span>{t('activeWork',language)} <strong>{number(active.length,language)}</strong></span><span>{t('readyToValidate',language)} <strong>{number(readyToValidate,language)}</strong></span><span>{statusLabel('ready_to_publish',language)} <strong>{number(dashboard.ready_to_publish,language)}</strong></span></div></div>
<div class="overview-grid"><div class="overview-main"><section class="panel"><div class="section-head"><h2>{t('needsAttention',language)}</h2>{#if attention.length>5}<button class="text-button" onclick={() => navigate('packages')}>{t('all',language)}</button>{/if}</div>{#if attention.length===0}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{:else}<div class="click-list">{#each attention.slice(0,5) as row (row.name)}{@const state = overviewStatus(row.lifecycle_display_status || row.lifecycle_state)}{@const action = packageAction(row)}<button class="click-row" disabled={actionLoading} aria-busy={actionLoading && actionContext?.row.name === row.name} onclick={event => openPackageAction(row,action,event.currentTarget)}><span class="row-status {state.tone}" aria-hidden="true">{state.icon}</span><span class="click-main"><strong>{row.name}</strong><small>{statusLabel(row.lifecycle_display_status || row.lifecycle_state,language)}</small></span><span class="row-action">{actionLoading && actionContext?.row.name === row.name ? t('overviewCheckingAction',language) : t(action.label,language)}</span></button>{/each}</div>{/if}</section>
<section class="panel"><div class="section-head"><h2>{t('recentRuns',language)}</h2><button class="text-button" onclick={() => navigate('runs')}>{t('all',language)}</button></div>{#if !(dashboard.latest_operations || []).length}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{:else}<div class="click-list">{#each (dashboard.latest_operations || []).slice(0,5) as row (row.id)}{@const state = overviewStatus(row.lifecycle_status || row.status)}<button class="click-row" onclick={() => navigate('runs',row.id)}><span class="row-status {state.tone}" aria-hidden="true">{state.icon}</span><span class="click-main"><strong>{row.package || row.id} · {row.action || row.mode || t('runSingular',language)}</strong><small>{when(row.updated,language)} · {row.id}</small></span><span class="row-action">{statusLabel(row.lifecycle_status || row.status,language)}</span></button>{/each}</div>{/if}</section></div><aside class="overview-side"><section class="panel compact-panel"><div class="section-head"><h2>{t('activeWork',language)}</h2>{#if active.length}<StatusChip label={number(active.length,language)} tone="info" icon="◌" />{/if}</div>{#if active.length}{#each active.slice(0,3) as row (row.id)}<button class="plain-link" onclick={() => navigate('runs',row.id)}><strong>{row.package || row.id}</strong><span>{statusLabel(row.lifecycle_status || row.status,language)}</span><b>{t('overviewFollowRun',language)}</b></button>{/each}{:else}<p class="muted compact-copy">{t('noItems',language)}</p>{/if}</section><section class="panel compact-panel"><div class="section-head"><h2>{t('repository',language)}</h2>{#if repository}<Status value={repository.status} {language}/>{/if}</div>{#if repository}<p class="repo-line">{repository.message}</p><p class="muted compact-copy">{t('signedMetadata',language)}: {repository.details?.signed_release_present ? '✓' : '—'}</p>{/if}<button class="text-button" onclick={() => navigate('packages')}>{t('overviewViewPackages',language)}</button></section></aside></div>
<dialog class="overview-action-dialog" bind:this={actionDialog} aria-labelledby="overview-action-title" onclick={closeOnBackdrop} oncancel={event => {if (actionPending) event.preventDefault();}} onclose={resetActionDialog}>
  {#if actionContext}
    <div class="modal-head"><h2 id="overview-action-title" tabindex="-1" bind:this={dialogTitle}>{statusLabel(actionContext.row.lifecycle_display_status || actionContext.row.lifecycle_state,language)}</h2><button type="button" class="icon-button" aria-label={t('close',language)} onclick={closeActionDialog}>×</button></div>
    <p class="modal-copy"><strong>{actionContext.row.name}</strong> — {t(actionContext.kind === 'update' ? 'overviewUpdateInfo' : actionContext.kind === 'validation' ? 'overviewValidationInfo' : 'overviewPublicationInfo',language)}</p>
    <ErrorNotice error={actionError} {language}/>
    {#if actionContext.kind === 'update' && ['network','timeout','ambiguous'].includes(actionError?.kind)}<p class="muted">{t('admissionUnknown',language)}</p>{/if}
    {#if actionContext.target && !actionError && !actionResult && actionContext.kind === 'publication'}
      <p class="overview-publication-identity"><strong>{actionContext.target.identity.name}</strong> · {actionContext.target.identity.version}</p>
      <label class="overview-publication-confirm"><input type="checkbox" bind:checked={publishConfirmed} disabled={actionPending}> {t('overviewConfirmPublication',language)}</label>
    {/if}
    {#if actionResult}<p class="overview-action-result" role="status">{t(actionResult === 'update' ? 'overviewBuildQueued' : actionResult === 'validation' ? 'overviewValidationStarted' : 'overviewPublicationDone',language)}</p>{/if}
    <div class="modal-actions"><button type="button" class="button secondary" disabled={actionPending} onclick={closeActionDialog}>{t('close',language)}</button><button type="button" class="button secondary" disabled={actionPending} onclick={visitActionDetail}>{t(actionContext.kind === 'update' ? ['network','timeout','ambiguous'].includes(actionError?.kind) ? 'viewRuns' : admittedRunId ? 'viewRun' : 'reviewRecipe' : actionContext.row.build?.latest_run_id ? 'viewRun' : 'overviewViewPackage',language)}</button>{#if actionContext.target && !actionError && !actionResult}<button type="button" class="button primary" disabled={actionPending || (actionContext.kind === 'publication' && !publishConfirmed)} onclick={submitAction}>{actionPending ? t('overviewActionWorking',language) : t(actionContext.kind === 'update' ? 'overviewStartBuild' : actionContext.kind === 'validation' ? 'overviewStartValidation' : 'overviewPublishToApt',language)}</button>{/if}</div>
  {/if}
</dialog>
{/if}
