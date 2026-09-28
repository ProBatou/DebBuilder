<script>
  import {statusLabel} from '../i18n/i18n.js';
  import StatusChip from './StatusChip.svelte';
  export let value = '', language = 'en';
  $: tone = /fail|error|block/.test(value) ? 'danger' : /warn|needed|update/.test(value) ? 'warning' : /run|queue|active/.test(value) ? 'info' : /success|complete|published|ready|ok|up_to_date|validated|prepared/.test(value) ? 'success' : 'neutral';
  $: icon = tone === 'danger' ? '!' : tone === 'success' ? '✓' : tone === 'info' ? '◌' : '●';
</script>
<StatusChip label={statusLabel(value,language)} {tone} {icon}/>
