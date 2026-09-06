const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

function functionSource(source, name, nextName) {
  const start = source.indexOf(`async function ${name}`);
  const end = source.indexOf(`async function ${nextName}`, start + 1);
  assert.notEqual(start, -1, `${name} was not found`);
  assert.notEqual(end, -1, `${nextName} was not found`);
  return source.slice(start, end);
}

(async () => {
  const uiCore = fs.readFileSync('static/ui_core.js', 'utf8');
  const postStart = uiCore.indexOf('async function postJson');
  const postEnd = uiCore.indexOf('function fmtTime', postStart);
  const requestContext = vm.createContext({
    fetch: async () => ({
      ok: true,
      status: 202,
      statusText: 'Accepted',
      json: async () => ({run_id: 'accepted-run', status: 'queued'}),
    }),
  });
  vm.runInContext(uiCore.slice(postStart, postEnd), requestContext);
  const accepted = await vm.runInContext("postJson('/api/run', {dry_run:true})", requestContext);
  assert.deepEqual({...accepted}, {run_id: 'accepted-run', status: 'queued'});
  assert.match(uiCore, /queued:\s*'Queued'/);
  assert.match(uiCore, /building:\s*'Running'/);
  assert.match(uiCore, /running:\s*'Running'/);

  const app = fs.readFileSync('static/app.js', 'utf8');
  const followed = [];
  const switched = [];
  const openedTestModals = [];
  const toasts = [];
  let recipePosts = 0;
  const recipeContext = vm.createContext({
    recipeRunSubmissionInFlight: false,
    assertRecipeVersionRevisionIsValid: () => {},
    collectWorkflow: () => ({name: 'demo', build: {output: {mode: 'all'}}}),
    buildOutputIsComplete: () => true,
    postJson: async (_url, body) => {
      recipePosts += 1;
      return {run_id: body.dry_run ? 'dry-run-id' : 'build-run-id', status: 'queued'};
    },
    showToast: message => toasts.push(message),
    loadExecutions: async () => {},
    switchView: view => switched.push(view),
    openExecution: async id => followed.push(id),
    openTestRunModal: payload => openedTestModals.push(payload),
    showConfirm: async () => true,
    refreshRecipeApplicability: () => {},
  });
  vm.runInContext(
    functionSource(app, 'dryRun', 'buildReal')
      + functionSource(app, 'buildReal', 'deleteCurrentRecipe'),
    recipeContext,
  );
  await vm.runInContext('Promise.all([dryRun(), dryRun()])', recipeContext);
  await vm.runInContext('buildReal()', recipeContext);
  assert.deepEqual(openedTestModals.map(row => row.runId), ['dry-run-id']);
  assert.equal(openedTestModals[0].workflow.name, 'demo');
  assert.deepEqual(followed, ['build-run-id']);
  assert.deepEqual(switched, ['logs']);
  assert.deepEqual(toasts, ['Test queued: dry-run-id', 'Build queued: build-run-id']);
  assert.equal(recipePosts, 2);

  const packages = fs.readFileSync('static/js/pages/packages.js', 'utf8');
  const packageFollowed = [];
  const packageContext = vm.createContext({
    packageRunSubmissions: new Set(),
    adminState: {packages: [{name: 'demo', recipe: 'demo-recipe'}]},
    showConfirm: async () => true,
    getJson: async () => ({name: 'demo-recipe'}),
    postJson: async () => ({run_id: 'package-run', status: 'queued'}),
    showToast: message => toasts.push(message),
    loadExecutions: async () => {},
    loadPackages: async () => {},
    switchView: view => switched.push(view),
    openExecution: async id => packageFollowed.push(id),
  });
  vm.runInContext(functionSource(packages, 'buildPackage', 'createRecipeFromDialog'), packageContext);
  await vm.runInContext("Promise.all([buildPackage('demo', true), buildPackage('demo', true)])", packageContext);
  assert.deepEqual(packageFollowed, ['package-run']);
  assert.equal(toasts.at(-1), 'Test queued: package-run');

  console.log('async run JS tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
