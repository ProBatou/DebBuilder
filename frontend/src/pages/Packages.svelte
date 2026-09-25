<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {navigate} from '../navigation/location.js';
  import {t} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import Status from '../components/Status.svelte';
  export let id = '', language = 'en';
  let rows = [], selected = null, error = null, detailError = null, query = '', controller, detailController, revision = 0;
  async function load() {controller?.abort(); controller = new AbortController(); error = null; try {rows = (await api.packages({signal:controller.signal})).packages || [];} catch (caught) {if(caught.name !== 'AbortError') error = caught;}}
  async function detail(name) {detailController?.abort(); detailController = new AbortController(); const token = ++revision; detailError = null; selected = null; if (!name) return; try {const value = (await api.package(name,{signal:detailController.signal})).package; if(token === revision) selected = value;} catch(caught) {if(caught.name !== 'AbortError' && token === revision) detailError = caught;}}
  onMount(() => {load(); return () => {controller?.abort(); detailController?.abort();};});
  $: detail(id);
  $: filtered = rows.filter(row => row.name?.toLowerCase().includes(query.toLowerCase()));
</script>
<h1>{t('packages',language)}</h1><ErrorNotice {error} retry={load} {language}/>
<div class="split"><section class="panel"><label>{t('search',language)} <input type="search" bind:value={query}></label><div class="list">{#each filtered as row (row.name)}<button class:active={id===row.name} class="row" onclick={() => navigate('packages',row.name)}><span><strong>{row.name}</strong><small>{row.version?.published || row.apt_version || '—'}</small></span><Status value={row.lifecycle_display_status || row.lifecycle_state || row.status} {language}/></button>{:else}<p>{t('noItems',language)}</p>{/each}</div></section>
<div class="detail">{#if id}<button class="back" onclick={() => navigate('packages')}>← {t('back',language)}</button>{/if}<ErrorNotice error={detailError} retry={() => detail(id)} {language}/>{#if selected}<section class="panel"><h2>{selected.name}</h2><Status value={selected.lifecycle_display_status || selected.lifecycle_state || selected.status} {language}/><dl><dt>{t('published',language)}</dt><dd>{selected.version?.published || selected.apt_version || '—'}</dd><dt>{t('built',language)}</dt><dd>{selected.version?.candidate || '—'}</dd><dt>{t('source',language)}</dt><dd>{selected.source?.repository || selected.source?.type || '—'}</dd><dt>{t('repository',language)}</dt><dd>{selected.repository?.url || '—'}</dd></dl>{#if selected.build?.latest_run_id}<button onclick={() => navigate('runs',selected.build.latest_run_id)}>{t('viewRun',language)} →</button>{/if}{#if selected.recipe}<p class="muted">{t('reviewRecipe',language)}: <code>{selected.recipe}</code> · {t('availableLater',language)}</p>{/if}</section>{:else if id && !detailError}<p>{t('loading',language)}</p>{:else}<p>{t('noPackage',language)}</p>{/if}</div></div>
<section class="panel"><h2>{t('repository',language)}</h2><p class="muted">{t('notExposed',language)}</p></section>
