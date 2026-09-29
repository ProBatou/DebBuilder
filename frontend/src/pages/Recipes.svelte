<script>
  import {onMount} from 'svelte';
  import {api} from '../api/client.js';
  import {loadRecipe} from '../features/recipes/persistence.js';
  import {navigate} from '../navigation/location.js';
  import {t} from '../i18n/i18n.js';
  import {isManaged} from '../features/recipes/model.js';
  import RecipeDetail from '../features/recipes/RecipeDetail.svelte';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  export let id = '', language = 'en';
  let rows = [], workflowRows = [], listingErrors = [], listError = null, listLoading = true, detailError = null, recipe = null;
  let query = '', listController, detailController, generation = 0, listGeneration = 0, revision = null, defaultRecipeId = '';
  function onSaved({recipe:stored, revision:fresh, newer}) {
    revision = fresh; load();
    if (!newer) recipe = stored;
  }
  async function load() {
    listController?.abort(); listController = new AbortController(); const token = ++listGeneration; listError = null; listLoading = true; rows = []; listingErrors = [];
    try {
      const [list, workflows] = await Promise.all([api.recipes({signal:listController.signal}), api.workflows({signal:listController.signal})]);
      if (token === listGeneration) {rows = (list.recipes || []).filter(row => !row.managed); if (!defaultRecipeId && rows.length) defaultRecipeId = rows[0].id; workflowRows = workflows.workflows || []; listingErrors = workflows.errors || [];}
    } catch (caught) {if (caught.name !== 'AbortError' && token === listGeneration) listError = caught;}
    finally {if (token === listGeneration) listLoading = false;}
  }
  async function detail(recipeId) {
    detailController?.abort(); detailController = new AbortController(); const token = ++generation;
    recipe = null; revision = null; detailError = null;
    if (!recipeId) return;
    if (recipeId === '_create' || recipeId === '_create-package') {navigate('packages'); return;}
    try {
      const loaded = await loadRecipe(recipeId,{signal:detailController.signal});
      if (token !== generation) return;
      if (isManaged(loaded.recipe)) {navigate('system','managed'); return;}
      recipe = loaded.recipe; revision = loaded.revision;
    } catch (caught) {if (caught.name !== 'AbortError' && token === generation) detailError = caught; return;}
  }
  onMount(() => {load(); return () => {listController?.abort(); detailController?.abort();};});
  $: displayId = id || defaultRecipeId;
  $: detail(displayId);
  $: visible = rows.filter(row => `${row.id} ${row.package || ''} ${row.repository || ''}`.toLowerCase().includes(query.toLowerCase()));
</script>
<ErrorNotice error={listError} retry={load} {language}/>
{#if listingErrors.length}<div class="notice error"><strong>{t('invalidRecipes',language)}</strong>{#each listingErrors as item}<p>{item.id}: {item.error?.message}</p>{/each}</div>{/if}
<div class="recipe-layout" class:mobile-editor={Boolean(id)}><aside class="panel recipe-picker"><div class="section-head"><h2>{t('recipes',language)}</h2><span class="count-label">{visible.length}</span></div><label class="search-label"><span>{t('search',language)}</span><input type="search" bind:value={query} placeholder={t('search',language)} /></label><div class="selection-list">{#if listLoading}<p>{t('loading',language)}</p>{:else}{#each visible as row (row.id)}<button class:selected={displayId===row.id} aria-current={displayId===row.id?'true':undefined} onclick={() => navigate('recipes',row.id)}><strong>{row.id}</strong><small>{row.repository || row.source} · {row.package || '—'}</small></button>{:else}{#if !listError}<div class="empty-state"><strong>{t('noItems',language)}</strong></div>{/if}{/each}{/if}</div></aside>
<div class="recipe-editor"><button class="back-button mobile-only" onclick={() => navigate('recipes')}>{t('back',language)}</button><ErrorNotice error={detailError} retry={() => detail(displayId)} {language}/>{#if recipe}<RecipeDetail {recipe} {revision} {language} writable={workflowRows.find(row => row.id === displayId)?.writable === true} saved={onSaved}/>{:else if displayId && !detailError}<p>{t('loading',language)}</p>{:else}<div class="empty-state"><strong>{t('selectRecipe',language)}</strong></div>{/if}</div></div>
