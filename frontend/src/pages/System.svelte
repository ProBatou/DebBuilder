<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {t} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  export let language = 'en';
  let snapshot = null, storage = null, error = null, controller, tab = 'health';
  async function load() {controller?.abort(); controller = new AbortController(); error = null; try {const [a,b] = await Promise.all([api.diagnostics({signal:controller.signal}),api.storage({signal:controller.signal})]); snapshot = a; storage = b.storage;} catch(caught) {if(caught.name !== 'AbortError') error = caught;}}
  onMount(() => {load(); return () => controller?.abort();});
</script>
<h1>{t('system',language)}</h1><div class="tabs" role="group" aria-label={t('system',language)}><button class:active={tab==='health'} onclick={() => tab='health'}>{t('health',language)}</button><button class:active={tab==='maintenance'} onclick={() => tab='maintenance'}>{t('maintenance',language)}</button><button class:active={tab==='developer'} onclick={() => tab='developer'}>{t('developer',language)}</button></div><ErrorNotice {error} retry={load} {language}/>
{#if tab === 'health'}<div class="grid">{#each snapshot?.checks || [] as check (check.id)}<section class="panel"><div class="section-head"><h2>{check.id}</h2><Status value={check.status} {language}/></div><p>{check.message}</p><dl>{#each Object.entries(check.details || {}) as [key,value]}<dt>{key}</dt><dd>{typeof value === 'boolean' ? (value ? '✓' : '—') : value}</dd>{/each}</dl></section>{/each}</div>
{:else if tab === 'maintenance'}<section class="panel"><h2>{t('maintenance',language)}</h2><p>{t('availableLater',language)}</p>{#if storage}<pre class="facts">{JSON.stringify(storage,null,2)}</pre>{/if}</section>
{:else}<section class="panel"><h2>{t('developer',language)}</h2><p><a href="/api/openapi.json" target="_blank" rel="noopener noreferrer">{t('openapi',language)}</a></p><p>{t('availableLater',language)}: {t('inspectors',language)}, {t('supportBundle',language)}</p></section>{/if}
