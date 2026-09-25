<script>
  import {translate} from './i18n.js';
  export let kind = 'Unknown';
  export let locale = 'en';
</script>
<span class={'provenance '+kind.toLowerCase()}>{translate(locale,'provenance.'+kind)}</span>
