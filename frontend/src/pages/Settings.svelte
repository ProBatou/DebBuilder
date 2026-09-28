<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {locale, setLocale, t} from '../i18n/i18n.js';
  import {theme, setTheme} from '../theme/theme.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';

  export let language = 'en';
  let settings = null, error = null, tab = 'general', controller;
  const tabs = ['general','repository','github','auth','notifications','automation','advanced'];
  const names = {
    en: ['General','Repository','GitHub','Authentication','Notifications','Automation','Advanced'],
    fr: ['Général','Dépôt','GitHub','Authentification','Notifications','Automatisation','Avancé'],
    de: ['Allgemein','Repository','GitHub','Authentifizierung','Benachrichtigungen','Automatisierung','Erweitert'],
    es: ['General','Repositorio','GitHub','Autenticación','Notificaciones','Automatización','Avanzado'],
  };
  const label = key => names[language]?.[tabs.indexOf(key)] || names.en[tabs.indexOf(key)];
  const facts = {
    general: [['Application name','general.app_name'],['Public URL','general.url']],
    repository: [['Public repository URL','apt.repository'],['Distribution','apt.distribution'],['Component','apt.component'],['Architecture','apt.architecture']],
    github: [['GitHub token configured','github.token_configured']],
    auth: [['Authentication mode','security.auth_mode'],['Issuer URL','security.oidc_issuer'],['Client ID','security.oidc_client_id'],['Redirect URI','security.oidc_redirect_uri'],['Client secret configured','security.oidc_client_secret_configured']],
    notifications: [['Type','notifications.type'],['Server URL','notifications.server_url'],['Topic','notifications.topic'],['Token configured','notifications.token_configured']],
    automation: [['Auto validate after build','automation.auto_validate_after_successful_build'],['Auto publish after validation','automation.auto_publish_after_successful_validation'],['Upstream checks enabled','automation.upstream_checks_enabled'],['Check interval (seconds)','automation.upstream_check_interval_seconds'],['Check concurrency','automation.upstream_check_concurrency']],
    advanced: [['Workspace cleanup enabled','workspace_cleanup.enabled'],['Failed workspaces to retain','workspace_cleanup.failed_workspaces_to_retain']],
  };
  const factNames = {
    fr: {
      general: ['Nom de l’application','URL publique'], repository: ['URL publique du dépôt','Distribution','Composant','Architecture'],
      github: ['Jeton GitHub configuré'], auth: ['Mode d’authentification','URL de l’émetteur','Identifiant client','URI de redirection','Secret client configuré'],
      notifications: ['Type','URL du serveur','Sujet','Jeton configuré'], automation: ['Valider après la construction','Publier après la validation','Vérification amont activée','Intervalle de vérification (secondes)','Vérifications simultanées'],
      advanced: ['Nettoyage des espaces activé','Espaces en échec à conserver'],
    },
    de: {
      general: ['Anwendungsname','Öffentliche URL'], repository: ['Öffentliche Repository-URL','Distribution','Komponente','Architektur'],
      github: ['GitHub-Token konfiguriert'], auth: ['Authentifizierungsmodus','Aussteller-URL','Client-ID','Weiterleitungs-URI','Client-Geheimnis konfiguriert'],
      notifications: ['Typ','Server-URL','Thema','Token konfiguriert'], automation: ['Nach dem Build validieren','Nach der Validierung veröffentlichen','Upstream-Prüfungen aktiviert','Prüfintervall (Sekunden)','Gleichzeitige Prüfungen'],
      advanced: ['Arbeitsbereichsbereinigung aktiviert','Fehlgeschlagene Arbeitsbereiche behalten'],
    },
    es: {
      general: ['Nombre de la aplicación','URL pública'], repository: ['URL pública del repositorio','Distribución','Componente','Arquitectura'],
      github: ['Token de GitHub configurado'], auth: ['Modo de autenticación','URL del emisor','ID de cliente','URI de redirección','Secreto de cliente configurado'],
      notifications: ['Tipo','URL del servidor','Tema','Token configurado'], automation: ['Validar después de compilar','Publicar después de validar','Comprobaciones de origen activadas','Intervalo de comprobación (segundos)','Comprobaciones simultáneas'],
      advanced: ['Limpieza de espacios activada','Espacios fallidos conservados'],
    },
  };
  const factLabel = (key,index,name) => factNames[language]?.[key]?.[index] || name;
  function value(path) {
    const result = path.split('.').reduce((current, part) => current?.[part], settings);
    if (path.endsWith('_configured')) return t(result ? 'configuredValue' : 'notConfiguredValue',language);
    if (typeof result === 'boolean') return t(result ? 'enabledValue' : 'disabledValue',language);
    return result === '' || result == null ? '—' : result;
  }
  async function select(next) {tab = next; await tick(); document.querySelector('.settings-nav button.active')?.scrollIntoView({block:'nearest',inline:'nearest'});}
  async function load() {
    controller?.abort(); controller = new AbortController(); error = null;
    try {settings = (await api.settings({signal:controller.signal})).settings;}
    catch (caught) {if (caught.name !== 'AbortError') error = caught;}
  }
  onMount(() => {load(); return () => controller?.abort();});
</script>
<div class="settings-layout"><nav class="settings-nav" aria-label={t('settings',language)}>{#each tabs as key}<button class:active={tab===key} aria-current={tab===key?'page':undefined} onclick={() => select(key)}>{label(key)}</button>{/each}</nav><div class="settings-content">
<ErrorNotice {error} retry={load} {language}/>
{#if settings}
{#if tab==='general'}<section class="panel"><h2>{t('appearance',language)}</h2><div class="field-grid"><label><span>{t('theme',language)}</span><select value={$theme} onchange={event => setTheme(event.currentTarget.value)}><option value="system">{t('systemTheme',language)}</option><option value="light">{t('light',language)}</option><option value="dark">{t('dark',language)}</option></select></label><label><span>{t('language',language)}</span><select value={$locale} onchange={event => setLocale(event.currentTarget.value)}><option value="en">EN</option><option value="fr">FR</option><option value="de">DE</option><option value="es">ES</option></select></label></div><p class="field-help">{t('localPreferences',language)}</p></section>{/if}
<section class="panel"><div class="section-head"><h2>{label(tab)}</h2><span class="count-label">{t('readOnly',language)}</span></div><div class="settings-facts">{#each facts[tab] as [name,path], index}<div><span>{factLabel(tab,index,name)}</span><strong>{value(path)}</strong></div>{/each}</div></section>
{:else if !error}<p>{t('loading',language)}</p>{/if}
</div></div>
