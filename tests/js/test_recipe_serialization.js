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

vm.runInContext(fs.readFileSync('static/js/recipe/archive_tree.js', 'utf8'), context, {filename: 'archive_tree.js'});
context.ArchiveTree = context.window.ArchiveTree;
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
  package: {name: 'typed-demo', version_revision: '1+b1', description: 'Typed demo\nLong Debian description.', runtime_dependencies: []},
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
  service: {enabled: false, name: 'typed-demo.service', command: '/opt/typed-demo/bin/serve', description: ' Typed demo worker ', working_directory: '/opt/typed-demo'},
};
context.renderWorkflow(roundTrip);
const collected = context.collectWorkflow();
assert.equal(collected.schema_version, 2);
assert.equal(collected.active, false);
assert.equal(collected.package.version_revision, '1+b1');
assert.equal(collected.package.description, roundTrip.package.description);
assert.deepEqual(JSON.parse(JSON.stringify(collected.package.runtime_dependencies)), []);
assert.deepEqual(JSON.parse(JSON.stringify(collected.build.source_changes)), roundTrip.build.source_changes);
assert.deepEqual(JSON.parse(JSON.stringify(collected.install.directories)), roundTrip.install.directories);
assert.deepEqual(JSON.parse(JSON.stringify(collected.install.config_files)), []);
assert.equal(collected.build.inactivity_timeout, null);
assert.equal(collected.build.maximum_runtime, null);
assert.equal(collected.service.description, roundTrip.service.description);
assert.equal(collected.service.working_directory, roundTrip.service.working_directory);

for (const description of ['Short description', 'First line\nSecond line\nThird line', '  leading and trailing spaces  ', 'UTF-8: café, € & <package>']) {
  context.renderWorkflow({name: 'description-demo', package: {name: 'description-demo', description}, install: {directories: []}});
  assert.equal(context.collectWorkflow().package.description, description);
}

context.renderWorkflow({
  name: 'legacy-service', package: {name: 'legacy-service'}, install: {directories: []},
  service: {name: 'legacy-service.service', command: '/opt/legacy-service/bin/serve'},
});
const legacyCollected = context.collectWorkflow();
assert.equal(legacyCollected.service.description, 'legacy-service');
assert.equal(legacyCollected.service.working_directory, '');
nodes.serviceWorkingDirectory.value = '/opt/legacy-service';
assert.equal(context.collectWorkflow().service.working_directory, '/opt/legacy-service');
nodes.serviceWorkingDirectory.value = '';
assert.equal(context.collectWorkflow().service.working_directory, '');

const archiveRecipe = {
  name:'archive-demo', package:{name:'archive-demo'}, source:{repository:'owner/archive-demo'},
  artifact:{mode:'upstream_archive', archive_source:'github_source', payload:{mode:'paths', include:['app/', 'server.py'], exclude:['app/dev/']}},
  install:{directories:[]},
};
context.renderWorkflow(archiveRecipe);
let archiveCollected = context.collectWorkflow();
assert.deepEqual(JSON.parse(JSON.stringify(archiveCollected.artifact.payload)), {mode:'paths', include:['app/', 'server.py'], exclude:['app/dev/']});
assert.equal('selected_files' in archiveCollected.artifact, false);

const entireRecipe = {
  ...archiveRecipe,
  artifact:{...archiveRecipe.artifact, payload:{mode:'entire_archive', include:[], exclude:['tests/']}},
};
context.renderWorkflow(entireRecipe);
archiveCollected = context.collectWorkflow();
assert.deepEqual(JSON.parse(JSON.stringify(archiveCollected.artifact.payload)), {mode:'entire_archive', include:[], exclude:['tests/']});

const legacyArchive = {
  ...archiveRecipe,
  artifact:{...archiveRecipe.artifact, payload:{mode:'paths', include:['server.py', 'bin/tool'], exclude:[], legacy_file_layout:'basename'}},
};
context.renderWorkflow(legacyArchive);
assert.deepEqual(JSON.parse(JSON.stringify(context.collectWorkflow().artifact.payload)), legacyArchive.artifact.payload);
context.ArchiveTree.setInventory(context.window.recipeArchiveState, {
  complete:true, entries:[
    {path:'bin/',kind:'directory',descendant_files:1}, {path:'bin/tool',kind:'file',size:1,mode:'0755'},
    {path:'server.py',kind:'file',size:1,mode:'0644'}, {path:'new.py',kind:'file',size:1,mode:'0644'},
  ], file_count:3, directory_count:1, entry_count:4,
});
context.ArchiveTree.includePath(context.window.recipeArchiveState, 'new.py');
archiveCollected = context.collectWorkflow();
assert.equal('legacy_file_layout' in archiveCollected.artifact.payload, false);
assert.deepEqual(JSON.parse(JSON.stringify(archiveCollected.artifact.payload.include)), ['bin/tool', 'new.py', 'server.py']);
context.ArchiveTree.setMode(context.window.recipeArchiveState, 'entire_archive');
context.ArchiveTree.setMode(context.window.recipeArchiveState, 'paths');
assert.deepEqual(JSON.parse(JSON.stringify(context.collectWorkflow().artifact.payload)), {mode:'paths', include:[], exclude:[]});
