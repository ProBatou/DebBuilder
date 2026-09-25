<script>
  import {recipeRows} from '../lib/fixtures.js';
  import {translate,countMessage} from '../lib/i18n.js';
  import Provenance from '../lib/Provenance.svelte';
  import RecipePlan from '../lib/RecipePlan.svelte';
  export let locale,scenario,focusRecipe='',openModal;
  $: t=(key,values={})=>translate(locale,key,values);
  let search='';
  let selected='zoraxy';
  let mobileEditor=false;
  let sourceChoice='releaseAsset';
  let confirmed=false;
  $: filtered=recipeRows.filter(row=>row.id.toLowerCase().includes(search.toLowerCase())||row.source.toLowerCase().includes(search.toLowerCase()));
  $: recipe=recipeRows.find(row=>row.id===selected)||recipeRows[0];
  $: sourceChoice=recipe.kind;
  $: blocker=scenario==='blocker' && recipe.id==='zoraxy' && !confirmed;
  $: if(focusRecipe && recipeRows.some(row=>row.id===focusRecipe)){selected=focusRecipe;mobileEditor=true;}
  const advanced=[
    ['recipes.sourceGroup',['recipes.source','recipes.tracking','recipes.sourceChanges']],
    ['recipes.buildGroup',['recipes.commands','recipes.environment','recipes.workingDir','recipes.timeouts','recipes.ensureDirs','recipes.outputPaths']],
    ['recipes.packageGroup',['recipes.packageMeta','recipes.depends','recipes.elf','recipes.destination','recipes.mappings','recipes.permissions','recipes.directories','recipes.account','recipes.scripts']],
    ['recipes.serviceGroup',['recipes.systemd','recipes.envFiles','recipes.limits']],
    ['recipes.operationsGroup',['recipes.automation','recipes.importExport','recipes.managedRestrictions']],
  ];
  function select(row){selected=row.id;mobileEditor=true;confirmed=false;}
</script>
<div class="recipe-layout" class:mobile-editor={mobileEditor}><aside class="panel recipe-picker"><div class="section-head"><h2>{t('nav.recipes')}</h2><span class="count-label">{countMessage(locale,'recipes.recipeCount.one','recipes.recipeCount.other',filtered.length)}</span></div><label class="search-label"><span>{t('recipes.search')}</span><input type="search" bind:value={search} placeholder={t('common.search')} /></label><div class="selection-list">{#each filtered as row}<button class:selected={recipe.id===row.id} aria-current={recipe.id===row.id?'true':undefined} on:click={() => select(row)}><strong>{row.id}</strong><small>{row.source}</small></button>{/each}</div></aside>
<div class="recipe-editor"><button class="back-button mobile-only" on:click={() => mobileEditor=false}>← {t('recipes.back')}</button><div class="editor-head"><div><p class="detail-breadcrumb">{t('nav.recipes')} / <strong>{recipe.id}</strong></p><h2>{recipe.id}</h2><p>{recipe.source}</p></div><div class="editor-actions"><button class="button secondary" on:click={() => openModal('import')}>{t('recipes.import')}</button><button class="button secondary" on:click={() => openModal('export')}>{t('recipes.export')}</button><button class="button secondary" on:click={() => openModal('json')}>{t('recipes.json')}</button></div></div>
<div class="progress-strip"><div><b>1</b><span>{t('recipes.source')}</span></div><div><b>2</b><span>{t('recipes.detection')}</span></div><div class="current"><b>3</b><span>{t('recipes.plan')}</span></div><div><b>4</b><span>{t('recipes.testBuild')}</span></div></div>
<div class="editor-content"><div class="editor-main"><section class="panel"><div class="section-head"><h2>{t('recipes.source')}</h2><Provenance {locale} kind="Configured" /></div><div class="field-grid"><label><span>{t('recipes.repo')}</span><input readonly value={recipe.source}></label><label><span>{t('recipes.sourceType')}</span><select bind:value={sourceChoice}><option value="releaseAsset">{t('recipes.releaseAsset')}</option><option value="sourceArchive">{t('recipes.sourceArchive')}</option><option value="repositorySource">{t('recipes.repositorySource')}</option><option value="upstreamDeb">{t('recipes.upstreamDeb')}</option></select></label></div><div class="resolved-source"><span aria-hidden="true">✓</span><div><strong>{recipe.id==='zoraxy'?t('recipes.exact'):t('recipes.notResolved')}</strong><p>{recipe.id==='zoraxy'?t('recipes.previousTest'):t('recipes.resolveDuringTest')} {#if recipe.id==='zoraxy'}<code>v3.1.4 · zoraxy_linux_amd64</code>{/if}</p></div><Provenance {locale} kind={recipe.id==='zoraxy'?'Resolved':'Unknown'} /></div></section>
<section class="panel"><div class="section-head"><h2>{t('recipes.detection')}</h2><Provenance {locale} kind="Detected" /></div><div class="detected-grid"><div><span>{t('recipes.detected')}</span><strong>{t('recipes.prebuilt')}</strong></div><div><span>{t('recipes.planBuild')}</span><strong>{t('recipes.noCompile')}</strong></div><div><span>{t('recipes.runtimeDeps')}</span><strong>{t('recipes.runtimeAfter')}</strong></div></div></section>
<RecipePlan {locale} {recipe} {blocker} {confirmed} onConfirm={() => confirmed=true} />
<section class="panel advanced-panel"><div class="section-head"><div><h2>{t('recipes.advanced')}</h2><p>{t('recipes.advancedHelp')}</p></div></div><div class="advanced-groups">{#each advanced as [group,items]}<details class="advanced-group" open={scenario==='long'}><summary><strong>{t(group)}</strong><span>{items.length}</span></summary><div class="advanced-fields">{#each items as key}<div><span>{t(key)}</span><Provenance {locale} kind="Configured" /></div>{/each}</div></details>{/each}</div><p class="field-help">{t('recipes.advancedFixture')}</p></section></div>
<aside class="next-step panel"><p class="eyebrow">{t('recipes.next')}</p><h2>{t(blocker?'recipes.confirmDir':'recipes.testPlan')}</h2><p>{t(blocker?'recipes.dirReason':'recipes.testBoundary')}</p><button class="button primary" disabled={blocker || scenario==='recovery'} on:click={() => openModal('test')}>{t('recipes.testPlan')} →</button><button class="button secondary" disabled={blocker || scenario==='recovery'} on:click={() => openModal('build')}>{t('recipes.buildPackage')}</button></aside></div></div></div>
