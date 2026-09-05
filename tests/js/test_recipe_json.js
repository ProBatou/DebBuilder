const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function node() {
  return {
    addEventListener: () => {},
    classList: {toggle: () => {}},
    dataset: {},
    hidden: false,
    value: '',
  };
}

const nodes = new Proxy({}, {
  get(target, id) {
    if (!target[id]) target[id] = node();
    return target[id];
  },
});

const context = vm.createContext({
  window: {},
  document: {createElement: () => node(), execCommand: () => true},
  navigator: {},
  Blob,
  URL,
  $: id => nodes[id],
});
vm.runInContext(fs.readFileSync('static/js/recipe/json_editor.js', 'utf8'), context, {filename: 'json_editor.js'});

const tools = context.window.recipeJsonTools;
const recipe = {
  schema_version: 1,
  name: 'typed-demo',
  active: false,
  package: {name: 'typed-demo', version_revision: '1+b1', runtime_dependencies: []},
  build: {source_changes: [], inactivity_timeout: null},
  install: {directories: [{path: '/var/lib/typed-demo'}]},
};
const text = tools.canonicalRecipeJson(recipe);
assert.equal(text.endsWith('\n'), true);
assert.deepEqual(JSON.parse(JSON.stringify(tools.parseRecipeJsonText(text))), recipe);
assert.equal(tools.parseRecipeJsonText(text).active, false);
assert.equal(tools.parseRecipeJsonText(text).package.version_revision, '1+b1');
assert.deepEqual(JSON.parse(JSON.stringify(tools.parseRecipeJsonText(text).build.source_changes)), []);
assert.equal(tools.parseRecipeJsonText(text).build.inactivity_timeout, null);
assert.throws(() => tools.parseRecipeJsonText(''), /empty/i);
assert.throws(() => tools.parseRecipeJsonText('{"name":'), /JSON syntax error/i);

const after = JSON.parse(text);
after.package.version_revision = '2';
after.install.directories.push({path: '/var/log/typed-demo'});
assert.deepEqual(
  JSON.parse(JSON.stringify(tools.recipeJsonChangedPaths(recipe, after))),
  ['$.install.directories', '$.package.version_revision'],
);
