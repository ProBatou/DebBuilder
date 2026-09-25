<script>
  import {translate} from './i18n.js';
  export let locale='en',title='',rows=[],fields=[],ordered=false,empty='',guidance='';
  $: t=(key)=>translate(locale,key);
  function update(index,key,value){rows=rows.map((row,i)=>i===index?(fields.length?{...row,[key]:value}:value):row);}
  function add(){rows=[...rows,fields.length?Object.fromEntries(fields.map(field=>[field.key,field.default??''])):''];}
  function remove(index){rows=rows.filter((_,i)=>i!==index);}
  function move(index,delta){const next=[...rows];[next[index],next[index+delta]]=[next[index+delta],next[index]];rows=next;}
</script>
<div class="recipe-list-editor">
  <div class="recipe-list-head"><div><h4>{title}</h4>{#if guidance}<small>{guidance}</small>{/if}</div><button type="button" class="button secondary" on:click={add}>+ {t('recipeUx.add')}</button></div>
  {#if !rows.length}<p class="recipe-empty">{empty||t('recipeUx.none')}</p>{/if}
  {#each rows as row,index}
    <div class="recipe-list-row">
      <span class="recipe-row-number">{index+1}</span>
      <div class="recipe-row-fields">
        {#if fields.length}
          {#each fields as field}
            {#if !field.showWhen||field.showWhen(row)}<label><span>{t(field.label)}</span>
              {#if field.options}
                <select value={row[field.key]??''} on:change={(event)=>update(index,field.key,event.currentTarget.value)}>{#each field.options as option}<option value={option.value}>{t(option.label)}</option>{/each}</select>
              {:else if field.multiline}
                <textarea rows="3" value={row[field.key]??''} on:input={(event)=>update(index,field.key,event.currentTarget.value)}></textarea>
              {:else}
                <input type={field.type||'text'} value={row[field.key]??''} placeholder={field.placeholder||''} on:input={(event)=>update(index,field.key,event.currentTarget.value)} />
              {/if}
            </label>{/if}
          {/each}
        {:else}<label><span>{title} {index+1}</span><input value={row} on:input={(event)=>update(index,'',event.currentTarget.value)} /></label>{/if}
      </div>
      <div class="recipe-row-actions">{#if ordered}<button type="button" aria-label={`${t('recipeUx.moveUp')} ${index+1}`} disabled={index===0} on:click={() => move(index,-1)}>↑</button><button type="button" aria-label={`${t('recipeUx.moveDown')} ${index+1}`} disabled={index===rows.length-1} on:click={() => move(index,1)}>↓</button>{/if}<button type="button" aria-label={`${t('recipeUx.remove')} ${index+1}`} on:click={() => remove(index)}>×</button></div>
    </div>
  {/each}
</div>
