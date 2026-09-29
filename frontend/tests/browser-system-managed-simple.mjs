import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
const recipe = JSON.parse(readFileSync(new URL('../../debbuilder/builtin_recipes/debbuilder.json',import.meta.url)));
const paths = ['active','package.maintainer','build.environment','build.inactivity_timeout','build.maximum_runtime','resource_limits'];
const initialRevision = 'a'.repeat(64), savedRevision = 'b'.repeat(64), conflictRevision = 'c'.repeat(64);
let current = structuredClone(recipe), revision = initialRevision, conflict = false;
const writes = [];
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200,headers={}) => route.fulfill({status,headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'});
    if (path === '/api/system/diagnostics') return respond({schema_version:1,status:'ok',checks:[]});
    if (path === '/api/storage') return respond({storage:{state:'ready',bytes:{managed_total:0},runs:{count:0}}});
    if (path === '/api/workflows') return respond({workflows:[{id:'debbuilder',managed:true,editable_paths:paths}],errors:[]});
    if (path === '/api/workflows/debbuilder' && request.method() === 'GET') return respond(current,200,{ETag:`"${revision}"`});
    if (path === '/api/recipes/validate' && request.method() === 'POST') {writes.push({path,body:request.postDataJSON()});return respond({valid:true});}
    if (path === '/api/workflows/debbuilder' && request.method() === 'POST') {
      const body = request.postDataJSON(); writes.push({path,body});
      if (conflict) {current = {...current,package:{...current.package,maintainer:'Server Operator <server@example.test>'}}; revision = conflictRevision; return respond({error:{code:'recipe_revision_conflict',message:'Recipe changed'}},409);}
      current = body.workflow; revision = savedRevision;
      return respond({recipe:current},200,{ETag:`"${revision}"`});
    }
    return respond({error:{code:'unexpected_request',message:path}},404);
  });
  await page.goto(`${base}/#/system/managed`);
  await page.getByRole('heading',{name:'System-managed self-build'}).last().waitFor();
  assert.equal(await page.getByRole('button',{name:'Plan',exact:true}).count(),0);
  assert.equal(await page.getByText('ProBatou/DebBuilder',{exact:true}).count(),1);
  assert.equal(await page.getByText('latest_release',{exact:true}).count(),1);
  assert.equal(await page.getByRole('heading',{name:'Next step'}).count(),0);
  assert.equal(await page.getByRole('button',{name:'Test',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'Build',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'Edit',exact:true}).count(),0);
  assert.equal(await page.locator('.managed-recipe-summary').count(),1);
  assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isDisabled(),true);
  assert.equal(await page.locator('.managed-recipe-summary').evaluate(node => Math.abs(node.getBoundingClientRect().width - node.parentElement.getBoundingClientRect().width) < 2),true);
  if (process.env.DEBBUILDER_MANAGED_CAPTURE) await page.screenshot({path:process.env.DEBBUILDER_MANAGED_CAPTURE,fullPage:true});
  assert.deepEqual(await page.locator('.managed-recipe-summary [data-recipe-path]').evaluateAll(nodes => nodes.map(node => node.dataset.recipePath)),[
    'active','package.maintainer','build.environment','build.inactivity_timeout','build.maximum_runtime',
    'resource_limits.memory_max_bytes','resource_limits.tasks_max','resource_limits.cpu_quota_percent',
    'resource_limits.io_read_bandwidth_max_bytes_per_sec','resource_limits.io_write_bandwidth_max_bytes_per_sec',
  ]);
  assert.equal(await page.locator('[data-recipe-path="source.repository"]').count(),0);
  assert.equal(await page.locator('[data-recipe-path="build.inactivity_timeout"] input').isVisible(),true);
  const maintainer = page.locator('[data-recipe-path="package.maintainer"] input');
  await maintainer.fill('Operator <operator@example.test>');
  await page.getByRole('button',{name:'Save',exact:true}).click();
  await page.waitForFunction(() => document.querySelector('.managed-recipe-actions .button.primary')?.disabled === true);
  assert.equal(writes[0].path,'/api/recipes/validate');
  assert.equal(writes[1].path,'/api/workflows/debbuilder');
  assert.equal(writes[1].body.expected_revision,initialRevision);
  assert.equal(writes[1].body.workflow.package.maintainer,'Operator <operator@example.test>');
  assert.equal(writes[1].body.workflow.source.repository,recipe.source.repository);
  await page.locator('[data-recipe-path="active"] input').uncheck();
  conflict = true;
  await page.getByRole('button',{name:'Save',exact:true}).click();
  await page.getByRole('heading',{name:'Recipe conflict'}).waitFor();
  assert.equal(await page.locator('[data-recipe-path="active"] input').isChecked(),false);
  assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isDisabled(),true);
  assert.equal(writes.at(-1).body.expected_revision,savedRevision);
  assert.deepEqual(errors,[]);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  console.log('System managed self-build simple view checks passed');
} finally {await browser.close();}
