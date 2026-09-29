import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5181';
const recipe = {schema_version:5,name:'recipe-one',active:true,package:{name:'example'}};
const runs = {
  validate:{id:'validate',package:'example',recipe_id:'recipe-one',mode:'build',status:'success',lifecycle_status:'validation_needed',allowed_actions:{validate:true,publish:false},version:{debian:'1.2-3'},artifact:{inspection:{package:'example',version:'1.2-3'}},steps:[]},
  publish:{id:'publish',package:'example',recipe_id:'recipe-one',mode:'build',status:'success',lifecycle_status:'ready_to_publish',allowed_actions:{validate:true,publish:true},version:{debian:'1.2-3'},artifact:{inspection:{package:'example',version:'1.2-3'}},steps:[]},
  prepared:{id:'prepared',package:'example',recipe_id:'recipe-one',mode:'dry_run',status:'prepared',lifecycle_status:'prepared',ready_for_build:true,allowed_actions:{validate:false,publish:false},steps:[]},
  failed:{id:'failed',package:'example',recipe_id:'recipe-one',mode:'build',status:'failed',lifecycle_status:'build_failed',allowed_actions:{validate:false,publish:false},steps:[]},
  validationFailed:{id:'validationFailed',package:'example',recipe_id:'recipe-one',mode:'build',status:'success',lifecycle_status:'validation_failed',allowed_actions:{validate:true,publish:false},steps:[]},
  publicationFailed:{id:'publicationFailed',package:'example',recipe_id:'recipe-one',mode:'build',status:'success',lifecycle_status:'publication_failed',allowed_actions:{validate:true,publish:true},steps:[]},
  running:{id:'running',package:'example',recipe_id:'recipe-one',mode:'build',status:'running',lifecycle_status:'building',allowed_actions:{validate:false,publish:false},steps:[]},
  completed:{id:'completed',package:'example',status:'completed',lifecycle_status:'published',allowed_actions:{validate:false,publish:false},steps:[]},
  cancelled:{id:'cancelled',package:'example',status:'cancelled',lifecycle_status:'cancelled',allowed_actions:{validate:false,publish:false},steps:[]},
};
const writes = [];
let recipeRevision = 'a'.repeat(64);
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200,headers={}) => route.fulfill({status,contentType:'application/json',headers,body:JSON.stringify(body)});
    if (request.method() === 'POST') {
      writes.push({path,body:request.postDataJSON()});
      if (path === '/api/executions/validate/validate') {
        runs.validate.lifecycle_status = 'validating'; runs.validate.allowed_actions.validate = false;
        return respond({validation:{attempt_id:'validation-one',status:'queued'}},202);
      }
      if (path === '/api/executions/publish/publish') {
        runs.publish.lifecycle_status = 'published'; runs.publish.allowed_actions.publish = false;
        return respond({publication:{status:'success'}},200);
      }
      if (path === '/api/recipes/validate') return respond({valid:true});
      if (path === '/api/run') return respond({run_id:'new-build',status:'queued'},202);
      return respond({error:{code:'unexpected_write',message:path}},500);
    }
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'local'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/executions') return respond({executions:Object.values(runs)});
    if (path === '/api/workflows/recipe-one') return respond(recipe,200,{ETag:`"${recipeRevision}"`});
    if (path.endsWith('/logs')) return respond({log:{text:'',offset:0}});
    const runId = path.startsWith('/api/executions/') ? decodeURIComponent(path.slice('/api/executions/'.length)) : null;
    if (runs[runId]) return respond({execution:runs[runId]});
    return respond({error:{code:'unexpected_read',message:path}},404);
  });

  const buttons = page.locator('.run-main .detail-buttons');
  const dialog = page.getByRole('dialog');
  await page.goto(`${base}/#/runs`);
  await page.locator('.run-main .detail-breadcrumb strong').getByText('validate').waitFor();
  assert.equal(await page.locator('.run-selection .selected').count(),1);
  const statusFilter = page.locator('.run-filters select');
  await statusFilter.selectOption('failed');
  assert.deepEqual((await page.locator('.run-selection .run-list-row small').allTextContents()).map(text => text.split(' · ').at(-1)),['failed','validationFailed','publicationFailed']);
  await statusFilter.selectOption('completed');
  assert.equal(await page.locator('.run-selection .run-list-row').count(),1);
  await statusFilter.selectOption('cancelled');
  assert.equal(await page.locator('.run-selection .run-list-row').count(),1);
  await statusFilter.selectOption('active');
  assert.equal(await page.locator('.run-selection .run-list-row').count(),1);
  await statusFilter.selectOption('all');
  await page.locator('.run-filters input').fill('no-such-run');
  await page.locator('.run-selection .empty-state').getByText('No items').waitFor();
  await page.locator('.run-filters input').clear();
  await page.goto(`${base}/#/runs/validate`);
  await buttons.getByRole('button',{name:/Start validation/}).click();
  const startValidation = dialog.getByRole('button',{name:'Start validation',exact:true});
  await startValidation.waitFor();
  assert.deepEqual(writes,[]);
  runs.validate.allowed_actions.validate = false;
  await startValidation.click();
  await dialog.getByText('This action is no longer available').waitFor();
  assert.deepEqual(writes,[]);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});
  runs.validate.allowed_actions.validate = true;
  await buttons.getByRole('button',{name:/Start validation/}).click();
  await startValidation.click();
  await dialog.getByText('Validation started.').waitFor();
  assert.deepEqual(writes[0],{path:'/api/executions/validate/validate',body:{}});
  await dialog.getByRole('button',{name:'Close'}).last().click();

  await page.goto(`${base}/#/runs/publish`);
  assert.equal(await buttons.getByRole('button',{name:/Start validation/}).count(),0);
  await buttons.getByRole('button',{name:/Publish to APT/}).click();
  const publish = dialog.getByRole('button',{name:'Publish to APT',exact:true});
  await publish.waitFor();
  assert.equal(await publish.isDisabled(),true);
  await dialog.getByRole('checkbox',{name:/confirm publication/}).check();
  runs.publish.artifact.inspection.version = '1.2-4';
  await publish.click();
  await dialog.getByText('This action is no longer available').waitFor();
  assert.equal(writes.length,1);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});
  runs.publish.artifact.inspection.version = '1.2-3';
  await buttons.getByRole('button',{name:/Publish to APT/}).click();
  await dialog.getByRole('checkbox',{name:/confirm publication/}).check();
  await publish.click();
  await dialog.getByText('Publication succeeded.').waitFor();
  assert.deepEqual(writes[1],{path:'/api/executions/publish/publish',body:{confirm:'publish:example:1.2-3'}});
  await dialog.getByRole('button',{name:'Close'}).last().click();

  await page.goto(`${base}/#/runs/prepared`);
  await buttons.getByRole('button',{name:/Start Build/}).click();
  const startBuild = dialog.getByRole('button',{name:'Start Build',exact:true});
  await startBuild.waitFor();
  assert.equal(writes.length,2);
  recipeRevision = 'b'.repeat(64);
  await startBuild.click();
  await dialog.getByText('This Run or saved Recipe has changed').waitFor();
  assert.equal(writes.length,2);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});
  recipeRevision = 'a'.repeat(64);
  await buttons.getByRole('button',{name:/Start Build/}).click();
  await startBuild.click();
  await dialog.getByText('Build queued.').waitFor();
  assert.deepEqual(writes.slice(2).map(write => write.path),['/api/recipes/validate','/api/run']);
  assert.deepEqual(writes[2].body.recipe,writes[3].body.workflow);
  assert.equal(writes[3].body.dry_run,false);
  await dialog.getByRole('button',{name:/View Run/}).click();
  await page.waitForURL('**/#/runs/new-build');

  await page.goto(`${base}/#/runs/failed`);
  await buttons.getByRole('button',{name:/Start Build/}).waitFor();
  await page.goto(`${base}/#/runs/validationFailed`);
  await buttons.getByRole('button',{name:/Start validation/}).waitFor();
  await page.goto(`${base}/#/runs/publicationFailed`);
  await buttons.getByRole('button',{name:/Publish to APT/}).waitFor();
  await page.goto(`${base}/#/runs/running`);
  await buttons.getByRole('button',{name:'Review Recipe'}).waitFor();
  assert.equal(await buttons.locator('button').count(),1);
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/#/runs/failed`);
  await buttons.getByRole('button',{name:/Start Build/}).click();
  await dialog.getByRole('button',{name:'Start Build',exact:true}).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('Run Build, validation, and publication actions passed');
} finally {await browser.close();}
