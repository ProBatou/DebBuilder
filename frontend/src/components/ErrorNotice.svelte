<script>export let error; export let retry = null; export let language = 'en'; import {t} from '../i18n/i18n.js';</script>
{#if error}<div class="notice error" role="alert"><strong>{error.code || t('unavailable', language)}</strong><p>{error.message}</p>{#if error.details?.path}<code>{error.details.path}</code>{/if}{#if retry}<button onclick={retry}>{t('retry', language)}</button>{/if}</div>{/if}
