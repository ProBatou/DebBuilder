import {fields} from './fields.js';
import {segments} from './draft.js';
import {t} from '../../i18n/i18n.js';

const fieldNames = {
  active: ['Recipe active','Recette active','Rezept aktiv','Receta activa'],
  'package.name': ['Package name','Nom du paquet','Paketname','Nombre del paquete'],
  'package.description': ['Package description','Description du paquet','Paketbeschreibung','Descripción del paquete'],
  'source.repository': ['GitHub repository','Dépôt GitHub','GitHub-Repository','Repositorio de GitHub'],
  'source.tracking': ['Version tracking','Suivi des versions','Versionsverfolgung','Seguimiento de versiones'],
  'source.ref': ['GitHub reference','Référence GitHub','GitHub-Referenz','Referencia de GitHub'],
  'source.version.source': ['Version source','Source du numéro de version','Versionsquelle','Origen de la versión'],
  'source.version.expression': ['Version pattern','Motif de version','Versionsmuster','Patrón de versión'],
  'artifact.mode': ['Artifact mode','Mode d’artéfact','Artefaktmodus','Modo del artefacto'],
  'artifact.type': ['Artifact format','Format de l’artéfact','Artefaktformat','Formato del artefacto'],
  'artifact.archive_source': ['Archive source','Source de l’archive','Archivquelle','Origen del archivo'],
  'artifact.asset_selection': ['Release asset selection','Choix du fichier publié','Auswahl der Release-Datei','Selección del archivo publicado'],
  'artifact.asset_name': ['Release asset name','Nom du fichier publié','Name der Release-Datei','Nombre del archivo publicado'],
  'artifact.name_pattern': ['File name pattern','Motif du nom de fichier','Dateinamensmuster','Patrón del nombre de archivo'],
  'artifact.payload': ['Artifact content','Contenu de l’artéfact','Artefaktinhalt','Contenido del artefacto'],
  'artifact.payload.mode': ['Included content','Contenu inclus','Enthaltener Inhalt','Contenido incluido'],
  'artifact.payload.include': ['Included files','Fichiers inclus','Enthaltene Dateien','Archivos incluidos'],
  'artifact.payload.exclude': ['Excluded files','Fichiers exclus','Ausgeschlossene Dateien','Archivos excluidos'],
  'build.commands': ['Build commands','Commandes de construction','Build-Befehle','Comandos de compilación'],
  'build.environment': ['Build environment','Environnement de construction','Build-Umgebung','Entorno de compilación'],
  'build.output.mode': ['Build output','Sortie de construction','Build-Ausgabe','Salida de compilación'],
  'build.output.path': ['Output path','Chemin de sortie','Ausgabepfad','Ruta de salida'],
  'build.output.paths': ['Output paths','Chemins de sortie','Ausgabepfade','Rutas de salida'],
  'install.destination': ['Install destination','Destination d’installation','Installationsziel','Destino de instalación'],
  'install.content.source': ['Installed content source','Source du contenu installé','Quelle der installierten Dateien','Origen del contenido instalado'],
  'install.config_files': ['Configuration files','Fichiers de configuration','Konfigurationsdateien','Archivos de configuración'],
  'service.enabled': ['Service enabled','Service activé','Dienst aktiviert','Servicio activado'],
  'service.name': ['Service name','Nom du service','Dienstname','Nombre del servicio'],
  'service.command': ['Service command','Commande du service','Dienstbefehl','Comando del servicio'],
  'automation.enabled': ['Automation enabled','Automatisation activée','Automatisierung aktiviert','Automatización activada'],
  'automation.policy': ['Automation policy','Politique d’automatisation','Automatisierungsregel','Política de automatización'],
  'package.runtime_dependencies': ['Runtime dependencies','Dépendances d’exécution','Laufzeitabhängigkeiten','Dependencias de ejecución'],
};

const choices = {
  'source.tracking': {
    latest_release: ['Latest release','Dernière version publiée','Neueste Veröffentlichung','Última versión publicada'],
    tag: ['Git tag','Étiquette Git','Git-Tag','Etiqueta Git'],
    manual: ['Manual reference','Référence manuelle','Manuelle Referenz','Referencia manual'],
  },
  'source.version.source': {
    tag: ['Git tag','Étiquette Git','Git-Tag','Etiqueta Git'],
    release_name: ['Release name','Nom de la version publiée','Name der Veröffentlichung','Nombre de la versión publicada'],
    regex: ['Version pattern','Motif de version','Versionsmuster','Patrón de versión'],
  },
  'artifact.mode': {
    source_build: ['Build from source','Construction depuis les sources','Aus Quellcode bauen','Compilar desde el código fuente'],
    upstream_archive: ['Published archive','Archive publiée','Veröffentlichtes Archiv','Archivo publicado'],
    upstream_deb: ['Existing Debian package','Paquet Debian existant','Vorhandenes Debian-Paket','Paquete Debian existente'],
  },
  'artifact.type': {
    deb: ['Debian package','Paquet Debian','Debian-Paket','Paquete Debian'],
    archive: ['Archive','Archive','Archiv','Archivo'],
  },
  'artifact.archive_source': {
    auto: ['Automatic','Automatique','Automatisch','Automático'],
    github_source: ['GitHub source archive','Archive source GitHub','GitHub-Quellarchiv','Archivo fuente de GitHub'],
    release_asset: ['Release file','Fichier de la version publiée','Release-Datei','Archivo de la versión publicada'],
  },
  'artifact.asset_selection': {
    exact: ['Exact name','Nom exact','Exakter Name','Nombre exacto'],
    pattern: ['Name pattern','Motif du nom','Namensmuster','Patrón del nombre'],
  },
  'artifact.payload.mode': {
    paths: ['Selected paths','Chemins sélectionnés','Ausgewählte Pfade','Rutas seleccionadas'],
    entire_archive: ['Entire archive','Archive complète','Ganzes Archiv','Archivo completo'],
  },
  'build.output.mode': {
    source: ['Source tree','Arborescence source','Quellverzeichnis','Árbol de fuentes'],
    path: ['One path','Un chemin','Ein Pfad','Una ruta'],
    paths: ['Selected paths','Chemins sélectionnés','Ausgewählte Pfade','Rutas seleccionadas'],
  },
  'install.content.source': {
    build_output: ['Build output','Résultat de la construction','Build-Ausgabe','Resultado de la compilación'],
    configured_files: ['Configured files','Fichiers configurés','Konfigurierte Dateien','Archivos configurados'],
  },
  'automation.policy': {
    manual: ['Disabled','Désactivée','Deaktiviert','Desactivada'],
    detect: ['Detect updates','Détecter les mises à jour','Updates erkennen','Detectar actualizaciones'],
    test: ['Test automatically','Tester automatiquement','Automatisch testen','Probar automáticamente'],
    build: ['Build automatically','Construire automatiquement','Automatisch bauen','Compilar automáticamente'],
    build_validate: ['Build and validate','Construire et valider','Bauen und validieren','Compilar y validar'],
    full: ['Build, validate and publish','Construire, valider et publier','Bauen, validieren und veröffentlichen','Compilar, validar y publicar'],
  },
  'package.priority': {
    required: ['Required','Requis','Erforderlich','Necesario'],
    important: ['Important','Important','Wichtig','Importante'],
    standard: ['Standard','Standard','Standard','Estándar'],
    optional: ['Optional','Facultatif','Optional','Opcional'],
    extra: ['Extra','Supplémentaire','Zusätzlich','Adicional'],
  },
};

const languageIndex = language => ({en:0,fr:1,de:2,es:3})[language] ?? 0;
const words = value => String(value).replaceAll('_',' ').replace(/\b\w/g, letter => letter.toUpperCase());

export function recipeOptionLabel(path, value, language = 'en') {
  return choices[path]?.[value]?.[languageIndex(language)] || words(value);
}

export function recipeReviewLabel(path, language = 'en') {
  const normalized = path.replace(/^\$\.?/,'');
  const exact = fieldNames[normalized];
  if (exact) return exact[languageIndex(language)];
  const entry = fields.find(field => normalized.startsWith(`${field.path}.`) || normalized.startsWith(`${field.path}[`));
  const parts = segments(normalized);
  const base = entry?.path || parts[0];
  const title = fieldNames[base]?.[languageIndex(language)] || words(base.replaceAll('.',' '));
  const rest = parts.slice(segments(base).length).map(part => /^\d+$/.test(part) ? String(Number(part) + 1) : words(part));
  if (!rest.length) return title;
  return `${title}${/^\d+$/.test(parts[segments(base).length]) ? ` ${rest.shift()}` : ''}${rest.length ? ` · ${rest.join(' · ')}` : ''}`;
}

export function recipeReviewValue(path, value, language = 'en') {
  if (value == null || value === '') return '—';
  if (typeof value === 'boolean') return t(value ? 'enabledValue' : 'disabledValue',language);
  if (typeof value === 'number') return new Intl.NumberFormat(language).format(value);
  const normalized = path.replace(/^\$\.?/,'');
  if (typeof value === 'string') return choices[normalized]?.[value] ? recipeOptionLabel(normalized,value,language) : value;
  if (Array.isArray(value)) return value.length && value.every(item => typeof item === 'string') ? value.join(', ') : `${value.length} ${t('entries',language)}`;
  if (normalized === 'artifact.payload') {
    const mode = recipeOptionLabel('artifact.payload.mode',value.mode || 'paths',language);
    const count = value.include?.length || 0;
    return value.mode === 'entire_archive' ? mode : `${mode} · ${count} ${t('entries',language)}`;
  }
  return `${Object.keys(value).length} ${t('entries',language)}`;
}
