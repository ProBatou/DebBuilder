<script>
  import RecipeField from './RecipeField.svelte';
  import {fields, applicable, ownership} from './fields.js';
  import {changedPaths} from './draft.js';
  import {t} from '../../i18n/i18n.js';
  export let editor, update, language = 'en', errors = null;
  export let section = 'plan';
  $: visible = fields.filter(entry => entry.section === section && applicable(entry,editor.draft));
  $: changes = changedPaths(editor);
  function errorsForField(path) {return Object.entries(errors?.fields || {}).filter(([key]) => key === `$.${path}` || key.startsWith(`$.${path}.`) || key.startsWith(`$.${path}[`)).flatMap(([,items]) => items);}
</script>
<div class="recipe-editor">
  <div class="tabs" role="group" aria-label="Recipe editor sections">
    {#each ['plan','customize','advanced','expert'] as tab}<button type="button" class:active={section===tab} onclick={() => section=tab}>{t(tab,language)}</button>{/each}
    <button type="button" class:active={section==='review'} onclick={() => section='review'}>{t('reviewChanges',language)}</button>
  </div>
  {#if section === 'review'}
    <section><h3>{t('reviewChanges',language)}</h3><p>{changes.length ? changes.join(', ') : t('noChanges',language)}</p><div class="recipe-review"><details><summary>{t('loadedBaseline',language)}</summary><pre>{JSON.stringify(editor.baseline,null,2)}</pre></details><details open><summary>{t('currentDraft',language)}</summary><pre>{JSON.stringify(editor.draft,null,2)}</pre></details></div></section>
  {:else}
    <div class="recipe-fields">
      {#each visible as entry (entry.path)}
        {@const disabled = ownership(entry.path,editor.managed,editor.editablePaths) === 'READ_ONLY_MANAGED' || entry.path.endsWith('runtime_dependency_detection.overrides') && !(editor.draft.artifact?.mode === 'upstream_archive' && editor.draft.artifact?.archive_source === 'release_asset' && editor.draft.package?.architecture === 'amd64')}
        <RecipeField {entry} {editor} {update} {disabled} {language} fieldError={errorsForField(entry.path)[0]}/>
        {#each errorsForField(entry.path) as error}<p class="notice error" role="alert" id={`recipe-error-${entry.path.replace(/[^A-Za-z0-9]/g,'-')}`}>{error.path || error.details?.path || '$'}: {error.message}</p>{/each}
      {/each}
    </div>
  {/if}
</div>
