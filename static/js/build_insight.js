/* global $, esc */

function insightOrigin(label, kind = 'configured') {
  return `<span class="value-origin ${esc(kind)}-origin">${esc(label)}</span>`;
}

function insightValue(value, fallback = '—') {
  if (Array.isArray(value)) return value.length ? value.join(', ') : fallback;
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

function insightDetails(rows) {
  return `<dl class="insight-detail-grid">${rows.filter(row => insightValue(row.value, '') !== '').map(row => `<div class="detail-pair"><dt>${esc(row.label)}</dt><dd>${esc(insightValue(row.value))}${row.origin ? insightOrigin(row.origin.label, row.origin.kind) : ''}</dd></div>`).join('')}</dl>`;
}

function insightCode(value) {
  return `<code class="insight-code">${esc(insightValue(value))}</code>`;
}

function stepDetails(result, name) {
  return (result.steps || []).find(step => step.name === name)?.details || {};
}

function renderExecutionDiagnostic(execution) {
  const node = $('executionDiagnostic');
  if (!node) return;
  const diagnostic = execution?.diagnostic;
  if (!diagnostic) {
    node.hidden = true;
    node.innerHTML = '';
    return;
  }
  const locations = diagnostic.where || [];
  const facts = diagnostic.facts || [];
  const rows = [...locations, ...facts];
  const recipeAction = diagnostic.recipe_step && execution.recipe_id
    ? `<button type="button" class="btn btn--ghost btn--sm diagnostic-recipe-action" data-diagnostic-recipe="${esc(execution.recipe_id)}" data-diagnostic-step="${esc(diagnostic.recipe_step)}">Open Recipe · ${esc(diagnostic.recipe_step)}</button>`
    : '';
  node.innerHTML = `<div class="diagnostic-head"><div><span class="eyebrow">Primary diagnostic</span><h4>${esc(diagnostic.title || 'Execution failed')}</h4></div><span class="badge failed">${esc(diagnostic.code || 'failed')}</span></div><div class="diagnostic-summary"><div><span>Why</span><p>${esc(diagnostic.reason || 'No detailed reason was recorded.')}</p></div>${rows.length ? `<div class="diagnostic-facts">${rows.map(row => {
    const multiline = String(row.value || '').includes('\n');
    const value = multiline ? `<pre>${esc(row.value)}</pre>` : `<strong>${esc(row.value)}</strong>`;
    return `<div><span>${esc(row.label)}</span>${value}</div>`;
  }).join('')}</div>` : ''}<div class="diagnostic-next"><span>What to do next</span><p>${esc(diagnostic.next_action || 'Review the raw log before retrying.')}</p>${recipeAction}</div></div>`;
  node.hidden = false;
}

function preflightSourceSection(result) {
  const source = result.source || stepDetails(result, 'source');
  const detection = result.detection || stepDetails(result, 'detection');
  const selected = detection.selected_asset || source.asset || {};
  return `<section class="insight-section"><div class="insight-section-head"><div><span class="eyebrow">Resolved</span><h3>Source & project</h3></div>${insightOrigin('Resolved', 'resolved')}</div>${insightDetails([
    {label:'Repository', value:source.repository}, {label:'Strategy', value:source.strategy || source.artifact_mode},
    {label:'Ref / tag', value:source.ref || source.tag}, {label:'Upstream version', value:source.upstream_version || result.versions?.upstream},
    {label:'Selected asset', value:selected.name}, {label:'Archive source', value:source.archive_source || source.source},
    {label:'Project type', value:detection.display_name || detection.project_type, origin:{label:'Detected', kind:'detected'}},
    {label:'Detected from', value:detection.detected_files, origin:{label:'Detected', kind:'detected'}},
    {label:'Build tools', value:detection.build_tools, origin:{label:'Detected', kind:'detected'}},
  ])}</section>`;
}

function dependencyGroup(label, values, origin, kind) {
  return `<div class="preflight-list-group"><div><strong>${esc(label)}</strong>${insightOrigin(origin, kind)}</div><p>${esc(insightValue(values, 'None'))}</p></div>`;
}

function outputRows(output = {}) {
  if (output.mode === 'paths') return (output.paths || []).map(row => row.configured_path || row.path);
  return [output.configured_path || output.path || output.mode].filter(Boolean);
}

function preflightBuildSection(result, workflow = {}) {
  const dependencies = result.dependencies || stepDetails(result, 'dependencies');
  const build = result.build || stepDetails(result, 'build');
  const plan = build.plan || {};
  const selection = plan.selection || {};
  const artifactMode = workflow.artifact?.mode || 'source_build';
  const commandOrigin = selection.source === 'detection_proposal'
    ? {label:'Suggested', kind:'suggested'}
    : selection.source === 'recipe'
      ? {label:'Configured', kind:'configured'}
      : ['static', 'detected_source'].includes(selection.source) || artifactMode !== 'source_build'
        ? {label:'Not required', kind:'resolved'}
        : {label:'Not recorded', kind:'resolved'};
  const commands = plan.commands || [];
  const noCommands = commandOrigin.label === 'Not recorded'
    ? 'No build command decision was recorded.'
    : artifactMode !== 'source_build'
      ? 'No build command applies to this upstream artifact mode.'
      : 'No build command is required for this source.';
  const workingDirectoryOrigin = plan.configured_working_directory !== undefined
    ? {label:'Configured', kind:'configured'} : {label:'Resolved', kind:'resolved'};
  return `<section class="insight-section"><div class="insight-section-head"><div><span class="eyebrow">Checked</span><h3>Dependencies & build plan</h3></div><span class="settings-badge warning">Commands not executed</span></div><div class="preflight-dependency-groups">${dependencyGroup('Build tools', dependencies.tools || dependencies.detected_tools, 'Detected', 'detected')}${dependencyGroup('Detected Debian packages', dependencies.detected, 'Detected', 'detected')}${dependencyGroup('Manual Debian packages', dependencies.manually_added, 'Configured', 'configured')}${dependencyGroup('Unavailable tools', dependencies.missing_tools, 'Checked', 'resolved')}${dependencyGroup('Missing Debian packages', dependencies.missing, 'Checked', 'resolved')}</div>${insightDetails([
    {label:'Working directory', value:plan.configured_working_directory || plan.working_directory, origin:workingDirectoryOrigin},
    {label:'Environment keys', value:plan.environment_keys, origin:{label:'Configured', kind:'configured'}},
    {label:'Inactivity timeout', value:plan.inactivity_timeout === null ? 'Disabled' : plan.inactivity_timeout ? `${plan.inactivity_timeout}s` : ''},
    {label:'Maximum runtime', value:plan.maximum_runtime === null ? 'Unlimited' : plan.maximum_runtime ? `${plan.maximum_runtime}s` : ''},
    {label:'Expected output path', value:outputRows(plan.output), origin:{label:'Configured', kind:'configured'}},
  ])}<div class="preflight-command-list"><div class="preflight-subhead"><strong>Build commands</strong>${insightOrigin(commandOrigin.label, commandOrigin.kind)}</div>${commands.length ? commands.map((row, index) => {
    const command = row.command || (row.arguments || []).join(' ');
    const copy = selection.source === 'detection_proposal' ? `<button type="button" class="btn btn--ghost btn--sm" data-copy-preflight-command="${esc(command)}">Copy suggestion</button>` : '';
    return `<div class="preflight-command"><span>${index + 1}</span>${insightCode(command)}${copy}</div>`;
  }).join('') : `<p class="muted">${esc(noCommands)}</p>`}${selection.source === 'detection_proposal' ? '<p class="preflight-note">Detected suggestions are read-only here and are not saved in the Recipe. Copy one, review it, then add it explicitly with Edit commands before a real Build.</p>' : ''}</div></section>`;
}

function preflightChangesSection(result, workflow) {
  const details = result.source_changes || stepDetails(result, 'source_changes');
  const configured = workflow.build?.source_changes || [];
  const applied = details.applied || details.failed?.applied || [];
  const failed = details.failed?.failed || details.failed || (details.failed_index ? details : null);
  if (!configured.length && !applied.length && !failed) return '';
  const byIndex = new Map(applied.map(row => [Number(row.index), row]));
  return `<section class="insight-section insight-section--wide"><div class="insight-section-head"><div><span class="eyebrow">Applied to resolved source</span><h3>Source changes</h3></div>${insightOrigin('Configured', 'configured')}</div><div class="preflight-change-list">${configured.map((change, offset) => {
    const index = offset + 1;
    const outcome = byIndex.get(index) || (Number(failed?.index || details.failed_index) === index ? failed : null);
    const status = outcome?.status === 'applied' ? 'Applied' : outcome ? 'Blocked' : 'Not reached';
    const state = status === 'Applied' ? 'active' : status === 'Blocked' ? 'locked' : 'neutral';
    return `<article class="preflight-change"><span class="settings-badge ${state}">${esc(status)}</span><div><strong>${index}. ${esc(change.operation || 'change')} · ${esc(change.path || '')}</strong><p>${outcome?.matches === null || outcome?.matches === undefined ? 'No text match required' : `${esc(outcome.matches)} exact match${outcome.matches === 1 ? '' : 'es'}`}${outcome?.anchor ? ` · anchor “${esc(outcome.anchor)}${outcome.anchor_truncated ? '…' : ''}”` : ''}</p></div></article>`;
  }).join('')}</div></section>`;
}

function disclosure(title, content) {
  if (!content) return '';
  return `<details class="insight-disclosure"><summary>${esc(title)}</summary><pre>${esc(content)}</pre></details>`;
}

function preflightDebianSection(result, workflow) {
  const staging = result.staging || stepDetails(result, 'staging');
  const metadata = stepDetails(result, 'debian_metadata');
  const mappings = staging.configurations || metadata.configurations || [];
  const directories = staging.directories || [];
  const scripts = staging.maintainer_scripts || metadata.maintainer_scripts || {};
  const control = staging.control || metadata.control || '';
  const packageData = workflow.package || {};
  return `<section class="insight-section"><div class="insight-section-head"><div><span class="eyebrow">Prepared</span><h3>Debian package plan</h3></div>${insightOrigin('Prepared', 'prepared')}</div>${insightDetails([
    {label:'Package', value:packageData.name}, {label:'Version', value:staging.version || result.version},
    {label:'Architecture', value:packageData.architecture}, {label:'Install destination', value:staging.install_destination},
    {label:'Payload owner', value:staging.ownership ? `${staging.ownership.user}:${staging.ownership.group}` : ''},
    {label:'Default modes', value:staging.permissions ? `directories ${staging.permissions.directories} · files ${staging.permissions.files}` : ''},
    {label:'Account', value:staging.account ? `${staging.account.user}:${staging.account.group} · user ${staging.account.create_user ? 'created' : 'existing'} · group ${staging.account.create_group ? 'created' : 'existing'}` : ''},
    {label:'Payload files', value:staging.content_file_count === undefined ? '' : `${staging.content_file_count} available in manifest`},
  ])}<div class="preflight-table" role="table" aria-label="Debian install mappings">${mappings.length ? mappings.map((row, index) => `<div class="preflight-table-row" role="row"><strong>${index + 1}</strong><code>${esc(row.source)}</code><span class="preflight-arrow">→</span><code class="preflight-destination">${esc(row.destination)}</code><span class="preflight-mapping-meta">${esc(row.policy)} · ${esc(row.owner)}:${esc(row.group)} · ${esc(row.mode)}</span></div>`).join('') : '<p class="muted">No additional file mapping.</p>'}</div>${directories.length ? `<div class="preflight-directory-list"><strong>Persistent directories</strong>${directories.map(row => `<code>${esc(row.path)} · ${esc(row.owner)}:${esc(row.group)} · ${esc(row.mode)}</code>`).join('')}</div>` : ''}<div class="insight-disclosures">${disclosure('Prepared DEBIAN/control', control)}${Object.entries(scripts).map(([name, content]) => disclosure(`Prepared ${name}`, content)).join('')}</div></section>`;
}

function preflightServiceSection(result, workflow) {
  const staging = result.staging || stepDetails(result, 'staging');
  const systemd = staging.systemd || stepDetails(result, 'systemd');
  const service = workflow.service || {};
  if (!systemd.configured && !service.configured && !service.name) return '';
  return `<section class="insight-section"><div class="insight-section-head"><div><span class="eyebrow">Prepared</span><h3>Systemd service</h3></div>${insightOrigin('Prepared', 'prepared')}</div>${insightDetails([
    {label:'Unit', value:service.name || systemd.path}, {label:'ExecStart', value:service.command},
    {label:'User / group', value:[service.user, service.group].filter(Boolean).join(':')}, {label:'WorkingDirectory', value:service.working_directory},
    {label:'Restart', value:service.restart}, {label:'After', value:service.after}, {label:'Wants', value:service.wants},
    {label:'Requires', value:service.requires}, {label:'Environment keys', value:Object.keys(service.environment || {})},
  ])}${disclosure('Prepared systemd unit', systemd.content || '')}</section>`;
}

function preflightFindings(result, workflow = {}) {
  const detection = result.detection || stepDetails(result, 'detection');
  const dependencies = result.dependencies || stepDetails(result, 'dependencies');
  const build = result.build || stepDetails(result, 'build');
  const staging = result.staging || stepDetails(result, 'staging');
  const findings = [{level:'information', text:'Build commands and dpkg-deb were not executed during this Test.'}];
  (detection.warnings || []).forEach(text => findings.push({level:'warning', text}));
  (staging.warnings || []).forEach(text => findings.push({
    level:String(text).includes('because build commands are not executed during dry-run') ? 'information' : 'warning', text,
  }));
  if ((build.plan?.selection || {}).source === 'detection_proposal') findings.push({level:'blocker', text:'Detected build commands are suggestions and must be reviewed and saved before a real Build.'});
  if (result.status !== 'failed' && (workflow.artifact?.mode || 'source_build') === 'source_build' && !(build.plan?.selection || {}).source) {
    findings.push({level:'warning', text:'The build command decision was not recorded; this report cannot confirm whether commands are required.'});
  }
  if ((dependencies.missing_tools || []).length) findings.push({level:'blocker', text:`Unavailable build tools: ${dependencies.missing_tools.join(', ')}`});
  if ((dependencies.missing || []).length) findings.push({level:'blocker', text:`Missing Debian build dependencies: ${dependencies.missing.join(', ')}`});
  if (result.status === 'failed') findings.push({level:'blocker', text:result.error?.message || 'The preflight stopped on an error.'});
  const unique = findings.filter((row, index) => findings.findIndex(candidate => candidate.level === row.level && candidate.text === row.text) === index);
  return unique;
}

function renderPreflightReport(result, workflow) {
  const node = $('recipePreflight');
  const content = $('recipePreflightContent');
  if (!node || !content) return;
  const findings = preflightFindings(result, workflow);
  const blockers = findings.filter(row => row.level === 'blocker').length;
  const warnings = findings.filter(row => row.level === 'warning').length;
  const status = blockers ? 'Action required' : warnings ? 'Review warnings' : 'Ready for Build';
  const statusClass = blockers ? 'locked' : warnings ? 'warning' : 'active';
  $('recipePreflightStatus').className = `settings-badge ${statusClass}`;
  $('recipePreflightStatus').textContent = status;
  $('recipePreflightSummary').textContent = `${blockers} blocker${blockers === 1 ? '' : 's'} · ${warnings} warning${warnings === 1 ? '' : 's'} · commands not executed`;
  content.innerHTML = `<div class="preflight-origin-legend">${insightOrigin('Configured', 'configured')}${insightOrigin('Detected', 'detected')}${insightOrigin('Suggested', 'suggested')}${insightOrigin('Resolved', 'resolved')}${insightOrigin('Prepared', 'prepared')}</div><div class="preflight-grid">${preflightSourceSection(result)}${preflightBuildSection(result, workflow)}${preflightChangesSection(result, workflow)}${preflightDebianSection(result, workflow)}${preflightServiceSection(result, workflow)}</div><section class="preflight-findings"><div class="insight-section-head"><div><span class="eyebrow">Decision</span><h3>Warnings & blockers</h3></div></div><div class="preflight-finding-list">${findings.map(row => `<article class="preflight-finding ${esc(row.level)}"><span>${esc(row.level)}</span><p>${esc(row.text)}</p></article>`).join('')}</div></section>`;
  node.dataset.stale = 'false';
  node.hidden = false;
  node.scrollIntoView?.({behavior:'smooth', block:'start'});
}

function clearPreflightReport() {
  const node = $('recipePreflight');
  if (node) { node.hidden = true; node.dataset.stale = 'false'; }
  if ($('recipePreflightContent')) $('recipePreflightContent').innerHTML = '';
}

function markPreflightStale() {
  const node = $('recipePreflight');
  if (!node || node.hidden || node.dataset.stale === 'true') return;
  node.dataset.stale = 'true';
  if ($('recipePreflightStatus')) {
    $('recipePreflightStatus').className = 'settings-badge warning';
    $('recipePreflightStatus').textContent = 'Recipe changed · run Test again';
  }
}
