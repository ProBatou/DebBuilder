export const isManaged = recipe => recipe?.management?.owner === 'application' && Boolean(recipe.management.builtin_id);

export function recipePlan(recipe) {
  const source = recipe.source || {}, artifact = recipe.artifact || {}, build = recipe.build || {};
  const install = recipe.install || {}, service = recipe.service || {}, pkg = recipe.package || {};
  const mode = artifact.mode || 'unknown';
  const hooks = Object.entries(install.maintainer_scripts || {}).filter(([, value]) => Boolean(value)).map(([name]) => name);
  const elf = pkg.runtime_dependency_detection || {};
  const elfEligible = mode === 'upstream_archive' && artifact.archive_source === 'release_asset' && pkg.architecture === 'amd64';
  const resources = recipe.resource_limits || recipe.resources || {};
  const hasResourceOverride = Object.values(resources).some(value => value !== null && value !== undefined);
  return {
    mode, source, artifact, build, install, service, pkg, hooks, elfEligible,
    summary: [
      ['Source', source.repository || source.provider, 'Configured'],
      ['Tracking', source.tracking || 'latest_release', source.tracking ? 'Configured' : 'Default'],
      ['Ref', source.ref, 'Configured'],
      ['Version policy', source.version, 'Configured'],
      ['Artifact mode', mode, artifact.mode ? 'Configured' : 'Unknown / pending'],
      ...(mode === 'source_build' ? [['Build strategy', build.commands?.length ? `${build.commands.length} configured commands` : 'No commands configured', 'Configured']] : []),
      ['Package', pkg.name, 'Configured'],
      ['Architecture', pkg.architecture, 'Configured'],
      ...(mode === 'source_build' ? [['Output', build.output, 'Configured']] : []),
      ['Install destination', install.destination, 'Configured'],
      ['Install content', install.content, 'Configured'],
      ['Installation strategy', install.content?.source, 'Configured'],
      ['Service', service.enabled ? service.name || 'Configured' : 'Not configured', service.enabled ? 'Configured' : 'Not applicable'],
      ['Ownership', install.owner, 'Configured'],
      ['Account', install.account, 'Configured'],
      ['Runtime Depends', pkg.runtime_dependencies, 'Configured'],
      ...(elfEligible ? [['ELF detection', elf.enabled ? 'Enabled' : 'Disabled', 'Configured']] : []),
      ['Automation', recipe.automation, 'Configured'],
      ['Resources', hasResourceOverride ? resources : 'Inherited / unset', hasResourceOverride ? 'Configured' : 'Default'],
      ['Lifecycle hooks', hooks.length ? `${hooks.join(', ')} configured` : 'None', hooks.length ? 'Configured' : 'Not applicable'],
    ].filter(([, value]) => value !== undefined && value !== null && value !== ''),
    sections: [
      ['Source', source], ['Artifact', artifact],
      ...(mode === 'source_build' ? [['Build', build]] : []),
      ['Package', pkg], ['Install', install],
      ...(service.enabled ? [['Service', service]] : []),
      ['Automation', recipe.automation], ['Resources', resources],
      ['Runtime APT repositories', recipe.runtime_apt_repositories],
    ].filter(([, value]) => value && (typeof value !== 'object' || Object.keys(value).length)),
  };
}
