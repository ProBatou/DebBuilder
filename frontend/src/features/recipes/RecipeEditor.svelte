<script>
  import RecipeField from './RecipeField.svelte';
  import {fields, applicable, ownership} from './fields.js';
  import {presentationSection} from './presentation.js';
  import {changedPaths,readPath} from './draft.js';
  import {t} from '../../i18n/i18n.js';
  export let editor, update, language = 'en', errors = null;
  export let section = 'plan';
  const sharedCustomize = new Set(['source.repository','source.tracking','source.ref','source.version.source','source.version.expression','artifact.mode','artifact.archive_source','artifact.asset_selection','artifact.asset_name','artifact.name_pattern','package.description','service.enabled']);
  $: visible = fields.filter(entry => (presentationSection(entry) === section || section === 'customize' && sharedCustomize.has(entry.path)) && applicable(entry,editor.draft));
  $: changes = changedPaths(editor);
  const groupNames = {
    plan: [['Source','source'],['Artifact','artifact'],['Package metadata','package'],['Install','install'],['Service','service'],['Automation','automation']],
    customize: [['Source','source'],['Artifact payload','artifact'],['Build and output','build'],['Package metadata','package'],['Install and files','install'],['Service','service'],['Other','other']],
    advanced: [['Build operations','build'],['Installation','install'],['Runtime dependencies','dependencies'],['Package details','package'],['Service operations','service'],['Resource limits','resource_limits'],['Other','other']],
    expert: [['Maintainer hooks','install'],['Service directives','service'],['Runtime dependency overrides','package'],['Runtime APT repositories','runtime_apt_repositories'],['Other','other']],
  };
  const groupTranslations = {
    fr: {Source:'Source',Artifact:'Artéfact','Package metadata':'Métadonnées du paquet',Install:'Installation',Service:'Service',Automation:'Automatisation','Build and output':'Construction et sortie','Artifact payload':'Contenu de l’artéfact','Install and files':'Installation et fichiers','Runtime dependencies':'Dépendances d’exécution',Other:'Autres','Build operations':'Opérations de construction','Package details':'Détails du paquet','Service operations':'Opérations du service','Resource limits':'Limites de ressources','Maintainer hooks':'Scripts de maintenance','Service directives':'Directives du service','Runtime dependency overrides':'Exceptions de dépendances','Runtime APT repositories':'Dépôts APT d’exécution'},
    de: {Source:'Quelle',Artifact:'Artefakt','Package metadata':'Paketmetadaten',Install:'Installation',Service:'Dienst',Automation:'Automatisierung','Build and output':'Build und Ausgabe','Artifact payload':'Artefaktinhalt','Install and files':'Installation und Dateien','Runtime dependencies':'Laufzeitabhängigkeiten',Other:'Sonstiges','Build operations':'Build Vorgänge','Package details':'Paketdetails','Service operations':'Dienstvorgänge','Resource limits':'Ressourcenlimits','Maintainer hooks':'Maintainer Skripte','Service directives':'Dienstdirektiven','Runtime dependency overrides':'Abhängigkeitsausnahmen','Runtime APT repositories':'Laufzeit APT Repositorys'},
    es: {Source:'Origen',Artifact:'Artefacto','Package metadata':'Metadatos del paquete',Install:'Instalación',Service:'Servicio',Automation:'Automatización','Build and output':'Compilación y salida','Artifact payload':'Contenido del artefacto','Install and files':'Instalación y archivos','Runtime dependencies':'Dependencias de ejecución',Other:'Otros','Build operations':'Operaciones de compilación','Package details':'Detalles del paquete','Service operations':'Operaciones del servicio','Resource limits':'Límites de recursos','Maintainer hooks':'Scripts de mantenimiento','Service directives':'Directivas del servicio','Runtime dependency overrides':'Excepciones de dependencias','Runtime APT repositories':'Repositorios APT de ejecución'},
  };
  const groupTitle = label => groupTranslations[language]?.[label] || label;
  const groupFor = path => path === 'active' ? 'package' : path === 'package.runtime_dependencies' ? 'dependencies' : path.split('.')[0];
  $: groups = (groupNames[section] || []).map(([label,key]) => ({label, entries:visible.filter(entry => groupFor(entry.path) === key || key === 'other' && !(groupNames[section] || []).some(([,known]) => known === groupFor(entry.path)))})).filter(group => group.entries.length);
  function errorsForField(path) {return Object.entries(errors?.fields || {}).filter(([key]) => key === `$.${path}` || key.startsWith(`$.${path}.`) || key.startsWith(`$.${path}[`)).flatMap(([,items]) => items);}
  const groupDescription = () => t(section === 'customize' ? 'customizeGroupHelp' : section === 'advanced' ? 'advancedGroupHelp' : 'expertGroupHelp',language);
  const groupHasError = group => group.entries.some(entry => errorsForField(entry.path).length);
  function changeValue(path,version) {
    const value = readPath(version,path.replace(/^\$\./,''));
    if (value == null || value === '') return '—';
    if (Array.isArray(value)) return `${value.length} ${t('entries',language)}`;
    if (typeof value === 'object') return t('structuredChange',language);
    return String(value);
  }
</script>
<div class="recipe-fields-editor">
  {#if section === 'review'}
    <section class="panel recipe-section"><h3>{t('reviewChanges',language)}</h3>{#if changes.length}<div class="recipe-review-head"><span>{t('loadedBaseline',language)}</span><strong>{t('currentDraft',language)}</strong></div><div class="recipe-review-facts">{#each changes as path}<div><code>{path}</code><span><small>{t('loadedBaseline',language)}</small>{changeValue(path,editor.baseline)}</span><strong><small>{t('currentDraft',language)}</small>{changeValue(path,editor.draft)}</strong></div>{/each}</div>{:else}<p>{t('noChanges',language)}</p>{/if}</section>
  {:else}
    <div class="editor-groups">
      {#each groups as group}<section class="panel recipe-section editor-group"><details class="recipe-task-group" open={section!=='advanced' || groupHasError(group)}><summary><strong>{groupTitle(group.label)}</strong><small>{groupDescription()}</small></summary><div class="recipe-form-grid recipe-fields">
      {#each group.entries as entry (entry.path)}
        {@const disabled = ownership(entry.path,editor.managed,editor.editablePaths) === 'READ_ONLY_MANAGED' || entry.path.endsWith('runtime_dependency_detection.overrides') && !(editor.draft.artifact?.mode === 'upstream_archive' && editor.draft.artifact?.archive_source === 'release_asset' && editor.draft.package?.architecture === 'amd64')}
        <RecipeField {entry} {editor} {update} {disabled} {language} fieldError={errorsForField(entry.path)[0]}/>
        {#each errorsForField(entry.path) as error}<p class="notice error" role="alert" id={`recipe-error-${entry.path.replace(/[^A-Za-z0-9]/g,'-')}`}>{error.path || error.details?.path || '$'}: {error.message}</p>{/each}
      {/each}
      </div></details></section>{/each}
    </div>
  {/if}
</div>
