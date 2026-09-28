<script>
  import {statusLabel} from '../i18n/i18n.js';
  import StatusChip from './StatusChip.svelte';
  import {statusSemantics} from './statusSemantics.js';
  export let value = '', language = 'en';
  $: ({tone,icon} = statusSemantics(value));
</script>
<StatusChip label={statusLabel(value,language)} {tone} {icon}/>
