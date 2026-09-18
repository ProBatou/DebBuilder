const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function node() {
  return {
    checked: true, disabled: false, hidden: false, title: '', value: '',
    dataset: {}, innerHTML: '', classList: {contains: () => true},
    addEventListener(type, callback) { this[`on${type}`] = callback; },
  };
}

const nodes = new Proxy({}, {get(target, key) { return target[key] ||= node(); }});
nodes.recipeAutomationPolicy.value = 'build';
const pendingGets = [];
const pendingPosts = [];
let timerId = 0;
const timers = new Map();

const context = vm.createContext({
  currentRecipeId: 'demo',
  currentRecipeManaged: false,
  STATUS_LABELS: {watching: 'Watching', checking: 'Checking', failed: 'Failed'},
  $: id => nodes[id],
  esc: value => String(value ?? ''),
  getJson: url => new Promise((resolve, reject) => pendingGets.push({url, resolve, reject})),
  postJson: (url, body) => new Promise((resolve, reject) => pendingPosts.push({url, body, resolve, reject})),
  showToast: () => {},
  switchView: () => {},
  openExecution: async () => {},
  setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
});
vm.runInContext(fs.readFileSync('static/js/recipe/automation.js', 'utf8'), context, {filename: 'automation.js'});

function status(state, extras = {}) {
  return {
    recipe_id: 'demo', recipe_active: true, eligible: true, state,
    state_active: state === 'checking', result: state === 'failed' ? 'failed' : 'active',
    automation: {enabled: true, policy: 'build', managed: false},
    detected: {version: '', ref: ''}, retry: {scheduled: false, not_before: null},
    blocked: null, run: null, scheduler: {state: 'running'},
    can_check_now: true, can_retry: state === 'failed', generation: 0,
    revision: `revision-${state}`, ...extras,
  };
}

(async () => {
  const older = context.loadRecipeAutomationStatus('demo');
  const newer = context.loadRecipeAutomationStatus('demo');
  pendingGets[1].resolve({automation: status('checking')});
  assert.equal(await newer, true);
  pendingGets[0].resolve({automation: status('watching')});
  assert.equal(await older, false);
  assert.match(nodes.recipeAutomationStatus.innerHTML, /Checking/);
  assert.equal(timers.size, 1, 'one active Recipe polling timer');

  context.stopRecipeAutomationPolling();
  assert.equal(timers.size, 0);
  const stale = context.loadRecipeAutomationStatus('demo');
  context.stopRecipeAutomationPolling();
  pendingGets[2].resolve({automation: status('failed')});
  assert.equal(await stale, false, 'response after teardown is ignored');

  vm.runInContext('recipeAutomationStatus = ' + JSON.stringify(status('watching')), context);
  const firstCheck = context.checkRecipeAutomationNow();
  const duplicateCheck = context.checkRecipeAutomationNow();
  assert.equal(pendingPosts.length, 1, 'duplicate Check now click is debounced');
  await duplicateCheck;
  pendingPosts[0].resolve({automation: {created: true, status: status('checking')}});
  await firstCheck;
  assert.match(nodes.recipeAutomationStatus.innerHTML, /Checking/);

  context.stopRecipeAutomationPolling();
  vm.runInContext('recipeAutomationStatus = ' + JSON.stringify(status('failed')), context);
  const retry = context.retryRecipeAutomation();
  assert.deepEqual(pendingPosts[1].body, {generation: 0, revision: 'revision-failed'});
  pendingPosts[1].resolve({automation: {created: true, status: status('checking', {generation: 1, revision: 'retry-1'})}});
  await retry;
  assert.match(nodes.recipeAutomationStatus.innerHTML, /Checking/);

  context.stopRecipeAutomationPolling();
  vm.runInContext('recipeAutomationStatus = ' + JSON.stringify(status('watching')), context);
  const supersededAction = context.checkRecipeAutomationNow();
  const supersedingRefresh = context.loadRecipeAutomationStatus('demo');
  pendingGets[3].resolve({automation: status('watching')});
  await supersedingRefresh;
  pendingPosts[2].resolve({automation: {created: true, status: status('checking')}});
  await supersededAction;
  assert.equal(vm.runInContext('recipeAutomationActionPending', context), false, 'superseded action releases its own debounce state');

  nodes.recipeAutomationEnabled.checked = false;
  context.refreshRecipeAutomationControls();
  assert.equal(nodes.btnAutomationCheckNow.disabled, true, 'unsaved local disable immediately blocks Check now');
  nodes.recipeAutomationEnabled.checked = true;

  context.renderRecipeAutomationStatus(status('retry_scheduled', {state_active: true}));
  assert.equal([...timers.values()][0].delay, 10000, 'delayed Retry uses low-frequency timestamp polling');

  console.log('recipe automation JS tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
