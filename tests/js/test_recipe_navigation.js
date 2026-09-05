const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const select = {value:'previous', options:[{value:'demo'}]};
const events = [];
const context = vm.createContext({
  adminState:{packages:[]},
  $: id => id === 'workflowSelect' ? select : null,
  closePackageDrawer: () => events.push('close'),
  refreshWorkflows: async () => events.push('refresh'),
  switchView: view => events.push(`view:${view}`),
  loadSelectedWorkflow: async () => events.push(`load:${select.value}`),
});
vm.runInContext(fs.readFileSync('static/js/pages/packages.js', 'utf8'), context, {filename:'packages.js'});

// Replace declarations used by openLinkedRecipe with deterministic test doubles.
context.refreshWorkflows = async () => events.push('refresh');
context.closePackageDrawer = () => events.push('close');
context.switchView = view => events.push(`view:${view}`);
context.loadSelectedWorkflow = async () => events.push(`load:${select.value}`);

(async () => {
  await context.openLinkedRecipe('demo');
  assert.equal(select.value, 'demo');
  assert.deepEqual(events, ['refresh', 'close', 'view:recipes', 'load:demo']);

  events.length = 0;
  select.value = 'previous';
  select.options = [{value:'another-recipe'}];
  await assert.rejects(() => context.openLinkedRecipe('deleted-recipe'), /no longer exists/);
  assert.equal(select.value, 'previous');
  assert.deepEqual(events, ['refresh']);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
