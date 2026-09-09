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
vm.runInContext(fs.readFileSync('static/settings.js', 'utf8'), context, {filename: 'settings.js'});

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
  runs: {count: 7, failed_count: 2, test_count: 3, artifact_count: 4, artifact_bytes: 1024},
};

const ready = context.renderStorageSummary(base);
for (const label of ['Managed storage', 'Runs', 'APT repository', 'Disposable workspace', 'Artifacts']) {
  assert.match(ready, new RegExp(label));
}
assert.match(ready, /12\.0 KiB/);
assert.match(ready, /2 failed · 3 Test/);
assert.match(ready, /4 final \.deb/);
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
