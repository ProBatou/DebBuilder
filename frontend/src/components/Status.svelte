<script>
  export let value = '', language = 'en';
  import {statusLabel} from '../i18n/i18n.js';
  $: label = statusLabel(value,language);
</script>
<span class:bad={/fail|error|block/.test(value)} class:good={/success|complete|published|ready|ok/.test(value)} class="chip">● {label}</span>
