/* global $ */
window.recipeSourceChanges = [];
window.recipeExtraDependencies = [];
window.recipeAdvancedFields = {};
window.recipeBuildOutput = {mode:'source', path:'', paths:[]};
window.recipeSuggestedOutputPaths = [];
window.recipeInstallMappings = [];
window.recipeServiceVisible = false;

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

function normalizeInstallMapping(row) {
  if (typeof row === 'string') return {source:row.replace(/^\/+/, ''), destination:row, policy:'dpkg_conffile'};
  return {source:String(row?.source || ''), destination:String(row?.destination || ''), policy:String(row?.policy || 'dpkg_conffile')};
}

function collectInstallMappings() {
  return (window.recipeInstallMappings || []).map(row => {
    const source = String(row.source || '').trim();
    const destination = String(row.destination || '').trim();
    return {source, destination, policy:['replace','dpkg_conffile','create_if_missing'].includes(row.policy) ? row.policy : 'dpkg_conffile'};
  }).filter(row => row.source || row.destination);
}

function installMappingRowHtml(row, {editable = false, index = 0} = {}) {
  const source = esc(row.source || '');
  const destination = esc(row.destination || '');
  if (!editable) return `<article class="install-mapping-row install-mapping-row-automatic" aria-label="Automatically installed mapping"><span class="mapping-readonly-value">${source}</span><span class="mapping-arrow">→</span><span class="mapping-readonly-value">${destination}</span><span class="mapping-column-placeholder" aria-hidden="true"></span><span class="mapping-column-placeholder" aria-hidden="true"></span></article>`;
  return `<article class="install-mapping-row"><input value="${source}" data-install-mapping-source="${index}" aria-label="Mapping source ${index + 1}" placeholder="dist/foo" required><span class="mapping-arrow">→</span><input value="${destination}" data-install-mapping-destination="${index}" aria-label="Mapping destination ${index + 1}" placeholder="/usr/bin/foo" required><select data-install-mapping-policy="${index}" aria-label="Mapping policy ${index + 1}"><option value="replace"${row.policy === 'replace' ? ' selected' : ''}>Replace</option><option value="dpkg_conffile"${row.policy === 'dpkg_conffile' ? ' selected' : ''}>Preserve if existing</option><option value="create_if_missing"${row.policy === 'create_if_missing' ? ' selected' : ''}>Create if missing</option></select><button type="button" class="ghost danger-text" data-remove-install-mapping="${index}">Remove</button></article>`;
}

function renderInstallMappings() {
  const mappings = window.recipeInstallMappings || [];
  if ($('installMappingList')) $('installMappingList').innerHTML = mappings.map((row, index) => installMappingRowHtml(row, {editable:true, index})).join('');
  if ($('installMappingEmpty')) $('installMappingEmpty').hidden = mappings.length !== 0;
}

function addInstallMapping() {
  window.recipeInstallMappings.push({source:'', destination:'', policy:'dpkg_conffile'});
  renderInstallMappings();
  const inputs = document.querySelectorAll('input[data-install-mapping-source]');
  inputs[inputs.length - 1]?.focus();
}

function removeInstallMapping(index) {
  window.recipeInstallMappings.splice(index, 1);
  renderInstallMappings();
}

function value(id) { return $(id)?.value.trim() || ''; }
function setValue(id, next) { if ($(id)) $(id).value = next ?? ''; }

function collectBuildOutput() {
  const output = window.recipeBuildOutput || {mode:'source', path:'', paths:[]};
  if (output.mode === 'paths') return {mode:'paths', paths:(output.paths || []).map(item => String(item).trim()).filter(Boolean)};
  if (output.mode === 'path') return {mode:'path', path:String(output.path || '').trim()};
  return {mode:'source'};
}

function buildOutputIsComplete(output = collectBuildOutput()) {
  if (output.mode === 'path') return !!output.path;
  if (output.mode === 'paths') return (output.paths || []).length > 0;
  return true;
}

function renderBuildOutputSuggestions() {
  const section = $('buildOutputSuggestions');
  const list = $('buildOutputSuggestionList');
  if (!section || !list) return;
  const configuredOutput = window.recipeBuildOutput || {};
  const configured = new Set([configuredOutput.path, ...(configuredOutput.paths || [])].map(item => String(item || '').trim()).filter(Boolean));
  const suggestions = (window.recipeSuggestedOutputPaths || []).filter(item => item && !configured.has(item));
  section.hidden = suggestions.length === 0;
  list.innerHTML = suggestions.map(item => `<div class="build-output-suggestion"><code>${esc(item)}</code><span class="value-origin suggested-origin">Suggested</span><button type="button" class="ghost compact-button" data-add-output-suggestion="${esc(item)}">Add</button></div>`).join('');
}

function renderBuildOutput() {
  const output = window.recipeBuildOutput || {mode:'source', path:'', paths:[]};
  setValue('buildOutputMode', output.mode);
  if ($('buildOutputPathField')) $('buildOutputPathField').hidden = output.mode !== 'path';
  if ($('buildOutputPathsEditor')) $('buildOutputPathsEditor').hidden = output.mode !== 'paths';
  setValue('buildOutputPath', output.path || '');
  if ($('buildOutputPath')) $('buildOutputPath').required = output.mode === 'path';
  const paths = output.paths || [];
  if ($('buildOutputPathList')) $('buildOutputPathList').innerHTML = paths.map((item, index) => `<article class="build-output-path-row"><input value="${esc(item)}" data-output-path-index="${index}" aria-label="Included output path ${index + 1}" placeholder="Relative file or directory path" required><button type="button" class="ghost danger-text" data-remove-output-path="${index}">Remove</button></article>`).join('');
  if ($('buildOutputPathEmpty')) $('buildOutputPathEmpty').hidden = paths.length !== 0;
  renderBuildOutputSuggestions();
  if (typeof renderInstallContentSummary === 'function') renderInstallContentSummary();
}

function setBuildOutputMode(mode) {
  const nextMode = ['source','path','paths'].includes(mode) ? mode : 'source';
  const output = window.recipeBuildOutput || {mode:'source', path:'', paths:[]};
  if (nextMode === 'path' && !output.path && output.paths?.length) output.path = output.paths[0];
  if (nextMode === 'paths' && !output.paths?.length && output.path) output.paths = [output.path];
  output.mode = nextMode;
  window.recipeBuildOutput = output;
  renderBuildOutput();
}

function addBuildOutputPath(path = '') {
  if (!Array.isArray(window.recipeBuildOutput.paths)) window.recipeBuildOutput.paths = [];
  window.recipeBuildOutput.paths.push(path);
  renderBuildOutput();
  const inputs = document.querySelectorAll('input[data-output-path-index]');
  inputs[inputs.length - 1]?.focus();
}

function removeBuildOutputPath(index) {
  window.recipeBuildOutput.paths.splice(index, 1);
  renderBuildOutput();
}

function setBuildOutputSuggestions(paths) {
  window.recipeSuggestedOutputPaths = [...new Set((paths || []).map(item => String(item).trim()).filter(Boolean))];
  renderBuildOutputSuggestions();
}

function collectWorkflow() {
  const name = value('recipeMetaName') || $('workflowName')?.value || 'recipe';
  const packageName = value('recipeMetaPackage') || name;
  const configuredFiles = $('installContentSource')?.value === 'configured_files';
  const accountProvisioning = $('installAccountProvisioning')?.value || 'existing';
  const ownerUser = value('installOwnerUser') || packageName;
  const ownerGroup = value('installOwnerGroup') || packageName;
  const createUser = accountProvisioning === 'ensure' ? ownerUser !== 'root' : accountProvisioning === 'custom' && !!$('installCreateUser')?.checked;
  const createGroup = accountProvisioning === 'ensure' ? ownerGroup !== 'root' : accountProvisioning === 'custom' && !!$('installCreateGroup')?.checked;
  const serviceVisible = !!window.recipeServiceVisible;
  const serviceComplete = !!value('serviceName') && !!value('serviceCommand');
  const advanced = window.recipeAdvancedFields || {};
  return {
    schema_version: 1,
    name,
    active: !!$('recipeMetaActive')?.checked,
    package: {
      name: packageName,
      version_revision: advanced.version_revision || '1',
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
      output: collectBuildOutput()
    },
    install: {
      destination: configuredFiles ? '' : (value('installDestination') || `/opt/${packageName}`),
      content: {source: $('installContentSource')?.value || 'build_output', path: ''},
      owner: {user: ownerUser, group: ownerGroup, create_user: createUser, create_group: createGroup},
      directory_mode: $('installDirectoryMode')?.value || '0755',
      file_mode: $('installFileMode')?.value || '0644',
      config_files: collectInstallMappings(),
      maintainer_scripts: {preinst: value('maintainerPreinst'), postinst: value('maintainerPostinst'), prerm: value('maintainerPrerm'), postrm: value('maintainerPostrm')}
    },
    service: {
      enabled: serviceVisible && serviceComplete && !!$('serviceEnabled')?.checked,
      name: serviceVisible ? value('serviceName') : '',
      description: serviceVisible ? (advanced.service_description || packageName) : '',
      type: serviceVisible ? ($('serviceType')?.value || 'simple') : '',
      user: serviceVisible ? value('serviceUser') : '',
      group: serviceVisible ? value('serviceGroup') : '',
      restart: serviceVisible ? ($('serviceRestart')?.value || 'on-failure') : '',
      command: serviceVisible ? value('serviceCommand') : '',
      environment_files: lines(value('serviceEnvironmentFiles')),
      environment: environment(value('serviceEnvironment')),
      after: lines(value('serviceAfter')), wants: lines(value('serviceWants')), requires: lines(value('serviceRequires')),
      restart_sec: value('serviceRestartSec'), timeout_start_sec: value('serviceTimeoutStartSec'), timeout_stop_sec: value('serviceTimeoutStopSec'),
      kill_signal: $('serviceKillSignal')?.value || '',
      exec_start_pre: lines(value('serviceExecStartPre')), exec_start_post: lines(value('serviceExecStartPost')), exec_stop: lines(value('serviceExecStop')),
      standard_output: $('serviceStandardOutput')?.value || '', standard_error: $('serviceStandardError')?.value || '',
      working_directory: serviceVisible ? (advanced.service_working_directory || '') : ''
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
  if ($('buildCommandPreview')) {
    $('buildCommandPreview').classList.toggle('build-command-empty', !(commands || []).length);
    $('buildCommandPreview').innerHTML = (commands || []).map(command => `<span>›</span><code>${esc(command)}</code>`).join('\n') || 'No build commands required.';
  }
  if (typeof refreshRecipeApplicability === 'function') refreshRecipeApplicability();
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
    window.recipeAdvancedFields = {version_revision: packageData.version_revision || '1', timeout: build.timeout || 120, service_description: service.description || '', service_working_directory: service.working_directory || ''};
    const configuredOutput = build.output || {};
    const outputMode = ['source','path','paths'].includes(configuredOutput.mode) ? configuredOutput.mode : (configuredOutput.path ? 'path' : 'source');
    window.recipeBuildOutput = {mode:outputMode, path:configuredOutput.path || '', paths:[...(configuredOutput.paths || [])]};
    window.recipeSuggestedOutputPaths = [];
    setValue('recipeMetaName', wf.name || ''); setValue('recipeMetaPackage', packageData.name || ''); setValue('recipeMetaGithub', source.repository || '');
    setValue('recipeMetaTracking', source.tracking || 'latest_release'); setValue('recipeMetaSourceRef', source.ref || ''); setValue('recipeMetaVersionSource', source.version?.source || 'tag'); setValue('recipeMetaVersionExpression', source.version?.expression || '');
    setValue('recipeArtifactMode', artifact.mode || 'source_build'); setValue('recipeArtifactPattern', artifact.name_pattern || '');
    $('recipeMetaActive').checked = wf.active !== false;
    if (typeof renderBuildEnvironment === 'function') renderBuildEnvironment({project_type:build.detected_project || '', detected_files:build.detected_files || [], build_dependencies:build.detected_dependencies || []});
    window.recipeExtraDependencies = [...(build.extra_dependencies || [])]; window.recipeSourceChanges = (build.source_changes || []).map(change => ({...change}));
    renderDependencyChips(); renderSourceChanges(); renderBuildCommands(build.commands || []);
    if (typeof renderDependencyCheck === 'function') renderDependencyCheck();
    setValue('buildWorkingDirectory', build.working_directory || '.'); setValue('buildEnvironment', environmentText(build.environment)); renderBuildOutput();
    setValue('installDestination', install.destination || ''); setValue('installContentSource', install.content?.source || 'build_output'); setValue('installDirectoryMode', install.directory_mode || '0755'); setValue('installFileMode', install.file_mode || '0644');
    setValue('packageArchitecture', packageData.architecture || 'amd64'); setValue('packageSection', packageData.section || 'misc'); setValue('packagePriority', packageData.priority || 'optional'); setValue('packageMaintainer', packageData.maintainer || ''); setValue('packageDescription', packageData.description || packageData.name); setValue('packageRuntimeDependencies', (packageData.runtime_dependencies || []).join(', '));
    setValue('installOwnerUser', owner.user || packageData.name); setValue('installOwnerGroup', owner.group || packageData.name); if ($('installCreateUser')) $('installCreateUser').checked = owner.create_user === true; if ($('installCreateGroup')) $('installCreateGroup').checked = owner.create_group === true; if (typeof renderAccountProvisioning === 'function') renderAccountProvisioning(owner); window.recipeInstallMappings = (install.config_files || []).map(row => normalizeInstallMapping(typeof row === 'string' ? row : {...row, policy:row.policy || install.config_policy || 'dpkg_conffile'})); renderInstallMappings();
    setValue('maintainerPreinst', scripts.preinst); setValue('maintainerPostinst', scripts.postinst); setValue('maintainerPrerm', scripts.prerm); setValue('maintainerPostrm', scripts.postrm);
    window.recipeServiceVisible = !!String(service.name || '').trim() && !!String(service.command || '').trim(); if ($('serviceEnabled')) $('serviceEnabled').checked = service.enabled === true; setValue('serviceType', service.type || ''); setValue('serviceName', service.name || ''); setValue('serviceUser', service.user || ''); setValue('serviceGroup', service.group || ''); setValue('serviceRestart', service.restart || ''); setValue('serviceCommand', service.command);
    setValue('serviceEnvironmentFiles', (service.environment_files || []).join('\n')); setValue('serviceEnvironment', environmentText(service.environment)); setValue('serviceAfter', (service.after || []).join(' ')); setValue('serviceWants', (service.wants || []).join(' ')); setValue('serviceRequires', (service.requires || []).join(' '));
    setValue('serviceRestartSec', service.restart_sec); setValue('serviceTimeoutStartSec', service.timeout_start_sec); setValue('serviceTimeoutStopSec', service.timeout_stop_sec); setValue('serviceKillSignal', service.kill_signal); setValue('serviceExecStartPre', (service.exec_start_pre || []).join('\n')); setValue('serviceExecStartPost', (service.exec_start_post || []).join('\n')); setValue('serviceExecStop', (service.exec_stop || []).join('\n')); setValue('serviceStandardOutput', service.standard_output); setValue('serviceStandardError', service.standard_error);
  } finally { renderingWorkflow = false; toggleVersionExpression(); if (typeof refreshRecipeApplicability === 'function') refreshRecipeApplicability(); }
}
