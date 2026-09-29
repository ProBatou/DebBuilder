import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
let recipe = JSON.parse(readFileSync(new URL('../../tests/fixtures/recipes/seerr.json',import.meta.url)));
let revision = 'a'.repeat(64);
let listCalls = 0;
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200,headers={}) => route.fulfill({status,contentType:'application/json',headers,body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/recipes') {listCalls++; return respond({recipes:[{id:'seerr',package:'seerr',repository:'example/seerr'}]});}
    if (path === '/api/workflows') return respond({workflows:[{id:'seerr',writable:true}],errors:[]});
    if (path === '/api/workflows/seerr' && request.method() === 'GET') return respond(recipe,200,{ETag:`"${revision}"`});
    if (path === '/api/workflows/seerr' && request.method() === 'POST') {
      recipe = request.postDataJSON().workflow;
      revision = 'b'.repeat(64);
      return respond({recipe},200,{ETag:`"${revision}"`});
    }
    if (path === '/api/recipes/validate') return respond({ok:true,recipe:request.postDataJSON().recipe,collision:null});
    if (path === '/api/dashboard') return respond({dashboard:{packages:0,ready_to_publish:0,package_rows:[],latest_operations:[]}});
    if (path === '/api/system/diagnostics') return respond({checks:[]});
    return respond({error:{code:'unexpected',message:path}},404);
  });
  await page.goto(`${base}/#/recipes/seerr`);
  await page.getByRole('heading',{name:'seerr'}).waitFor();
  await page.getByRole('button',{name:'Customize',exact:true}).click();
  const field = page.locator('[data-recipe-path="package.description"] textarea');
  const group = field.locator('xpath=ancestor::details[1]');
  if (!await group.evaluate(node => node.open)) await group.locator('summary').click();
  await field.fill('Saved description');
  await page.getByRole('button',{name:'Save',exact:true}).click();
  await page.locator('.recipe-plan').waitFor();
  assert.equal(recipe.package.description,'Saved description');
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Overview'}).click();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Recipes'}).click();
  await page.getByRole('heading',{name:'seerr'}).waitFor();
  await page.getByRole('button',{name:'Customize',exact:true}).click();
  assert.equal(await field.inputValue(),'Saved description');
  assert.ok(listCalls >= 2,'Recipe list revalidates on revisit');
  assert.deepEqual(errors,[]);
  console.log('Recipe save and session cache checks passed');
} finally {await browser.close();}
