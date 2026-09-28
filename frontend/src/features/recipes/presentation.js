const advanced = new Set([
  'build.extra_dependencies','build.source_changes','build.ensure_directories',
  'install.config_files','install.directories','install.owner.user','install.owner.group',
  'install.owner.create_user','install.owner.create_group','install.account.user',
  'install.account.group','install.account.create_user','install.account.create_group',
  'service.user','service.group','service.environment','service.environment_files',
  'package.runtime_dependencies',
]);
const expert = new Set(['service.after','service.wants','service.requires']);

// Field ownership stays in fields.js; this only chooses the operator-facing level.
export function presentationSection(entry) {
  if (expert.has(entry.path)) return 'expert';
  if (advanced.has(entry.path)) return 'advanced';
  return entry.section;
}
