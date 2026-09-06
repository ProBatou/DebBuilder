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
const result = {
  status:'prepared', version:'2.0-1', versions:{upstream:'2.0', debian:'2.0-1'},
  source:{repository:'owner/demo', strategy:'latest_release', tag:'v2.0', upstream_version:'2.0'},
  detection:{project_type:'nodejs', display_name:'Node.js', detected_files:['package.json'], build_tools:['node','npm'], proposed_commands:[veryLongCommand], warnings:['Lockfile is missing']},
  dependencies:{tools:['node','npm'], detected:['nodejs'], manually_added:['pkg-config'], missing_tools:[], missing:[]},
  source_changes:{requested:1, applied_count:1, applied:[{index:1, operation:'replace', path:'src/app.js', matches:1, status:'applied', anchor:'oldCall()'}]},
  build:{executed:false, reason:'dry_run', plan:{selection:{source:'detection_proposal', confirmed:false}, commands:[{command:veryLongCommand}], configured_working_directory:'.', environment_keys:['NODE_ENV'], inactivity_timeout:300, maximum_runtime:900, output:{mode:'path', configured_path:'dist', path:'/run/source/dist', exists:false}}},
  staging:{version:'2.0-1', install_destination:'/opt/demo', content_file_count:0, ownership:{user:'demo',group:'demo'}, permissions:{directories:'0755',files:'0644'}, account:{user:'demo',group:'demo',create_user:true,create_group:true}, configurations:[{source:'config/demo.yml',destination:'/etc/demo.yml',policy:'dpkg_conffile',owner:'root',group:'root',mode:'0644'}], directories:[{path:'/var/lib/demo',owner:'demo',group:'demo',mode:'0750'}], warnings:['Build output is unavailable because build commands are not executed during dry-run'], control:'Package: demo\nVersion: 2.0-1\n', maintainer_scripts:{postinst:'#!/bin/sh\nset -e\n'}, systemd:{configured:true,path:'/usr/lib/systemd/system/demo.service',content:'[Service]\nExecStart=/opt/demo/bin/demo\n'}},
  steps:[],
};
context.renderPreflightReport(result, workflow);
assert.equal(nodes.recipePreflight.hidden, false);
assert.equal(nodes.recipePreflightStatus.textContent, 'Action required');
assert.match(nodes.recipePreflightContent.innerHTML, /Build commands are not executed during a Test/);
assert.match(nodes.recipePreflightContent.innerHTML, /preflight-overview/);
assert.match(nodes.recipePreflightContent.innerHTML, /Build requirements &amp; plan/);
assert.match(nodes.recipePreflightContent.innerHTML, /preflight-package-overview/);
assert.match(nodes.recipePreflightContent.innerHTML, /Detected suggestion; save it in the Recipe/);
assert.match(nodes.recipePreflightContent.innerHTML, /Source details/);
assert.match(nodes.recipePreflightContent.innerHTML, /Build details/);
assert.match(nodes.recipePreflightContent.innerHTML, /Package details/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /preflight-origin-legend/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /value-origin/);
assert.doesNotMatch(nodes.recipePreflightContent.innerHTML, /insight-origin/);
assert.match(nodes.recipePreflightContent.innerHTML, /data-copy-preflight-command=/);
assert.match(nodes.recipePreflightContent.innerHTML, /save it in the Recipe before a real Build/);
assert.match(nodes.recipePreflightContent.innerHTML, /Source changes/);
assert.match(nodes.recipePreflightContent.innerHTML, /Debian package plan/);
assert.match(nodes.recipePreflightContent.innerHTML, /Systemd service/);
assert.match(nodes.recipePreflightContent.innerHTML, /WorkingDirectory/);
assert.match(nodes.recipePreflightContent.innerHTML, /Lockfile is missing/);
assert.match(nodes.recipePreflightContent.innerHTML, /blocker/);
assert.match(nodes.recipePreflightContent.innerHTML, /long-argument-/);
assert.equal(nodes.recipePreflight.scrolled, true);

function hasBlocker(findings, text = '') {
  return findings.some(row => row.level === 'blocker' && (!text || row.text.includes(text)));
}

const configured = {
  ...result,
  detection:{...result.detection, proposed_commands:['npm run suggested']},
  build:{...result.build, plan:{...result.build.plan, selection:{source:'recipe', confirmed:true}, commands:[{command:'make configured'}]}},
  staging:{...result.staging, warnings:[]},
};
assert.equal(hasBlocker(context.preflightFindings(configured, workflow), 'Detected build commands'), false);

const staticProject = {
  status:'prepared', detection:{project_type:'static', proposed_commands:[]}, dependencies:{missing_tools:[], missing:[]},
  build:{executed:false, plan:{selection:{source:'static', confirmed:true}, commands:[], output:{mode:'source'}}}, staging:{warnings:[]}, steps:[],
};
assert.equal(hasBlocker(context.preflightFindings(staticProject, {artifact:{mode:'source_build'}})), false);
assert.match(context.preflightBuildSection(staticProject, {artifact:{mode:'source_build'}}), /No build command is required for this source/);
assert.match(context.preflightBuildSection(staticProject, {artifact:{mode:'source_build'}}), /No additional build requirements were found/);
assert.match(context.preflightSourceSection(staticProject, {artifact:{mode:'upstream_archive'}}), /No source build required/);

const incompleteSource = {
  ...staticProject,
  detection:{project_type:'nodejs', proposed_commands:['npm run build']},
  build:{executed:false, plan:{selection:{source:'detection_proposal', confirmed:false}, commands:[{command:'npm run build'}]}},
};
assert.equal(hasBlocker(context.preflightFindings(incompleteSource, {artifact:{mode:'source_build'}}), 'Detected build commands'), true);

for (const mode of ['upstream_deb', 'upstream_archive']) {
  const upstream = {...staticProject, detection:{project_type:mode}, build:{executed:false, reason:mode, plan:{commands:[]}}};
  assert.equal(hasBlocker(context.preflightFindings(upstream, {artifact:{mode}})), false);
}

const oldIncomplete = {status:'prepared', steps:[], build:{}, staging:{warnings:[]}, dependencies:{}};
const oldFindings = context.preflightFindings(oldIncomplete, {artifact:{mode:'source_build'}});
assert.equal(hasBlocker(oldFindings), false);
assert.equal(oldFindings.some(row => row.level === 'warning' && row.text.includes('not recorded')), true);

const previewMapping = {...configured, staging:{...configured.staging, warnings:['Install mapping 1 failed; source is unavailable during preview']}};
const previewFindings = context.preflightFindings(previewMapping, workflow);
assert.equal(hasBlocker(previewFindings, 'Install mapping'), false);
assert.equal(previewFindings.some(row => row.level === 'warning' && row.text.includes('Install mapping')), true);

context.markPreflightStale();
assert.equal(nodes.recipePreflight.dataset.stale, 'true');
assert.match(nodes.recipePreflightStatus.textContent, /run Test again/);

context.clearPreflightReport();
assert.equal(nodes.recipePreflight.hidden, true);
assert.equal(nodes.recipePreflightContent.innerHTML, '');
