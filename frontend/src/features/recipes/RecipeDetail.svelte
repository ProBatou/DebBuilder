<script>
  import {recipePlan} from './model.js';
  import StructuredValue from './StructuredValue.svelte';
  import ErrorNotice from '../../components/ErrorNotice.svelte';
  import {t} from '../../i18n/i18n.js';
  export let recipe, inspection = null, automation = null, inspectionError = null, automationError = null, language = 'en';
  $: plan = recipePlan(recipe);
  const groups = [
    ['source',['Source','Tracking','Ref','Version policy','Artifact mode','Build strategy','Output']],
    ['packages',['Package','Architecture','Install destination','Install content','Installation strategy','Ownership','Account']],
    ['runtime',['Service','Runtime Depends','ELF detection','Automation','Resources','Lifecycle hooks']],
  ];
  $: planGroups = groups.map(([name,labels]) => ({name, facts: plan.summary.filter(([label]) => labels.includes(label))}));
</script>
<section class="panel">
  <div class="section-head"><h2>{recipe.name}</h2><span class="chip">{t('readOnly',language)}</span></div>
  <p class="muted">{recipe.active === false ? t('inactive',language) : t('active',language)} · Recipe v{recipe.schema_version}</p>
  <h3>{t('plan',language)}</h3>
  <p class="muted">{t('noMatchingEvidence',language)} · {t('testRequired',language)}</p>
  <div class="plan-grid">{#each planGroups as group}<section><h3>{t(group.name,language)}</h3><dl class="plan-facts">{#each group.facts as [label,value,provenance]}<dt>{t(label,language)} <small>· {t(provenance,language)}</small></dt><dd><StructuredValue {value}/></dd>{/each}</dl></section>{/each}</div>
  {#if plan.mode === 'source_build'}<p>{t('sourceBuild',language)} · {plan.build.commands?.length || 0} {t('buildCommands',language)}</p>{:else}<p>{t('prebuilt',language)} · {plan.mode}</p>{/if}
  {#if !plan.service.enabled}<p>{t('serviceNotConfigured',language)}</p>{/if}
  {#if plan.pkg.runtime_dependency_detection?.overrides?.length}<details><summary>{t('elfOverrides',language)}</summary><StructuredValue value={plan.pkg.runtime_dependency_detection.overrides}/></details>{/if}
</section>
<section class="panel"><h2>{t('inspection',language)}</h2><ErrorNotice error={inspectionError} {language}/>{#if inspection}<StructuredValue value={inspection}/>{:else if !inspectionError}<p>{t('loading',language)}</p>{/if}</section>
<section class="panel"><h2>{t('automation',language)}</h2><ErrorNotice error={automationError} {language}/>{#if automation}<StructuredValue value={automation}/>{:else if !automationError}<p>{t('loading',language)}</p>{/if}</section>
<section class="panel"><h2>{t('advanced',language)}</h2><p class="muted">{t('readOnly',language)}</p>{#each plan.sections as [label,value]}<details><summary>{label}</summary><StructuredValue {value}/></details>{/each}</section>
<details class="panel"><summary>{t('canonicalJson',language)}</summary><pre class="facts">{JSON.stringify(recipe,null,2)}</pre></details>
