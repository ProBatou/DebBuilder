const assert = require('assert');
const ArchiveTree = require('../../static/js/recipe/archive_tree.js');

const inventory = {
  complete:true, file_count:5, directory_count:4, entry_count:9,
  entries:[
    {path:'foo/', kind:'directory', descendant_files:2},
    {path:'foo/a.txt', kind:'file', size:1, mode:'0644'},
    {path:'foo/nested/', kind:'directory', descendant_files:1},
    {path:'foo/nested/b.txt', kind:'file', size:1, mode:'0644'},
    {path:'foobar/', kind:'directory', descendant_files:1},
    {path:'foobar/c.txt', kind:'file', size:1, mode:'0644'},
    {path:'tests/', kind:'directory', descendant_files:1},
    {path:'tests/test.js', kind:'file', size:1, mode:'0644'},
    {path:'server.js', kind:'file', size:1, mode:'0644'},
  ],
};

const tree = ArchiveTree.buildTree(inventory);
assert.deepEqual(tree.roots.map(node => node.path), ['foo/', 'foobar/', 'server.js', 'tests/']);
assert.deepEqual(tree.byPath.get('foo/').children.map(node => node.path), ['foo/a.txt', 'foo/nested/']);
assert.equal(ArchiveTree.parentPath('foo/nested/b.txt'), 'foo/nested/');
assert.equal(ArchiveTree.selectorMatches('foo/', 'foo/a.txt'), true);
assert.equal(ArchiveTree.selectorMatches('foo/', 'foobar/c.txt'), false);

const state = ArchiveTree.createState({mode:'paths', include:[], exclude:[]});
ArchiveTree.setInventory(state, inventory);
assert.deepEqual(ArchiveTree.visibleNodes(state.tree, state.expanded).map(node => node.path), ['foo/', 'foobar/', 'server.js', 'tests/']);
assert.equal(ArchiveTree.toggleExpanded(state, 'foo/'), true);
assert.deepEqual(ArchiveTree.visibleNodes(state.tree, state.expanded).map(node => node.path), ['foo/', 'foo/a.txt', 'foo/nested/', 'foobar/', 'server.js', 'tests/']);
ArchiveTree.toggleExpanded(state, 'foo/nested/');
assert.ok(ArchiveTree.visibleNodes(state.tree, state.expanded).some(node => node.path === 'foo/nested/b.txt'));
ArchiveTree.toggleExpanded(state, 'foo/');
assert.ok(!ArchiveTree.visibleNodes(state.tree, state.expanded).some(node => node.path.startsWith('foo/') && node.path !== 'foo/'));

assert.equal(ArchiveTree.includePath(state, 'server.js'), true);
assert.equal(ArchiveTree.includePath(state, 'foo/'), true);
assert.deepEqual(state.payload.include, ['foo/', 'server.js']);
assert.equal(ArchiveTree.includePath(state, 'foo/a.txt'), false);
assert.equal(ArchiveTree.canExclude(state, 'tests/'), false);
assert.equal(ArchiveTree.excludePath(state, 'tests/'), false);
assert.equal(ArchiveTree.excludePath(state, 'foo/nested/'), true);
assert.deepEqual(state.payload.exclude, ['foo/nested/']);
assert.equal(ArchiveTree.entryState(state, 'foo/'), 'Included recursively');
assert.equal(ArchiveTree.entryState(state, 'foo/a.txt'), '');
assert.equal(ArchiveTree.entryState(state, 'foo/nested/'), 'Excluded recursively');
assert.equal(ArchiveTree.entryState(state, 'foo/nested/b.txt'), 'Excluded');
assert.deepEqual(ArchiveTree.selectionSummary(state), {
  mode:'paths', selectedDirectories:1, explicitFiles:1, excludedDirectories:1,
  excludedFiles:0, exclusionCount:1, resolvedFiles:2, missing:[],
});
assert.equal(ArchiveTree.removeExclude(state, 'foo/nested/'), true);
assert.equal(ArchiveTree.removeInclude(state, 'server.js'), true);
assert.deepEqual(state.payload.include, ['foo/']);

assert.equal(ArchiveTree.setMode(state, 'entire_archive'), true);
assert.deepEqual(state.payload, {mode:'entire_archive', include:[], exclude:[]});
assert.equal(ArchiveTree.excludePath(state, 'tests/'), true);
assert.equal(ArchiveTree.selectionSummary(state).resolvedFiles, 4);
assert.equal(ArchiveTree.entryState(state, 'foo/a.txt'), '');
assert.equal(ArchiveTree.entryState(state, 'tests/'), 'Excluded recursively');
assert.equal(ArchiveTree.setMode(state, 'paths'), true);
assert.deepEqual(state.payload, {mode:'paths', include:[], exclude:[]});

const redundant = ArchiveTree.createState({mode:'paths', include:['foo/a.txt'], exclude:[]});
ArchiveTree.setInventory(redundant, inventory);
ArchiveTree.includePath(redundant, 'foo/');
assert.deepEqual(redundant.payload.include, ['foo/']);

const legacy = ArchiveTree.createState({mode:'paths', include:['server.js', 'foo/a.txt'], exclude:[], legacy_file_layout:'basename'});
assert.deepEqual(legacy.payload, {mode:'paths', include:['server.js', 'foo/a.txt'], exclude:[], legacy_file_layout:'basename'});
ArchiveTree.setInventory(legacy, inventory);
ArchiveTree.toggleExpanded(legacy, 'foo/');
assert.equal(legacy.payload.legacy_file_layout, 'basename');
ArchiveTree.includePath(legacy, 'foobar/c.txt');
assert.equal('legacy_file_layout' in legacy.payload, false);

ArchiveTree.markStale(legacy);
const before = JSON.stringify(legacy.payload);
assert.equal(ArchiveTree.includePath(legacy, 'tests/test.js'), false);
assert.equal(ArchiveTree.excludePath(legacy, 'tests/'), false);
assert.equal(JSON.stringify(legacy.payload), before);
assert.ok(ArchiveTree.selectionSummary(legacy).explicitFiles > 0);
ArchiveTree.setInventory(legacy, inventory);
assert.equal(ArchiveTree.includePath(legacy, 'tests/test.js'), true);
