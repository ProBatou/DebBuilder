import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
const recipe = JSON.parse(readFileSync(new URL('../../tests/fixtures/recipes/seerr.json',import.meta.url)));
const revision = 'a'.repeat(64);
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900},acceptDownloads:true});
  const validated = [], writes = [], pageErrors = [];
  let releaseRaceValidation, notifyRace;
  const raceRequested = new Promise(resolve => notifyRace = resolve);
  page.on('pageerror',error => pageErrors.push(error.message));
  page.on('requestfailed',request => pageErrors.push(`${request.url()}: ${request.failure()?.errorText}`));
  page.on('console',message => {if (message.type() === 'error') pageErrors.push(message.text());});
  page.on('request',request => {if (request.method() !== 'GET' && !request.url().includes('/api/recipes/validate')) writes.push(request.url());});
  await page.route('**/api/**',async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,headers={}) => route.fulfill({status:200,contentType:'application/json',headers,body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'local'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/recipes') return respond({recipes:[{id:recipe.name,package:recipe.package.name,repository:recipe.source.repository}]});
    if (path === '/api/workflows') return respond({workflows:[{id:recipe.name,writable:true}],errors:[]});
    if (path === `/api/workflows/${recipe.name}`) return respond(recipe,{ETag:`"${revision}"`});
    if (path === '/api/recipes/validate') {
      const draft = JSON.parse(route.request().postData()).recipe;
      validated.push(draft);
      if (draft.package.description === 'invalid-from-server') return route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({error:{code:'invalid_recipe',message:'Description rejected',path:'$.package.description'}})});
      if (draft.package.description === 'race-from-json') await new Promise(resolve => {releaseRaceValidation = resolve; notifyRace();});
      return respond({ok:true,recipe:draft,id:draft.name,collision:null});
    }
    return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:{code:'unexpected',message:path}})});
  });

  await page.goto(`${base}/#/recipes/seerr`);
  try {await page.getByRole('heading',{name:'seerr'}).waitFor({timeout:5000});}
  catch (error) {console.error(pageErrors,await page.content()); throw error;}
  await page.getByRole('button',{name:'Expert',exact:true}).click();
  await page.locator('.recipe-json summary').click();
  const exportButton = page.getByRole('button',{name:'Export JSON'});
  const firstDownload = page.waitForEvent('download');
  await exportButton.click();
  const initialFile = await firstDownload;
  assert.equal(initialFile.suggestedFilename(),'seerr.json');
  assert.deepEqual(JSON.parse(readFileSync(await initialFile.path(),'utf8')),recipe);

  await page.getByRole('button',{name:'Edit JSON'}).click();
  const textarea = page.getByRole('textbox',{name:'Recipe JSON'});
  assert.equal(await textarea.evaluate(node => node === document.activeElement),true);
  await textarea.fill('{invalid');
  assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isDisabled(),true);
  await page.getByRole('button',{name:'Apply to draft'}).click();
  await page.getByRole('alert').getByText(/Invalid JSON/).waitFor();

  await textarea.fill(JSON.stringify({...recipe,name:'another-recipe'}));
  await page.getByRole('button',{name:'Apply to draft'}).click();
  await page.getByRole('alert').getByText('The Recipe ID must match the selected Recipe.').waitFor();
  const rejected = JSON.stringify({...recipe,package:{...recipe.package,description:'invalid-from-server'}});
  await textarea.fill(rejected);
  await page.getByRole('button',{name:'Apply to draft'}).click();
  await page.getByRole('alert').getByText(/Description rejected/).waitFor();
  assert.equal(await textarea.inputValue(),rejected);
  const firstEdit = {...recipe,package:{...recipe.package,name:'seerr-updated',description:'Edited via JSON'}};
  await textarea.fill(JSON.stringify(firstEdit,null,2));
  await page.getByRole('button',{name:'Customize',exact:true}).click();
  await page.getByRole('button',{name:'Expert',exact:true}).click();
  assert.equal(await page.locator('.recipe-json').evaluate(node => node.open),true);
  assert.equal(await textarea.inputValue(),JSON.stringify(firstEdit,null,2));
  await page.getByRole('button',{name:'Apply to draft'}).click();
  await page.locator('.recipe-json .facts').waitFor();
  assert.equal(validated.at(-1).package.description,'Edited via JSON');
  assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isDisabled(),false);
  await page.getByRole('button',{name:'Customize',exact:true}).click();
  await page.locator('.recipe-task-group').filter({hasText:'Package metadata'}).first().locator('summary').click();
  assert.equal(await page.locator('[data-recipe-path="package.name"] input').inputValue(),'seerr-updated');
  await page.getByRole('button',{name:'Plan',exact:true}).click();
  assert.equal(await page.locator('.plan-rows').getByText('seerr-updated',{exact:true}).count(),1);
  await page.getByRole('button',{name:'Expert',exact:true}).click();
  await page.locator('.recipe-json summary').click();
  const secondDownload = page.waitForEvent('download');
  await exportButton.click();
  assert.equal(JSON.parse(readFileSync(await (await secondDownload).path(),'utf8')).package.description,'Edited via JSON');

  const imported = {...recipe,package:{...recipe.package,description:'Imported from file'}};
  await page.locator('.recipe-json input[type=file]').setInputFiles({name:'seerr.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(imported))});
  assert.equal(await textarea.inputValue(),JSON.stringify(imported));
  await page.getByRole('button',{name:'Apply to draft'}).click();
  await page.locator('.recipe-json .facts').waitFor();
  assert.equal(validated.at(-1).package.description,'Imported from file');

  await page.getByRole('button',{name:'Edit JSON'}).click();
  await textarea.fill(JSON.stringify({...imported,package:{...imported.package,description:'race-from-json'}}));
  await page.getByRole('button',{name:'Apply to draft'}).click();
  await raceRequested;
  await page.getByRole('button',{name:'Customize',exact:true}).click();
  await page.locator('.recipe-task-group').filter({hasText:'Package metadata'}).first().locator('summary').click();
  await page.locator('[data-recipe-path="package.name"] input').fill('seerr-local');
  releaseRaceValidation();
  await page.getByRole('button',{name:'Expert',exact:true}).click();
  await page.getByRole('alert').getByText(/draft changed since the JSON editor opened/i).waitFor();
  await page.getByRole('button',{name:'Customize',exact:true}).click();
  assert.equal(await page.locator('[data-recipe-path="package.name"] input').inputValue(),'seerr-local');
  await page.getByRole('button',{name:'Expert',exact:true}).click();
  await page.getByRole('button',{name:'Discard JSON edits'}).click();
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert.equal(await page.locator('.plan-rows').getByText('seerr',{exact:true}).count(),1);
  assert.equal(await page.locator('.recipe-savebar').count(),0);
  assert.deepEqual(writes,[]);
  assert.deepEqual(pageErrors.filter(message => !message.includes('422 (Unprocessable Entity)')),[]);
  console.log('Recipe JSON edit, import, export and draft checks passed');
} finally {await browser.close();}
