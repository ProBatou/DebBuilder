const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('static/js/pages/packages.js', 'utf8');
const start = source.indexOf('const packageRunSubmissions');
const end = source.indexOf('async function createPackageUi', start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);

const packageRow = {
  name: 'demo', recipe: 'demo-recipe',
  automation: {state: 'failed', can_retry: true, generation: 0, revision: 'failed-0'},
};
const posts = [];
const gets = [];
let renders = 0;
let timerId = 0;
let drawerOpen = false;
const timers = new Map();
const context = vm.createContext({
  adminState: {
    packages: [packageRow], selectedPackage: 'demo',
    packageValidationPollTimer: null, packageValidationRevision: 0,
  },
  STATUS_LABELS: {},
  $: id => id === 'packageDrawer' && drawerOpen ? {classList: {contains: () => true}} : null,
  esc: value => String(value ?? ''),
  badge: value => String(value ?? ''),
  fmtTime: value => String(value ?? ''),
  getJson: url => new Promise(resolve => gets.push({url, resolve})),
  postJson: (url, body) => new Promise(resolve => posts.push({url, body, resolve})),
  showToast: () => {},
  setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
  packageByName: name => name === packageRow.name ? packageRow : undefined,
});
vm.runInContext(source.slice(start, end), context, {filename: 'package-automation.js'});
context.renderPackages = () => { renders += 1; };
context.renderOpenPackage = () => { renders += 1; };

(async () => {
  const retry = context.retryPackageAutomation('demo');
  assert.deepEqual(posts[0].body, {generation: 0, revision: 'failed-0'});
  context.adminState.packageValidationRevision += 1;
  posts[0].resolve({automation: {status: {state: 'checking'}}});
  await retry;
  assert.equal(packageRow.automation.state, 'failed', 'stale Retry response must not regress package state');
  assert.equal(renders, 0);

  packageRow.automation = {state: 'watching', can_check_now: true};
  const check = context.checkPackageAutomation('demo');
  const duplicate = context.checkPackageAutomation('demo');
  assert.equal(posts.length, 2, 'duplicate package Check now click is debounced');
  await duplicate;
  context.adminState.packageValidationRevision += 1;
  posts[1].resolve({automation: {created: true, status: {state: 'checking'}}});
  await check;
  assert.equal(packageRow.automation.state, 'watching', 'stale Check now response must not overwrite package state');

  packageRow.validation = {};
  packageRow.automation = {state: 'building', state_active: true};
  context.adminState.selectedPackage = 'demo';
  context.schedulePackageValidationPoll('demo', null, 1500);
  const scheduled = [...timers.values()].at(-1);
  const poll = scheduled.callback();
  assert.equal(gets[0].url, '/api/recipes/demo-recipe/automation', 'automation-only polling uses the narrow status route');
  gets[0].resolve({automation: {state: 'building', state_active: true}});
  await poll;
  assert.equal(gets.length, 1, 'active automation does not rebuild the package inventory');

  drawerOpen = true;
  packageRow.validation = {attempt_id: 'old', status: 'failed'};
  packageRow.automation = {state: 'retry_scheduled', state_active: true};
  context.renderOpenPackage = row => {
    renders += 1;
    context.schedulePackageValidationPoll(row.name, row.validation.attempt_id, 1500);
  };
  context.schedulePackageValidationPoll('demo', 'old', 1500);
  const retryPoll = [...timers.values()].at(-1).callback();
  gets[1].resolve({automation: {
    state: 'validating', state_active: true,
    validation: {attempt_id: 'new', status: 'queued'},
  }});
  await retryPoll;
  assert.equal(packageRow.validation.attempt_id, 'new', 'drawer follows the new automatic attempt');
  assert.equal(renders, 2);
  const nextPoll = [...timers.values()].at(-1).callback();
  assert.equal(gets[2].url, '/api/packages/demo');
  gets[2].resolve({package: {...packageRow, validation: {attempt_id: 'new', status: 'running'}}});
  await nextPoll;
  assert.equal(renders, 4, 'the replacement Validation continues to be polled');

  assert.match(context.actionButtons(packageRow), /refresh-package-observation/, 'linked package exposes explicit observation refresh');
  assert.doesNotMatch(context.actionButtons({...packageRow, recipe: ''}), /refresh-package-observation/, 'unlinked package hides observation refresh');
  const refresh = context.refreshPackageObservation('demo');
  assert.equal(posts[2].url, '/api/recipes/demo-recipe/observation/refresh');
  assert.deepEqual(posts[2].body, {});
  posts[2].resolve({ok: true, observation: {}});
  while (!gets[3]) await Promise.resolve();
  assert.equal(gets[3].url, '/api/packages/demo');
  gets[3].resolve({package: {...packageRow, observation: {state: 'observed'}}});
  await refresh;

  console.log('package automation JS tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
