<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {navigate} from '../navigation/location.js';
  import {t} from '../i18n/i18n.js';
  import {isManaged} from '../features/recipes/model.js';
  import RecipeDetail from '../features/recipes/RecipeDetail.svelte';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  export let id = '', language = 'en';
  let rows = [], workflowRows = [], listingErrors = [], listError = null, listLoading = true, detailError = null, recipe = null;
  let inspection = null, automation = null, inspectionError = null, automationError = null;
  let query = '', listController, detailController, generation = 0, listGeneration = 0;
  async function load() {
    listController?.abort(); listController = new AbortController(); const token = ++listGeneration; listError = null; listLoading = true; rows = []; listingErrors = [];
    try {
      const [list, workflows] = await Promise.all([api.recipes({signal:listController.signal}), api.workflows({signal:listController.signal})]);
      if (token === listGeneration) {rows = (list.recipes || []).filter(row => !row.managed); workflowRows = workflows.workflows || []; listingErrors = workflows.errors || [];}
    } catch (caught) {if (caught.name !== 'AbortError' && token === listGeneration) listError = caught;}
    finally {if (token === listGeneration) listLoading = false;}
  }
  async function detail(recipeId) {
    detailController?.abort(); detailController = new AbortController(); const token = ++generation;
    recipe = null; inspection = null; automation = null; detailError = null; inspectionError = null; automationError = null;
    if (!recipeId) return;
    try {
      const loaded = await api.workflow(recipeId,{signal:detailController.signal});
      if (token !== generation) return;
      if (isManaged(loaded)) {navigate('system','managed'); return;}
      recipe = loaded;
    } catch (caught) {if (caught.name !== 'AbortError' && token === generation) detailError = caught; return;}
    const [inspected, automated] = await Promise.allSettled([
      api.recipeInspection(recipeId,{signal:detailController.signal}), api.automation(recipeId,{signal:detailController.signal})
    ]);
    if (token !== generation) return;
    if (inspected.status === 'fulfilled') inspection = inspected.value.inspection;
    else if (inspected.reason.name !== 'AbortError') inspectionError = inspected.reason;
    if (automated.status === 'fulfilled') automation = automated.value.automation;
    else if (automated.reason.name !== 'AbortError') automationError = automated.reason;
  }
  onMount(() => {load(); return () => {listController?.abort(); detailController?.abort();};});
  $: detail(id);
  $: visible = rows.filter(row => `${row.id} ${row.package || ''} ${row.repository || ''}`.toLowerCase().includes(query.toLowerCase()));
</script>
<h1>{t('recipes',language)}</h1><p class="muted">{t('readOnly',language)}</p><ErrorNotice error={listError} retry={load} {language}/>
{#if listingErrors.length}<div class="notice error"><strong>{t('invalidRecipes',language)}</strong>{#each listingErrors as item}<p>{item.id}: {item.error?.message}</p>{/each}</div>{/if}
<div class="split"><section class="panel"><label>{t('search',language)} <input type="search" bind:value={query}></label><div class="list">{#if listLoading}<p>{t('loading',language)}</p>{:else}{#each visible as row (row.id)}<button class:active={id===row.id} class="row" onclick={() => navigate('recipes',row.id)}><span><strong>{row.id}</strong><small>{row.repository || row.source} · {row.package || '—'}</small></span><span class="chip">{row.active === false ? t('inactive',language) : t('active',language)}</span></button>{:else}{#if !listError}<p>{t('noItems',language)}</p>{/if}{/each}{/if}</div></section>
<div class="detail">{#if id}<button class="back" onclick={() => navigate('recipes')}>← {t('back',language)}</button>{/if}<ErrorNotice error={detailError} retry={() => detail(id)} {language}/>{#if recipe}<RecipeDetail {recipe} {inspection} {automation} {inspectionError} {automationError} {language} writable={workflowRows.find(row => row.id === id)?.writable === true}/>{:else if id && !detailError}<p>{t('loading',language)}</p>{:else}<p>{t('selectRecipe',language)}</p>{/if}</div></div>
