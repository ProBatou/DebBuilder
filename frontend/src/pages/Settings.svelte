<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {cached, remember, uiState, rememberUi} from '../features/sessionCache.js';
  import {locale, setLocale, t} from '../i18n/i18n.js';
  import {theme, setTheme} from '../theme/theme.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';

  export let language = 'en';
  let settings = cached('settings') || null, draft = settings ? structuredClone(settings) : null, error = null, saveError = null;
  let tab = uiState('settings').tab || 'general', saving = false, savedTab = null, controller;
  let dirty = {}, secrets = {github:'', notifications:'', auth:''};
  const tabs = ['general','repository','github','auth','notifications','automation','advanced'];
  const names = {
    en:['General','Repository','GitHub','Authentication','Notifications','Automation','Advanced'],
    fr:['Général','Dépôt','GitHub','Authentification','Notifications','Automatisation','Avancé'],
    de:['Allgemein','Repository','GitHub','Authentifizierung','Benachrichtigungen','Automatisierung','Erweitert'],
    es:['General','Repositorio','GitHub','Autenticación','Notificaciones','Automatización','Avanzado'],
  };
  const labels = {
    en:{
      general:['Application name','Public URL'],repository:['Public repository URL','Distribution','Component','Architecture'],
      github:['GitHub token'],auth:['Authentication mode','Issuer URL','Client ID','Redirect URI','Client secret'],
      notifications:['Type','Server URL','Topic','Token'],
      automation:['Auto validate after build','Auto publish after validation','Upstream checks enabled','Check interval (seconds)','Check concurrency'],
      advanced:['Workspace cleanup enabled','Failed workspaces to retain','Pressure minimum free bytes','Pressure minimum free (%)','Pressure target free bytes','Pressure target free (%)','Memory limit (bytes)','Task limit','CPU quota (%)','Read bandwidth (bytes/s)','Write bandwidth (bytes/s)'],
    },
    fr:{
      general:['Nom de l’application','URL publique'],repository:['URL publique du dépôt','Distribution','Composant','Architecture'],
      github:['Jeton GitHub'],auth:['Mode d’authentification','URL de l’émetteur','Identifiant client','URI de redirection','Secret client'],
      notifications:['Type','URL du serveur','Sujet','Jeton'],
      automation:['Valider après la construction','Publier après la validation','Vérification amont activée','Intervalle de vérification (secondes)','Vérifications simultanées'],
      advanced:['Nettoyage des espaces activé','Espaces en échec à conserver','Minimum libre sous pression (octets)','Minimum libre sous pression (%)','Cible libre après pression (octets)','Cible libre après pression (%)','Limite de mémoire (octets)','Limite de tâches','Quota processeur (%)','Débit de lecture (octets/s)','Débit d’écriture (octets/s)'],
    },
    de:{
      general:['Anwendungsname','Öffentliche URL'],repository:['Öffentliche Repository-URL','Distribution','Komponente','Architektur'],
      github:['GitHub-Token'],auth:['Authentifizierungsmodus','Aussteller-URL','Client-ID','Weiterleitungs-URI','Client-Geheimnis'],
      notifications:['Typ','Server-URL','Thema','Token'],
      automation:['Nach dem Build validieren','Nach der Validierung veröffentlichen','Upstream-Prüfungen aktiviert','Prüfintervall (Sekunden)','Gleichzeitige Prüfungen'],
      advanced:['Arbeitsbereichsbereinigung aktiviert','Fehlgeschlagene Arbeitsbereiche behalten','Druckgrenze freier Speicher (Bytes)','Druckgrenze freier Speicher (%)','Ziel freier Speicher (Bytes)','Ziel freier Speicher (%)','Speicherlimit (Bytes)','Task-Limit','CPU-Quote (%)','Lesebandbreite (Bytes/s)','Schreibbandbreite (Bytes/s)'],
    },
    es:{
      general:['Nombre de la aplicación','URL pública'],repository:['URL pública del repositorio','Distribución','Componente','Arquitectura'],
      github:['Token de GitHub'],auth:['Modo de autenticación','URL del emisor','ID de cliente','URI de redirección','Secreto de cliente'],
      notifications:['Tipo','URL del servidor','Tema','Token'],
      automation:['Validar después de compilar','Publicar después de validar','Comprobaciones de origen activadas','Intervalo de revisión (segundos)','Revisiones simultáneas'],
      advanced:['Limpieza de espacios activada','Espacios fallidos conservados','Mínimo libre bajo presión (bytes)','Mínimo libre bajo presión (%)','Objetivo libre tras presión (bytes)','Objetivo libre tras presión (%)','Límite de memoria (bytes)','Límite de tareas','Cuota de CPU (%)','Ancho de lectura (bytes/s)','Ancho de escritura (bytes/s)'],
    },
  };
  const chrome = {
    en:{save:'Save changes',saving:'Saving…',saved:'Changes saved',discard:'Discard changes',secret:'Leave blank to keep the current value.',unlimited:'Leave blank for no limit.',configured:'Configured',missing:'Not configured',unsaved:'Unsaved changes',limits:'Resource limits',failed:'Settings could not be saved'},
    fr:{save:'Enregistrer',saving:'Enregistrement…',saved:'Modifications enregistrées',discard:'Annuler les modifications',secret:'Laissez vide pour conserver la valeur actuelle.',unlimited:'Laissez vide pour ne pas fixer de limite.',configured:'Configuré',missing:'Non configuré',unsaved:'Modifications non enregistrées',limits:'Limites de ressources',failed:'Impossible d’enregistrer les paramètres'},
    de:{save:'Änderungen speichern',saving:'Speichert…',saved:'Änderungen gespeichert',discard:'Änderungen verwerfen',secret:'Leer lassen, um den aktuellen Wert beizubehalten.',unlimited:'Leer lassen für kein Limit.',configured:'Konfiguriert',missing:'Nicht konfiguriert',unsaved:'Ungespeicherte Änderungen',limits:'Ressourcenlimits',failed:'Einstellungen konnten nicht gespeichert werden'},
    es:{save:'Guardar cambios',saving:'Guardando…',saved:'Cambios guardados',discard:'Descartar cambios',secret:'Déjalo vacío para conservar el valor actual.',unlimited:'Déjalo vacío para no establecer un límite.',configured:'Configurado',missing:'Sin configurar',unsaved:'Cambios sin guardar',limits:'Límites de recursos',failed:'No se pudieron guardar los ajustes'},
  };
  const copy = key => chrome[language]?.[key] || chrome.en[key];
  const label = key => names[language]?.[tabs.indexOf(key)] || names.en[tabs.indexOf(key)];
  const fieldLabel = (key,index) => labels[language]?.[key]?.[index] || labels.en[key][index];
  const fields = {
    general:[{path:'general.app_name',required:true,maxlength:80},{path:'general.url',type:'url'}],
    repository:[{path:'apt.repository',type:'url'},{path:'apt.distribution',required:true,pattern:'[A-Za-z0-9._-]+'},{path:'apt.component',required:true,pattern:'[A-Za-z0-9._-]+'},{path:'apt.architecture',type:'select',options:['amd64','arm64','armhf','i386','all']}],
    github:[{path:'github.token',type:'secret'}],
    auth:[{path:'security.auth_mode',type:'select',options:['none','header','oidc']},{path:'security.oidc_issuer',type:'url'},{path:'security.oidc_client_id'},{path:'security.oidc_redirect_uri',type:'url'},{path:'security.oidc_client_secret',type:'secret'}],
    notifications:[{path:'notifications.type',type:'select',options:['none','ntfy']},{path:'notifications.server_url',type:'url'},{path:'notifications.topic',pattern:'[A-Za-z0-9._-]+'},{path:'notifications.token',type:'secret'}],
    automation:[{path:'automation.auto_validate_after_successful_build',type:'toggle'},{path:'automation.auto_publish_after_successful_validation',type:'toggle'},{path:'automation.upstream_checks_enabled',type:'toggle'},{path:'automation.upstream_check_interval_seconds',type:'number',min:60,max:86400},{path:'automation.upstream_check_concurrency',type:'number',min:1,max:8}],
    advanced:[{path:'workspace_cleanup.enabled',type:'toggle'},{path:'workspace_cleanup.failed_workspaces_to_retain',type:'number',min:0,max:1000,required:true},{path:'workspace_cleanup.pressure_minimum_free_bytes',type:'number',min:1,max:9007199254740991,required:true},{path:'workspace_cleanup.pressure_minimum_free_percent',type:'number',min:1,max:99,required:true},{path:'workspace_cleanup.pressure_target_free_bytes',type:'number',min:1,max:9007199254740991,required:true},{path:'workspace_cleanup.pressure_target_free_percent',type:'number',min:1,max:99,required:true},{path:'resource_limits.memory_max_bytes',type:'number',min:1},{path:'resource_limits.tasks_max',type:'number',min:1},{path:'resource_limits.cpu_quota_percent',type:'number',min:1},{path:'resource_limits.io_read_bandwidth_max_bytes_per_sec',type:'number',min:1},{path:'resource_limits.io_write_bandwidth_max_bytes_per_sec',type:'number',min:1}],
  };
  const sectionFor = {general:'general',repository:'apt',github:'github',auth:'security',notifications:'notifications',automation:'automation'};
  const secretTab = {github:'github',notifications:'notifications',security:'auth'};
  const secretStatus = {github:'github.token_configured',notifications:'notifications.token_configured',auth:'security.oidc_client_secret_configured'};
  function currentValue(path) {
    const [section, name] = path.split('.');
    if (name === 'token' || name === 'oidc_client_secret') return secrets[secretTab[section]];
    return draft?.[section]?.[name] ?? '';
  }
  function changed(field, value) {
    const [section, name] = field.path.split('.');
    if (field.type === 'secret') secrets = {...secrets,[secretTab[section]]:value};
    else draft = {...draft,[section]:{...draft[section],[name]:value}};
    if (field.path === 'automation.auto_publish_after_successful_validation' && value) draft = {...draft,automation:{...draft.automation,auto_validate_after_successful_build:true}};
    if (field.path === 'automation.auto_validate_after_successful_build' && !value) draft = {...draft,automation:{...draft.automation,auto_publish_after_successful_validation:false}};
    dirty = {...dirty,[tab]:true}; savedTab = null; saveError = null;
  }
  function discard() {
    const sections = tab === 'advanced' ? ['workspace_cleanup','resource_limits'] : [sectionFor[tab]];
    draft = {...draft,...Object.fromEntries(sections.map(section => [section,structuredClone(settings[section])]))};
    if (tab in secrets) secrets = {...secrets,[tab]:''};
    dirty = {...dirty,[tab]:false}; savedTab = null; saveError = null;
  }
  function payloadFor(key) {
    const payload = {};
    for (const field of fields[key]) {
      const [section, name] = field.path.split('.');
      let value = currentValue(field.path);
      if (field.type === 'secret') {value = value.trim(); if (!value) continue;}
      else if (field.type === 'number') value = value === '' && section === 'resource_limits' ? null : Number(value);
      else if (typeof value === 'string') value = value.trim();
      (payload[section] ||= {})[name] = value;
    }
    return payload;
  }
  async function save(event) {
    event.preventDefault();
    if (!dirty[tab] || saving || !event.currentTarget.reportValidity()) return;
    const activeTab = tab, changes = payloadFor(activeTab);
    if (!Object.keys(changes).length) {discard(); return;}
    saving = true; saveError = null; savedTab = null;
    try {
      settings = (await api.updateSettings(changes)).settings;
      remember('settings',settings);
      draft = {...draft,...Object.fromEntries(Object.keys(changes).filter(key => key !== 'github').map(key => [key,structuredClone(settings[key])]))};
      if (activeTab in secrets) secrets = {...secrets,[activeTab]:''};
      dirty = {...dirty,[activeTab]:false}; savedTab = activeTab;
    } catch (caught) {saveError = caught;}
    finally {saving = false;}
  }
  async function select(next) {tab = next; rememberUi('settings',{tab}); saveError = null; await tick(); document.querySelector('.settings-nav button.active')?.scrollIntoView({block:'nearest',inline:'nearest'});}
  async function load() {
    controller?.abort(); controller = new AbortController(); error = null;
    try {
      const received = (await api.settings({signal:controller.signal})).settings;
      if (!received || typeof received !== 'object' || Array.isArray(received)) throw new Error(t('unavailable',language));
      settings = received; remember('settings',received); draft = structuredClone(received); dirty = {}; secrets = {github:'',notifications:'',auth:''};
    }
    catch (caught) {if (caught.name !== 'AbortError') error = caught;}
  }
  onMount(() => {load(); return () => {rememberUi('settings',{tab}); controller?.abort();};});
</script>
<div class="settings-layout"><nav class="settings-nav" aria-label={t('settings',language)}>{#each tabs as key}<button class:active={tab===key} aria-current={tab===key?'page':undefined} onclick={() => select(key)}>{label(key)}{#if dirty[key]} <span class="settings-unsaved" aria-label={copy('unsaved')}>●</span>{/if}</button>{/each}</nav><div class="settings-content">
<ErrorNotice {error} retry={load} {language}/>
{#if tab==='general'}<section class="panel"><h2>{t('appearance',language)}</h2><div class="field-grid"><label><span>{t('theme',language)}</span><select value={$theme} onchange={event => setTheme(event.currentTarget.value)}><option value="system">{t('systemTheme',language)}</option><option value="light">{t('light',language)}</option><option value="dark">{t('dark',language)}</option></select></label><label><span>{t('language',language)}</span><select value={$locale} onchange={event => setLocale(event.currentTarget.value)}><option value="en">EN</option><option value="fr">FR</option><option value="de">DE</option><option value="es">ES</option></select></label></div><p class="field-help">{t('localPreferences',language)}</p></section>{/if}
{#if draft}
<section class="panel"><div class="section-head"><h2>{label(tab)}</h2></div>
  <form class="settings-form" onsubmit={save}>
    <div class="settings-edit-fields">
      {#each fields[tab] as field, index}
        {#if tab==='advanced' && index===6}<h3 class="settings-group-title">{copy('limits')}</h3>{/if}
        <label class:settings-toggle={field.type==='toggle'}>
          <span>{fieldLabel(tab,index)}</span>
          {#if field.type==='toggle'}
            <input type="checkbox" checked={!!draft?.[field.path.split('.')[0]]?.[field.path.split('.')[1]]} onchange={event => changed(field,event.currentTarget.checked)} disabled={saving}/>
          {:else if field.type==='select'}
            <select value={draft?.[field.path.split('.')[0]]?.[field.path.split('.')[1]] ?? ''} onchange={event => changed(field,event.currentTarget.value)} disabled={saving}>{#each field.options as option}<option value={option}>{option}</option>{/each}</select>
          {:else if field.type==='secret'}
            <input type="password" value={secrets[secretTab[field.path.split('.')[0]]]} oninput={event => changed(field,event.currentTarget.value)} autocomplete="new-password" required={tab==='auth' && draft.security.auth_mode==='oidc' && !settings.security.oidc_client_secret_configured} disabled={saving}/>
            <small>{copy('secret')} {settings?.[secretStatus[tab]?.split('.')[0]]?.[secretStatus[tab]?.split('.')[1]] ? copy('configured') : copy('missing')}.</small>
          {:else}
            <input type={field.type==='number'?'number':field.type==='url'?'url':'text'} value={draft?.[field.path.split('.')[0]]?.[field.path.split('.')[1]] ?? ''} oninput={event => changed(field,event.currentTarget.value)} required={field.required || (tab==='auth' && draft.security.auth_mode==='oidc' && index>0 && index<4) || (tab==='notifications' && draft.notifications.type==='ntfy' && index>0 && index<3) || (field.type==='number' && tab==='automation') || (field.type==='number' && tab==='advanced' && index===1)} min={field.min} max={field.max} maxlength={field.maxlength} pattern={field.pattern} step={field.type==='number'?'1':undefined} disabled={saving}/>
            {#if tab==='advanced' && index>=6}<small>{copy('unlimited')}</small>{/if}
          {/if}
        </label>
      {/each}
    </div>
    {#if saveError}<div class="notice error" role="alert"><strong>{copy('failed')}</strong><p>{saveError.message}</p>{#if saveError.path}<code>{saveError.path}</code>{/if}</div>{/if}
    <div class="settings-actions"><button class="button primary" type="submit" disabled={!dirty[tab] || saving}>{saving ? copy('saving') : copy('save')}</button>{#if dirty[tab]}<button class="button" type="button" onclick={discard} disabled={saving}>{copy('discard')}</button>{/if}{#if savedTab===tab}<span role="status">{copy('saved')}</span>{/if}</div>
  </form>
</section>
{:else if !error}<p>{t('loading',language)}</p>{/if}
</div></div>
