import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5178';
const rows = [
  {name:'needs-validation',lifecycle_display_status:'validation_needed',build:{latest_run_id:'build-validate'}},
  {name:'ready-to-publish',lifecycle_display_status:'ready_to_publish',build:{latest_run_id:'build-publish'}},
  {name:'update-package',lifecycle_display_status:'update_available',recipe:'recipe-update',version:{source:'3.0.0'},build:{latest_run_id:''}},
];
const actionFor = name => name === 'needs-validation' ? 'validate' : name === 'update-package' ? 'build' : 'publish';
const writes = [];
let validationAllowed = true;
let pauseFirstValidationRead = true;
let releaseValidationRead;
const validationReadGate = new Promise(resolve => {releaseValidationRead = resolve;});
let recipeRevision = 'a'.repeat(64);
const recipe = {schema_version:5,name:'recipe-update',active:true,package:{name:'update-package'}};
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200,headers={}) => route.fulfill({status,contentType:'application/json',headers,body:JSON.stringify(body)});
    if (request.method() === 'POST') {
      writes.push({path,body:request.postDataJSON()});
      if (path === '/api/executions/build-validate/validate') return respond({validation:{attempt_id:'attempt-one',status:'queued'}},202);
      if (path === '/api/executions/build-publish/publish') return respond({publication:{status:'success'}},200);
      if (path === '/api/recipes/validate') return respond({valid:true});
      if (path === '/api/run') return respond({run_id:'build-update',status:'queued'},202);
      return respond({error:{code:'unexpected_write',message:path}},500);
    }
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'local'});
    if (path === '/api/status') return respond({ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'});
    if (path === '/api/dashboard') return respond({dashboard:{packages:3,ready_to_publish:1,package_rows:rows,latest_operations:[]}});
    if (path === '/api/system/diagnostics') return respond({checks:[]});
    if (path === '/api/workflows/recipe-update') return respond(recipe,200,{ETag:`"${recipeRevision}"`});
    const name = path.startsWith('/api/packages/') ? decodeURIComponent(path.slice('/api/packages/'.length)) : null;
    const row = rows.find(item => item.name === name);
    if (row) {
      if (name === 'needs-validation' && pauseFirstValidationRead) {pauseFirstValidationRead = false; await validationReadGate;}
      return respond({package:{...row,allowed_actions:{[actionFor(name)]:name !== 'needs-validation' || validationAllowed}}});
    }
    const runId = path.startsWith('/api/executions/') ? decodeURIComponent(path.slice('/api/executions/'.length)) : null;
    const runRow = rows.find(item => item.build.latest_run_id === runId);
    if (runRow) return respond({execution:{id:runId,package:runRow.name,version:{debian:'1.2-3'},artifact:{inspection:{package:runRow.name,version:'1.2-3'}},allowed_actions:{[actionFor(runRow.name)]:runRow.name !== 'needs-validation' || validationAllowed}}});
    return respond({error:{code:'unexpected_read',message:path}},404);
  });
  await page.goto(`${base}/#/overview`);
  try {await page.locator('.overview-grid').waitFor({timeout:5000});}
  catch (error) {console.error(errors,await page.locator('body').innerText()); throw error;}
  assert.deepEqual(writes,[]);

  const validationRow = page.locator('.overview-main .click-row').filter({hasText:'needs-validation'});
  await validationRow.click();
  const dialog = page.getByRole('dialog');
  await validationRow.getByText('Checking…').waitFor();
  assert.equal(await dialog.isVisible(),false);
  assert.equal(await validationRow.getAttribute('aria-busy'),'true');
  releaseValidationRead();
  await dialog.getByRole('button',{name:'Start validation'}).waitFor();
  assert.equal(await dialog.getByText('Loading…').count(),0);
  assert.deepEqual(writes,[]);
  assert.equal(await page.evaluate(() => document.activeElement?.id),'overview-action-title');
  await dialog.locator('.modal-copy').click();
  assert.equal(await dialog.isVisible(),true);
  await page.mouse.click(10,10);
  await dialog.waitFor({state:'hidden'});
  assert.equal(await validationRow.evaluate(element => document.activeElement === element),true);
  await validationRow.click();
  await dialog.getByRole('button',{name:'Start validation'}).waitFor();
  await dialog.getByRole('button',{name:'Start validation'}).click();
  await dialog.getByText('Validation started.').waitFor();
  assert.deepEqual(writes,[{path:'/api/executions/build-validate/validate',body:{}}]);
  await dialog.getByRole('button',{name:'Close'}).last().click();

  await page.getByRole('button',{name:/ready-to-publish.*Publish package/}).click();
  const publishButton = dialog.getByRole('button',{name:'Publish to APT'});
  await publishButton.waitFor();
  assert.equal(await publishButton.isDisabled(),true);
  await dialog.getByRole('checkbox',{name:/confirm publication/}).check();
  await publishButton.click();
  await dialog.getByText('Publication succeeded.').waitFor();
  assert.deepEqual(writes[1],{path:'/api/executions/build-publish/publish',body:{confirm:'publish:ready-to-publish:1.2-3'}});
  await dialog.getByRole('button',{name:'Close'}).last().click();

  validationAllowed = false;
  await page.getByRole('button',{name:/needs-validation.*Validate package/}).click();
  await dialog.getByText('This action is no longer available').waitFor();
  assert.equal(await dialog.getByRole('button',{name:'Start validation'}).count(),0);
  assert.equal(writes.length,2);
  await dialog.getByRole('button',{name:'Close'}).last().click();

  await page.getByRole('button',{name:/update-package.*Build update/}).click();
  const startBuild = dialog.getByRole('button',{name:'Start Build',exact:true});
  await startBuild.waitFor();
  assert.equal(writes.length,2);
  recipeRevision = 'b'.repeat(64);
  await startBuild.click();
  await dialog.getByText('This update or Recipe has changed').waitFor();
  assert.equal(writes.length,2);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  recipeRevision = 'a'.repeat(64);
  await page.getByRole('button',{name:/update-package.*Build update/}).click();
  await startBuild.waitFor();
  await startBuild.click();
  await dialog.getByText('Build queued.').waitFor();
  assert.deepEqual(writes.slice(2).map(write => write.path),['/api/recipes/validate','/api/run']);
  assert.deepEqual(writes[2].body.recipe,writes[3].body.workflow);
  assert.equal(writes[3].body.dry_run,false);
  await dialog.getByRole('button',{name:/View Run/}).click();
  assert.ok(page.url().endsWith('#/runs/build-update'));
  assert.deepEqual(errors,[]);
  console.log('Overview validation, publication, and direct update Build checks passed');
} finally {await browser.close();}
