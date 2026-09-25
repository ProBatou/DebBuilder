<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {navigate} from '../navigation/location.js';
  import {t,when,number} from '../i18n/i18n.js';
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
</script>
<h1>{t('overview',language)}</h1>
{#if loading}<p>{t('loading',language)}</p>{/if}<ErrorNotice {error} retry={load} {language}/>
{#if dashboard}
  <div class="summary"><article class="panel"><strong>{number(dashboard.packages,language)}</strong><span>{t('packages',language)}</span></article><article class="panel"><strong>{number(dashboard.updates,language)}</strong><span>{t('needsAttention',language)}</span></article><article class="panel"><strong>{number(dashboard.ready_to_publish,language)}</strong><span>{t('published',language)}</span></article></div>
  <div class="grid"><section class="panel"><h2>{t('needsAttention',language)}</h2>{#each attention.slice(0,5) as row (row.name)}<button class="row" onclick={() => navigate('packages',row.name)}><strong>{row.name}</strong><Status value={row.lifecycle_display_status || row.lifecycle_state} {language}/></button>{:else}<p class="muted">{t('noItems',language)}</p>{/each}</section>
  <section class="panel"><h2>{t('activeWork',language)}</h2>{#each active as row (row.id)}<button class="row" onclick={() => navigate('runs',row.id)}><strong>{row.package || row.id}</strong><Status value={row.lifecycle_status || row.status} {language}/></button>{:else}<p class="muted">{t('noItems',language)}</p>{/each}</section></div>
  <div class="grid"><section class="panel"><h2>{t('recentRuns',language)}</h2>{#each dashboard.latest_operations || [] as row (row.id)}<button class="row" onclick={() => navigate('runs',row.id)}><span><strong>{row.package || row.id}</strong><small>{when(row.updated,language)} · {row.id}</small></span><Status value={row.lifecycle_status || row.status} {language}/></button>{:else}<p class="muted">{t('noItems',language)}</p>{/each}</section>
  <section class="panel"><h2>{t('repository',language)}</h2>{#if repository}<Status value={repository.status} {language}/><p>{repository.message}</p><p class="muted">{t('signedMetadata',language)}: {repository.details?.signed_release_present ? '✓' : '—'}</p>{/if}</section></div>
{/if}
