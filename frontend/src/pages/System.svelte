<script>
  import {onMount, tick} from 'svelte';
  import {api} from '../api/client.js';
  import {loadRecipe} from '../features/recipes/persistence.js';
  import {t, statusLabel} from '../i18n/i18n.js';
  import ErrorNotice from '../components/ErrorNotice.svelte';
  import StatusChip from '../components/StatusChip.svelte';
  import {statusSemantics} from '../components/statusSemantics.js';
  import RecipeDetail from '../features/recipes/RecipeDetail.svelte';
  import {isManaged} from '../features/recipes/model.js';
  const maintenanceText = {
    en:{storage:'Storage',history:'Execution history',failedRuns:'Failed Runs',testRuns:'Test Runs',repositoryStorage:'APT repository',disposable:'Disposable workspace',runArtifacts:'Run artifacts',retention:'Retention and pruning',policy:'Automatic workspace cleanup',enabled:'Enabled',disabled:'Disabled',retained:'Failed workspaces to retain',clear:'Clear execution history',checking:'Checking…',deleting:'Clearing…',none:'No completed execution history to clear.',preview:'Completed Runs selected for removal',clearDescription:'This removes their visible history, detailed logs and disposable workspace files. Active Runs, Recipes, packages and published APT entries are excluded.',confirm:'Confirm removal',cancel:'Cancel',cleared:'Execution history cleared.',partial:'Some Runs could not be cleared.',refreshFailed:'History changed, but storage could not be refreshed.',recoveryReady:'No recovery block reported for new work.',recoveryBlocked:'Recovery is blocking new Build and Test work.',recoveryUnknown:'Recovery status is unavailable.',viewRuns:'View Runs',unknown:'Unknown',lastMeasured:'Measured'},
    fr:{storage:'Stockage',history:'Historique des exécutions',failedRuns:'Exécutions en échec',testRuns:'Tests',repositoryStorage:'Dépôt APT',disposable:'Espace de travail jetable',runArtifacts:'Artéfacts des exécutions',retention:'Conservation et nettoyage',policy:'Nettoyage automatique des espaces de travail',enabled:'Activé',disabled:'Désactivé',retained:'Espaces en échec à conserver',clear:'Effacer l’historique des exécutions',checking:'Vérification…',deleting:'Effacement…',none:'Aucun historique d’exécution terminée à effacer.',preview:'Exécutions terminées sélectionnées pour suppression',clearDescription:'Cette action supprime leur historique visible, leurs journaux détaillés et leurs fichiers de travail jetables. Les exécutions actives, recettes, paquets et publications APT sont exclus.',confirm:'Confirmer la suppression',cancel:'Annuler',cleared:'Historique des exécutions effacé.',partial:'Certaines exécutions n’ont pas pu être effacées.',refreshFailed:'L’historique a changé, mais le stockage n’a pas pu être actualisé.',recoveryReady:'Aucun blocage de reprise signalé pour les nouveaux travaux.',recoveryBlocked:'La reprise bloque les nouvelles constructions et les nouveaux tests.',recoveryUnknown:'État de reprise indisponible.',viewRuns:'Voir les exécutions',unknown:'Inconnu',lastMeasured:'Mesuré'},
    de:{storage:'Speicher',history:'Ausführungsverlauf',failedRuns:'Fehlgeschlagene Läufe',testRuns:'Testläufe',repositoryStorage:'APT-Repository',disposable:'Temporärer Arbeitsbereich',runArtifacts:'Laufartefakte',retention:'Aufbewahrung und Bereinigung',policy:'Automatische Arbeitsbereichbereinigung',enabled:'Aktiviert',disabled:'Deaktiviert',retained:'Fehlgeschlagene Arbeitsbereiche behalten',clear:'Ausführungsverlauf löschen',checking:'Prüft…',deleting:'Löscht…',none:'Kein abgeschlossener Ausführungsverlauf zum Löschen.',preview:'Abgeschlossene Läufe zum Entfernen ausgewählt',clearDescription:'Sichtbaren Verlauf, detaillierte Protokolle und temporäre Arbeitsdateien entfernen. Aktive Läufe, Rezepte, Pakete und veröffentlichte APT-Einträge bleiben erhalten.',confirm:'Entfernen bestätigen',cancel:'Abbrechen',cleared:'Ausführungsverlauf gelöscht.',partial:'Einige Läufe konnten nicht gelöscht werden.',refreshFailed:'Verlauf geändert, aber Speicher konnte nicht aktualisiert werden.',recoveryReady:'Keine Wiederherstellungssperre für neue Aufgaben gemeldet.',recoveryBlocked:'Wiederherstellung blockiert neue Builds und Tests.',recoveryUnknown:'Wiederherstellungsstatus nicht verfügbar.',viewRuns:'Läufe anzeigen',unknown:'Unbekannt',lastMeasured:'Gemessen'},
    es:{storage:'Almacenamiento',history:'Historial de ejecuciones',failedRuns:'Ejecuciones fallidas',testRuns:'Pruebas',repositoryStorage:'Repositorio APT',disposable:'Espacio temporal',runArtifacts:'Artefactos de ejecuciones',retention:'Retención y limpieza',policy:'Limpieza automática de espacios',enabled:'Activada',disabled:'Desactivada',retained:'Espacios fallidos que conservar',clear:'Borrar historial de ejecuciones',checking:'Comprobando…',deleting:'Borrando…',none:'No hay ejecuciones terminadas que borrar.',preview:'Ejecuciones terminadas seleccionadas para borrar',clearDescription:'Elimina su historial visible, registros detallados y archivos temporales. Excluye ejecuciones activas, recetas, paquetes y publicaciones APT.',confirm:'Confirmar borrado',cancel:'Cancelar',cleared:'Historial de ejecuciones borrado.',partial:'No se pudieron borrar algunas ejecuciones.',refreshFailed:'Cambió el historial, pero no se pudo actualizar el almacenamiento.',recoveryReady:'No se ha comunicado un bloqueo de recuperación para nuevos trabajos.',recoveryBlocked:'La recuperación bloquea nuevas compilaciones y pruebas.',recoveryUnknown:'Estado de recuperación no disponible.',viewRuns:'Ver ejecuciones',unknown:'Desconocido',lastMeasured:'Medido'},
  };
  const maintenanceCopy = key => maintenanceText[language]?.[key] || maintenanceText.en[key];
  const failureText = {
    en: {invalidPreview:'Invalid execution history preview',unconfirmedDelete:'Execution history deletion was not confirmed',managedUnavailable:'Managed Recipe metadata unavailable',supportUnavailable:'Support bundle unavailable'},
    fr: {invalidPreview:'Aperçu de l’historique invalide',unconfirmedDelete:'La suppression de l’historique n’a pas été confirmée',managedUnavailable:'Métadonnées de la recette gérée indisponibles',supportUnavailable:'Archive de support indisponible'},
    de: {invalidPreview:'Ungültige Vorschau des Ausführungsverlaufs',unconfirmedDelete:'Das Löschen des Ausführungsverlaufs wurde nicht bestätigt',managedUnavailable:'Metadaten des verwalteten Rezepts nicht verfügbar',supportUnavailable:'Supportpaket nicht verfügbar'},
    es: {invalidPreview:'Vista previa del historial no válida',unconfirmedDelete:'No se confirmó el borrado del historial',managedUnavailable:'Metadatos de la receta gestionada no disponibles',supportUnavailable:'Paquete de soporte no disponible'},
  };
  const failureCopy = key => failureText[language]?.[key] || failureText.en[key];
  const maintenanceStates = {
    en:{ready:'Ready',partial:'Partial',stale:'Stale',error:'Error',collecting:'Measuring',blocked:'Blocked',notBlocked:'Not blocked',unknown:'Unknown'},
    fr:{ready:'Prêt',partial:'Partiel',stale:'Ancien',error:'Erreur',collecting:'Mesure en cours',blocked:'Bloqué',notBlocked:'Non bloqué',unknown:'Inconnu'},
    de:{ready:'Bereit',partial:'Teilweise',stale:'Veraltet',error:'Fehler',collecting:'Wird gemessen',blocked:'Blockiert',notBlocked:'Nicht blockiert',unknown:'Unbekannt'},
    es:{ready:'Listo',partial:'Parcial',stale:'Antiguo',error:'Error',collecting:'Midiéndose',blocked:'Bloqueado',notBlocked:'Sin bloqueo',unknown:'Desconocido'},
  };
  const maintenanceState = key => maintenanceStates[language]?.[key] || maintenanceStates.en[key] || key;
  const storageTone = state => state === 'ready' ? 'ready' : state === 'partial' || state === 'stale' ? 'warning' : state === 'error' ? 'danger' : state === 'collecting' ? 'info' : 'neutral';
  function size(bytes) {
    if (bytes == null || !Number.isFinite(Number(bytes)) || Number(bytes) < 0) return '—';
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    const units = ['KiB','MiB','GiB','TiB'];
    let value = Number(bytes), index = -1;
    do {value /= 1024; index++;} while (value >= 1024 && index < units.length - 1);
    return `${new Intl.NumberFormat(language,{maximumFractionDigits:1}).format(value)} ${units[index]}`;
  }
  const diagnosticNames = {
    en: {'application.runtime':'Application runtime','settings.documents':'Settings','repository.publication':'Repository publication','validation.oci':'Validation capability','execution.admission':'Build and Test admission','execution.containment':'Command containment','application.mutations':'Application changes','automation.scheduler':'Automation schedule','automation.orchestrator':'Automation service'},
    fr: {'application.runtime':'Exécution de l’application','settings.documents':'Paramètres','repository.publication':'Publication du dépôt','validation.oci':'Capacité de validation','execution.admission':'Admission construction et test','execution.containment':'Confinement des commandes','application.mutations':'Modifications de l’application','automation.scheduler':'Calendrier d’automatisation','automation.orchestrator':'Service d’automatisation'},
    de: {'application.runtime':'Anwendungslaufzeit','settings.documents':'Einstellungen','repository.publication':'Repository-Veröffentlichung','validation.oci':'Validierungsfähigkeit','execution.admission':'Build- und Testzulassung','execution.containment':'Befehlsisolation','application.mutations':'Anwendungsänderungen','automation.scheduler':'Automatisierungszeitplan','automation.orchestrator':'Automatisierungsdienst'},
    es: {'application.runtime':'Ejecución de la aplicación','settings.documents':'Ajustes','repository.publication':'Publicación del repositorio','validation.oci':'Capacidad de validación','execution.admission':'Admisión de compilación y prueba','execution.containment':'Contención de comandos','application.mutations':'Cambios de la aplicación','automation.scheduler':'Programación de automatización','automation.orchestrator':'Servicio de automatización'},
  };
  const checkName = check => diagnosticNames[language]?.[check.id] || diagnosticNames.en[check.id] || check.id;
  const diagnosticBadge = (value, lang) => ({...statusSemantics(value), label:value === 'ok' ? 'OK' : statusLabel(value,lang)});
  const developerText = {
    en:{recipeInspection:'Recipe inspection',runInspection:'Run inspection',selectRecipe:'Select a Recipe',selectRun:'Select a Run',inspect:'Inspect',support:'Download support bundle',supportHelp:'Includes diagnostics and any selected Recipe or Run inspections.',safeSummary:'Read-only summary without raw records or logs.',nothing:'No items available',loading:'Loading…',downloading:'Preparing download…',recipeLabel:'Recipe',runLabel:'Run',diagnostics:'Diagnostics',retry:'Retry'},
    fr:{recipeInspection:'Inspection de recette',runInspection:'Inspection d’exécution',selectRecipe:'Choisir une recette',selectRun:'Choisir une exécution',inspect:'Inspecter',support:'Télécharger l’archive de support',supportHelp:'Contient les diagnostics et les inspections des recettes ou exécutions sélectionnées.',safeSummary:'Résumé en lecture seule, sans enregistrements bruts ni journaux.',nothing:'Aucun élément disponible',loading:'Chargement…',downloading:'Préparation du téléchargement…',recipeLabel:'Recette',runLabel:'Exécution',diagnostics:'Diagnostics',retry:'Réessayer'},
    de:{recipeInspection:'Rezeptinspektion',runInspection:'Laufinspektion',selectRecipe:'Rezept auswählen',selectRun:'Lauf auswählen',inspect:'Prüfen',support:'Supportpaket herunterladen',supportHelp:'Enthält Diagnosen und ausgewählte Rezept- oder Laufinspektionen.',safeSummary:'Schreibgeschützte Übersicht ohne Rohdaten oder Protokolle.',nothing:'Keine Einträge verfügbar',loading:'Wird geladen…',downloading:'Download wird vorbereitet…',recipeLabel:'Rezept',runLabel:'Lauf',diagnostics:'Diagnosen',retry:'Erneut versuchen'},
    es:{recipeInspection:'Inspección de receta',runInspection:'Inspección de ejecución',selectRecipe:'Seleccionar receta',selectRun:'Seleccionar ejecución',inspect:'Inspeccionar',support:'Descargar paquete de soporte',supportHelp:'Incluye diagnósticos e inspecciones de las recetas o ejecuciones seleccionadas.',safeSummary:'Resumen de solo lectura sin registros ni datos brutos.',nothing:'No hay elementos disponibles',loading:'Cargando…',downloading:'Preparando descarga…',recipeLabel:'Receta',runLabel:'Ejecución',diagnostics:'Diagnósticos',retry:'Reintentar'},
  };
  const developerCopy = key => developerText[language]?.[key] || developerText.en[key];
  const recipeInspectionFields = {
    identity:['recipe_id','active','source','managed','package','architecture','revision'],
    source:['provider','repository_configured','tracking','ref_configured','version_source'],
    build:['detected_project','command_count','output_mode','output_path_count','source_change_count'],
    artifact:['mode','type','architecture','archive_source','asset_selection','archive_format'],
    installation:['content_source','destination_configured','account_provisioning','directory_count','config_mapping_count'],
    service:['configured','enabled','type','restart'], automation:['enabled','policy','eligible'], observation:['classification'],
  };
  const runInspectionFields = {
    identity:['run_id','recipe_id','mode','status','terminal'], lifecycle:['created_at','started_at','finished_at','current_stage','step_count'],
    artifact:['available','package','architecture','size','sha256'], validation:['attempt_count','status','attempt_id','recovery_blocked'],
    publication:['attempt_count','status','published','suite','component'], execution:['cancellable','recovery_status','recovery_blocked','containment_backend'],error:['code','stage'],
  };
  const inspectionTerms = {
    fr:{identity:'Identité',source:'Source',build:'Construction',artifact:'Artéfact',installation:'Installation',service:'Service',automation:'Automatisation',observation:'Observation',lifecycle:'Cycle de vie',validation:'Validation',publication:'Publication',execution:'Exécution',error:'Erreur',recipe_id:'ID de la recette',run_id:'ID de l’exécution',active:'Active',managed:'Gérée',package:'Paquet',architecture:'Architecture',revision:'Révision',provider:'Fournisseur',repository_configured:'Dépôt configuré',tracking:'Suivi',ref_configured:'Référence configurée',version_source:'Source de version',detected_project:'Projet détecté',command_count:'Nombre de commandes',output_mode:'Mode de sortie',output_path_count:'Chemins de sortie',source_change_count:'Changements de source',mode:'Mode',type:'Type',archive_source:'Source de l’archive',asset_selection:'Sélection de l’artéfact',archive_format:'Format de l’archive',content_source:'Source du contenu',destination_configured:'Destination configurée',account_provisioning:'Création de compte',directory_count:'Nombre de dossiers',config_mapping_count:'Fichiers de configuration',configured:'Configuré',enabled:'Activé',restart:'Redémarrage',policy:'Politique',eligible:'Éligible',classification:'Classification',status:'État',terminal:'Terminée',created_at:'Créée',started_at:'Démarrée',finished_at:'Terminée le',current_stage:'Étape actuelle',step_count:'Nombre d’étapes',available:'Disponible',size:'Taille',sha256:'SHA-256',attempt_count:'Nombre de tentatives',attempt_id:'ID de tentative',recovery_blocked:'Reprise bloquée',published:'Publiée',suite:'Distribution',component:'Composant',cancellable:'Annulation possible',recovery_status:'État de reprise',containment_backend:'Confinement',code:'Code',stage:'Étape'},
  };
  const inspectionSections = (inspection,kind) => Object.entries(kind === 'recipe' ? recipeInspectionFields : runInspectionFields).map(([section,fields]) => ({
    section, rows:fields.filter(field => inspection?.[section]?.[field] !== null && inspection?.[section]?.[field] !== undefined).map(field => ({field,value:inspection[section][field]}))
  })).filter(item => item.rows.length);
  const inspectionValue = value => typeof value === 'boolean' ? (value ? ({fr:'Oui',de:'Ja',es:'Sí'})[language] || 'Yes' : ({fr:'Non',de:'Nein',es:'No'})[language] || 'No') : String(value);
  const inspectionLabel = value => inspectionTerms[language]?.[value] || value.replaceAll('_',' ');
  export let id = '', language = 'en';
  let snapshot = null, storage = null, error = null, controller, tab = 'health';
  let clearDialog, clearTitle, clearPreview = null, clearLoading = false, clearError = null, clearResult = null;
  $: recoveryCheck = snapshot?.checks?.find(check => check.id === 'execution.admission');
  $: recoveryBlocked = recoveryCheck?.details?.recovery_blocked;
  async function previewClear() {
    if (clearLoading) return;
    clearLoading = true; clearError = null; clearResult = null; clearPreview = null;
    try {
      const preview = await api.deleteExecutionLogs({all:true,dry_run:true});
      if (!Number.isSafeInteger(preview.count) || !Array.isArray(preview.ids) || preview.ids.length !== preview.count || new Set(preview.ids).size !== preview.count) throw new Error(failureCopy('invalidPreview'));
      if (preview.count === 0) clearResult = maintenanceCopy('none');
      else {clearPreview = preview; clearDialog.showModal(); await tick(); clearTitle?.focus();}
    } catch (caught) {clearError = caught;}
    finally {clearLoading = false;}
  }
  async function confirmClear() {
    if (!clearPreview || clearLoading) return;
    clearLoading = true; clearError = null;
    try {
      const selected = clearPreview.ids;
      const result = await api.deleteExecutionLogs({ids:selected});
      const reported = [...(result.deleted || []).map(row => row.id), ...(result.errors || []).map(row => row.id)];
      if (!Array.isArray(result.deleted) || !Array.isArray(result.errors) || result.deleted.some(row => row.history_deleted !== true || row.visible !== false) || reported.length !== selected.length || new Set(reported).size !== selected.length || reported.some(id => !selected.includes(id))) throw new Error(failureCopy('unconfirmedDelete'));
      clearPreview = null;
      clearResult = result.errors.length ? `${maintenanceCopy('partial')} ${result.deleted.length}/${result.deleted.length + result.errors.length}` : `${maintenanceCopy('cleared')} ${result.deleted.length}`;
      try {storage = (await api.storage()).storage;} catch {clearResult += ` ${maintenanceCopy('refreshFailed')}`;}
    } catch (caught) {clearError = caught; clearPreview = null;}
    finally {clearLoading = false;}
  }
  let managed = null, managedRevision = null, managedError = null, managedController, managedGeneration = 0, allowedOverrides = [];
  async function loadManaged() {
    managedController?.abort(); managedController = new AbortController(); const token = ++managedGeneration; managedError = null; managed = null;
    allowedOverrides = [];
    try {
      const listing = await api.workflows({signal:managedController.signal});
      const entry = (listing.workflows || []).find(row => row.managed === true);
      if (!entry) throw new Error(failureCopy('managedUnavailable'));
      const canonical = await loadRecipe(entry.id,{signal:managedController.signal});
      if (!isManaged(canonical.recipe)) throw new Error(failureCopy('managedUnavailable'));
      if (token !== managedGeneration) return;
      allowedOverrides = entry.editable_paths || []; managed = canonical.recipe; managedRevision = canonical.revision;
    } catch(caught) {if(caught.name !== 'AbortError' && token === managedGeneration) managedError = caught;}
  }
  function managedSaved({recipe,revision}) {managed = recipe; managedRevision = revision;}
  let developerRecipes = [], developerRuns = [], developerRecipeId = '', developerRunId = '';
  let developerLoading = false, developerError = null, developerController, developerGeneration = 0;
  let recipeInspection = null, runInspection = null, recipeInspectionError = null, runInspectionError = null;
  let recipeInspectLoading = false, runInspectLoading = false, supportLoading = false, supportError = null, supportController;
  const developerInspectControllers = {recipe:null,run:null};
  const developerInspectGenerations = {recipe:0,run:0};
  async function loadDeveloper() {
    developerController?.abort(); developerController = new AbortController(); const token = ++developerGeneration;
    developerLoading = true; developerError = null;
    try {
      const [recipes,runs] = await Promise.all([api.workflows({signal:developerController.signal}),api.runs({signal:developerController.signal})]);
      if (token !== developerGeneration) return;
      developerRecipes = recipes.workflows || []; developerRuns = runs.executions || [];
      if (developerRecipeId && !developerRecipes.some(row => row.id === developerRecipeId)) inspectDeveloper('recipe','');
      if (developerRunId && !developerRuns.some(row => row.id === developerRunId)) inspectDeveloper('run','');
    } catch(caught) {if (caught.name !== 'AbortError' && token === developerGeneration) developerError = caught;}
    finally {if (token === developerGeneration) developerLoading = false;}
  }
  async function inspectDeveloper(kind, selected) {
    developerInspectControllers[kind]?.abort();
    const controller = new AbortController();
    developerInspectControllers[kind] = controller;
    const token = ++developerInspectGenerations[kind];
    if (kind === 'recipe') developerRecipeId = selected;
    else developerRunId = selected;
    if (kind === 'recipe') {recipeInspectLoading = true; recipeInspectionError = null; recipeInspection = null;}
    else {runInspectLoading = true; runInspectionError = null; runInspection = null;}
    if (!selected) {if (kind === 'recipe') recipeInspectLoading = false; else runInspectLoading = false; return;}
    try {
      const result = kind === 'recipe' ? await api.recipeInspection(selected,{signal:controller.signal}) : await api.runInspection(selected,{signal:controller.signal});
      if (token !== developerInspectGenerations[kind]) return;
      if (kind === 'recipe') recipeInspection = result.inspection;
      else runInspection = result.inspection;
    } catch(caught) {
      if (token !== developerInspectGenerations[kind] || caught.name === 'AbortError') return;
      if (kind === 'recipe') recipeInspectionError = caught;
      else runInspectionError = caught;
    } finally {if (token === developerInspectGenerations[kind]) {if (kind === 'recipe') recipeInspectLoading = false; else runInspectLoading = false;}}
  }
  async function downloadSupport() {
    if (supportLoading) return;
    supportLoading = true; supportError = null;
    const current = new AbortController(); supportController = current;
    let timedOut = false;
    const timeout = setTimeout(() => {timedOut = true; current.abort();},60000);
    const params = new URLSearchParams();
    if (developerRecipeId) params.set('recipe_id',developerRecipeId);
    if (developerRunId) params.set('run_id',developerRunId);
    try {
      const response = await fetch(`/api/support-bundle${params.size ? `?${params}` : ''}`,{credentials:'same-origin',headers:{Accept:'application/zip'},signal:current.signal});
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.error?.message || failureCopy('supportUnavailable'));
      }
      if (response.headers.get('content-type')?.split(';')[0] !== 'application/zip') throw new Error(failureCopy('supportUnavailable'));
      const objectUrl = URL.createObjectURL(await response.blob());
      try {
        const link = document.createElement('a'); link.href = objectUrl; link.download = 'debbuilder-support.zip';
        document.body.appendChild(link); link.click(); link.remove();
      } finally {setTimeout(() => URL.revokeObjectURL(objectUrl),1000);}
    } catch(caught) {if (timedOut) supportError = new Error(failureCopy('supportUnavailable')); else if (!current.signal.aborted) supportError = caught;}
    finally {clearTimeout(timeout); if (supportController === current) supportController = null; supportLoading = false;}
  }
  async function selectTab(next) {tab = next; if (next === 'managed') loadManaged(); if (next === 'developer') loadDeveloper(); await tick(); document.querySelector('.system-tabs button.active')?.scrollIntoView({block:'nearest',inline:'nearest'});}
  async function load() {
    controller?.abort(); const current = new AbortController(); controller = current; error = null;
    const [diagnostics,stored] = await Promise.allSettled([api.diagnostics({signal:current.signal}),api.storage({signal:current.signal})]);
    if (current.signal.aborted) return;
    if (diagnostics.status === 'fulfilled') snapshot = diagnostics.value;
    if (stored.status === 'fulfilled') storage = stored.value.storage;
    error = [diagnostics,stored].find(result => result.status === 'rejected' && result.reason?.name !== 'AbortError')?.reason || null;
  }
  onMount(() => {load(); return () => {controller?.abort(); managedController?.abort(); developerController?.abort(); developerInspectControllers.recipe?.abort(); developerInspectControllers.run?.abort(); supportController?.abort();};});
  $: if (id === 'managed') {tab = 'managed'; loadManaged(); tick().then(() => document.querySelector('.system-tabs button.active')?.scrollIntoView({block:'nearest',inline:'nearest'}));}
</script>
<div class="segmented-tabs system-tabs" role="group" aria-label={t('system',language)}>{#each ['health','maintenance','developer','managed'] as key}<button class:active={tab===key} aria-current={tab===key?'page':undefined} onclick={() => selectTab(key)}>{key==='managed'?t('managedSelfBuild',language):t(key,language)}</button>{/each}</div>
<ErrorNotice {error} retry={load} {language}/>
{#if tab==='health'}
  <div class="system-grid system-health-layout">
  <section class="panel system-health"><div class="section-head"><h2>{t('diagnosis',language)}</h2></div>
    <div class="system-check-grid">{#each snapshot?.checks || [] as check (check.id)}<article class="system-check-card"><div class="system-check-head"><h3>{checkName(check)}</h3><StatusChip {...diagnosticBadge(check.status,language)}/></div><p>{check.message}</p>{#if check.id==='repository.publication' && (check.details?.signed_release_present === true || check.details?.last_publication_at)}<div class="system-check-facts">{#if check.details?.signed_release_present === true}<span>{t('signedMetadata',language)}: {t('available',language)}</span>{/if}{#if check.details?.last_publication_at}<span>{t('published',language)}: {check.details.last_publication_at}</span>{/if}</div>{/if}</article>{:else}{#if !error}<p>{snapshot ? t('noItems',language) : t('loading',language)}</p>{/if}{/each}</div>
  </section>
  </div>
{:else if tab==='maintenance'}
  <div class="maintenance-layout">
    <section class="panel maintenance-card">
      <div class="section-head"><h2>{maintenanceCopy('storage')}</h2>{#if storage}<StatusChip label={maintenanceState(storage.state)} tone={storageTone(storage.state)} icon="●"/>{/if}</div>
      {#if storage}<div class="detail-facts">
        <div><span>{maintenanceCopy('history')}</span><strong>{storage.runs?.count == null ? '—' : new Intl.NumberFormat(language).format(storage.runs.count)}</strong></div>
        <div><span>{t('managedStorage',language)}</span><strong>{size(storage.bytes?.managed_total)}</strong></div>
        <div><span>{maintenanceCopy('repositoryStorage')}</span><strong>{size(storage.bytes?.repository)}</strong></div>
        <div><span>{maintenanceCopy('disposable')}</span><strong>{size(storage.categories?.disposable)}</strong></div>
        <div><span>{maintenanceCopy('runArtifacts')}</span><strong>{size(storage.runs?.artifact_bytes)}</strong></div>
      </div>{#if storage.measured_at}<p class="muted maintenance-measured">{maintenanceCopy('lastMeasured')}: {new Intl.DateTimeFormat(language,{dateStyle:'medium',timeStyle:'short'}).format(new Date(storage.measured_at))}</p>{/if}{:else if !error}<p>{t('loading',language)}</p>{/if}
    </section>
    <section class="panel maintenance-card">
      <h2>{t('maintenance',language)}</h2>
      <div class="detail-facts">
        <div><span>{maintenanceCopy('policy')}</span><strong>{storage?.retention_policy?.enabled == null ? '—' : maintenanceCopy(storage.retention_policy.enabled ? 'enabled' : 'disabled')}</strong></div>
        <div><span>{maintenanceCopy('retained')}</span><strong>{storage?.retention_policy?.failed_workspaces_to_retain ?? '—'}</strong></div>
      </div>
      <button class="button secondary maintenance-clear" type="button" disabled={clearLoading} onclick={previewClear}>{clearLoading && !clearPreview ? maintenanceCopy('checking') : maintenanceCopy('clear')}</button>
      {#if clearResult}<p class="maintenance-feedback" role="status">{clearResult}</p>{/if}
      {#if clearError && !clearDialog?.open}<ErrorNotice error={clearError} {language}/>{/if}
    </section>
    <section class="panel maintenance-card"><h2>{t('recovery',language)}</h2><p class="muted">{recoveryBlocked === true ? maintenanceCopy('recoveryBlocked') : recoveryBlocked === false ? maintenanceCopy('recoveryReady') : maintenanceCopy('recoveryUnknown')}</p><StatusChip label={maintenanceState(recoveryBlocked === true ? 'blocked' : recoveryBlocked === false ? 'notBlocked' : 'unknown')} tone={recoveryBlocked === true ? 'warning' : recoveryBlocked === false ? 'ready' : 'neutral'} icon="●"/></section>
  </div>
{:else if tab==='developer'}
  <div class="system-developer">
    <section class="panel developer-tools"><div class="section-head"><h2>{t('developer',language)}</h2><a href="/api/openapi.json" target="_blank" rel="noopener noreferrer">{t('openapi',language)}</a></div>
      <p class="muted">{developerCopy('safeSummary')}</p>
      {#if developerError}<ErrorNotice error={developerError} retry={loadDeveloper} {language}/>{/if}
      <div class="developer-selector-grid">
        <div class="developer-selector"><label for="developer-recipe">{developerCopy('recipeInspection')}</label><select id="developer-recipe" value={developerRecipeId} disabled={developerLoading} onchange={event => inspectDeveloper('recipe',event.currentTarget.value)}><option value="">{developerCopy('selectRecipe')}</option>{#each developerRecipes as row (row.id)}<option value={row.id}>{row.id}</option>{/each}</select></div>
        <div class="developer-selector"><label for="developer-run">{developerCopy('runInspection')}</label><select id="developer-run" value={developerRunId} disabled={developerLoading} onchange={event => inspectDeveloper('run',event.currentTarget.value)}><option value="">{developerCopy('selectRun')}</option>{#each developerRuns as row (row.id)}<option value={row.id}>{row.id}</option>{/each}</select></div>
      </div>
      {#if developerLoading}<p class="muted" role="status">{developerCopy('loading')}</p>{:else if !developerError && !developerRecipes.length && !developerRuns.length}<p class="muted">{developerCopy('nothing')}</p>{/if}
    </section>
    {#if developerRecipeId}<section class="panel developer-inspection"><h2>{developerCopy('recipeInspection')} · {developerRecipeId}</h2>{#if recipeInspectLoading}<p class="muted developer-inspection-feedback" role="status">{developerCopy('loading')}</p>{:else if recipeInspectionError}<ErrorNotice error={recipeInspectionError} retry={() => inspectDeveloper('recipe',developerRecipeId)} {language}/>{:else if recipeInspection}<div class="developer-inspection-grid">{#each inspectionSections(recipeInspection,'recipe') as item}<article class="system-check-card"><h3>{inspectionLabel(item.section)}</h3><dl>{#each item.rows as row}<div><dt>{inspectionLabel(row.field)}</dt><dd>{inspectionValue(row.value)}</dd></div>{/each}</dl></article>{/each}</div>{/if}</section>{/if}
    {#if developerRunId}<section class="panel developer-inspection"><h2>{developerCopy('runInspection')} · {developerRunId}</h2>{#if runInspectLoading}<p class="muted developer-inspection-feedback" role="status">{developerCopy('loading')}</p>{:else if runInspectionError}<ErrorNotice error={runInspectionError} retry={() => inspectDeveloper('run',developerRunId)} {language}/>{:else if runInspection}<div class="developer-inspection-grid">{#each inspectionSections(runInspection,'run') as item}<article class="system-check-card"><h3>{inspectionLabel(item.section)}</h3><dl>{#each item.rows as row}<div><dt>{inspectionLabel(row.field)}</dt><dd>{inspectionValue(row.value)}</dd></div>{/each}</dl>{#if item.section === 'lifecycle' && runInspection.lifecycle?.steps?.length}<ol class="developer-steps">{#each runInspection.lifecycle.steps as step}<li>{step.id}: {step.status}</li>{/each}</ol>{/if}</article>{/each}</div>{/if}</section>{/if}
    <section class="panel developer-support"><div><h2>{t('supportBundle',language)}</h2><p class="muted">{developerCopy('supportHelp')}</p></div><button class="button secondary" type="button" disabled={supportLoading} onclick={downloadSupport}>{supportLoading ? developerCopy('downloading') : developerCopy('support')}</button>{#if supportError}<ErrorNotice error={supportError} {language}/>{/if}</section>
    <section class="panel"><h2>{developerCopy('diagnostics')}</h2><details class="developer-payload"><summary>{t('inspectDiagnosticPayload',language)}</summary><pre class="facts">{JSON.stringify(snapshot?.checks || [],null,2)}</pre></details></section>
  </div>
{:else}
  <ErrorNotice error={managedError} retry={loadManaged} {language}/>
  {#if managed}<div class="managed-self-build-simple"><RecipeDetail recipe={managed} revision={managedRevision} {language} editablePaths={allowedOverrides} compactManaged saved={managedSaved}/></div>{:else if !managedError}<p>{t('loading',language)}</p>{/if}
{/if}
<dialog class="system-clear-dialog" bind:this={clearDialog} aria-labelledby="system-clear-title" onclick={event => {if (event.target === clearDialog && !clearLoading) clearDialog.close();}} oncancel={event => {if (clearLoading) event.preventDefault();}}>
  <div class="modal-head"><h2 id="system-clear-title" tabindex="-1" bind:this={clearTitle}>{maintenanceCopy('clear')}</h2><button type="button" class="icon-button" aria-label={t('close',language)} disabled={clearLoading} onclick={() => clearDialog.close()}>×</button></div>
  {#if clearPreview}<p class="system-clear-count"><strong>{clearPreview.count}</strong> {maintenanceCopy('preview')}</p><p class="modal-copy">{maintenanceCopy('clearDescription')}</p>{/if}
  <ErrorNotice error={clearError} {language}/>
  {#if clearResult}<p class="maintenance-feedback" role="status">{clearResult}</p>{/if}
  <div class="modal-actions"><button type="button" class="button secondary" disabled={clearLoading} onclick={() => clearDialog.close()}>{clearResult ? t('close',language) : maintenanceCopy('cancel')}</button>{#if clearPreview}<button type="button" class="button primary" disabled={clearLoading} onclick={confirmClear}>{clearLoading ? maintenanceCopy('deleting') : maintenanceCopy('confirm')}</button>{/if}</div>
</dialog>
