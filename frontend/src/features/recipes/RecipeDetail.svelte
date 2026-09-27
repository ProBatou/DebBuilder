<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../../api/client.js';
  import {setNavigationGuard} from '../../navigation/location.js';
  import {hydrate, candidate, dirty, cancel, equal, validationErrors} from './draft.js';
  import {fields} from './fields.js';
  import RecipeEditor from './RecipeEditor.svelte';
  import {recipePlan} from './model.js';
  import StructuredValue from './StructuredValue.svelte';
  import ErrorNotice from '../../components/ErrorNotice.svelte';
  import {t} from '../../i18n/i18n.js';
  export let recipe, inspection = null, automation = null, inspectionError = null, automationError = null, language = 'en', writable = true, editablePaths = [];
  let editor = null, loaded = null, editing = false, validating = false, validation = null, errors = null, section = 'plan';
  let discardDialog, resolveNavigation, pendingNavigation;
  $: if (recipe !== loaded) {loaded = recipe; editor = hydrate(recipe,{managed: Boolean(recipe.management),editablePaths}); editing = false; validation = null; errors = null;}
  $: changed = editor && dirty(editor);
  function update(next) {editor = next; validation = null; errors = null;}
  function reset() {editor = cancel(editor); editing = false; validation = null; errors = null;}
  async function validate() {
    const snapshot = candidate(editor);
    const source = loaded;
    validating = true; validation = null; errors = null;
    try {const response = await api.validateRecipe(snapshot); if (editing && loaded === source && equal(editor.draft,snapshot)) validation = response;}
    catch (error) {
      if (!editing || loaded !== source || !equal(editor.draft,snapshot)) return;
      errors = validationErrors(error);
      const path = error.path || error.details?.path || '$';
      const target = fields.find(entry => path === `$.${entry.path}` || path.startsWith(`$.${entry.path}.`) || path.startsWith(`$.${entry.path}[`));
      if (target) section = target.section;
      await tick();
      const control = [...document.querySelectorAll('[data-recipe-path]')].find(node => node.dataset.recipePath === target?.path);
      control?.querySelector('input,textarea,select,button')?.focus();
    } finally {validating = false;}
  }
  function navigationGuard() {
    if (!editing || !changed) return true;
    if (pendingNavigation) return pendingNavigation;
    pendingNavigation = new Promise(resolve => {resolveNavigation = resolve; discardDialog.showModal();});
    return pendingNavigation;
  }
  function decide(discard) {discardDialog.close(); const resolve = resolveNavigation; resolveNavigation = null; pendingNavigation = null; resolve?.(discard);}
  function beforeUnload(event) {if (editing && changed) {event.preventDefault(); event.returnValue = '';}}
  onMount(() => {const release = setNavigationGuard(navigationGuard); window.addEventListener('beforeunload',beforeUnload); return () => {release(); window.removeEventListener('beforeunload',beforeUnload);};});
  $: plan = recipePlan(recipe);
  const groups = [
    ['source',['Source','Tracking','Ref','Version policy','Artifact mode','Build strategy','Output']],
    ['packages',['Package','Architecture','Install destination','Install content','Installation strategy','Ownership','Account']],
    ['runtime',['Service','Runtime Depends','ELF detection','Automation','Resources','Lifecycle hooks']],
  ];
  $: planGroups = groups.map(([name,labels]) => ({name, facts: plan.summary.filter(([label]) => labels.includes(label))}));
</script>
<dialog bind:this={discardDialog} oncancel={event => {event.preventDefault(); decide(false);}} aria-labelledby="discard-title"><h2 id="discard-title">{t('discardChanges',language)}</h2><p>{t('unsavedChanges',language)}</p><div class="actions"><button type="button" onclick={() => decide(false)}>{t('keepEditing',language)}</button><button type="button" onclick={() => decide(true)}>{t('discard',language)}</button></div></dialog>
<section class="panel">
  <div class="section-head"><h2>{recipe.name}</h2><span class="chip">{editing ? (changed ? t('unsaved',language) : t('editing',language)) : writable ? t('view',language) : t('readOnly',language)}</span></div>
  <p class="muted">{recipe.active === false ? t('inactive',language) : t('active',language)} · Recipe v{recipe.schema_version}</p>
  {#if !editing && (writable || editor.managed)}<button type="button" onclick={() => editing = true}>{t('edit',language)}</button>{/if}
  {#if editing}
    <div class="actions"><button type="button" onclick={validate} disabled={validating}>{validating ? t('validating',language) : t('validate',language)}</button><button type="button" onclick={reset}>{t('cancel',language)}</button></div>
    {#if validation}<p class="notice" role="status">{t('validated',language)}</p>{/if}
    {#if errors}<div class="notice error" role="alert"><strong>{errors.sections ? Object.keys(errors.sections).join(', ') : ''}</strong><p>{errors.global[0]?.message || Object.values(errors.sections)[0]?.[0]?.message}</p><small>{errors.global[0]?.path || Object.values(errors.sections)[0]?.[0]?.path || '$'}</small></div>{/if}
    <RecipeEditor {editor} {update} {language} {errors} bind:section/>
  {:else}
  <h3>{t('plan',language)}</h3>
  <p class="muted">{t('noMatchingEvidence',language)} · {t('testRequired',language)}</p>
  <div class="plan-grid">{#each planGroups as group}<section><h3>{t(group.name,language)}</h3><dl class="plan-facts">{#each group.facts as [label,value,provenance]}<dt>{t(label,language)} <small>· {t(provenance,language)}</small></dt><dd><StructuredValue {value}/></dd>{/each}</dl></section>{/each}</div>
  {#if plan.mode === 'source_build'}<p>{t('sourceBuild',language)} · {plan.build.commands?.length || 0} {t('buildCommands',language)}</p>{:else}<p>{t('prebuilt',language)} · {plan.mode}</p>{/if}
  {#if !plan.service.enabled}<p>{t('serviceNotConfigured',language)}</p>{/if}
  {#if plan.pkg.runtime_dependency_detection?.overrides?.length}<details><summary>{t('elfOverrides',language)}</summary><StructuredValue value={plan.pkg.runtime_dependency_detection.overrides}/></details>{/if}
  {/if}
</section>
{#if !editing}
<section class="panel"><h2>{t('inspection',language)}</h2><ErrorNotice error={inspectionError} {language}/>{#if inspection}<StructuredValue value={inspection}/>{:else if !inspectionError}<p>{t('loading',language)}</p>{/if}</section>
<section class="panel"><h2>{t('automation',language)}</h2><ErrorNotice error={automationError} {language}/>{#if automation}<StructuredValue value={automation}/>{:else if !automationError}<p>{t('loading',language)}</p>{/if}</section>
<section class="panel"><h2>{t('advanced',language)}</h2><p class="muted">{t('readOnly',language)}</p>{#each plan.sections as [label,value]}<details><summary>{label}</summary><StructuredValue {value}/></details>{/each}</section>
<details class="panel"><summary>{t('canonicalJson',language)}</summary><pre class="facts">{JSON.stringify(recipe,null,2)}</pre></details>
{/if}
