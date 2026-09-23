const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function element() {
  return {hidden:true, innerHTML:'', textContent:'', className:'', dataset:{}, scrollIntoView() { this.scrolled = true; }};
}

const nodes = Object.fromEntries(['executionDiagnostic','recipePreflight','recipePreflightContent','recipePreflightStatus','recipePreflightSummary'].map(id => [id, element()]));
const context = vm.createContext({
  adminState: {selectedExecution: null, diagnosticExpandedRunId: ''},
  $: id => nodes[id] || null,
  esc: value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character])),
});
vm.runInContext(fs.readFileSync('static/js/build_insight.js', 'utf8'), context, {filename:'build_insight.js'});

const diagnosticExecution = {id:'failed-run', recipe_id:'demo', diagnostic:{
  title:'Build command timed out', code:'build_command_timeout', reason:'Command exceeded its limit',
  where:[{label:'Step', value:'Build'}, {label:'Working directory', value:'/source'}],
  facts:[{label:'Command', value:'npm run a-very-long-build-command-with-many-arguments-and-no-shortcut'}, {label:'Timeout reason', value:'maximum_runtime'}],
  next_action:'Increase the configured maximum runtime.', recipe_step:'build',
}};
context.adminState.selectedExecution = diagnosticExecution;
context.renderExecutionDiagnostic(diagnosticExecution);
assert.equal(nodes.executionDiagnostic.hidden, false);
assert.match(nodes.executionDiagnostic.innerHTML, /Primary diagnostic/);
assert.match(nodes.executionDiagnostic.innerHTML, /maximum_runtime/);
assert.match(nodes.executionDiagnostic.innerHTML, /data-diagnostic-step="build"/);
assert.match(nodes.executionDiagnostic.innerHTML, /a-very-long-build-command/);
assert.match(nodes.executionDiagnostic.innerHTML, /Show details/);
assert.match(nodes.executionDiagnostic.innerHTML, /diagnostic-details" hidden/);
context.setExecutionDiagnosticExpanded(true);
assert.match(nodes.executionDiagnostic.innerHTML, /Hide details/);
assert.doesNotMatch(nodes.executionDiagnostic.innerHTML, /diagnostic-details" hidden/);
const compactDiagnostic = context.executionDiagnosticHtml(diagnosticExecution, {includeDetails:false});
assert.match(compactDiagnostic, /Command exceeded its limit/);
assert.match(compactDiagnostic, /Increase the configured maximum runtime/);
assert.doesNotMatch(compactDiagnostic, /diagnostic-toggle-row/);

context.renderExecutionDiagnostic({status:'failed', error:{message:'Old run failed'}});
assert.equal(nodes.executionDiagnostic.hidden, true);
assert.equal(nodes.executionDiagnostic.innerHTML, '');

const workflow = {
  package:{name:'demo', architecture:'amd64'},
  build:{source_changes:[{operation:'replace', path:'src/app.js'}]},
  service:{configured:true, name:'demo.service', command:'/opt/demo/bin/demo', user:'demo', group:'demo', working_directory:'/opt/demo', restart:'on-failure', after:['network.target'], environment:{DEMO:'1'}},
};
const veryLongCommand = `npm run build -- ${'long-argument-'.repeat(30)}`;
function projectedResult({status = 'prepared', version = {}, source = {}, detection = {}, dependencies = {}, sourceChanges = {}, build = {}, staging = {}} = {}) {
  return {
    status, version, source,
    steps: [
      {name:'source', details:source},
      {name:'detection', details:detection},
      {name:'dependencies', details:dependencies},
      {name:'source_changes', details:sourceChanges},
      {name:'build', details:build},
      {name:'staging', details:staging},
    ],
  };
}

function withStepDetails(value, name, details) {
  return {...value, steps:value.steps.map(step => step.name === name ? {...step, details} : step)};
}

const result = {
  ...projectedResult({
    version:{upstream:'2.0', debian:'2.0-1'},
    source:{repository:'owner/demo', strategy:'latest_release', tag:'v2.0', upstream_version:'2.0'},
    detection:{project_type:'nodejs', display_name:'Node.js', detected_files:['package.json'], build_tools:['node','npm'], warnings:['Lockfile is missing']},
    dependencies:{tools:['node','npm'], detected:['nodejs'], manually_added:['pkg-config'], missing_tools:[], missing:[]},
    sourceChanges:{applied:[{index:1, operation:'replace', path:'src/app.js', matches:1, status:'applied', anchor:'oldCall()'}]},
    build:{plan:{selection:{source:'detection_proposal'}, command_count:1, configured_working_directory:'.', environment_keys:['NODE_ENV'], inactivity_timeout:300, maximum_runtime:900, output:{mode:'path', configured_path:'dist'}}},
    staging:{version:'2.0-1', install_destination:'/opt/demo', content_file_count:0, ownership:{user:'demo',group:'demo'}, permissions:{directories:'0755',files:'0644'}, account:{user:'demo',group:'demo',create_user:true,create_group:true}, configurations:[{source:'config/demo.yml',destination:'/etc/demo.yml',policy:'dpkg_conffile',owner:'root',group:'root',mode:'0644'}], directories:[{path:'/var/lib/demo',owner:'demo',group:'demo',mode:'0750'}], warnings:['Build output is unavailable because build commands are not executed during dry-run'], systemd:{configured:true,path:'/usr/lib/systemd/system/demo.service'}},
  }),
  detection:{proposed_commands:[veryLongCommand]},
};
context.renderPreflightReport(result, workflow);
assert.equal(nodes.recipePreflight.hidden, false);
assert.equal(nodes.recipePreflightStatus.textContent, 'Action required');
assert.match(nodes.recipePreflightContent.innerHTML, /Build commands are not executed during a Test/);
assert.match(nodes.recipePreflightContent.innerHTML, /preflight-overview/);
assert.match(nodes.recipePreflightContent.innerHTML, /Build requirements &amp; plan/);
assert.match(nodes.recipePreflightContent.innerHTML, /preflight-package-overview/);
assert.match(nodes.recipePreflightContent.innerHTML, /Detected suggestions must be reviewed and saved/);
assert.match(nodes.recipePreflightContent.innerHTML, /Source details/);
assert.match(nodes.recipePreflightContent.innerHTML, /Build details/);
assert.match(nodes.recipePreflightContent.innerHTML, /Package details/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /preflight-origin-legend/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /value-origin/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /insight-origin/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /data-copy-preflight-command=/);
assert.match(nodes.recipePreflightContent.innerHTML, /Review the Recipe to inspect or edit command text/);
assert.match(nodes.recipePreflightContent.innerHTML, /Source changes/);
assert.match(nodes.recipePreflightContent.innerHTML, /Debian package plan/);
assert.match(nodes.recipePreflightContent.innerHTML, /Systemd service/);
assert.match(nodes.recipePreflightContent.innerHTML, /insight-section--service/);
assert.match(nodes.recipePreflightContent.innerHTML, /WorkingDirectory/);
assert.match(nodes.recipePreflightContent.innerHTML, /Lockfile is missing/);
assert.match(nodes.recipePreflightContent.innerHTML, /blocker/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /long-argument-/);
assert.equal(nodes.recipePreflight.scrolled, true);

function hasBlocker(findings, text = '') {
  return findings.some(row => row.level === 'blocker' && (!text || row.text.includes(text)));
}

const configured = {
  ...withStepDetails(
    withStepDetails(result, 'build', {plan:{...context.stepDetails(result, 'build').plan, selection:{source:'recipe'}, command_count:1}}),
    'staging', {...context.stepDetails(result, 'staging'), warnings:[]},
  ),
};
assert.equal(hasBlocker(context.preflightFindings(configured, workflow), 'Detected build commands'), false);

const staticProject = projectedResult({
  detection:{project_type:'static'}, dependencies:{missing_tools:[], missing:[]},
  build:{plan:{selection:{source:'static'}, command_count:0, output:{mode:'source'}}}, staging:{warnings:[]},
});
assert.equal(hasBlocker(context.preflightFindings(staticProject, {artifact:{mode:'source_build'}})), false);
assert.match(context.preflightBuildSection(staticProject, {artifact:{mode:'source_build'}}), /No build command is required for this source/);
assert.match(context.preflightBuildSection(staticProject, {artifact:{mode:'source_build'}}), /No additional build requirements were found/);
assert.match(context.preflightSourceSection(staticProject, {artifact:{mode:'upstream_archive'}}), /No source build required/);

const archivePaths = {
  ...staticProject,
  source:{archive_payload:{mode:'paths', selected_directories:2, explicit_files:1, selected_files:184, excluded_directories:1, excluded_files:0}},
};
const archivePathsHtml = context.preflightSourceSection(archivePaths, {artifact:{mode:'upstream_archive'}});
assert.match(archivePathsHtml, /Archive payload/);
assert.match(archivePathsHtml, /preflight-archive-payload-facts/);
assert.match(archivePathsHtml, /2 directories · 1 explicit file/);
assert.match(archivePathsHtml, /184 resolved files · 1 exclusion/);
assert.doesNotMatch(archivePathsHtml, /relative_path/);

const entireArchive = {
  ...archivePaths,
  source:{archive_payload:{mode:'entire_archive', selected_directories:0, explicit_files:0, selected_files:912, excluded_directories:3, excluded_files:0}},
};
const entireArchiveHtml = context.preflightSourceSection(entireArchive, {artifact:{mode:'upstream_archive'}});
assert.match(entireArchiveHtml, /Entire archive/);
assert.match(entireArchiveHtml, /912 resolved files · 3 exclusions/);
assert.match(entireArchiveHtml, /Archive payload · 912 files/);

const rawFile = {
  ...staticProject,
  source:{payload_kind:'raw_file',file_count:1,asset:{name:'demo-linux-amd64',payload_kind:'raw_file',file_count:1},archive_payload:{mode:'raw_file',selected_files:1}},
  steps:withStepDetails(staticProject, 'detection', {project_type:'upstream_archive',payload_kind:'raw_file',file_count:1,selected_asset:{name:'demo-linux-amd64',payload_kind:'raw_file',file_count:1},archive_payload:{mode:'raw_file',selected_files:1}}).steps,
};
const rawFileHtml = context.preflightSourceSection(rawFile, {artifact:{mode:'upstream_archive'}});
assert.match(rawFileHtml, /Raw file · 1 file/);
assert.doesNotMatch(rawFileHtml, /Archive payload/);

const actualArchiveWins = {
  ...rawFile,
  source:{...rawFile.source,payload_kind:'archive',file_count:4,asset:{name:'misleading-name',payload_kind:'archive',file_count:4},archive_payload:{mode:'entire_archive',selected_files:4,excluded_directories:0,excluded_files:0}},
  steps:withStepDetails(rawFile, 'detection', {project_type:'upstream_archive',payload_kind:'archive',file_count:4,selected_asset:{name:'misleading-name',payload_kind:'archive',file_count:4},archive_payload:{mode:'entire_archive',selected_files:4,excluded_directories:0,excluded_files:0}}).steps,
};
assert.match(context.preflightSourceSection(actualArchiveWins, {artifact:{mode:'upstream_archive'}}), /Archive payload · 4 files/);

const serviceHtml = context.preflightServiceSection(result, workflow);
assert.match(serviceHtml, /insight-section--service/);
assert.doesNotMatch(serviceHtml, /Prepared systemd unit/);
assert.doesNotMatch(serviceHtml, /preflight-secondary-details/);

const incompleteSource = withStepDetails(
  withStepDetails(staticProject, 'detection', {project_type:'nodejs'}),
  'build', {plan:{selection:{source:'detection_proposal'}, command_count:1}},
);
assert.equal(hasBlocker(context.preflightFindings(incompleteSource, {artifact:{mode:'source_build'}}), 'Detected build commands'), true);

for (const mode of ['upstream_deb', 'upstream_archive']) {
  const upstream = withStepDetails(withStepDetails(staticProject, 'detection', {project_type:mode}), 'build', {plan:{command_count:0}});
  assert.equal(hasBlocker(context.preflightFindings(upstream, {artifact:{mode}})), false);
}

const incomplete = {status:'prepared', version:{upstream:'', debian:''}, source:{}, steps:[]};
const incompleteFindings = context.preflightFindings(incomplete, {artifact:{mode:'source_build'}});
assert.equal(hasBlocker(incompleteFindings), false);
assert.equal(incompleteFindings.some(row => row.level === 'warning' && row.text.includes('not recorded')), true);

const previewMapping = withStepDetails(configured, 'staging', {...context.stepDetails(configured, 'staging'), warnings:['Install mapping 1 failed; source is unavailable during preview']});
const previewFindings = context.preflightFindings(previewMapping, workflow);
assert.equal(hasBlocker(previewFindings, 'Install mapping'), false);
assert.equal(previewFindings.some(row => row.level === 'warning' && row.text.includes('Install mapping')), true);

context.markPreflightStale();
assert.equal(nodes.recipePreflight.dataset.stale, 'true');
assert.match(nodes.recipePreflightStatus.textContent, /run Test again/);

context.clearPreflightReport();
assert.equal(nodes.recipePreflight.hidden, true);
assert.equal(nodes.recipePreflightContent.innerHTML, '');
