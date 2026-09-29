import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

// Start Behavior Lab on 8765 and Vite on 5174 before this isolated integration check.
const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({headless:true});
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    const failures=[];
    const mutations=[], reads=[];
    page.on('pageerror', error => failures.push(error.message));
    page.on('request', request => {const path = new URL(request.url()).pathname; if (!path.startsWith('/api/')) return; if (request.method() === 'GET') reads.push(path); else mutations.push(`${request.method()} ${path}`);});
    await page.goto(base);
    await page.getByRole('heading',{name:'Overview'}).waitFor();
    async function openNavigation(name) {
      if (viewport.width < 600) await page.getByRole('button',{name:'Menu'}).click();
      await page.getByRole('button',{name,exact:true}).first().click();
    }
    await openNavigation('Packages');
    await page.locator('.package-rows .package-row').filter({hasText:'bashrc'}).first().click();
    const packageId = await page.locator('.package-detail h2').textContent();
    assert.ok(packageId);
    assert.ok(page.url().includes(encodeURIComponent(packageId)));
    if (viewport.width < 600) await page.getByRole('button',{name:/Back/}).first().click();
    await page.getByRole('button',{name:/View repository inventory/}).click();
    await page.getByRole('heading',{name:'Published entries'}).waitFor();
    assert.ok(await page.locator('.repo-packages>div').count() >= 3);
    await page.getByRole('button',{name:/Back/}).first().click();
    if (viewport.width < 600) await page.locator('.package-rows .package-row').filter({hasText:packageId}).first().click();
    const packageRecipeId = await page.evaluate(async name => (await (await fetch(`/api/packages/${encodeURIComponent(name)}`)).json()).package.recipe, packageId);
    await page.getByRole('button',{name:'Review Recipe'}).click();
    await page.getByText('No matching current evidence').waitFor();
    assert.ok(page.url().endsWith(`#/recipes/${encodeURIComponent(packageRecipeId)}`));
    await openNavigation('Recipes');
    await page.locator('.recipe-picker .selection-list button').first().click();
    await page.getByRole('button',{name:'Expert',exact:true}).click();
    await page.getByText('View canonical JSON').waitFor();
    await page.getByRole('button',{name:'Plan',exact:true}).click();
    assert.equal(await page.locator('.recipe-savebar').count(),0);
    await page.getByText('Lifecycle hooks').first().waitFor();
    await page.goto(`${base}/#/recipes/archive-agent`);
    await page.getByText('Prebuilt artifact').first().waitFor();
    assert.equal(await page.getByText('Existing ELF overrides').count(),0);
    await page.getByRole('button',{name:'Advanced',exact:true}).click();
    assert.equal(await page.locator('[data-recipe-path="package.runtime_dependency_detection.enabled"]').count(),1);
    assert.equal(await page.getByText('build commands').count(),0);
    await page.goto(`${base}/#/recipes/vendor-cli`);
    await page.getByText('Prebuilt artifact').first().waitFor();
    await page.getByText('Service not configured').waitFor();
    await page.getByRole('button',{name:'Advanced',exact:true}).click();
    assert.equal(await page.locator('[data-recipe-path="package.runtime_dependency_detection.enabled"]').count(),0);
    await page.goto(`${base}/#/recipes/seerr`);
    await page.getByText('Source build').waitFor();
    await page.getByText('postinst configured').waitFor();
    await openNavigation('Packages');
    await page.locator('.package-rows .package-row').filter({hasText:'debbuilder'}).first().click();
    await page.goto(`${base}/#/recipes/debbuilder`);
    await page.getByText('GitHub source',{exact:true}).waitFor();
    assert.ok(page.url().includes('#/system/managed'));
    await openNavigation('Runs');
    await page.locator('.run-selection .run-list-row').first().click();
    const runId = await page.locator('.detail code').first().textContent();
    assert.ok(runId);
    assert.ok(page.url().includes(encodeURIComponent(runId)));
    await page.getByText('Stages',{exact:true}).waitFor();
    await page.getByText('Logs',{exact:true}).waitFor();
    if (await page.getByRole('button',{name:'Review Recipe'}).count()) {
      const runRecipeId = await page.evaluate(async value => (await (await fetch(`/api/executions/${encodeURIComponent(value)}`)).json()).execution.recipe_id, runId);
      await page.getByRole('button',{name:'Review Recipe'}).click();
      await page.getByText('No matching current evidence').waitFor();
      assert.ok(page.url().endsWith(`#/recipes/${encodeURIComponent(runRecipeId)}`));
      await openNavigation('Runs');
      await page.locator('.run-selection .run-list-row').first().click();
    }
    await page.getByRole('combobox',{name:'Log detail'}).selectOption('raw');
    assert.equal(await page.locator('.log-options').count(),0);
    let runRequests = 0;
    page.on('request', request => {if (request.url().includes(`/api/executions/${encodeURIComponent(runId)}`)) runRequests++;});
    await openNavigation('System');
    await page.getByText('Application runtime',{exact:true}).waitFor();
    const stoppedCount = runRequests;
    await page.waitForTimeout(1800);
    assert.equal(runRequests,stoppedCount);
    assert.ok(await page.locator('.system-grid .panel').count() >= 1);
    await page.getByRole('button',{name:'System-managed self-build',exact:true}).click();
    await page.getByText('GitHub source',{exact:true}).waitFor();
    await page.getByText('Definition version',{exact:true}).waitFor();
    await openNavigation('Settings');
    const theme = page.getByLabel('Theme');
    await theme.selectOption('dark');
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await theme.selectOption('light');
    assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
    const language = page.locator('.settings-content .field-grid select').nth(1);
    for (const [code,label] of [['fr','Paramètres'],['de','Einstellungen'],['es','Ajustes'],['en','Settings']]) {
      await language.selectOption(code);
      await page.getByRole('heading',{name:label,exact:true}).waitFor();
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.deepEqual(failures,[]);
    assert.deepEqual(mutations,[]);
    const allowedReads = /^\/api\/(?:auth\/status|status|dashboard|packages(?:\/[^/]+)?|repository\/inventory|recipes(?:\/[^/]+\/(?:inspect|automation))?|workflows(?:\/[^/]+)?|executions(?:\/[^/]+(?:\/logs)?)?|system\/diagnostics|storage|settings)$/;
    assert.deepEqual(reads.filter(path => !allowedReads.test(path)),[]);
    await page.close();
  }
  console.log('Behavior Lab desktop/mobile API browser checks passed');
} finally {await browser.close();}
