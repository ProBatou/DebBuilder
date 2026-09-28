<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {navigate} from '../navigation/location.js';
  import {t,when,number,statusLabel} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  import StatusChip from '../components/StatusChip.svelte';
  export let language = 'en';
  let dashboard = null, diagnostics = null, error = null, loading = true, controller;
  async function load() {
    controller?.abort(); controller = new AbortController(); loading = true; error = null;
    try {const [a,b] = await Promise.all([api.dashboard({signal:controller.signal}),api.diagnostics({signal:controller.signal})]); dashboard = a.dashboard; diagnostics = b;}
    catch (caught) {if (caught.name !== 'AbortError') error = caught;}
    finally {loading = false;}
  }
  onMount(() => {load(); return () => controller?.abort();});
  $: attention = (dashboard?.package_rows || []).filter(row => /fail|error|update_available|ready_to_publish|validation_needed/.test(row.lifecycle_display_status || row.lifecycle_state || ''));
  $: active = (dashboard?.latest_operations || []).filter(row => row.lifecycle_active);
  $: repository = diagnostics?.checks?.find(row => row.id === 'repository.publication');
  const toneFor = value => /fail|error/.test(value || '') ? 'danger' : /run|queue|active/.test(value || '') ? 'info' : /ready|complete|publish/.test(value || '') ? 'success' : 'warning';
  const iconFor = value => toneFor(value) === 'danger' ? '!' : toneFor(value) === 'info' ? '◌' : toneFor(value) === 'success' ? '✓' : '●';
  $: healthy = diagnostics?.checks?.every(row => row.status === 'ok' || row.status === 'healthy') && !attention.length;
</script>
{#if loading}<p>{t('loading',language)}</p>{/if}<ErrorNotice {error} retry={load} {language}/>
{#if dashboard}
{#if diagnostics?.checks?.some(check => check.status === 'error' || check.status === 'failed')}<div class="attention-notice" role="status"><span class="notice-mark" aria-hidden="true">!</span><div><strong>{t('needsAttention',language)}</strong><p>{diagnostics.checks.find(check => check.status === 'error' || check.status === 'failed')?.message}</p></div><button class="button secondary" onclick={() => navigate('system')}>{t('open',language)}</button></div>{/if}
<div class="overview-headline"><StatusChip label={healthy ? t('health',language) : t('needsAttention',language)} tone={healthy ? 'success' : 'warning'} icon={healthy ? '✓' : '!'} /><div class="compact-stats"><span>{t('packages',language)} <strong>{number(dashboard.packages,language)}</strong></span><span>{t('activeWork',language)} <strong>{number(active.length,language)}</strong></span><span>{statusLabel('ready_to_publish',language)} <strong>{number(dashboard.ready_to_publish,language)}</strong></span></div></div>
<div class="overview-grid"><div class="overview-main"><section class="panel"><div class="section-head"><h2>{t('needsAttention',language)}</h2>{#if attention.length>5}<button class="text-button" onclick={() => navigate('packages')}>{t('all',language)} →</button>{/if}</div>{#if attention.length===0}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{:else}<div class="click-list">{#each attention.slice(0,5) as row (row.name)}<button class="click-row" onclick={() => navigate('packages',row.name)}><span class="row-status {toneFor(row.lifecycle_display_status || row.lifecycle_state)}" aria-hidden="true">{iconFor(row.lifecycle_display_status || row.lifecycle_state)}</span><span class="click-main"><strong>{row.name}</strong><small>{statusLabel(row.lifecycle_display_status || row.lifecycle_state,language)}</small></span><span class="row-action">{t('open',language)} →</span></button>{/each}</div>{/if}</section>
<section class="panel"><div class="section-head"><h2>{t('recentRuns',language)}</h2><button class="text-button" onclick={() => navigate('runs')}>{t('all',language)} →</button></div>{#if !(dashboard.latest_operations || []).length}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{:else}<div class="click-list">{#each (dashboard.latest_operations || []).slice(0,5) as row (row.id)}<button class="click-row" onclick={() => navigate('runs',row.id)}><span class="row-status {toneFor(row.lifecycle_status || row.status)}" aria-hidden="true">{iconFor(row.lifecycle_status || row.status)}</span><span class="click-main"><strong>{row.package || row.id} · {row.action || row.mode || 'Run'}</strong><small>{when(row.updated,language)} · {row.id}</small></span><span class="row-action">{statusLabel(row.lifecycle_status || row.status,language)}</span></button>{/each}</div>{/if}</section></div><aside class="overview-side"><section class="panel compact-panel"><div class="section-head"><h2>{t('activeWork',language)}</h2>{#if active.length}<StatusChip label={number(active.length,language)} tone="info" icon="◌" />{/if}</div>{#if active.length}{#each active.slice(0,3) as row (row.id)}<button class="plain-link" onclick={() => navigate('runs',row.id)}><strong>{row.package || row.id}</strong><span>{statusLabel(row.lifecycle_status || row.status,language)}</span><b>{t('open',language)} →</b></button>{/each}{:else}<p class="muted compact-copy">{t('noItems',language)}</p>{/if}</section><section class="panel compact-panel"><div class="section-head"><h2>{t('repository',language)}</h2>{#if repository}<Status value={repository.status} {language}/>{/if}</div>{#if repository}<p class="repo-line">{repository.message}</p><p class="muted compact-copy">{t('signedMetadata',language)}: {repository.details?.signed_release_present ? '✓' : '—'}</p>{/if}<button class="text-button" onclick={() => navigate('packages')}>{t('open',language)} →</button></section></aside></div>
{/if}
