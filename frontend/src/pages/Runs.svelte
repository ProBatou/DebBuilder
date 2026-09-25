<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {createPoller} from '../features/polling.js';
  import {navigate} from '../navigation/location.js';
  import {t,when} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  export let id = '', language = 'en';
  let rows = [], run = null, logs = '', offset = 0, verbosity = 'normal', following = true;
  let query = '', filter = 'all', error = null, detailError = null, logError = null, optionsOpen = false;
  let listController, detailController, listPoller, detailPoller, selectedId = '', generation = 0, logNode;
  const active = row => row?.lifecycle_active === true || ['queued','running','cancelling'].includes(row?.status);
  async function loadList(signal) {const response = await api.runs({signal}); return response.executions || [];}
  async function refreshList() {listController?.abort(); listController = new AbortController(); try {rows = await loadList(listController.signal); error = null;} catch(caught) {if(caught.name !== 'AbortError') error = caught;}}
  async function loadDetail(runId, signal) {
    const response = await api.run(runId, {signal});
    const next = response.execution;
    // The cursor refers to the backend's rendered representation for this verbosity.
    // Fetch only its suffix, even after the Run becomes terminal.
    const chunk = (await api.logs(runId, verbosity, offset, {signal})).log;
    return {next, chunk};
  }
  async function applyDetail(value) {
    run = value.next;
    logs += value.chunk?.text || '';
    offset = value.chunk?.offset ?? offset;
    detailError = null; logError = null;
    if (following) {await tick(); logNode?.scrollTo({top:logNode.scrollHeight});}
    if (!active(run)) detailPoller?.stop();
  }
  function select(runId) {
    detailPoller?.stop(); detailController?.abort();
    selectedId = runId; ++generation; run = null; logs = ''; offset = 0; detailError = null; logError = null; following = true;
    if (!runId) return;
    const token = generation;
    detailPoller = createPoller(signal => loadDetail(runId,signal), {
      onData: value => {if (token === generation && selectedId === runId) applyDetail(value);},
      onError: caught => {if (token === generation) detailError = caught;},
    });
    detailPoller.start();
  }
  function changeVerbosity(value) {if (!['compact','normal','verbose','raw'].includes(value)) return; verbosity = value; logs = ''; offset = 0; select(id); optionsOpen = false;}
  function outside(event) {if (optionsOpen && !event.target.closest('.log-options')) optionsOpen = false;}
  onMount(() => {
    listPoller = createPoller(loadList, {interval:5000, onData:value => {rows = value; error = null;}, onError:caught => error = caught});
    listPoller.start();
    return () => {listPoller.stop(); detailPoller?.stop(); listController?.abort(); detailController?.abort();};
  });
  $: if (id !== selectedId) select(id);
  $: visible = rows.filter(row => (filter === 'all' || (filter === 'active' ? active(row) : (row.lifecycle_status || row.status) === filter)) && `${row.package || ''} ${row.id} ${row.action || ''}`.toLowerCase().includes(query.toLowerCase()));
  $: dependency = run?.steps?.find(step => step.name === 'staging')?.details?.runtime_dependency_detection;
</script>
<svelte:window onpointerdown={outside} onkeydown={(event) => {if(event.key === 'Escape') optionsOpen = false;}}/>
<h1>{t('runs',language)}</h1><ErrorNotice {error} retry={refreshList} {language}/>
<div class="split"><section class="panel"><div class="filters"><label>{t('search',language)} <input type="search" bind:value={query}></label><label>{t('status',language)} <select bind:value={filter}><option value="all">{t('all',language)}</option><option value="active">{t('activeWork',language)}</option><option value="failed">failed</option><option value="completed">completed</option><option value="cancelled">cancelled</option></select></label></div><div class="list">{#each visible as row (row.id)}<button class:active={id===row.id} class="row" onclick={() => navigate('runs',row.id)}><span><strong>{row.package || row.id}</strong><small>{when(row.updated,language)} · {row.id}</small></span><Status value={row.lifecycle_status || row.status} {language}/></button>{:else}<p>{t('noItems',language)}</p>{/each}</div></section>
<div class="detail">{#if id}<button class="back" onclick={() => navigate('runs')}>← {t('back',language)}</button>{/if}<ErrorNotice error={detailError} retry={() => select(id)} {language}/>{#if run}<section class="panel"><h2>{run.package || run.id}</h2><p class="muted"><code>{run.id}</code> · {when(run.updated,language)}</p><Status value={run.lifecycle_status || run.status} {language}/>
  {#if run.recovery_blocker || run.validations?.at(-1)?.recovery_blocker}<div class="notice error"><strong>{t('recovery',language)}</strong><p>{run.recovery_blocker?.reason || run.recovery_blocker?.code || run.validations.at(-1).recovery_blocker.reason || run.validations.at(-1).recovery_blocker.code}</p></div>{/if}
  {#if run.cancellation}<p>{run.cancellation.reason || run.cancellation.code}</p>{/if}
  {#if run.diagnostic}<section class="subpanel"><h3>{t('diagnosis',language)}</h3><p>{run.diagnostic.title}</p><p>{run.diagnostic.next_action}</p>{#each run.diagnostic.facts || [] as fact}<p><strong>{fact.label}:</strong> {Array.isArray(fact.value) ? fact.value.join(', ') : fact.value}</p>{/each}</section>{/if}
  {#if run.error}<div class="notice error"><strong>{run.error.code}</strong><p>{run.error.message}</p>{#if run.error.details?.path}<code>{run.error.details.path}</code>{/if}</div>{/if}
  <h3>{t('stages',language)}</h3><div class="stages">{#each run.steps || [] as step (step.name)}<div><span>{step.name}</span><Status value={step.status} {language}/>{#if step.summary}<small>{step.summary}</small>{/if}{#if step.error}<small>{step.error.code}: {step.error.message}</small>{/if}</div>{/each}</div>
  {#if dependency}<section class="subpanel"><h3>{t('dependencies',language)}</h3><p>{dependency.status} · {dependency.detected_count || 0} {t('detected',language)} · {dependency.bundled_count || 0} {t('bundled',language)} · {dependency.unresolved_count || 0} {t('unresolved',language)}</p><details><summary>{t('details',language)}</summary><p>{t('overrides',language)}: {dependency.overridden_count || 0}</p><p>{t('depends',language)}: {(dependency.effective_depends || []).join(', ') || '—'}</p></details></section>{/if}
  <section class="subpanel"><div class="section-head"><h3>{t('logs',language)}</h3><div><button onclick={() => following = !following}>{following ? t('pause',language) : t('follow',language)}</button><span class="log-options"><button aria-expanded={optionsOpen} onclick={() => optionsOpen = !optionsOpen}>{t('options',language)}</button>{#if optionsOpen}<div class="popover">{#each ['compact','normal','verbose','raw'] as option}<button onclick={() => changeVerbosity(option)}>{t(option,language)}</button>{/each}</div>{/if}</span></div></div><ErrorNotice error={logError} {language}/><pre bind:this={logNode} class="log-output">{logs}</pre></section>
</section>{:else if id && !detailError}<p>{t('loading',language)}</p>{:else}<p>{t('noRun',language)}</p>{/if}</div></div>
