<script>
  export let value;
</script>
{#if Array.isArray(value)}
  {#if value.length}<ol class="structured">{#each value as item}<li><svelte:self value={item}/></li>{/each}</ol>{:else}<span>—</span>{/if}
{:else if value && typeof value === 'object'}
  {#if Object.keys(value).length}<dl class="structured">{#each Object.entries(value) as [key,item]}<dt>{key}</dt><dd><svelte:self value={item}/></dd>{/each}</dl>{:else}<span>—</span>{/if}
{:else}<span>{value === true ? '✓' : value === false ? '—' : value ?? '—'}</span>{/if}
