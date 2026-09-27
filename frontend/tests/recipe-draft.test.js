import assert from 'node:assert/strict';
import test from 'node:test';
import {spawnSync} from 'node:child_process';
import {hydrate, candidate, dirty, change, remove, move, cancel, equal, changedPaths, validationErrors, readPath} from '../src/features/recipes/draft.js';
import {ownership} from '../src/features/recipes/fields.js';

const python = `import json, pathlib
from debbuilder.recipe_schema import recipe_document_for_storage
from tests.ui.showcase import showcase_recipes
paths = list(pathlib.Path('tests/fixtures/recipes').glob('*.json')) + [pathlib.Path('debbuilder/builtin_recipes/debbuilder.json')]
corpus = {str(path): recipe_document_for_storage(json.loads(path.read_text())) for path in paths}
corpus.update({'showcase/' + name: recipe for name, recipe in showcase_recipes().items()})
print(json.dumps(corpus))`;
const result = spawnSync('python3',['-c',python],{cwd:new URL('../../',import.meta.url).pathname,encoding:'utf8'});
if (result.status !== 0) throw new Error(result.stderr);
const corpus = JSON.parse(result.stdout);
const recipes = Object.values(corpus);
const simple = recipes.find(recipe => recipe.name === 'seerr');
const managed = recipes.find(recipe => recipe.management);

test('all canonical fixture documents round-trip without edits', () => {
  assert.ok(recipes.length >= 10);
  for (const [name, recipe] of Object.entries(corpus)) {
    const editor = hydrate(recipe);
    assert.deepEqual(candidate(editor),recipe,name);
    assert.equal(dirty(editor),false,name);
    assert.ok(equal(editor.baseline,editor.draft));
  }
});

test('one field changes only its requested path and restoring it clears dirty', () => {
  for (const [path, value] of [
    ['package.description','Edited description'],['source.tracking','tag'],
    ['build.commands[0]','echo changed'],['build.environment.TEST_KEY','value'],
    ['service.user','operator'],['resource_limits.tasks_max',16],
    ['automation.enabled',true],['install.maintainer_scripts.postinst','echo ready'],
  ]) {
    const editor = hydrate(simple);
    const updated = change(editor,path,value);
    assert.deepEqual(changedPaths(updated),[`$.${path}`],path);
    assert.deepEqual(candidate(editor),simple,path);
    assert.equal(dirty(updated),true,path);
    const original = readPath(simple,path);
    // Paths absent from the baseline must be removed to restore absence.
    const restored = original === undefined ? remove(updated,path) : change(updated,path,original);
    assert.equal(dirty(restored),false,path);
  }
});

test('structured environment keys remain literal, including punctuation', () => {
  const editor = hydrate(simple);
  const original = editor.draft.build.environment;
  const updated = change(editor,'build.environment',Object.fromEntries([...Object.entries(original),['A.B','literal']]));
  assert.equal(updated.draft.build.environment['A.B'],'literal');
  assert.equal(dirty(updated),true);
  assert.equal(dirty(change(updated,'build.environment',original)),false);
});

test('array add, remove, reorder and Cancel preserve exact baseline', () => {
  let editor = hydrate(simple);
  const path = 'build.commands', before = editor.draft.build.commands;
  editor = change(editor,path,[...before,'echo extra']);
  editor = remove(editor,`${path}[${before.length}]`);
  assert.equal(dirty(editor),false);
  editor = change(editor,path,['first','second',...before]);
  editor = move(editor,path,0,1);
  assert.equal(dirty(editor),true);
  assert.deepEqual(candidate(cancel(editor)),simple);
});

test('schema-dependent mode changes stay compact and switching back restores the exact draft', () => {
  const archive = recipes.find(recipe => recipe.artifact.mode === 'upstream_archive') || {
    ...simple, artifact:{...simple.artifact, mode:'upstream_archive',type:'archive',payload:{mode:'paths',include:['bin/app'],exclude:[]}},
  };
  let editor = hydrate(archive);
  editor = change(editor,'artifact.mode','source_build');
  assert.equal(editor.draft.artifact.type,'deb');
  assert.equal(Object.hasOwn(editor.draft.artifact,'payload'),false);
  editor = change(editor,'artifact.mode','upstream_archive');
  assert.equal(dirty(editor),false);
  editor = change(editor,'build.output.mode','paths');
  assert.equal(Object.hasOwn(editor.draft.build.output,'path'),false);
  editor = change(editor,'build.output.mode',archive.build.output.mode);
  assert.equal(dirty(editor),false);
  editor = change(editor,'artifact.payload.mode','entire_archive');
  assert.deepEqual(editor.draft.artifact.payload.include,[]);
  editor = change(editor,'artifact.payload.mode','paths');
  assert.equal(dirty(editor),false);
  let installEditor = change(hydrate(simple),'install.content.source','configured_files');
  assert.equal(installEditor.draft.install.destination,'');
  installEditor = change(installEditor,'install.content.source','build_output');
  assert.equal(dirty(installEditor),false);
});

test('managed allowlist is backend supplied and forbidden writes fail closed', () => {
  assert.ok(managed);
  const editablePaths = ['active','package.maintainer','build.environment','build.inactivity_timeout','build.maximum_runtime','resource_limits'];
  const editor = hydrate(managed,{managed:true,editablePaths});
  assert.deepEqual(candidate(editor),managed);
  assert.equal(ownership('source.repository',true,editablePaths),'READ_ONLY_MANAGED');
  assert.throws(() => change(editor,'source.repository','someone/else'),/read-only/);
  assert.throws(() => remove(editor,'management.operator_overrides'),/read-only/);
  assert.throws(() => change(hydrate(simple),'source.provider','other'),/read-only/);
  const updated = change(editor,'active',!managed.active);
  assert.deepEqual(changedPaths(updated),['$.active']);
});

test('validation paths retain backend technical text', () => {
  const error = {message:'Invalid command',path:'$.build.commands[2]'};
  const mapped = validationErrors(error);
  assert.deepEqual(mapped.fields['$.build.commands[2]'],[error]);
  assert.deepEqual(mapped.items['$.build.commands[2]'],[error]);
  assert.deepEqual(mapped.sections.build,[error]);
});
