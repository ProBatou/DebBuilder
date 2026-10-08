const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const context = vm.createContext({
  Date,
  Promise,
  console,
  esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
  statusBadge: (label, state) => `<badge class="${state}">${label}</badge>`,
  setTimeout: () => 1,
  clearTimeout: () => {},
  $: () => null,
});
const settingsSource = fs.readFileSync('static/settings.js', 'utf8');
vm.runInContext(settingsSource, context, {filename: 'settings.js'});
assert.match(settingsSource, /cleanup runs after startup, every five minutes/);
assert.match(settingsSource, /published Run-local \.deb is pruned only after exact repository verification/);
assert.match(settingsSource, /Terminal staging manifests are pruned after retained workspace evidence is cleared/);
for (const field of [
  'pressure_minimum_free_bytes',
  'pressure_minimum_free_percent',
  'pressure_target_free_bytes',
  'pressure_target_free_percent',
]) {
  assert.match(settingsSource, new RegExp(field));
}
for (const label of [
  'Pressure minimum free bytes',
  'Pressure minimum free (%)',
  'Pressure target free bytes',
  'Pressure target free (%)',
]) {
  assert.match(settingsSource, new RegExp(label.replace(/[()]/g, '\\$&')));
}
assert.match(settingsSource, /max="9007199254740991"/);

const elements = {
  settingAppName:{value:'DebBuilder'}, settingPublicUrl:{value:''},
  settingRepoUrl:{value:'https://repo.example.test'}, settingSuite:{value:'stable'},
  settingComponent:{value:'main'}, settingArch:{value:'amd64'},
  settingGithubToken:{value:''}, settingNtfyServer:{value:''},
  settingNtfyTopic:{value:''}, settingNtfyToken:{value:''},
  settingOidcIssuer:{value:''}, settingOidcClientId:{value:''},
  settingOidcRedirectUri:{value:''}, settingOidcSecret:{value:''},
  settingAutoValidateAfterBuild:{checked:false}, settingAutoPublishAfterValidation:{checked:false},
  settingWorkspaceCleanupEnabled:{checked:true}, settingFailedWorkspacesToRetain:{value:'5'},
  settingPressureMinimumFreeBytes:{value:'9007199254740990'},
  settingPressureMinimumFreePercent:{value:'10'},
  settingPressureTargetFreeBytes:{value:'9007199254740991'},
  settingPressureTargetFreePercent:{value:'15'},
};
context.$ = id => elements[id] || null;
vm.runInContext('currentSettings={resource_limits:{}}', context);
const exactPayload = context.settingsPayload();
assert.equal(exactPayload.workspace_cleanup.pressure_minimum_free_bytes, 9007199254740990);
assert.equal(exactPayload.workspace_cleanup.pressure_target_free_bytes, Number.MAX_SAFE_INTEGER);
assert.equal(
  JSON.parse(JSON.stringify(exactPayload)).workspace_cleanup.pressure_target_free_bytes,
  Number.MAX_SAFE_INTEGER,
);

assert.equal(context.formatStorageBytes(0), '0 B');
assert.equal(context.formatStorageBytes(1024), '1.00 KiB');
assert.equal(context.formatStorageBytes(12 * 1024 * 1024), '12.0 MiB');
assert.equal(context.formatStorageBytes(-1), '—');

const base = {
  state: 'ready',
  measured_at: new Date().toISOString(),
  partial: false,
  diagnostics: [],
  roots: {repository_within_data: true},
  bytes: {managed_total: 12 * 1024, repository: 4 * 1024},
  categories: {disposable: 2 * 1024},
  runs: {count: 7, failed_count: 2, test_count: 3, artifact_count: 4, artifact_bytes: 1024, pruned_artifact_count: 2, pruned_artifact_bytes: 512},
};

const ready = context.renderStorageSummary(base);
for (const label of ['Managed storage', 'Runs', 'APT repository', 'Disposable workspace', 'Run-local artifacts']) {
  assert.match(ready, new RegExp(label));
}
assert.match(ready, /12\.0 KiB/);
assert.match(ready, /2 failed · 3 Test/);
assert.match(ready, /4 local · 2 pruned/);
assert.match(ready, /READY/);

const partial = context.renderStorageSummary({...base, state: 'partial', partial: true});
assert.match(partial, /shown totals are partial/);
assert.match(partial, /PARTIAL/);

const stale = context.renderStorageSummary({...base, state: 'stale', partial: true});
assert.match(stale, /last completed measurement/);
assert.match(stale, /STALE/);

const error = context.renderStorageSummary({
  ...base,
  state: 'error',
  diagnostics: ['scan <failed>'],
});
assert.match(error, /scan &lt;failed>/);
assert.match(error, /ERROR/);

const collecting = context.renderStorageSummary(null);
assert.match(collecting, /Collecting storage information/);
assert.match(collecting, /No completed measurement yet/);

const css = fs.readFileSync('static/css/pages.css', 'utf8');
assert.match(css, /\.storage-stat-grid\s*\{[^}]*repeat\(5,/s);
assert.match(css, /@media \(max-width: 900px\)[\s\S]*\.storage-stat-grid\s*\{[^}]*repeat\(2,/);
assert.match(css, /@media \(max-width: 600px\)[\s\S]*\.storage-stat-grid\s*\{[^}]*grid-template-columns:\s*1fr/);

console.log('storage settings JS tests passed');
