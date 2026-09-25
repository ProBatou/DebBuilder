<script>
  import {translate} from './i18n.js';
  import StatusChip from './StatusChip.svelte';
  import Provenance from './Provenance.svelte';
  export let locale,recipe,blocker=false,confirmed=false,onConfirm=()=>{};
  $: t=(key,values={})=>translate(locale,key,values);
  $: rows=[
    ['recipes.planSource',recipe.source+' · '+(recipe.id==='zoraxy'?'v3.1.4':t('recipes.notResolved')),recipe.id==='zoraxy'?'Resolved':'Unknown'],
    ['recipes.planBuild',t('recipes.noCompile'),'Detected'],
    ['recipes.planOutput',recipe.id.replaceAll('-','_')+'_linux_amd64','Suggested'],
    ['recipes.planInstall','/usr/local/bin/'+recipe.id+' · 0755','Configured'],
    ['recipes.planService',recipe.id==='zoraxy'?'zoraxy.service':t('recipes.noService'),'Configured'],
    ['recipes.workingDir','/opt/'+recipe.id+' · root:root · 0755',confirmed?'Configured':blocker?'Suggested':'Resolved'],
    ['recipes.planDependencies',t('recipes.runtimeAfter'),'Unknown'],
    ['recipes.planSupport',t('recipes.notChecked'),'Unknown'],
  ];
</script>
<section class="panel plan-panel"><div class="section-head"><h2>{t('recipes.planSummary')}</h2><StatusChip label={t(blocker?'scenario.blocker':'recipes.readyTest')} tone={blocker?'danger':'success'} icon={blocker?'!':'✓'} /></div><div class="plan-rows">{#each rows as [key,value,origin]}<div><span>{t(key)}</span><strong>{value}</strong><Provenance {locale} kind={origin} /></div>{/each}</div>{#if blocker}<div class="inline-alert" role="alert"><strong>{t('recipes.dirBlocker')}</strong><p>{t('recipes.dirReason')}</p><button class="button secondary" on:click={onConfirm}>{t('recipes.confirmDir')}</button></div>{:else}<div class="inline-note">✓ {confirmed?t('recipes.dirConfirmed'):t('recipes.testBoundary')}</div>{/if}</section>
