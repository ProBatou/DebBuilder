import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5175';
const api = process.env.DEBBUILDER_DEV_API || 'http://127.0.0.1:8875';
async function expandField(page,path) {
  const group = page.locator(`[data-recipe-path="${path}"]`).locator('xpath=ancestor::details[1]');
  if (!await group.evaluate(node => node.open)) await group.locator('summary').click();
}
const browser = await chromium.launch({headless:true});
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    const errors = [], saves = [], mutations = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (request.method() !== 'GET' && new URL(request.url()).pathname.startsWith('/api/'))
        mutations.push(`${request.method()} ${new URL(request.url()).pathname}`);
      if (request.method() === 'POST' && new URL(request.url()).pathname.startsWith('/api/workflows/'))
        saves.push(JSON.parse(request.postData()));
    });
    await page.goto(`${base}/#/recipes/seerr`);
    await page.getByRole('heading',{name:'seerr'}).waitFor();
    await page.getByRole('button',{name:'Customize',exact:true}).click();
    await expandField(page,'package.description');
    const description = page.locator('[data-recipe-path="package.description"] textarea');
    const original = await description.inputValue();
    await description.fill(`${original} saved ${viewport.width}`);
    let releaseSave;
    const gate = new Promise(resolve => {releaseSave = resolve;});
    const delaySave = async route => {if (route.request().method() === 'POST') await gate; await route.continue();};
    await page.route('**/api/workflows/seerr',delaySave);
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.getByRole('button',{name:'Saving…'}).waitFor();
    assert.equal(await description.isDisabled(),true);
    assert.equal(await page.getByRole('button',{name:'Saving…'}).isDisabled(),true);
    releaseSave();
    await page.locator('.recipe-plan').waitFor();
    await page.unroute('**/api/workflows/seerr',delaySave);
    assert.match(saves.at(-1).expected_revision,/^[0-9a-f]{64}$/);
    assert.equal(saves.at(-1).workflow.package.description,`${original} saved ${viewport.width}`);
    const afterSave = await (await page.request.get(`${api}/api/workflows/seerr`)).json();
    assert.equal(afterSave.package.description,`${original} saved ${viewport.width}`);
    await page.getByRole('button',{name:'Customize',exact:true}).click();
    await expandField(page,'package.description');
    await description.fill(`${original} local ${viewport.width}`);
    const serverVersion = {...afterSave, active: !afterSave.active};
    const latestResponse = await page.request.get(`${api}/api/workflows/seerr`);
    const serverRevision = latestResponse.headers()['etag'].slice(1,-1);
    const serverSave = await page.request.post(`${api}/api/workflows/seerr`,{data:{workflow:serverVersion,expected_revision:serverRevision}});
    assert.equal(serverSave.status(),200);
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await page.getByRole('heading',{name:'Recipe conflict'}).waitFor();
    assert.equal(await description.inputValue(),`${original} local ${viewport.width}`);
    await page.getByRole('button',{name:'Keep editing'}).last().click();
    assert.equal(await description.inputValue(),`${original} local ${viewport.width}`);
    assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isDisabled(),true);
    await page.getByRole('alert').getByRole('button',{name:'Review changes'}).click();
    await page.getByRole('button',{name:'Discard my draft and reload'}).click();
    await page.locator('.recipe-plan').waitFor();
    await page.getByRole('button',{name:'Customize',exact:true}).click();
    await expandField(page,'package.description');
    await description.fill(`${original} rejected ${viewport.width}`);
    for (const [status,message] of [[401,'Sign in required'],[403,'Access denied'],[422,'Server rejected field'],[503,'Temporary server failure']]) {
      const failSave = async route => {
        if (route.request().method() === 'POST') await route.fulfill({status,contentType:'application/json',body:JSON.stringify({error:{code:status === 422 ? 'invalid_recipe' : status === 401 ? 'authentication_required' : status === 403 ? 'forbidden' : 'request_failed',message,path:'$.package.description'}})});
        else await route.continue();
      };
      await page.route('**/api/workflows/seerr',failSave);
      await page.getByRole('button',{name:'Save',exact:true}).click();
      await page.getByText(message,{exact:false}).first().waitFor();
      assert.equal(await description.inputValue(),`${original} rejected ${viewport.width}`);
      await page.unroute('**/api/workflows/seerr',failSave);
    }
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    await page.goto(`${base}/#/packages`);
    await page.getByRole('button',{name:'Add package'}).click();
    const newId = `lab-new-${viewport.width}-${Date.now()}`;
    await page.getByLabel('Package name').fill(newId);
    await page.getByLabel('GitHub repository').fill('example/new-recipe');
    await page.getByRole('button',{name:'Create package'}).click();
    await page.locator('.package-detail h2').getByText(newId).waitFor();
    assert.equal(saves.at(-1).create_only,true);
    assert.ok((await (await page.request.get(`${api}/api/workflows/${newId}`)).json()).name === newId);
    const saveCount = saves.length;
    if (viewport.width < 600) await page.locator('.package-detail-back').click();
    await page.getByRole('button',{name:'Add package'}).click();
    await page.getByLabel('Package name').fill(newId);
    await page.getByLabel('GitHub repository').fill('example/collision');
    await page.getByRole('button',{name:'Create package'}).click();
    await page.getByText('A package with this name already exists.').waitFor();
    assert.equal(saves.length,saveCount);
    await page.locator('.package-create-dialog').getByRole('button',{name:'Cancel'}).click();
    await page.goto(`${base}/#/system/managed`);
    await page.getByRole('heading',{name:'System-managed self-build'}).last().waitFor();
    assert.equal(await page.locator('[data-recipe-path="source.repository"]').count(),0);
    await page.locator('[data-recipe-path="active"] input').click();
    const managedSaveRequest = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/api/workflows/debbuilder'));
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await managedSaveRequest;
    assert.match(saves.at(-1).expected_revision,/^[0-9a-f]{64}$/);
    await page.locator('[data-recipe-path="active"] input').click();
    const localManaged = await page.locator('[data-recipe-path="active"] input').isChecked();
    const remoteManaged = await page.request.get(`${api}/api/workflows/debbuilder`);
    const remoteRevision = remoteManaged.headers()['etag'].slice(1,-1);
    const remoteRecipe = await remoteManaged.json();
    remoteRecipe.package.maintainer = `Remote Operator ${viewport.width} ${Date.now()} <remote@example.test>`;
    assert.equal((await page.request.post(`${api}/api/workflows/debbuilder`,{data:{workflow:remoteRecipe,expected_revision:remoteRevision}})).status(),200);
    const managedConflictLoad = page.waitForResponse(response => response.url().endsWith('/api/workflows/debbuilder') && response.request().method() === 'GET');
    await page.getByRole('button',{name:'Save',exact:true}).click();
    await managedConflictLoad;
    await page.getByRole('heading',{name:'Recipe conflict'}).waitFor();
    assert.equal(await page.locator('[data-recipe-path="active"] input').isChecked(),localManaged);
    assert.equal(await page.getByRole('button',{name:'Save',exact:true}).isDisabled(),true);
    await page.getByRole('button',{name:'Discard my draft and reload'}).click();
    assert.deepEqual(errors,[]);
    assert.ok(mutations.length > 0);
    assert.deepEqual(mutations.filter(route => !['POST /api/recipes/validate','POST /api/recipes/draft'].includes(route) && !route.startsWith('POST /api/workflows/')),[]);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    await page.close();
  }
  console.log('Recipe Save/Create/conflict/managed desktop/mobile checks passed');
} finally {await browser.close();}
