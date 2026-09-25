<script>export let value = ''; export let language = 'en'; import {t} from '../i18n/i18n.js';</script>
<span class:bad={/fail|error|block/.test(value)} class:good={/success|complete|published|ready|ok/.test(value)} class="chip">● {t(value,language)}</span>
