<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {navigate} from '../navigation/location.js';
  import {t,when,number,statusLabel} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
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
  $: healthy = diagnostics?.checks?.every(row => row.status === 'ok' || row.status === 'healthy') && !attention.length;
</script>
{#if loading}<p>{t('loading',language)}</p>{/if}<ErrorNotice {error} retry={load} {language}/>
{#if dashboard}
  <div class="overview-headline"><span class:good={healthy} class="chip">{healthy ? '✓' : '!'} {healthy ? t('health',language) : t('needsAttention',language)}</span><div class="compact-stats"><span>{t('packages',language)} <strong>{number(dashboard.packages,language)}</strong></span><span>{t('activeWork',language)} <strong>{number(active.length,language)}</strong></span><span>{statusLabel('ready_to_publish',language)} <strong>{number(dashboard.ready_to_publish,language)}</strong></span></div></div>
  <div class="overview-grid"><div class="overview-main">
    <section class="panel"><div class="section-head"><h2>{t('needsAttention',language)}</h2>{#if attention.length > 5}<button class="text-button" onclick={() => navigate('packages')}>{t('all',language)} →</button>{/if}</div><div class="click-list">{#each attention.slice(0,5) as row (row.name)}<button class="click-row" onclick={() => navigate('packages',row.name)}><span class="row-status">!</span><span class="click-main"><strong>{row.name}</strong><small>{statusLabel(row.lifecycle_display_status || row.lifecycle_state,language)}</small></span><span class="row-action">{t('open',language)} →</span></button>{:else}<p class="empty-state">{t('noItems',language)}</p>{/each}</div></section>
    <section class="panel"><div class="section-head"><h2>{t('recentRuns',language)}</h2><button class="text-button" onclick={() => navigate('runs')}>{t('all',language)} →</button></div><div class="click-list">{#each (dashboard.latest_operations || []).slice(0,5) as row (row.id)}<button class="click-row" onclick={() => navigate('runs',row.id)}><span class="row-status">{row.lifecycle_active ? '◌' : '✓'}</span><span class="click-main"><strong>{row.package || row.id} · {row.action || row.mode || 'Run'}</strong><small>{when(row.updated,language)} · {row.id}</small></span><span class="row-action">{statusLabel(row.lifecycle_status || row.status,language)}</span></button>{:else}<p class="empty-state">{t('noItems',language)}</p>{/each}</div></section>
  </div><aside class="overview-side"><section class="panel"><h2>{t('activeWork',language)}</h2>{#each active as row (row.id)}<button class="plain-link" onclick={() => navigate('runs',row.id)}><strong>{row.package || row.id}</strong><small>{row.lifecycle_status || row.status}</small></button>{:else}<p class="muted">{t('noItems',language)}</p>{/each}</section><section class="panel"><div class="section-head"><h2>{t('repository',language)}</h2>{#if repository}<Status value={repository.status} {language}/>{/if}</div>{#if repository}<p>{repository.message}</p><p class="muted">{t('signedMetadata',language)}: {repository.details?.signed_release_present ? '✓' : '—'}</p>{/if}<button class="text-button" onclick={() => navigate('packages')}>{t('open',language)} →</button></section></aside></div>
{/if}
