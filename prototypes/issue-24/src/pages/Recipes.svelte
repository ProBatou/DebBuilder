<script>
  import {tick} from 'svelte';
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
  let activeCapability='';
  let capabilityDraft='';
  let capabilityError='';
  let capabilitySaved={};
  let drawer;
  let capabilityTrigger;
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
  const capabilityDefaults={
    'recipes.commands':'npm ci\nnpm run build',
    'recipes.environment':'NODE_ENV=production',
    'recipes.workingDir':'/app',
    'recipes.timeouts':'1800',
    'recipes.elf':'auto',
    'recipes.automation':'true',
    'recipes.packageMeta':'Package description for this build',
    'recipes.systemd':'debbuilder-worker.service'
  };
  function capabilityStatus(key){
    const value=capabilitySaved[`${recipe.id}:${key}`]??capabilityDefaults[key]??'';
    if(key==='recipes.managedRestrictions')return 'Disabled';
    if(key==='recipes.automation')return value==='true'?'Enabled':'Disabled';
    if(value)return 'Configured';
    return ['recipes.tracking','recipes.elf','recipes.limits'].includes(key)?'Default':'None';
  }
  function capabilityPreview(key){const value=capabilitySaved[`${recipe.id}:${key}`]??capabilityDefaults[key]??'';return value&&key!=='recipes.automation'?value.replaceAll('\n',' · '):t('recipes.capabilitySummary.'+key.split('.')[1]);}
  async function openCapability(key){capabilityTrigger=document.activeElement;activeCapability=key;capabilityDraft=capabilitySaved[`${recipe.id}:${key}`]??capabilityDefaults[key]??'';capabilityError='';await tick();drawer?.focus();}
  async function closeCapability(){activeCapability='';capabilityError='';await tick();capabilityTrigger?.focus();}
  function saveCapability(){
    if(activeCapability==='recipes.timeouts' && (!Number.isInteger(Number(capabilityDraft))||Number(capabilityDraft)<1||Number(capabilityDraft)>86400)){capabilityError=t('recipes.capabilityRange');return;}
    if(activeCapability==='recipes.workingDir' && capabilityDraft && !capabilityDraft.startsWith('/')){capabilityError=t('recipes.capabilityPath');return;}
    capabilitySaved={...capabilitySaved,[`${recipe.id}:${activeCapability}`]:capabilityDraft};closeCapability();
  }
  function select(row){selected=row.id;mobileEditor=true;confirmed=false;activeCapability='';}
</script>
<svelte:window on:keydown={(event)=>{if(!activeCapability)return;if(event.key==='Escape'){event.preventDefault();closeCapability();}else if(event.key==='Tab'&&drawer){const focusables=[...drawer.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')];if(!focusables.length)return;const first=focusables[0],last=focusables.at(-1);if(event.shiftKey&&(document.activeElement===first||document.activeElement===drawer)){event.preventDefault();last.focus();}else if(!event.shiftKey&&(document.activeElement===last||document.activeElement===drawer)){event.preventDefault();first.focus();}}}} />
<div class="recipe-layout" class:mobile-editor={mobileEditor}><aside class="panel recipe-picker"><div class="section-head"><h2>{t('nav.recipes')}</h2><span class="count-label">{countMessage(locale,'recipes.recipeCount.one','recipes.recipeCount.other',filtered.length)}</span></div><label class="search-label"><span>{t('recipes.search')}</span><input type="search" bind:value={search} placeholder={t('common.search')} /></label><div class="selection-list">{#each filtered as row}<button class:selected={recipe.id===row.id} aria-current={recipe.id===row.id?'true':undefined} on:click={() => select(row)}><strong>{row.id}</strong><small>{row.source}</small></button>{/each}</div></aside>
<div class="recipe-editor"><button class="back-button mobile-only" on:click={() => mobileEditor=false}>← {t('recipes.back')}</button><div class="editor-head"><div><p class="detail-breadcrumb">{t('nav.recipes')} / <strong>{recipe.id}</strong></p><h2>{recipe.id}</h2><p>{recipe.source}</p></div><div class="editor-actions"><details class="recipe-menu"><summary class="button secondary">{t('recipes.moreActions')}</summary><div role="group" aria-label={t('recipes.moreActions')}><button on:click={() => openModal('import')}>{t('recipes.import')}</button><button on:click={() => openModal('export')}>{t('recipes.export')}</button><button on:click={() => openModal('json')}>{t('recipes.json')}</button></div></details></div></div>
<div class="progress-strip"><div><b>1</b><span>{t('recipes.source')}</span></div><div><b>2</b><span>{t('recipes.detection')}</span></div><div class="current"><b>3</b><span>{t('recipes.plan')}</span></div><div><b>4</b><span>{t('recipes.testBuild')}</span></div></div>
<div class="editor-content"><div class="editor-main"><section class="panel"><div class="section-head"><h2>{t('recipes.source')}</h2><Provenance {locale} kind="Configured" /></div><div class="field-grid"><label><span>{t('recipes.repo')}</span><input readonly value={recipe.source}></label><label><span>{t('recipes.sourceType')}</span><select bind:value={sourceChoice}><option value="releaseAsset">{t('recipes.releaseAsset')}</option><option value="sourceArchive">{t('recipes.sourceArchive')}</option><option value="repositorySource">{t('recipes.repositorySource')}</option><option value="upstreamDeb">{t('recipes.upstreamDeb')}</option></select></label></div><div class="resolved-source"><span aria-hidden="true">✓</span><div><strong>{recipe.id==='zoraxy'?t('recipes.exact'):t('recipes.notResolved')}</strong><p>{recipe.id==='zoraxy'?t('recipes.previousTest'):t('recipes.resolveDuringTest')} {#if recipe.id==='zoraxy'}<code>v3.1.4 · zoraxy_linux_amd64</code>{/if}</p></div><Provenance {locale} kind={recipe.id==='zoraxy'?'Resolved':'Unknown'} /></div></section>
<section class="panel"><div class="section-head"><h2>{t('recipes.detection')}</h2><Provenance {locale} kind="Detected" /></div><div class="detected-grid"><div><span>{t('recipes.detected')}</span><strong>{t('recipes.prebuilt')}</strong></div><div><span>{t('recipes.planBuild')}</span><strong>{t('recipes.noCompile')}</strong></div><div><span>{t('recipes.runtimeDeps')}</span><strong>{t('recipes.runtimeAfter')}</strong></div></div></section>
<RecipePlan {locale} {recipe} {blocker} {confirmed} onConfirm={() => confirmed=true} />
<section class="panel advanced-panel"><div class="section-head"><div><h2>{t('recipes.advanced')}</h2><p>{t('recipes.advancedHelp')}</p></div></div><div class="advanced-groups">{#each advanced as [group,items]}<details class="advanced-group" open={scenario==='long'}><summary><strong>{t(group)}</strong><span>{items.length}</span></summary><div class="advanced-fields">{#each items as key}<button class="capability-row" on:click={() => openCapability(key)}><span><strong>{t(key)}</strong><small>{capabilitySaved[`${recipe.id}:${key}`] !== undefined ? capabilitySaved[`${recipe.id}:${key}`].replaceAll('\n',' · ') : capabilityPreview(key)}</small></span><Provenance {locale} kind={capabilityStatus(key)} /><span aria-hidden="true">›</span></button>{/each}</div></details>{/each}</div><p class="field-help">{t('recipes.advancedFixture')}</p></section></div>
<aside class="next-step panel"><p class="eyebrow">{t('recipes.next')}</p><h2>{t(blocker?'recipes.confirmDir':'recipes.testPlan')}</h2><p>{t(blocker?'recipes.dirReason':'recipes.testBoundary')}</p><button class="button primary" disabled={blocker || scenario==='recovery'} on:click={() => openModal('test')}>{t('recipes.testPlan')} →</button><button class="button secondary" disabled={blocker || scenario==='recovery'} on:click={() => openModal('build')}>{t('recipes.buildPackage')}</button></aside></div></div></div>
{#if activeCapability}<div class="capability-overlay" role="presentation" on:click={closeCapability}></div><div bind:this={drawer} tabindex="-1" class="capability-drawer" role="dialog" aria-modal="true" aria-label={t(activeCapability)}><div class="capability-head"><button class="back-button" on:click={closeCapability}>← {t('common.back')}</button><h2>{t(activeCapability)}</h2><Provenance {locale} kind={capabilityStatus(activeCapability)} /></div><p>{t('recipes.capabilitySummary.'+activeCapability.split('.')[1])}</p><strong class:dirty={capabilityDraft!==(capabilitySaved[`${recipe.id}:${activeCapability}`]??capabilityDefaults[activeCapability]??'')} class="capability-save-status">{t(capabilityDraft===(capabilitySaved[`${recipe.id}:${activeCapability}`]??capabilityDefaults[activeCapability]??'')?'editor.saved':'editor.unsaved')}</strong>
{#if activeCapability==='recipes.managedRestrictions'}<div class="inline-alert">{t('recipes.capabilityManaged')}</div>
{:else if activeCapability==='recipes.automation'}<label class="capability-toggle"><input type="checkbox" checked={capabilityDraft==='true'} on:change={(event)=>capabilityDraft=event.currentTarget.checked?'true':'false'} />{t('recipes.automation')}</label>
{:else if activeCapability==='recipes.elf'}<label class="capability-field">{t(activeCapability)}<select bind:value={capabilityDraft}><option value="auto">{t('recipes.capabilityAuto')}</option><option value="enabled">{t('provenance.Enabled')}</option><option value="disabled">{t('provenance.Disabled')}</option></select></label>
{:else if activeCapability==='recipes.timeouts'||activeCapability==='recipes.limits'}<label class="capability-field">{t(activeCapability)}<input type="number" min="1" max="86400" bind:value={capabilityDraft} /></label>
{:else if ['recipes.workingDir','recipes.destination','recipes.account','recipes.tracking'].includes(activeCapability)}<label class="capability-field">{t(activeCapability)}<input type="text" bind:value={capabilityDraft} /></label>
{:else}<label class="capability-field">{t(activeCapability)}<textarea rows="7" bind:value={capabilityDraft}></textarea></label>{/if}
{#if capabilityError}<p class="field-error" role="alert">{capabilityError}</p>{/if}
{#if activeCapability!=='recipes.managedRestrictions'}<div class="capability-actions"><button class="button primary" on:click={saveCapability} disabled={capabilityDraft===(capabilitySaved[`${recipe.id}:${activeCapability}`]??capabilityDefaults[activeCapability]??'')}>{t('editor.save')}</button><button class="button secondary" on:click={closeCapability}>{t('editor.cancel')}</button></div>{/if}</div>{/if}
