/* global $, esc */

function insightValue(value, fallback = '—') {
  if (Array.isArray(value)) return value.length ? value.join(', ') : fallback;
  if (value === null || value === undefined || value === '') return fallback;
  return String(value);
}

function insightDetails(rows) {
  return `<dl class="insight-detail-grid">${rows.filter(row => insightValue(row.value, '') !== '').map(row => `<div class="detail-pair"><dt>${esc(row.label)}</dt><dd>${esc(insightValue(row.value))}</dd></div>`).join('')}</dl>`;
}

function insightCode(value) {
  return `<code class="insight-code">${esc(insightValue(value))}</code>`;
}

function insightSectionHeading(title, note = '') {
  return `<div><h3>${esc(title)}</h3>${note ? `<p class="insight-section-note">${esc(note)}</p>` : ''}</div>`;
}

function preflightFacts(rows, className = 'preflight-key-facts') {
  return `<dl class="${className}">${rows.filter(row => insightValue(row.value, '') !== '').map(row => `<div><dt>${esc(row.label)}</dt><dd>${esc(insightValue(row.value))}</dd></div>`).join('')}</dl>`;
}

function preflightTechnicalDetails(title, rows) {
  if (!rows.some(row => insightValue(row.value, '') !== '')) return '';
  return `<details class="preflight-technical-details"><summary>${esc(title)}</summary>${insightDetails(rows)}</details>`;
}

function preflightCount(label, count) {
  return `${count} ${label}${count === 1 ? '' : 's'}`;
}

function stepDetails(result, name) {
  return (result.steps || []).find(step => step.name === name)?.details || {};
}

function executionDiagnosticHtml(execution, {expanded = false, includeDetails = true} = {}) {
  const diagnostic = execution?.diagnostic;
  if (!diagnostic) return '';
  const locations = diagnostic.where || [];
  const facts = diagnostic.facts || [];
  const recipeAction = diagnostic.recipe_step && execution.recipe_id
    ? `<button type="button" class="btn btn--ghost btn--sm diagnostic-recipe-action" data-diagnostic-recipe="${esc(execution.recipe_id)}" data-diagnostic-step="${esc(diagnostic.recipe_step)}">Open Recipe · ${esc(diagnostic.recipe_step)}</button>`
    : '';
  const details = includeDetails ? `<div class="diagnostic-toggle-row"><button type="button" class="btn btn--ghost btn--sm" data-diagnostic-toggle aria-expanded="${expanded ? 'true' : 'false'}">${expanded ? 'Hide details' : 'Show details'}</button></div><div class="diagnostic-details" ${expanded ? '' : 'hidden'}><div class="diagnostic-facts">${facts.map(row => {
    const multiline = String(row.value || '').includes('\n');
    const value = multiline ? `<pre>${esc(row.value)}</pre>` : `<strong>${esc(row.value)}</strong>`;
    return `<div><span>${esc(row.label)}</span>${value}</div>`;
  }).join('')}</div><div class="diagnostic-next"><span>What to do next</span><p>${esc(diagnostic.next_action || 'Review the raw log before retrying.')}</p>${recipeAction}</div></div>` : diagnostic.next_action ? `<div class="diagnostic-next"><span>What to do next</span><p>${esc(diagnostic.next_action)}</p></div>` : '';
  return `<div class="diagnostic-head"><div><span class="eyebrow">Primary diagnostic</span><h4>${esc(diagnostic.title || 'Execution failed')}</h4></div><span class="badge failed">${esc(diagnostic.code || 'failed')}</span></div><p class="diagnostic-reason">${esc(diagnostic.reason || 'No detailed reason was recorded.')}</p>${locations.length ? `<div class="diagnostic-context">${locations.map(row => `<span><b>${esc(row.label)}</b> ${esc(row.value)}</span>`).join('')}</div>` : ''}${details}`;
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
  const expanded = typeof adminState !== 'undefined' && adminState.diagnosticExpandedRunId === execution?.id;
  node.innerHTML = executionDiagnosticHtml(execution, {expanded});
  node.hidden = false;
}

function setExecutionDiagnosticExpanded(expanded) {
  if (typeof adminState === 'undefined' || !adminState.selectedExecution) return;
  adminState.diagnosticExpandedRunId = expanded ? adminState.selectedExecution.id : '';
  renderExecutionDiagnostic(adminState.selectedExecution);
}

function preflightSourceSection(result, workflow = {}) {
  const source = result.source || stepDetails(result, 'source');
  const detection = result.detection || stepDetails(result, 'detection');
  const selected = detection.selected_asset || source.asset || {};
  const artifactMode = workflow.artifact?.mode || source.artifact_mode || result.artifact?.mode;
  const sourceBuildRequired = artifactMode !== 'upstream_deb' && artifactMode !== 'upstream_archive';
  const version = source.upstream_version || result.versions?.upstream || result.version;
  return `<section class="insight-section"><div class="insight-section-head">${insightSectionHeading('Source & project')}</div><div class="preflight-overview"><div class="preflight-overview-identity"><span>Repository</span><strong>${esc(insightValue(source.repository))}</strong>${version ? `<p>${esc(version)}</p>` : ''}</div>${preflightFacts([
    {label:'Source', value:source.strategy || source.archive_source || source.artifact_mode},
    {label:'Project', value:sourceBuildRequired ? detection.display_name || detection.project_type : 'No source build required'},
    {label:'Selected payload', value:selected.name},
  ])}</div>${preflightTechnicalDetails('Source details', [
    {label:'Ref / tag', value:source.ref || source.tag}, {label:'Archive source', value:source.archive_source || source.source},
    {label:'Detected from', value:detection.detected_files}, {label:'Detected build tools', value:detection.build_tools},
  ])}</section>`;
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
  const commands = plan.commands || [];
  const noCommands = !selection.source
    ? 'No build command decision was recorded.'
    : artifactMode !== 'source_build'
      ? 'No build command applies to this upstream artifact mode.'
      : 'No build command is required for this source.';
  const tools = dependencies.tools || dependencies.detected_tools || [];
  const packages = [...(dependencies.detected || []), ...(dependencies.manually_added || [])];
  const missingTools = dependencies.missing_tools || [];
  const missingPackages = dependencies.missing || [];
  const requirementFacts = [
    {label:'Build tools', value:tools}, {label:'Debian packages', value:packages},
  ];
  const hasRequirements = requirementFacts.some(row => Array.isArray(row.value) ? row.value.length : row.value);
  const missing = [...missingTools, ...missingPackages];
  const commandCaption = selection.source === 'detection_proposal' ? 'Detected suggestion; save it in the Recipe before a real Build.' : selection.source === 'recipe' ? 'Saved in the Recipe.' : '';
  return `<section class="insight-section"><div class="insight-section-head">${insightSectionHeading('Build requirements & plan', 'Build commands are not executed during a Test.')}</div>${hasRequirements ? preflightFacts(requirementFacts) : '<p class="preflight-healthy-state">No additional build requirements were found.</p>'}${missing.length ? `<div class="preflight-requirement-problem"><strong>Missing requirements</strong><p>${esc(missing.join(', '))}</p></div>` : ''}<div class="preflight-command-list"><div class="preflight-subhead"><strong>Build commands</strong></div>${commands.length ? commands.map((row, index) => {
    const command = row.command || (row.arguments || []).join(' ');
    const copy = selection.source === 'detection_proposal' ? `<button type="button" class="btn btn--ghost btn--sm" data-copy-preflight-command="${esc(command)}">Copy suggestion</button>` : '';
    return `<div class="preflight-command"><span>${index + 1}</span>${insightCode(command)}${copy}</div>`;
  }).join('') : `<p class="preflight-build-decision">${esc(noCommands)}</p>`}${commandCaption ? `<p class="preflight-command-caption">${esc(commandCaption)}</p>` : ''}</div>${preflightTechnicalDetails('Build details', [
    {label:'Working directory', value:plan.configured_working_directory || plan.working_directory},
    {label:'Environment keys', value:plan.environment_keys},
    {label:'Inactivity timeout', value:plan.inactivity_timeout === null ? 'Disabled' : plan.inactivity_timeout ? `${plan.inactivity_timeout}s` : ''},
    {label:'Maximum runtime', value:plan.maximum_runtime === null ? 'Unlimited' : plan.maximum_runtime ? `${plan.maximum_runtime}s` : ''},
    {label:'Expected output path', value:outputRows(plan.output)},
  ])}</section>`;
}

function preflightChangesSection(result, workflow) {
  const details = result.source_changes || stepDetails(result, 'source_changes');
  const configured = workflow.build?.source_changes || [];
  const applied = details.applied || details.failed?.applied || [];
  const failed = details.failed?.failed || details.failed || (details.failed_index ? details : null);
  if (!configured.length && !applied.length && !failed) return '';
  const byIndex = new Map(applied.map(row => [Number(row.index), row]));
  return `<details class="preflight-secondary-details"><summary><span>Source changes</span><small>${preflightCount('change', configured.length)}</small></summary><div class="preflight-change-list">${configured.map((change, offset) => {
    const index = offset + 1;
    const outcome = byIndex.get(index) || (Number(failed?.index || details.failed_index) === index ? failed : null);
    const status = outcome?.status === 'applied' ? 'Applied' : outcome ? 'Blocked' : 'Not reached';
    const state = status === 'Applied' ? 'applied' : status === 'Blocked' ? 'blocked' : 'pending';
    return `<article class="preflight-change preflight-change--${state}"><span class="preflight-change-state">${esc(status)}</span><div><strong>${index}. ${esc(change.operation || 'change')} · ${esc(change.path || '')}</strong><p>${outcome?.matches === null || outcome?.matches === undefined ? 'No text match required' : `${esc(outcome.matches)} exact match${outcome.matches === 1 ? '' : 'es'}`}${outcome?.anchor ? ` · anchor “${esc(outcome.anchor)}${outcome.anchor_truncated ? '…' : ''}”` : ''}</p></div></article>`;
  }).join('')}</div></details>`;
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
  const payloadSummary = mappings.length ? preflightCount('payload mapping', mappings.length) : staging.content_file_count === undefined ? 'No additional payload mappings' : preflightCount('payload file', staging.content_file_count);
  return `<section class="insight-section insight-section--wide"><div class="insight-section-head">${insightSectionHeading('Debian package plan')}</div><div class="preflight-package-overview"><div><span>Debian package</span><strong>${esc(insightValue(packageData.name))}</strong><p>${esc([staging.version || result.version, packageData.architecture].filter(Boolean).join(' · '))}</p></div><div><span>Payload</span><strong>${esc(payloadSummary)}</strong><p>${mappings.length ? 'Review the install mapping below.' : 'No additional mapping is needed.'}</p></div></div>${mappings.length ? `<div class="preflight-payload-mappings"><strong>Payload mappings</strong><div class="preflight-table" role="table" aria-label="Debian install mappings">${mappings.map((row, index) => `<div class="preflight-table-row" role="row"><strong>${index + 1}</strong><code>${esc(row.source)}</code><span class="preflight-arrow">→</span><code class="preflight-destination">${esc(row.destination)}</code><span class="preflight-mapping-meta">${esc(row.policy)} · ${esc(row.owner)}:${esc(row.group)} · ${esc(row.mode)}</span></div>`).join('')}</div></div>` : ''}${preflightTechnicalDetails('Package details', [
    {label:'Install destination', value:staging.install_destination}, {label:'Payload owner', value:staging.ownership ? `${staging.ownership.user}:${staging.ownership.group}` : ''},
    {label:'Default modes', value:staging.permissions ? `directories ${staging.permissions.directories} · files ${staging.permissions.files}` : ''},
    {label:'Account', value:staging.account ? `${staging.account.user}:${staging.account.group} · user ${staging.account.create_user ? 'created' : 'existing'} · group ${staging.account.create_group ? 'created' : 'existing'}` : ''},
    {label:'Persistent directories', value:directories.map(row => `${row.path} · ${row.owner}:${row.group} · ${row.mode}`)},
  ])}<div class="insight-disclosures">${disclosure('Prepared DEBIAN/control', control)}${Object.entries(scripts).map(([name, content]) => disclosure(`Prepared ${name}`, content)).join('')}</div></section>`;
}

function preflightServiceSection(result, workflow) {
  const staging = result.staging || stepDetails(result, 'staging');
  const systemd = staging.systemd || stepDetails(result, 'systemd');
  const service = workflow.service || {};
  if (!systemd.configured && !service.configured && !service.name) return '';
  return `<details class="preflight-secondary-details"><summary><span>Systemd service</span><small>${esc(service.name || systemd.path || 'configured')}</small></summary>${insightDetails([
    {label:'Unit', value:service.name || systemd.path}, {label:'ExecStart', value:service.command},
    {label:'User / group', value:[service.user, service.group].filter(Boolean).join(':')}, {label:'WorkingDirectory', value:service.working_directory},
    {label:'Restart', value:service.restart}, {label:'After', value:service.after}, {label:'Wants', value:service.wants},
    {label:'Requires', value:service.requires}, {label:'Environment keys', value:Object.keys(service.environment || {})},
  ])}${disclosure('Prepared systemd unit', systemd.content || '')}</details>`;
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

function preflightReportPresentation(result, workflow) {
  const findings = preflightFindings(result, workflow);
  const blockers = findings.filter(row => row.level === 'blocker').length;
  const warnings = findings.filter(row => row.level === 'warning').length;
  const status = blockers ? 'Action required' : warnings ? 'Review warnings' : 'Ready for Build';
  const statusClass = blockers ? 'locked' : warnings ? 'warning' : 'active';
  return {
    status, statusClass,
    summary: `${blockers} blocker${blockers === 1 ? '' : 's'} · ${warnings} warning${warnings === 1 ? '' : 's'} · commands not executed`,
    content: `<div class="preflight-grid">${preflightSourceSection(result, workflow)}${preflightBuildSection(result, workflow)}${preflightDebianSection(result, workflow)}</div><section class="preflight-findings"><div class="insight-section-head">${insightSectionHeading('Warnings & blockers')}</div><div class="preflight-finding-list">${findings.map(row => `<article class="preflight-finding preflight-finding--${esc(row.level)}"><span>${esc(row.level === 'information' ? 'Note' : row.level)}</span><p>${esc(row.text)}</p></article>`).join('')}</div></section><div class="preflight-secondary-stack">${preflightChangesSection(result, workflow)}${preflightServiceSection(result, workflow)}</div>`,
  };
}

function renderPreflightReport(result, workflow) {
  const node = $('recipePreflight');
  const content = $('recipePreflightContent');
  if (!node || !content) return;
  const presentation = preflightReportPresentation(result, workflow);
  $('recipePreflightStatus').className = `settings-badge ${presentation.statusClass}`;
  $('recipePreflightStatus').textContent = presentation.status;
  $('recipePreflightSummary').textContent = presentation.summary;
  content.innerHTML = presentation.content;
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
