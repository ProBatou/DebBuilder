const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function control(id) {
  return {id, dataset: {}, disabled: false, hidden: false};
}

const controls = [
  control('recipeMetaActive'), control('packageMaintainer'), control('buildEnvironment'),
  control('buildInactivityTimeout'), control('buildMaximumRuntime'), control('recipeMetaGithub'),
  control('btnAddBuildDependency'),
];
const nodes = Object.fromEntries(controls.map(node => [node.id, node]));
Object.assign(nodes, {
  recipeManagedBadge: {hidden: true},
  recipeManagedNotice: {hidden: true},
  btnDeleteRecipeTop: {hidden: false, disabled: false},
  recipeLoadErrors: {
    hidden: true,
    children: [],
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); },
  },
});

const app = fs.readFileSync('static/app.js', 'utf8');
const start = app.indexOf('const MANAGED_RECIPE_CONTROL_BY_PATH');
const end = app.indexOf('function renderAccountProvisioning', start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);

const effective = {
  schema_version: 3,
  name: 'debbuilder',
  active: true,
  resource_limits: {memory_max_bytes: null, tasks_max: 128, cpu_quota_percent: null, io_read_bandwidth_max_bytes_per_sec: null, io_write_bandwidth_max_bytes_per_sec: null},
  package: {name: 'debbuilder', maintainer: 'Default <default@example.test>'},
  source: {repository: 'ProBatou/DebBuilder'},
  build: {environment: {}, inactivity_timeout: 300, maximum_runtime: null, detected_project: 'python'},
  management: {owner: 'application', definition_version: 1},
};
const form = {
  ...effective,
  active: false,
  package: {...effective.package, maintainer: 'Ops <ops@example.test>'},
  source: {repository: 'stale/form-value'},
  build: {...effective.build, environment: {HTTP_PROXY: 'http://proxy'}, detected_project: null},
  resource_limits: {...effective.resource_limits, tasks_max: 64},
};

const context = vm.createContext({
  currentRecipeManaged: true,
  currentRecipeEditablePaths: [
    'active', 'package.maintainer', 'build.environment',
    'build.inactivity_timeout', 'build.maximum_runtime',
    'resource_limits',
  ],
  currentRecipeDocument: effective,
  collectWorkflow: () => form,
  structuredClone,
  $: id => nodes[id] || null,
  document: {
    querySelectorAll: () => controls,
    createElement: () => ({textContent: ''}),
  },
});
vm.runInContext(app.slice(start, end), context, {filename: 'app-managed-recipe.js'});

context.applyRecipeManagementUi();
for (const id of ['recipeMetaActive', 'packageMaintainer', 'buildEnvironment', 'buildInactivityTimeout', 'buildMaximumRuntime']) {
  assert.equal(nodes[id].disabled, false, `${id} should remain editable`);
}
assert.equal(nodes.recipeMetaGithub.disabled, true);
assert.equal(nodes.btnAddBuildDependency.disabled, true);
assert.equal(nodes.recipeManagedBadge.hidden, false);
assert.equal(nodes.recipeManagedNotice.hidden, false);
assert.equal(nodes.btnDeleteRecipeTop.hidden, true);
assert.equal(nodes.btnDeleteRecipeTop.disabled, true);

const submitted = vm.runInContext('workflowForCurrentRecipe()', context);
assert.equal(submitted.active, false);
assert.equal(submitted.package.maintainer, 'Ops <ops@example.test>');
assert.deepEqual({...submitted.build.environment}, {HTTP_PROXY: 'http://proxy'});
assert.equal(submitted.source.repository, 'ProBatou/DebBuilder');
assert.equal(submitted.build.detected_project, 'python');
assert.deepEqual({...submitted.management}, effective.management);
assert.equal(submitted.resource_limits.tasks_max, 64);

context.currentRecipeManaged = false;
context.applyRecipeManagementUi();
assert.equal(nodes.recipeMetaGithub.disabled, false);
assert.equal(nodes.btnAddBuildDependency.disabled, false);
assert.equal(nodes.recipeManagedBadge.hidden, true);
assert.equal(nodes.recipeManagedNotice.hidden, true);
assert.equal(nodes.btnDeleteRecipeTop.hidden, false);

context.renderRecipeLoadErrors([{id: 'broken', error: {message: 'Invalid JSON', path: '$.build'}}]);
assert.equal(nodes.recipeLoadErrors.hidden, false);
assert.match(nodes.recipeLoadErrors.children[0].textContent, /broken.*Invalid JSON.*\$\.build/);
context.renderRecipeLoadErrors([]);
assert.equal(nodes.recipeLoadErrors.hidden, true);

const html = fs.readFileSync('static/index.html', 'utf8');
assert.match(html, /id="recipeManagedBadge"[^>]*hidden>Built-in/);
assert.match(html, /id="recipeManagedNotice"/);

console.log('managed Recipe JS tests passed');
