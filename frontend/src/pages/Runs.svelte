<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {admitDraftRun} from '../features/recipes/admission.js';
  import {loadRecipe} from '../features/recipes/persistence.js';
  import {createPoller} from '../features/polling.js';
  import {navigate} from '../navigation/location.js';
  import {openRecipe} from '../navigation/recipe.js';
  import {t,when,statusLabel} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import {operatorStatusSemantics} from '../components/statusSemantics.js';
  export let id = '', language = 'en';
  let rows = [], run = null, logs = '', offset = 0, verbosity = 'normal', following = true;
  let query = '', filter = 'all', error = null, detailError = null, logError = null;
  let navigationError = null;
  let actionDialog, actionCloseButton, actionTrigger, actionContext = null, actionError = null, actionResult = null;
  let actionLoading = false, actionPending = false, publishConfirmed = false, admittedRunId = '', actionController, actionRevision = 0;
  const stageNames = {
    en: {source:'Fetch source',detection:'Detect project',dependencies:'Check dependencies',source_changes:'Apply source changes',build:'Build',staging:'Stage package',debian_metadata:'Debian metadata',systemd:'Service',package:'Create package',artifact:'Artifact',validation:'Validation',publication:'APT publication'},
    fr: {source:'Obtenir la source',detection:'Détecter le projet',dependencies:'Vérifier les dépendances',source_changes:'Appliquer les changements',build:'Construire',staging:'Préparer le paquet',debian_metadata:'Métadonnées Debian',systemd:'Service',package:'Créer le paquet',artifact:'Artéfact',validation:'Validation',publication:'Publication APT'},
    de: {source:'Quelle abrufen',detection:'Projekt erkennen',dependencies:'Abhängigkeiten prüfen',source_changes:'Quelländerungen anwenden',build:'Bauen',staging:'Paket vorbereiten',debian_metadata:'Debian Metadaten',systemd:'Dienst',package:'Paket erstellen',artifact:'Artefakt',validation:'Validierung',publication:'APT-Veröffentlichung'},
    es: {source:'Obtener origen',detection:'Detectar proyecto',dependencies:'Verificar dependencias',source_changes:'Aplicar cambios',build:'Compilar',staging:'Preparar paquete',debian_metadata:'Metadatos Debian',systemd:'Servicio',package:'Crear paquete',artifact:'Artefacto',validation:'Validación',publication:'Publicación APT'},
  };
  const stageLabel = name => stageNames[language]?.[name] || name;
  async function review(recipeId) {navigationError = null; try {await openRecipe(recipeId);} catch(caught) {navigationError = caught;}}
  let listController, listPoller, detailPoller, selectedId = '', defaultRunId = '', generation = 0, logNode;
  const active = row => row?.lifecycle_active === true || ['queued','running','cancelling'].includes(row?.status);
  const completedStates = new Set(['completed','published','validated','success']);
  function matchesStatusFilter(row, selectedFilter) {
    const state = row.lifecycle_status || row.status || '';
    if (selectedFilter === 'all') return true;
    if (selectedFilter === 'active') return active(row) || operatorStatusSemantics(state).tone === 'info';
    if (selectedFilter === 'failed') return operatorStatusSemantics(state).tone === 'danger';
    if (selectedFilter === 'completed') return completedStates.has(state);
    if (selectedFilter === 'cancelled') return state.endsWith('cancelled');
    return false;
  }
  const stoppedBeforeCompletion = value => ['failed','cancelled'].includes(value?.status);
  const validationActionStates = new Set(['validation_needed','ready_to_validate','validation_failed','validation_cancelled']);
  const publicationActionStates = new Set(['ready_to_publish','publication_available','publication_failed']);
  const actionKey = kind => kind === 'validation' ? 'validate' : 'publish';
  function runAction(value) {
    if (publicationActionStates.has(value?.lifecycle_status) && value.allowed_actions?.publish === true) return 'publication';
    if (validationActionStates.has(value?.lifecycle_status) && value.allowed_actions?.validate === true) return 'validation';
    if ((value?.ready_for_build === true || value?.status === 'failed') && (value.recipe_id || value.recipe)) return 'build';
    return null;
  }
  const publicationIdentity = value => {
    const name = value.artifact?.inspection?.package || value.package || value.recipe_id;
    const version = value.artifact?.inspection?.version || value.version?.debian;
    return name && version ? {name,version,confirmation:`publish:${name}:${version}`} : null;
  };
  async function currentActionTarget(context, signal) {
    const current = (await api.run(context.run.id,{signal})).execution;
    if (current?.id !== context.run.id || runAction(current) !== context.kind) throw new Error(t(context.kind === 'build' ? 'runBuildUnavailable' : 'runActionUnavailable',language));
    if (context.kind === 'build') {
      const recipeId = current.recipe_id || current.recipe;
      if (recipeId !== (context.run.recipe_id || context.run.recipe)) throw new Error(t('runBuildUnavailable',language));
      const loaded = await loadRecipe(recipeId,{signal});
      if (loaded.recipe?.active === false) throw new Error(t('runBuildUnavailable',language));
      return {run:current,recipe:loaded.recipe,revision:loaded.revision};
    }
    const identity = context.kind === 'publication' ? publicationIdentity(current) : null;
    if (context.kind === 'publication' && !identity) throw new Error(t('runActionUnavailable',language));
    return {run:current,identity};
  }
  async function openAction(kind, trigger) {
    actionTrigger = trigger;
    actionController?.abort(); actionController = new AbortController();
    const token = ++actionRevision;
    actionContext = {run,kind,target:null};
    actionError = null; actionResult = null; actionLoading = true; actionPending = false; publishConfirmed = false; admittedRunId = '';
    await tick();
    actionDialog.showModal(); actionCloseButton.focus();
    try {
      const target = await currentActionTarget(actionContext,actionController.signal);
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
    else document.querySelector('.run-main .detail-buttons button')?.focus();
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
      const current = await currentActionTarget(context,actionController.signal);
      if (current.run.lifecycle_status !== context.target.run.lifecycle_status || current.identity?.confirmation !== context.target.identity?.confirmation) throw new Error(t('runActionUnavailable',language));
      if (context.kind === 'build') {
        if (current.revision !== context.target.revision) throw new Error(t('runBuildUnavailable',language));
        admittedRunId = await admitDraftRun({draft:current.recipe},false);
      } else if (context.kind === 'validation') await api.startValidation(current.run.id);
      else await api.publishArtifact(current.run.id,current.identity.confirmation);
      if (token === actionRevision) {
        actionResult = context.kind;
        if (context.kind !== 'build') select(context.run.id,true);
        refreshList();
      }
    } catch (caught) {
      if (caught.name !== 'AbortError' && token === actionRevision) actionError = caught;
    } finally {if (token === actionRevision) actionPending = false;}
  }
  function lifecycleStages(value) {
    if (!value) return [];
    const validation = value.validations?.at(-1);
    const publication = value.publications?.at(-1);
    const validationStatus = value.validation_status || validation?.status || 'not_run';
    const publicationStatus = value.publication_status || publication?.status || 'not_run';
    const buildStopped = value.mode !== 'dry_run' && stoppedBeforeCompletion(value);
    const stages = [];
    if (validationStatus !== 'not_run' || value.lifecycle_status === 'validation_needed' || buildStopped) {
      stages.push({name:'validation',status:validationStatus === 'not_run' ? buildStopped ? 'not_reached' : 'validation_needed' : validationStatus,summary:validation?.error?.message});
    }
    if (publicationStatus !== 'not_run' || value.lifecycle_status === 'ready_to_publish' || value.allowed_actions?.publish === true || buildStopped || ['failed','cancelled'].includes(validationStatus)) {
      stages.push({name:'publication',status:publicationStatus === 'not_run' ? value.lifecycle_status === 'ready_to_publish' ? 'ready_to_publish' : 'not_reached' : publicationStatus,summary:publication?.error?.message});
    }
    return stages;
  }
  async function loadList(signal) {const response = await api.runs({signal}); return response.executions || [];}
  function updateList(value) {rows = value; error = null; if (!defaultRunId && value.length) defaultRunId = value[0].id;}
  async function refreshList() {listController?.abort(); listController = new AbortController(); try {updateList(await loadList(listController.signal));} catch(caught) {if(caught.name !== 'AbortError') error = caught;}}
  async function loadDetail(runId, signal) {
    const response = await api.run(runId, {signal});
    const next = response.execution;
    // The cursor refers to the backend's rendered representation for this verbosity.
    // Fetch only its suffix, even after the Run becomes terminal.
    const chunk = (await api.logs(runId, verbosity, offset, {signal})).log;
    return {next, chunk};
  }
  async function applyDetail(value, token) {
    run = value.next;
    logs += value.chunk?.text || '';
    offset = value.chunk?.offset ?? offset;
    detailError = null; logError = null;
    if (!active(run)) detailPoller?.stop();
    if (following) {await tick(); if (token === generation) logNode?.scrollTo({top:logNode.scrollHeight});}
  }
  function select(runId, preserveRun = false) {
    detailPoller?.stop();
    selectedId = runId; ++generation; if (!preserveRun) run = null; logs = ''; offset = 0; detailError = null; logError = null; if (!preserveRun) following = true;
    if (!runId) return;
    const token = generation;
    detailPoller = createPoller(signal => loadDetail(runId,signal), {
      onData: value => {if (token === generation && selectedId === runId) applyDetail(value, token);},
      onError: caught => {if (token === generation) detailError = caught;},
    });
    detailPoller.start();
  }
  function changeVerbosity(value) {if (!['compact','normal','verbose','raw'].includes(value)) return; verbosity = value; select(selectedId,true);}
  onMount(() => {
    listPoller = createPoller(loadList, {interval:5000, onData:updateList, onError:caught => error = caught});
    listPoller.start();
    return () => {++actionRevision; listPoller.stop(); detailPoller?.stop(); listController?.abort(); actionController?.abort();};
  });
  $: displayId = id || defaultRunId;
  $: if (displayId !== selectedId) select(displayId);
  $: visible = rows.filter(row => matchesStatusFilter(row,filter) && `${row.package || ''} ${row.id} ${row.action || ''}`.toLowerCase().includes(query.toLowerCase()));
  $: dependency = run?.steps?.find(step => step.name === 'staging')?.details?.runtime_dependency_detection;
  $: failedStep = run?.steps?.find(step => step.status === 'failed');
  $: nextAction = runAction(run);
  $: visibleSteps = [
    ...(run?.steps || []).filter(step => step.status !== 'skipped').map(step => step.status === 'pending' && stoppedBeforeCompletion(run) ? {...step,status:'not_reached'} : step),
    ...lifecycleStages(run),
  ];
</script>
<ErrorNotice {error} retry={refreshList} {language}/>
<div class="run-layout runs-layout" class:mobile-detail={Boolean(id)}><section class="panel run-list"><div class="section-head"><h2>{t('runs',language)}</h2><small>{visible.length} / {rows.length}</small></div><div class="filters run-filters"><label><span>{t('search',language)}</span><input type="search" bind:value={query}></label><label><span>{t('status',language)}</span><select bind:value={filter}><option value="all">{t('all',language)}</option><option value="active">{t('activeWork',language)}</option><option value="failed">{statusLabel('failed',language)}</option><option value="completed">{statusLabel('completed',language)}</option><option value="cancelled">{statusLabel('cancelled',language)}</option></select></label></div><div class="selection-list run-selection">{#each visible as row (row.id)}<button class:selected={displayId===row.id} class="run-list-row" onclick={() => navigate('runs',row.id)}><span><strong>{row.package || row.id}</strong><small>{when(row.updated,language)} · {row.id}</small></span><Status value={row.lifecycle_status || row.status} {language} semantic={operatorStatusSemantics}/></button>{:else}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{/each}</div></section>
<div class="detail run-detail" class:no-selection={!displayId}>
  {#if id}<button class="back-button mobile-only" onclick={() => navigate('runs')}>{t('back',language)}</button>{/if}
  <ErrorNotice error={detailError} retry={() => select(displayId)} {language}/>
  {#if run}
    <section class="panel run-main">
      <p class="detail-breadcrumb">{t('runs',language)} / <strong>{run.id}</strong></p>
      <div class="run-title"><div><h2>{run.package || run.id}</h2><p class="muted">{run.version?.debian || run.version?.upstream || run.id} · {when(run.updated,language)}</p></div><Status value={run.lifecycle_status || run.status} {language} semantic={operatorStatusSemantics}/></div>
      <div class="detail-facts">
        <div><span>{t('started',language)}</span><strong>{when(run.started_at || run.created_at || run.updated,language)}</strong></div>
        {#if run.recipe_id || run.recipe}<div><span>{t('recipeFact',language)}</span><strong>{run.recipe_id || run.recipe}</strong></div>{/if}
      </div>
      {#if run.recipe_id || run.recipe || nextAction}<div class="detail-buttons">{#if run.recipe_id || run.recipe}<button class="button secondary" onclick={() => review(run.recipe_id || run.recipe)}>{t('reviewRecipe',language)}</button>{/if}{#if nextAction}<button class="button primary" onclick={event => openAction(nextAction,event.currentTarget)}>{t(nextAction === 'build' ? 'overviewStartBuild' : nextAction === 'validation' ? 'overviewStartValidation' : 'overviewPublishToApt',language)}</button>{/if}</div><ErrorNotice error={navigationError} {language}/>{/if}
    </section>
    <section class="panel run-stages"><h3>{t('stages',language)}</h3><div class="stage-list">{#each visibleSteps as step, index (step.name)}<div class:stage-current={['queued','running','cancelling'].includes(step.status)} class:stage-failed={step.status==='failed'} class:stage-complete={step.status==='success'} class:stage-action={['validation_needed','ready_to_publish'].includes(step.status)} class:stage-not-reached={step.status==='not_reached'}><span class="stage-number">{step.status==='failed'?'!':['queued','running','cancelling'].includes(step.status)?'◌':step.status==='success'||step.status==='completed'?'✓':index+1}</span><strong title={step.summary || step.name}>{stageLabel(step.name)}</strong><small>{statusLabel(step.status,language)}</small></div>{/each}</div></section>
    {#if dependency}<section class="panel run-dependencies"><div class="section-head"><h3>{t('dependencies',language)}</h3><Status value={dependency.status} {language} semantic={operatorStatusSemantics}/></div><div class="detail-facts"><div><span>{t('detected',language)}</span><strong>{dependency.detected_count || 0}</strong></div><div><span>{t('bundled',language)}</span><strong>{dependency.bundled_count || 0}</strong></div>{#if dependency.unresolved_count}<div><span>{t('unresolved',language)}</span><strong>{dependency.unresolved_count}</strong></div>{/if}{#if dependency.effective_depends?.length}<div><span>{t('depends',language)}</span><strong>{dependency.effective_depends.join(', ')}</strong></div>{/if}</div></section>{/if}
    {#if run.error || run.diagnostic || failedStep || run.recovery_blocker}<section class="panel run-diagnosis"><h3>{t('diagnosis',language)}</h3><div class="inline-alert"><strong>{run.diagnostic?.title || failedStep?.summary || run.error?.message || run.recovery_blocker?.reason || t('unavailable',language)}</strong>{#if failedStep}<p>{t('failureStage',language)}: {stageLabel(failedStep.name)}</p>{/if}{#if run.diagnostic?.next_action}<p>{t('remediation',language)}: {run.diagnostic.next_action}</p>{/if}{#if run.recovery_blocker?.reason}<p>{run.recovery_blocker.reason}</p>{/if}</div>{#if run.error?.code || run.error?.message}<details class="run-error-technical"><summary>{t('errorDetail',language)}</summary>{#if run.error.code}<code>{run.error.code}</code>{/if}{#if run.error.message}<p>{run.error.message}</p>{/if}</details>{/if}</section>{/if}
    <section class="panel run-logs"><div class="section-head"><h2>{t('logs',language)}</h2><div class="run-log-controls"><label class="log-verbosity"><span class="sr-only">{t('verbosity',language)}</span><select aria-label={t('verbosity',language)} value={verbosity} onchange={event => changeVerbosity(event.currentTarget.value)}>{#each ['compact','normal','verbose','raw'] as option}<option value={option}>{t(option,language)}</option>{/each}</select></label>{#if active(run)}<button class="button secondary" onclick={() => following = !following}>{following ? t('pause',language) : t('follow',language)}</button>{/if}</div></div><ErrorNotice error={logError} {language}/><pre bind:this={logNode} class="log-output">{logs}</pre></section>
    <details class="panel run-technical"><summary>{t('technicalFacts',language)}</summary><div class="detail-facts"><div><span>{t('runId',language)}</span><strong><code>{run.id}</code></strong></div>{#if run.mode}<div><span>{t('runMode',language)}</span><strong>{run.mode}</strong></div>{/if}{#if run.finished_at}<div><span>{t('finished',language)}</span><strong>{when(run.finished_at,language)}</strong></div>{/if}{#if run.duration != null}<div><span>{t('duration',language)}</span><strong>{run.duration} s</strong></div>{/if}{#if run.source?.ref}<div><span>{t('sourceRef',language)}</span><strong>{run.source.ref}</strong></div>{/if}{#if run.artifact?.path}<div><span>{t('artifactFact',language)}</span><strong>{run.artifact.path}</strong></div>{/if}</div></details>
  {:else if displayId && !detailError}<p>{t('loading',language)}</p>{:else}<p>{t('noRun',language)}</p>{/if}
</div></div>
<dialog class="run-action-dialog" bind:this={actionDialog} aria-labelledby="run-action-title" oncancel={event => {if (actionPending) event.preventDefault();}} onclose={resetAction}>
  {#if actionContext}
    <div class="modal-head"><h2 id="run-action-title">{t(actionContext.kind === 'build' ? 'overviewStartBuild' : actionContext.kind === 'validation' ? 'overviewStartValidation' : 'overviewPublishToApt',language)}</h2><button type="button" class="icon-button" bind:this={actionCloseButton} aria-label={t('close',language)} onclick={closeAction}>×</button></div>
    <p class="modal-copy"><strong>{actionContext.run.package || actionContext.run.id}</strong> — {t(actionContext.kind === 'build' ? 'runBuildInfo' : actionContext.kind === 'validation' ? 'overviewValidationInfo' : 'overviewPublicationInfo',language)}</p>
    {#if actionLoading}<p class="muted">{t('loading',language)}</p>{/if}
    <ErrorNotice error={actionError} {language}/>
    {#if actionContext.kind === 'build' && ['network','timeout','ambiguous'].includes(actionError?.kind)}<p class="muted">{t('admissionUnknown',language)}</p>{/if}
    {#if actionContext.kind === 'build' && actionContext.target && !actionError && !actionResult}<p class="overview-publication-identity"><strong>{t('recipeFact',language)}:</strong> {actionContext.target.run.recipe_id || actionContext.target.run.recipe}</p>{/if}
    {#if actionContext.target && !actionError && !actionResult && actionContext.kind === 'publication'}
      <p class="overview-publication-identity"><strong>{actionContext.target.identity.name}</strong> · {actionContext.target.identity.version}</p>
      <label class="overview-publication-confirm"><input type="checkbox" bind:checked={publishConfirmed} disabled={actionPending}> {t('overviewConfirmPublication',language)}</label>
    {/if}
    {#if actionResult}<p class="overview-action-result" role="status">{t(actionResult === 'build' ? 'overviewBuildQueued' : actionResult === 'validation' ? 'runValidationStarted' : 'runPublicationDone',language)}</p>{/if}
    <div class="modal-actions"><button type="button" class="button secondary" disabled={actionPending} onclick={closeAction}>{t('close',language)}</button>{#if actionContext.kind === 'build' && (admittedRunId || ['network','timeout','ambiguous'].includes(actionError?.kind))}<button type="button" class="button secondary" disabled={actionPending} onclick={visitBuildRun}>{t(admittedRunId ? 'viewRun' : 'viewRuns',language)}</button>{/if}{#if actionContext.target && !actionError && !actionResult}<button type="button" class="button primary" disabled={actionPending || (actionContext.kind === 'publication' && !publishConfirmed)} onclick={submitAction}>{actionPending ? t('overviewActionWorking',language) : t(actionContext.kind === 'build' ? 'overviewStartBuild' : actionContext.kind === 'validation' ? 'overviewStartValidation' : 'overviewPublishToApt',language)}</button>{/if}</div>
  {/if}
</dialog>
