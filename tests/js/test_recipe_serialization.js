const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function element() {
  let value = '';
  return {
    checked: true,
    classList: {toggle: () => {}},
    dataset: {},
    hidden: false,
    innerHTML: '',
    placeholder: '',
    get value() { return value; },
    set value(next) { value = String(next ?? ''); },
  };
}

const nodes = new Proxy({}, {
  get(target, id) {
    if (!target[id]) target[id] = element();
    return target[id];
  },
});

const context = vm.createContext({
  window: {},
  document: {querySelectorAll: () => []},
  $: id => nodes[id],
  esc: value => String(value ?? ''),
  refreshRecipeApplicability: () => {},
  renderAccountProvisioning: () => {},
  toggleVersionExpression: () => {},
});

vm.runInContext(fs.readFileSync('static/recipe_serialization.js', 'utf8'), context, {filename: 'recipe_serialization.js'});

nodes.installDirectories.placeholder = '/var/lib/example | example | example | 0750';
for (const versionRevision of ['1', '2', '1+b1']) {
  context.renderWorkflow({
    name: 'demo',
    package: {name: 'demo', version_revision: versionRevision},
    install: {directories: []},
  });
  assert.equal(nodes.recipePackageVersionRevision.value, versionRevision);
  assert.equal(context.collectWorkflow().package.version_revision, versionRevision);
}
context.renderWorkflow({name: 'demo', package: {name: 'demo'}, install: {directories: []}});
assert.equal(nodes.recipePackageVersionRevision.value, '1');
nodes.recipePackageVersionRevision.value = '';
assert.equal(context.collectWorkflow().package.version_revision, '');
assert.equal(nodes.installDirectories.value, '');
assert.equal(JSON.stringify(context.collectWorkflow().install.directories), '[]');
assert.equal(context.installDirectoriesText([]), '');

nodes.installDirectories.value = '/var/lib/demo | demo | demo | 0750';
assert.equal(JSON.stringify(context.collectWorkflow().install.directories), JSON.stringify([
  {path: '/var/lib/demo', owner: 'demo', group: 'demo', mode: '0750'},
]));

nodes.installDirectories.value = '';
assert.equal(JSON.stringify(context.collectWorkflow().install.directories), '[]');

const roundTrip = {
  name: 'typed-demo',
  active: false,
  package: {name: 'typed-demo', version_revision: '1+b1', runtime_dependencies: []},
  source: {repository: 'owner/typed-demo', version: {source: 'tag'}},
  artifact: {mode: 'source_build'},
  build: {
    source_changes: [{operation: 'create_file', path: 'config.ini', content: 'enabled=true\n'}],
    extra_dependencies: [], commands: [], environment: {}, output: {mode: 'source'},
    inactivity_timeout: null, maximum_runtime: null,
  },
  install: {
    directories: [{path: '/var/lib/typed-demo', owner: 'typed-demo', group: 'typed-demo', mode: '0750'}],
    config_files: [], content: {source: 'build_output'},
  },
  service: {enabled: false},
};
context.renderWorkflow(roundTrip);
const collected = context.collectWorkflow();
assert.equal(collected.active, false);
assert.equal(collected.package.version_revision, '1+b1');
assert.deepEqual(JSON.parse(JSON.stringify(collected.package.runtime_dependencies)), []);
assert.deepEqual(JSON.parse(JSON.stringify(collected.build.source_changes)), roundTrip.build.source_changes);
assert.deepEqual(JSON.parse(JSON.stringify(collected.install.directories)), roundTrip.install.directories);
assert.deepEqual(JSON.parse(JSON.stringify(collected.install.config_files)), []);
assert.equal(collected.build.inactivity_timeout, null);
assert.equal(collected.build.maximum_runtime, null);
