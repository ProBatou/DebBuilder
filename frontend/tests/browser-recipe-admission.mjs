import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5175';
const api = process.env.DEBBUILDER_DEV_API || 'http://127.0.0.1:8875';
const browser = await chromium.launch({headless:true});
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    page.on('dialog', dialog => dialog.accept());
    const pageErrors = [], mutations = [], validationBodies = [], runBodies = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() !== 'POST' || !pathname.startsWith('/api/')) return;
      mutations.push(pathname);
      if (pathname === '/api/recipes/validate') validationBodies.push(JSON.parse(request.postData()));
      if (pathname === '/api/run') runBodies.push(JSON.parse(request.postData()));
    });
    const routeRun = async route => route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({run_id:`run-${runBodies.length}-${viewport.width}`,status:'queued'})});
    await page.route('**/api/run',routeRun);
    for (const [action,dryRun] of [['Test',true],['Build',false]]) {
      await page.goto(`${base}/#/recipes/seerr`);
      await page.getByRole('heading',{name:'seerr'}).waitFor();
      const persisted = await page.request.get(`${api}/api/workflows/seerr`);
      const baseline = await persisted.json();
      const etag = persisted.headers()['etag'];
      await page.getByRole('button',{name:'Edit',exact:true}).click();
      const description = page.locator('[data-recipe-path="package.description"] textarea');
      const edited = `${baseline.package.description} ${action} unsaved ${viewport.width}`;
      await description.fill(edited);
      const beforeValidation = validationBodies.length, beforeRun = runBodies.length;
      await page.getByRole('button',{name:action,exact:true}).click();
      await page.getByText(`${action} queued`,{exact:true}).waitFor();
      assert.equal(validationBodies.length,beforeValidation+1);
      assert.equal(runBodies.length,beforeRun+1);
      assert.deepEqual(validationBodies.at(-1).recipe,runBodies.at(-1).workflow);
      assert.equal(runBodies.at(-1).workflow.package.description,edited);
      assert.equal(runBodies.at(-1).dry_run,dryRun);
      assert.equal(await description.inputValue(),edited);
      await page.getByText('Unsaved changes',{exact:true}).waitFor();
      const persistedAfter = await page.request.get(`${api}/api/workflows/seerr`);
      assert.equal(persistedAfter.headers()['etag'],etag);
      assert.deepEqual(await persistedAfter.json(),baseline);
      await page.getByRole('button',{name:'View Run'}).click();
      await page.getByRole('dialog').getByRole('button',{name:'Keep editing'}).click();
      assert.equal(await description.inputValue(),edited);
      assert.ok(page.url().endsWith('#/recipes/seerr'));
      await page.getByRole('button',{name:'View Run'}).click();
      await page.getByRole('dialog').getByRole('button',{name:'Discard draft'}).click();
      assert.ok(page.url().endsWith(`#/runs/run-${runBodies.length}-${viewport.width}`));
    }
    await page.goto(`${base}/#/recipes`);
    await page.getByLabel('Recipe ID').fill(`unsaved-${viewport.width}`);
    await page.getByLabel('GitHub repository').fill('example/unsaved');
    await page.getByRole('button',{name:'Continue'}).click();
    await page.getByText('Save the Recipe once before running it.').waitFor();
    assert.equal(await page.getByRole('button',{name:'Test',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('button',{name:'Build',exact:true}).isDisabled(),true);
    await page.evaluate(() => {location.hash = '/recipes/seerr';});
    await page.getByRole('dialog').getByRole('button',{name:'Discard draft'}).click();
    await page.getByRole('heading',{name:'seerr'}).waitFor();
    await page.getByRole('button',{name:'Edit',exact:true}).click();
    await page.locator('[data-recipe-path="active"] input').uncheck();
    await page.getByText('Enable this Recipe before running it.').waitFor();
    assert.equal(await page.getByRole('button',{name:'Test',exact:true}).isDisabled(),true);
    assert.equal(await page.getByRole('button',{name:'Build',exact:true}).isDisabled(),true);
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    let releaseValidation;
    const gate = new Promise(resolve => releaseValidation = resolve);
    const delayValidation = async route => {await gate; await route.continue();};
    await page.route('**/api/recipes/validate',delayValidation);
    const beforeValidation = validationBodies.length, beforeRun = runBodies.length;
    await page.evaluate(() => {
      document.querySelector('.recipe-run-actions button:first-child').click();
      document.querySelector('.recipe-run-actions button:first-child').click();
      document.querySelector('.recipe-run-actions button:nth-child(2)').click();
    });
    await page.waitForFunction(() => document.querySelector('.recipe-run-actions button:nth-child(2)')?.disabled === true);
    assert.equal(await page.getByRole('button',{name:'Build',exact:true}).isDisabled(),true);
    releaseValidation();
    await page.getByText('Test queued',{exact:true}).waitFor();
    assert.equal(validationBodies.length,beforeValidation+1);
    assert.equal(runBodies.length,beforeRun+1);
    await page.unroute('**/api/recipes/validate',delayValidation);
    let releaseBuildValidation;
    const buildGate = new Promise(resolve => releaseBuildValidation = resolve);
    const delayBuildValidation = async route => {await buildGate; await route.continue();};
    await page.route('**/api/recipes/validate',delayBuildValidation);
    const beforeBuildRace = runBodies.length;
    await page.evaluate(() => {
      document.querySelector('.recipe-run-actions button:nth-child(2)').click();
      document.querySelector('.recipe-run-actions button:first-child').click();
    });
    releaseBuildValidation();
    await page.getByText('Build queued',{exact:true}).waitFor();
    assert.equal(runBodies.length,beforeBuildRace+1);
    assert.equal(runBodies.at(-1).dry_run,false);
    await page.unroute('**/api/recipes/validate',delayBuildValidation);
    await page.unroute('**/api/run',routeRun);
    for (const [status,code,message] of [
      [401,'authentication_required','Sign in required'],
      [403,'forbidden','Access denied'],
      [409,'recipe_disabled','Recipe is disabled'],
      [422,'release_asset_not_found','Release asset was not found'],
      [429,'execution_queue_full','The Build/Test queue is full; try again later'],
      [503,'execution_manager_unavailable','Execution manager is unavailable'],
      [503,'github_unavailable','GitHub is unavailable'],
    ]) {
      const rejectRun = async route => route.fulfill({status,contentType:'application/json',body:JSON.stringify({error:{code,message}})});
      await page.route('**/api/run',rejectRun);
      const before = runBodies.length;
      await page.getByRole('button',{name:'Build',exact:true}).click();
      await page.getByText(message,{exact:true}).first().waitFor();
      assert.equal(runBodies.length,before+1);
      await page.unroute('**/api/run',rejectRun);
    }
    const rejectValidation = async route => route.fulfill({status:422,contentType:'application/json',body:JSON.stringify({error:{code:'invalid_recipe',message:'Invalid resource limit',path:'$.resource_limits.memory_max_bytes'}})});
    await page.route('**/api/recipes/validate',rejectValidation);
    const beforeInvalid = runBodies.length;
    await page.getByRole('button',{name:'Test',exact:true}).click();
    await page.getByText('Invalid resource limit',{exact:true}).first().waitFor();
    assert.equal(runBodies.length,beforeInvalid);
    await page.unroute('**/api/recipes/validate',rejectValidation);
    const disconnected = route => route.abort('failed');
    await page.route('**/api/run',disconnected);
    const beforeNetwork = runBodies.length;
    await page.getByRole('button',{name:'Test',exact:true}).click();
    await page.getByText('The admission result is unknown. Check Runs before trying again.').waitFor();
    await page.waitForTimeout(700);
    assert.equal(runBodies.length,beforeNetwork+1);
    await page.unroute('**/api/run',disconnected);
    await page.route('**/api/run',routeRun);
    await page.goto(`${base}/#/system/managed`);
    await page.getByRole('heading',{name:'debbuilder'}).waitFor();
    await page.getByRole('button',{name:'Edit',exact:true}).click();
    await page.getByRole('button',{name:'Advanced configuration',exact:true}).click();
    const maintainer = page.locator('[data-recipe-path="package.maintainer"] input');
    const managedValue = `Managed Operator ${viewport.width} <operator@example.test>`;
    await maintainer.fill(managedValue);
    await page.getByRole('button',{name:'Test',exact:true}).click();
    await page.getByText('Test queued',{exact:true}).waitFor();
    assert.equal(runBodies.at(-1).workflow.management.owner,'application');
    assert.equal(runBodies.at(-1).workflow.package.maintainer,managedValue);
    assert.equal(await maintainer.inputValue(),managedValue);
    for (const theme of ['dark','light','system']) {
      await page.getByLabel('Theme').selectOption(theme);
      const applied = await page.locator('html').getAttribute('data-theme');
      assert.ok(theme === 'system' ? ['dark','light'].includes(applied) : applied === theme);
    }
    const language = page.locator('.preferences select').nth(1);
    for (const [code,label] of [['fr','Tester'],['de','Testen'],['es','Probar'],['en','Test']]) {
      await language.selectOption(code);
      await page.getByRole('button',{name:label,exact:true}).waitFor();
    }
    assert.equal(mutations.some(path => path.startsWith('/api/workflows/') || ['/api/recipes/import','/api/executions/validate','/api/executions/publish'].includes(path)),false);
    assert.deepEqual(pageErrors,[]);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    await page.close();
  }
  console.log('Recipe Test/Build draft, navigation, disabled, duplicate, and failure browser checks passed');
} finally {await browser.close();}
