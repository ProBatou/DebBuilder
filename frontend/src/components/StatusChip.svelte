<script>
  export let label;
  export let tone = 'neutral';
  export let icon = '●';
</script>
<span class={'status-chip '+tone}><span class="status-icon" aria-hidden="true">{icon}</span>{label}</span>
