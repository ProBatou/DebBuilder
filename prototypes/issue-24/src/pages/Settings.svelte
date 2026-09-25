<script>
  import {translate,localeNames} from '../lib/i18n.js';
  export let locale,theme,changeTheme,changeLocale,openModal;
  $: t=(key,values={})=>translate(locale,key,values);
  let tab='general';
  const fields={
    general:[['settings.appName','DebBuilder'],['settings.publicUrl','https://packages.example.org']],
    repository:[['settings.repoUrl','https://packages.example.org/debian'],['settings.suite','Luminous'],['settings.component','main'],['settings.arch','amd64']],
    github:[['settings.githubToken','••••••••']],
    auth:[['settings.issuer','https://id.example.org'],['settings.clientId','debbuilder'],['settings.redirect','https://packages.example.org/auth/callback'],['settings.clientSecret','••••••••']],
    notifications:[['settings.ntfyServer','https://ntfy.example.org'],['settings.topic','debbuilder'],['settings.ntfyToken','••••••••']],
    automation:[['settings.autoValidate','settings.enabledFixture'],['settings.autoPublish','settings.disabledFixture']],
  };
</script>
<div class="settings-layout"><nav class="settings-nav" aria-label={t('nav.settings')}>{#each ['general','repository','github','auth','notifications','automation','advanced'] as key}<button class:active={tab===key} aria-current={tab===key?'page':undefined} on:click={() => tab=key}>{t('settings.'+key)}</button>{/each}</nav><div class="settings-content">
{#if tab==='general'}<section class="panel"><h2>{t('settings.appearance')}</h2><div class="field-grid"><label><span>{t('settings.theme')}</span><select value={theme} on:change={(event)=>changeTheme(event.currentTarget.value)}><option value="system">{t('settings.themeSystem')}</option><option value="light">{t('settings.themeLight')}</option><option value="dark">{t('settings.themeDark')}</option></select></label><label><span>{t('settings.language')}</span><select value={locale} on:change={(event)=>changeLocale(event.currentTarget.value)}>{#each Object.entries(localeNames) as [code,name]}<option value={code}>{name}</option>{/each}</select></label></div><p class="field-help">{t('settings.localHelp')}</p></section>{/if}
{#if tab==='advanced'}<section class="panel"><h2>{t('settings.advanced')}</h2><p class="muted">{t('settings.advancedHelp')}</p></section>{:else}<section class="panel"><div class="section-head"><h2>{t('settings.'+tab)}</h2><span class="muted">{t('settings.fixtureHelp')}</span></div><div class="settings-fields">{#each fields[tab] as [key,value]}<div><span>{t(key)}</span><strong>{value.startsWith('settings.')?t(value):value}</strong></div>{/each}</div>{#if tab==='auth'}<p class="field-help">{t('settings.authMode')} {t('settings.secretHelp')}</p>{/if}{#if tab==='github'||tab==='notifications'}<p class="field-help">{t('settings.secretHelp')}</p>{/if}{#if tab==='notifications'}<button class="button secondary" on:click={() => openModal('notification')}>{t('settings.testNotification')}</button>{/if}</section>{/if}</div></div>
