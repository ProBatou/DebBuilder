// Ownership is explicit. Canonical fields absent from this table are preserved
// by the draft model and classified PRESERVE_UNOWNED.
const field = (path, section, type = 'text', options = null) => ({path, section, type, options});
export const fields = [
  field('active','plan','boolean'), field('package.name','plan'), field('package.description','plan','multiline'),
  field('source.repository','plan'), field('source.tracking','plan','select',['latest_release','tag','manual']),
  field('source.ref','plan'), field('source.version.source','plan','select',['tag','release_name','regex']),
  field('source.version.expression','plan'), field('artifact.mode','plan','select',['source_build','upstream_archive','upstream_deb']),
  field('artifact.archive_source','plan','select',['auto','github_source','release_asset']),
  field('artifact.asset_selection','plan','select',['exact','pattern']),
  field('artifact.asset_name','plan'), field('artifact.name_pattern','plan'),
  field('install.destination','plan'), field('service.enabled','plan','boolean'),
  field('automation.enabled','plan','boolean'), field('automation.policy','plan','select',['manual','detect','test','build','build_validate','full']),
  field('build.commands','customize','strings'), field('build.environment','customize','environment'),
  field('build.extra_dependencies','customize','strings'), field('build.source_changes','customize','objects'),
  field('build.ensure_directories','customize','strings'), field('build.output.mode','customize','select',['source','path','paths']),
  field('build.output.path','customize'), field('build.output.paths','customize','strings'),
  field('artifact.payload.mode','customize','select',['paths','entire_archive']),
  field('artifact.payload.include','customize','strings'), field('artifact.payload.exclude','customize','strings'),
  field('install.content.source','customize','select',['build_output','configured_files']),
  field('install.config_files','customize','objects'), field('install.directories','customize','objects'),
  field('install.owner.user','customize'), field('install.owner.group','customize'),
  field('install.owner.create_user','customize','boolean'), field('install.owner.create_group','customize','boolean'),
  field('install.account.user','customize'), field('install.account.group','customize'),
  field('install.account.create_user','customize','boolean'), field('install.account.create_group','customize','boolean'),
  field('service.name','customize'), field('service.command','customize'), field('service.user','customize'),
  field('service.group','customize'), field('service.environment','customize','environment'),
  field('service.environment_files','customize','strings'), field('package.runtime_dependencies','customize','strings'),
  field('resource_limits.memory_max_bytes','advanced','nullable-number'),
  field('resource_limits.tasks_max','advanced','nullable-number'), field('resource_limits.cpu_quota_percent','advanced','nullable-number'),
  field('resource_limits.io_read_bandwidth_max_bytes_per_sec','advanced','nullable-number'),
  field('resource_limits.io_write_bandwidth_max_bytes_per_sec','advanced','nullable-number'),
  field('build.inactivity_timeout','advanced','nullable-number'), field('build.maximum_runtime','advanced','nullable-number'),
  field('build.working_directory','advanced'), field('service.after','advanced','strings'),
  field('package.maintainer','advanced'), field('package.version_revision','advanced'),
  field('package.section','advanced'), field('package.priority','advanced','select',['required','important','standard','optional','extra']),
  field('service.wants','advanced','strings'), field('service.requires','advanced','strings'),
  field('package.runtime_dependency_detection.enabled','advanced','boolean'),
  field('package.runtime_dependency_detection.overrides','expert','objects'),
  field('runtime_apt_repositories','expert','objects'),
  ...['preinst','postinst','prerm','postrm'].map(name => field(`install.maintainer_scripts.${name}`,'expert','multiline')),
  ...['type','restart','restart_sec','timeout_start_sec','timeout_stop_sec','kill_signal','standard_output','standard_error','working_directory','limit_nofile','kill_mode','syslog_identifier'].map(name => field(`service.${name}`,'expert')),
  ...['exec_start_pre','exec_start_post','exec_stop','conflicts','ambient_capabilities'].map(name => field(`service.${name}`,'expert','strings')),
];
export function ownership(path, managed = false, editablePaths = []) {
  if (path === 'schema_version' || path === 'management' || path.startsWith('management.')) return 'HIDDEN_DERIVED';
  if (path === 'name' || path === 'service.configured') return 'READ_ONLY_DERIVED';
  if (path.startsWith('build.detected_')) return 'READ_ONLY_DERIVED';
  if (managed && !editablePaths.some(allowed => path === allowed || path.startsWith(`${allowed}.`))) return 'READ_ONLY_MANAGED';
  const entry = fields.find(item => path === item.path || path.startsWith(`${item.path}.`) || path.startsWith(`${item.path}[`));
  return entry ? `EDITABLE_${entry.section.toUpperCase()}` : 'PRESERVE_UNOWNED';
}
export function applicable(entry, recipe) {
  const p = entry.path, mode = recipe.artifact?.mode;
  if (p === 'source.ref') return ['tag','manual'].includes(recipe.source?.tracking);
  if (p === 'source.version.expression') return recipe.source?.version?.source === 'regex';
  if (p === 'build.output.path') return mode === 'source_build' && recipe.build?.output?.mode === 'path';
  if (p === 'build.output.paths') return mode === 'source_build' && recipe.build?.output?.mode === 'paths';
  if (p.startsWith('build.') && !p.startsWith('build.detected_')) return mode === 'source_build';
  if (p.startsWith('artifact.archive_source') || p.startsWith('artifact.asset_selection') || p.startsWith('artifact.payload')) return mode === 'upstream_archive';
  if (p === 'artifact.asset_name') return Boolean(recipe.artifact?.asset_name) || mode === 'upstream_archive' && recipe.artifact?.archive_source === 'release_asset' && recipe.artifact?.asset_selection === 'exact';
  if (p === 'artifact.name_pattern') return Boolean(recipe.artifact?.name_pattern) || mode === 'upstream_deb' || mode === 'upstream_archive' && recipe.artifact?.archive_source === 'release_asset' && recipe.artifact?.asset_selection === 'pattern';
  if (p === 'install.destination') return recipe.install?.content?.source !== 'configured_files';
  if (p.startsWith('service.') && p !== 'service.enabled') return recipe.service?.enabled || Boolean(recipe.service?.name || recipe.service?.command);
  if (p.startsWith('package.runtime_dependency_detection')) return mode === 'upstream_archive' && recipe.artifact?.archive_source === 'release_asset' && recipe.package?.architecture === 'amd64' || p.endsWith('.overrides') && Boolean(recipe.package?.runtime_dependency_detection?.overrides?.length);
  return true;
}
