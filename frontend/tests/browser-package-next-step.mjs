import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5181';
const recipe = {...JSON.parse(readFileSync(new URL('../../tests/fixtures/recipes/seerr.json',import.meta.url))),name:'recipe-update'};
const packages = [
  {name:'needs-validation',recipe:'recipe-validation',state:'validation_needed',run:'run-validation',actions:{validate:true}},
  {name:'ready-to-publish',recipe:'recipe-publication',state:'ready_to_publish',run:'run-publication',actions:{publish:true}},
  {name:'update-available',recipe:'recipe-update',state:'update_available',run:'',actions:{build:true}},
  {name:'build-failed',recipe:'recipe-failed',state:'build_failed',run:'run-failed',actions:{build:true}},
  {name:'building-now',recipe:'recipe-running',state:'building',run:'run-running',actions:{}},
  {name:'up-to-date',recipe:'recipe-current',state:'up_to_date',run:'run-current',actions:{}},
  {name:'without-recipe',recipe:'',state:'recipe_missing',run:'',actions:{}},
];
const detail = row => ({name:row.name,recipe:row.recipe,lifecycle_display_status:row.state,source:{type:'github',repository:`example/${row.name}`},version:{source:row.name === 'update-available' ? '3.0.0' : '',candidate:row.run?'1.2-3':'',published:''},build:{latest_run_id:row.run},allowed_actions:row.actions});
const writes = [];
let validationAllowed = true;
let publicationVersion = '1.2-3';
let recipeRevision = 'a'.repeat(64);
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200,headers={}) => route.fulfill({status,contentType:'application/json',headers,body:JSON.stringify(body)});
    if (request.method() !== 'GET') {
      writes.push({path,body:request.postDataJSON()});
      if (path === '/api/executions/run-validation/validate') {
        packages[0].state = 'validating'; packages[0].actions.validate = false;
        return respond({validation:{attempt_id:'attempt-one',status:'queued'}},202);
      }
      if (path === '/api/executions/run-publication/publish') {
        packages[1].state = 'published'; packages[1].actions.publish = false;
        return respond({publication:{status:'success'}},200);
      }
      if (path === '/api/recipes/validate') return respond({valid:true});
      if (path === '/api/run') return respond({run_id:'run-update',status:'queued'},202);
      return respond({error:{code:'unexpected_write',message:path}},500);
    }
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'local'});
    if (path === '/api/status') return respond({ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'});
    if (path === '/api/packages') return respond({packages:packages.map(detail)});
    if (path.startsWith('/api/packages/')) {
      const row = packages.find(item => item.name === decodeURIComponent(path.slice('/api/packages/'.length)));
      if (row) return respond({package:{...detail(row),allowed_actions:{...row.actions,validate:row.name === 'needs-validation' ? validationAllowed && row.actions.validate : row.actions.validate}}});
    }
    if (path.startsWith('/api/executions/')) {
      const row = packages.find(item => item.run === decodeURIComponent(path.slice('/api/executions/'.length)));
      if (row) {
        const version = row.name === 'ready-to-publish' ? publicationVersion : '1.2-3';
        return respond({execution:{id:row.run,package:row.name,version:{debian:version},artifact:{inspection:{package:row.name,version}},allowed_actions:{...row.actions,validate:row.name === 'needs-validation' ? validationAllowed && row.actions.validate : row.actions.validate}}});
      }
    }
    if (path === '/api/workflows/recipe-update') return respond(recipe,200,{ETag:`"${recipeRevision}"`});
    if (path === '/api/recipes') return respond({recipes:[]});
    if (path === '/api/workflows') return respond({workflows:[],errors:[]});
    if (path.startsWith('/api/recipes/')) return respond({inspection:{},automation:{}});
    return respond({error:{code:'unexpected_read',message:path}},404);
  });

  const step = page.locator('.package-detail');
  await page.goto(`${base}/#/packages/needs-validation`);
  await step.getByRole('button',{name:/Start validation/}).waitFor();
  assert.equal(await step.getByText('Next step').count(),0);
  if (process.env.DEBBUILDER_PACKAGE_DESKTOP_CAPTURE) await page.screenshot({path:process.env.DEBBUILDER_PACKAGE_DESKTOP_CAPTURE,fullPage:true});
  if (process.env.DEBBUILDER_PACKAGE_DARK_CAPTURE) {
    await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
    await page.screenshot({path:process.env.DEBBUILDER_PACKAGE_DARK_CAPTURE,fullPage:true});
    await page.evaluate(() => document.documentElement.dataset.theme = 'light');
  }
  assert.deepEqual(writes,[]);
  validationAllowed = false;
  await step.getByRole('button',{name:/Start validation/}).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('This action is no longer available').waitFor();
  assert.equal(await dialog.getByRole('button',{name:'Start validation',exact:true}).count(),0);
  assert.deepEqual(writes,[]);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});
  validationAllowed = true;
  await step.getByRole('button',{name:/Start validation/}).click();
  await dialog.getByRole('button',{name:'Start validation',exact:true}).waitFor();
  assert.deepEqual(writes,[]);
  await dialog.getByRole('button',{name:'Start validation',exact:true}).click();
  await dialog.getByText('Validation started.').waitFor();
  assert.deepEqual(writes[0],{path:'/api/executions/run-validation/validate',body:{}});
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});

  await page.goto(`${base}/#/packages/ready-to-publish`);
  await step.getByRole('button',{name:/Publish to APT/}).click();
  const publish = dialog.getByRole('button',{name:'Publish to APT',exact:true});
  await publish.waitFor();
  assert.equal(await publish.isDisabled(),true);
  await dialog.getByRole('checkbox',{name:/confirm publication/}).check();
  publicationVersion = '1.2-4';
  await publish.click();
  await dialog.getByText('This action is no longer available').waitFor();
  assert.equal(writes.length,1);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});
  publicationVersion = '1.2-3';
  await step.getByRole('button',{name:/Publish to APT/}).click();
  await publish.waitFor();
  await dialog.getByRole('checkbox',{name:/confirm publication/}).check();
  await publish.click();
  await dialog.getByText('Publication succeeded.').waitFor();
  assert.deepEqual(writes[1],{path:'/api/executions/run-publication/publish',body:{confirm:'publish:ready-to-publish:1.2-3'}});
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});

  await page.goto(`${base}/#/packages/update-available`);
  await step.getByRole('button',{name:/Build update/}).click();
  const startBuild = dialog.getByRole('button',{name:'Start Build',exact:true});
  await startBuild.waitFor();
  assert.equal(writes.length,2);
  recipeRevision = 'b'.repeat(64);
  await startBuild.click();
  await dialog.getByText('This update or Recipe has changed').waitFor();
  assert.equal(writes.length,2);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await dialog.waitFor({state:'hidden'});
  recipeRevision = 'a'.repeat(64);
  await step.getByRole('button',{name:/Build update/}).click();
  await startBuild.waitFor();
  await startBuild.click();
  await dialog.getByText('Build queued.').waitFor();
  assert.deepEqual(writes.slice(2).map(write => write.path),['/api/recipes/validate','/api/run']);
  assert.deepEqual(writes[2].body.recipe,writes[3].body.workflow);
  assert.equal(writes[3].body.dry_run,false);
  await dialog.getByRole('button',{name:/View Run/}).click();
  await page.waitForURL('**/#/runs/run-update');

  await page.goto(`${base}/#/packages/build-failed`);
  await step.getByRole('button',{name:/View diagnosis and logs/}).click();
  await page.waitForURL('**/#/runs/run-failed');

  await page.goto(`${base}/#/packages/building-now`);
  await step.getByRole('button',{name:/Follow Run/}).waitFor();
  await page.goto(`${base}/#/packages/up-to-date`);
  await step.getByRole('button',{name:/Review Recipe/}).waitFor();
  await page.goto(`${base}/#/packages/without-recipe`);
  await step.getByRole('heading',{name:'without-recipe'}).waitFor();
  assert.equal(await step.locator('.package-detail-action').count(),0);
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/#/packages/update-available`);
  await step.getByRole('button',{name:/Build update/}).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await step.getByRole('button',{name:/Build update/}).click();
  await dialog.getByRole('button',{name:'Start Build',exact:true}).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  if (process.env.DEBBUILDER_PACKAGE_CAPTURE) await page.screenshot({path:process.env.DEBBUILDER_PACKAGE_CAPTURE,fullPage:true});
  assert.deepEqual(errors,[]);
  console.log('Package next-step actions and state-specific card checks passed');
} finally {await browser.close();}
