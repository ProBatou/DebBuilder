<script>
  import {tick} from 'svelte';
  import {readPath, change, remove, move} from './draft.js';
  import {t} from '../../i18n/i18n.js';
  export let entry, editor, update, disabled = false, fieldError = null, language = 'en';
  let root;
  $: value = readPath(editor.draft, entry.path);
  $: label = entry.path.split('.').at(-1).replaceAll('_',' ');
  $: errorId = `recipe-error-${entry.path.replace(/[^A-Za-z0-9]/g,'-')}`;
  const templates = {
    'build.source_changes': {operation:'replace',path:'',search:'',content:''},
    'install.config_files': {source:'',destination:'',policy:'dpkg_conffile'},
    'install.directories': {path:'',owner:'root',group:'root',mode:'0755'},
    'package.runtime_dependency_detection.overrides': {soname:'',action:'ignore',reason:'',relation:''},
    'runtime_apt_repositories': {id:'',uri:'',suite:'',components:[],signing_key:{armored:''}},
  };
  function set(path, next) {update(change(editor,path,next));}
  async function addRow() {set(entry.path,[...(value || []), structuredClone(templates[entry.path] || '')]); await tick(); root.querySelector('.recipe-row:last-of-type input, .recipe-row:last-of-type textarea')?.focus();}
  async function removeRow(index) {update(remove(editor,`${entry.path}[${index}]`)); await tick(); (root.querySelectorAll('.recipe-row input')[Math.min(index, (value || []).length - 1)] || root.querySelector('button'))?.focus();}
  function editObject(index, key, next) {set(`${entry.path}[${index}].${key}`,next);}
  function editEnvironment(oldKey, newKey, nextValue) {
    set(entry.path,Object.fromEntries([...Object.entries(value || {}).filter(([key]) => key !== oldKey),[newKey,nextValue]]));
  }
  async function removeEnvironment(key) {set(entry.path,Object.fromEntries(Object.entries(value || {}).filter(([name]) => name !== key))); await tick(); root.querySelector('.recipe-row input, fieldset > button')?.focus();}
  async function addEnvironment() {let i=1;while(Object.hasOwn(value || {},`NEW_KEY_${i}`)) i++;editEnvironment('',`NEW_KEY_${i}`,'');await tick();root.querySelector('.recipe-row:last-of-type input')?.focus();}
</script>
<div class="recipe-field" class:collection={entry.type === 'strings' || entry.type === 'objects' || entry.type === 'environment'} data-recipe-path={entry.path} bind:this={root}>
  {#if entry.type === 'boolean'}
    <label><input type="checkbox" checked={value === true} {disabled} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? errorId : undefined} onchange={e => set(entry.path,e.currentTarget.checked)}> {label}</label>
  {:else if entry.type === 'select'}
    <label>{label}<select value={value ?? ''} {disabled} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? errorId : undefined} onchange={e => set(entry.path,e.currentTarget.value)}>{#each entry.options as option}<option value={option}>{option}</option>{/each}</select></label>
  {:else if entry.type === 'multiline'}
    <label>{label}<textarea value={value ?? ''} {disabled} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? errorId : undefined} oninput={e => set(entry.path,e.currentTarget.value)}></textarea></label>
  {:else if entry.type === 'nullable-number'}
    <label>{label}<input type="number" value={value ?? ''} {disabled} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? errorId : undefined} oninput={e => set(entry.path,e.currentTarget.value === '' ? null : Number(e.currentTarget.value))}></label>
  {:else if entry.type === 'strings' || entry.type === 'objects'}
    <fieldset aria-describedby={fieldError ? errorId : undefined}><legend>{label}</legend>
      {#if !(value || []).length}<p class="recipe-empty">{t('noItems',language)}</p>{/if}
      {#each value || [] as row, index (index)}
        <div class="recipe-row">
          {#if entry.type === 'strings'}
            <label>{label} {index + 1}<input value={row} {disabled} oninput={e => set(`${entry.path}[${index}]`,e.currentTarget.value)}></label>
          {:else}
            {#each Object.entries(row) as [key, item]}
              {#if typeof item === 'string'}<label>{key.replaceAll('_',' ')}<input value={item} {disabled} oninput={e => editObject(index,key,e.currentTarget.value)}></label>
              {:else if typeof item === 'boolean'}<label><input type="checkbox" checked={item} {disabled} onchange={e => editObject(index,key,e.currentTarget.checked)}> {key}</label>
              {:else if Array.isArray(item)}<fieldset><legend>{key}</legend>{#each item as child, childIndex}<div class="recipe-row"><label>{key} {childIndex + 1}<input value={child} {disabled} oninput={e => set(`${entry.path}[${index}].${key}[${childIndex}]`,e.currentTarget.value)}></label>{#if !disabled}<button type="button" onclick={() => update(remove(editor,`${entry.path}[${index}].${key}[${childIndex}]`))} aria-label={`${t('remove',language)} ${key} ${childIndex + 1}`}>×</button>{/if}</div>{/each}{#if !disabled}<button type="button" class="button secondary recipe-add" onclick={() => set(`${entry.path}[${index}].${key}`,[...item,''])}>+ {t('add',language)} {key}</button>{/if}</fieldset>
              {:else if item && typeof item === 'object'}<fieldset><legend>{key}</legend>{#each Object.entries(item) as [nestedKey,nestedValue]}<label>{nestedKey}<textarea value={nestedValue} {disabled} oninput={e => set(`${entry.path}[${index}].${key}.${nestedKey}`,e.currentTarget.value)}></textarea></label>{/each}</fieldset>
              {:else}<span>{key}: {String(item)}</span>{/if}
            {/each}
          {/if}
          {#if !disabled}<div class="recipe-row-actions"><button type="button" disabled={index===0} onclick={() => update(move(editor,entry.path,index,index-1))} aria-label={`${t('move',language)} ${label} ${index + 1} ${t('up',language)}`}>↑</button><button type="button" disabled={index===(value.length-1)} onclick={() => update(move(editor,entry.path,index,index+1))} aria-label={`${t('move',language)} ${label} ${index + 1} ${t('down',language)}`}>↓</button><button type="button" onclick={() => removeRow(index)} aria-label={`${t('remove',language)} ${label} ${index + 1}`}>×</button></div>{/if}
        </div>
      {/each}
      {#if !disabled}<button type="button" class="button secondary recipe-add" onclick={addRow}>+ {t('add',language)} {label}</button>{/if}
    </fieldset>
  {:else if entry.type === 'environment'}
    <fieldset><legend>{label}</legend>
      {#each Object.entries(value || {}) as [key, item] (key)}<div class="recipe-row"><label>{t('key',language)}<input value={key} {disabled} onchange={e => editEnvironment(key,e.currentTarget.value,item)}></label><label>{t('value',language)}<input value={item} {disabled} oninput={e => editEnvironment(key,key,e.currentTarget.value)}></label>{#if !disabled}<button type="button" onclick={() => removeEnvironment(key)} aria-label={`${t('remove',language)} ${key}`}>×</button>{/if}</div>{/each}
      {#if !disabled}<button type="button" class="button secondary recipe-add" onclick={addEnvironment}>+ {t('add',language)} {label}</button>{/if}
    </fieldset>
  {:else}
    <label>{label}<input value={value ?? ''} {disabled} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? errorId : undefined} oninput={e => set(entry.path,e.currentTarget.value)}></label>
  {/if}
</div>
