/* global $ */
window.recipeSourceChanges = [];
window.recipeExtraDependencies = [];
window.recipeAdvancedFields = {};

function lines(value) {
  return String(value || '').split(/\r?\n|,/).map(row => row.trim()).filter(Boolean);
}

function environment(value) {
  const result = {};
  lines(value).forEach(row => {
    const split = row.indexOf('=');
    if (split > 0) result[row.slice(0, split).trim()] = row.slice(split + 1);
  });
  return result;
}

function environmentText(value) {
  return Object.entries(value || {}).map(([key, item]) => `${key}=${item}`).join('\n');
}

function configFiles(value) {
  return String(value || '').split(/\r?\n/).map(row => row.trim()).filter(Boolean).map(row => {
    const split = row.indexOf('=>');
    return split < 0 ? row : {source: row.slice(0, split).trim(), destination: row.slice(split + 2).trim()};
  });
}

function configFilesText(value) {
  return (value || []).map(row => typeof row === 'string' ? row : `${row.source} => ${row.destination}`).join('\n');
}

function value(id) { return $(id)?.value.trim() || ''; }
function setValue(id, next) { if ($(id)) $(id).value = next ?? ''; }

function collectWorkflow() {
  const name = value('recipeMetaName') || $('workflowName')?.value || 'recipe';
  const packageName = value('recipeMetaPackage') || name;
  const outputPath = value('buildExpectedOutput');
  const configuredFiles = $('installContentSource')?.value === 'configured_files';
  const serviceConfigured = !!$('serviceConfigured')?.checked;
  const advanced = window.recipeAdvancedFields || {};
  return {
    schema_version: 1,
    name,
    active: !!$('recipeMetaActive')?.checked,
    package: {
      name: packageName,
      version_revision: '1',
      architecture: $('packageArchitecture')?.value || 'amd64',
      section: value('packageSection') || 'misc',
      priority: value('packagePriority') || 'optional',
      maintainer: value('packageMaintainer'),
      description: value('packageDescription') || packageName,
      runtime_dependencies: lines(value('packageRuntimeDependencies'))
    },
    source: {
      provider: 'github',
      repository: value('recipeMetaGithub'),
      tracking: $('recipeMetaTracking')?.value || 'latest_release',
      ref: value('recipeMetaSourceRef'),
      version: {source: $('recipeMetaVersionSource')?.value || 'tag', expression: value('recipeMetaVersionExpression')}
    },
    artifact: {
      mode: $('recipeArtifactMode')?.value || 'source_build', type: 'deb',
      architecture: $('packageArchitecture')?.value || 'amd64',
      name_pattern: value('recipeArtifactPattern'), match_package: true, match_version: true
    },
    build: {
      detected_project: $('buildDetectedProject')?.dataset.value || null,
      detected_files: lines($('buildDetectedFiles')?.dataset.value),
      detected_dependencies: lines($('buildDetectedDependencies')?.dataset.value),
      extra_dependencies: [...window.recipeExtraDependencies],
      source_changes: window.recipeSourceChanges.map(change => ({...change})),
      commands: lines(value('buildCommands')),
      environment: environment(value('buildEnvironment')),
      working_directory: value('buildWorkingDirectory') || '.',
      timeout: advanced.timeout || 120,
      output: advanced.output?.mode === 'paths' ? {...advanced.output} : {mode: outputPath ? 'path' : 'source', path: outputPath}
    },
    install: {
      destination: configuredFiles ? '' : (value('installDestination') || `/opt/${packageName}`),
      content: {source: $('installContentSource')?.value || 'build_output', path: ''},
      owner: {user: value('installOwnerUser') || packageName, group: value('installOwnerGroup') || packageName, create_user: !!$('installCreateUser')?.checked, create_group: !!$('installCreateGroup')?.checked},
      directory_mode: $('installDirectoryMode')?.value || '0755',
      file_mode: $('installFileMode')?.value || '0644',
      config_files: configFiles(value('installConfigFiles')),
      config_policy: $('installConfigPolicy')?.value || 'dpkg_conffile',
      maintainer_scripts: {preinst: value('maintainerPreinst'), postinst: value('maintainerPostinst'), prerm: value('maintainerPrerm'), postrm: value('maintainerPostrm')}
    },
    service: {
      configured: serviceConfigured,
      enabled: serviceConfigured && !!$('serviceEnabled')?.checked,
      name: serviceConfigured ? value('serviceName') : '',
      description: serviceConfigured ? (advanced.service_description || packageName) : '',
      type: serviceConfigured ? ($('serviceType')?.value || 'simple') : '',
      user: serviceConfigured ? value('serviceUser') : '',
      group: serviceConfigured ? value('serviceGroup') : '',
      restart: serviceConfigured ? ($('serviceRestart')?.value || 'on-failure') : '',
      command: value('serviceCommand'),
      environment_files: lines(value('serviceEnvironmentFiles')),
      environment: environment(value('serviceEnvironment')),
      after: lines(value('serviceAfter')), wants: lines(value('serviceWants')), requires: lines(value('serviceRequires')),
      restart_sec: value('serviceRestartSec'), timeout_start_sec: value('serviceTimeoutStartSec'), timeout_stop_sec: value('serviceTimeoutStopSec'),
      kill_signal: $('serviceKillSignal')?.value || '',
      exec_start_pre: lines(value('serviceExecStartPre')), exec_start_post: lines(value('serviceExecStartPost')), exec_stop: lines(value('serviceExecStop')),
      standard_output: $('serviceStandardOutput')?.value || '', standard_error: $('serviceStandardError')?.value || '',
      working_directory: serviceConfigured ? (advanced.service_working_directory || '') : ''
    },
    steps: []
  };
}

function renderDependencyChips() {
  if (!$('buildDependencyChips')) return;
  $('buildDependencyChips').innerHTML = window.recipeExtraDependencies.map((item,index) => `<span class="extra">${esc(item)} <button type="button" data-remove-dependency="${index}" aria-label="Remove ${esc(item)}">×</button></span>`).join('') || '<span class="muted">No manual dependencies</span>';
}

const CHANGE_LABELS = {replace:'Replace',insert_before:'Insert before',insert_after:'Insert after',remove:'Remove',create_file:'Create file',remove_file:'Remove file'};
function renderSourceChanges() {
  if (!$('sourceChangeList')) return;
  $('sourceChangeList').innerHTML = window.recipeSourceChanges.map((change, index) => `<article class="source-change-row"><span class="change-type">${CHANGE_LABELS[change.operation] || esc(change.operation)}</span><div class="change-summary"><strong>${esc(change.path)}</strong><span>${esc(change.operation.replace('_', ' '))}</span></div><div class="change-actions"><button type="button" class="ghost" data-edit-change-index="${index}">Edit</button><button type="button" class="ghost danger-text" data-remove-change-index="${index}">Remove</button></div></article>`).join('') || '<p class="muted">No source changes.</p>';
}

function renderBuildCommands(commands) {
  setValue('buildCommands', (commands || []).join('\n'));
  if ($('buildCommandPreview')) $('buildCommandPreview').innerHTML = (commands || []).map(command => `<span>›</span><code>${esc(command)}</code>`).join('\n') || '<code>No build commands configured.</code>';
}

function renderWorkflow(wf) {
  renderingWorkflow = true;
  try {
    const packageData = wf.package || {name:wf.package_name || wf.name || ''};
    const source = wf.source || {repository:wf.github_repository || '', tracking:wf.version_tracking || 'latest_release', version:{source:wf.version_source || 'tag', expression:wf.version_expression || ''}};
    const artifact = wf.artifact || {mode:'source_build'};
    const build = wf.build || {};
    const install = wf.install || {};
    const owner = install.owner || {};
    const scripts = install.maintainer_scripts || {};
    const service = wf.service || {};
    window.recipeAdvancedFields = {output: {...(build.output || {})}, timeout: build.timeout || 120, service_description: service.description || '', service_working_directory: service.working_directory || ''};
    setValue('recipeMetaName', wf.name || ''); setValue('recipeMetaPackage', packageData.name || ''); setValue('recipeMetaGithub', source.repository || '');
    setValue('recipeMetaTracking', source.tracking || 'latest_release'); setValue('recipeMetaSourceRef', source.ref || ''); setValue('recipeMetaVersionSource', source.version?.source || 'tag'); setValue('recipeMetaVersionExpression', source.version?.expression || '');
    setValue('recipeArtifactMode', artifact.mode || 'source_build'); setValue('recipeArtifactPattern', artifact.name_pattern || '');
    $('recipeMetaActive').checked = wf.active !== false;
    if ($('buildDetectedProject')) { $('buildDetectedProject').textContent = build.detected_project || 'Not detected'; $('buildDetectedProject').dataset.value = build.detected_project || ''; }
    if ($('buildDetectedFiles')) { $('buildDetectedFiles').textContent = (build.detected_files || []).join(', ') || 'No source inspected yet'; $('buildDetectedFiles').dataset.value = (build.detected_files || []).join('\n'); }
    if ($('buildDetectedDependencies')) { $('buildDetectedDependencies').textContent = (build.detected_dependencies || []).join(', ') || 'None'; $('buildDetectedDependencies').dataset.value = (build.detected_dependencies || []).join('\n'); }
    window.recipeExtraDependencies = [...(build.extra_dependencies || [])]; window.recipeSourceChanges = (build.source_changes || []).map(change => ({...change}));
    renderDependencyChips(); renderSourceChanges(); renderBuildCommands(build.commands || []);
    if ($('buildAvailableDependencies')) $('buildAvailableDependencies').textContent = 'Not checked'; if ($('buildMissingDependencies')) $('buildMissingDependencies').textContent = 'Not checked'; $('buildDependencyState')?.classList.remove('has-missing');
    setValue('buildWorkingDirectory', build.working_directory || '.'); setValue('buildEnvironment', environmentText(build.environment)); setValue('buildExpectedOutput', build.output?.mode === 'source' ? '' : build.output?.mode === 'paths' ? (build.output.paths || []).join(', ') : build.output?.path || 'dist');
    setValue('installDestination', install.destination || ''); setValue('installContentSource', install.content?.source || 'build_output'); setValue('installDirectoryMode', install.directory_mode || '0755'); setValue('installFileMode', install.file_mode || '0644');
    setValue('packageArchitecture', packageData.architecture || 'amd64'); setValue('packageSection', packageData.section || 'misc'); setValue('packagePriority', packageData.priority || 'optional'); setValue('packageMaintainer', packageData.maintainer || ''); setValue('packageDescription', packageData.description || packageData.name); setValue('packageRuntimeDependencies', (packageData.runtime_dependencies || []).join(', '));
    setValue('installOwnerUser', owner.user || packageData.name); setValue('installOwnerGroup', owner.group || packageData.name); if ($('installCreateUser')) $('installCreateUser').checked = owner.create_user === true; if ($('installCreateGroup')) $('installCreateGroup').checked = owner.create_group === true; setValue('installConfigFiles', configFilesText(install.config_files)); setValue('installConfigPolicy', install.config_policy || 'dpkg_conffile');
    setValue('maintainerPreinst', scripts.preinst); setValue('maintainerPostinst', scripts.postinst); setValue('maintainerPrerm', scripts.prerm); setValue('maintainerPostrm', scripts.postrm);
    if ($('serviceConfigured')) $('serviceConfigured').checked = service.configured === true || service.enabled === true; if ($('serviceEnabled')) $('serviceEnabled').checked = service.enabled === true; setValue('serviceType', service.type || ''); setValue('serviceName', service.name || ''); setValue('serviceUser', service.user || ''); setValue('serviceGroup', service.group || ''); setValue('serviceRestart', service.restart || ''); setValue('serviceCommand', service.command);
    setValue('serviceEnvironmentFiles', (service.environment_files || []).join('\n')); setValue('serviceEnvironment', environmentText(service.environment)); setValue('serviceAfter', (service.after || []).join(' ')); setValue('serviceWants', (service.wants || []).join(' ')); setValue('serviceRequires', (service.requires || []).join(' '));
    setValue('serviceRestartSec', service.restart_sec); setValue('serviceTimeoutStartSec', service.timeout_start_sec); setValue('serviceTimeoutStopSec', service.timeout_stop_sec); setValue('serviceKillSignal', service.kill_signal); setValue('serviceExecStartPre', (service.exec_start_pre || []).join('\n')); setValue('serviceExecStartPost', (service.exec_start_post || []).join('\n')); setValue('serviceExecStop', (service.exec_stop || []).join('\n')); setValue('serviceStandardOutput', service.standard_output); setValue('serviceStandardError', service.standard_error);
  } finally { renderingWorkflow = false; toggleVersionExpression(); if (typeof refreshRecipeApplicability === 'function') refreshRecipeApplicability(); }
}
