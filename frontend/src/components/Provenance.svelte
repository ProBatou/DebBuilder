<script>
  import {t} from '../i18n/i18n.js';
  export let kind = 'Unknown';
  export let language = 'en';
  $: tone = String(kind).toLowerCase().split(' ')[0];
</script>
<span class={'provenance '+tone}>{t(kind,language)}</span>
