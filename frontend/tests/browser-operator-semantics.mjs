import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({headless:true});
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    const writes = [], logUrls = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      const url = new URL(request.url());
      if (!url.pathname.startsWith('/api/')) return;
      if (request.method() !== 'GET') writes.push(`${request.method()} ${url.pathname}`);
      if (url.pathname.endsWith('/logs')) logUrls.push(url.toString());
    });
    await page.goto(`${base}/#/overview`);
    await page.locator('.overview-grid').waitFor();
    assert.equal(await page.getByRole('button',{name:/New Recipe/}).count(),0);
    assert.equal(await page.locator('.overview-main .row-status.warning').first().textContent(),'●');
    await page.goto(`${base}/#/recipes/seerr`);
    await page.locator('.recipe-plan').waitFor();
    assert.equal(await page.getByRole('button',{name:/New Recipe/}).count(),0);
    assert.equal(await page.getByText('Existing ELF overrides').count(),0);
    assert.equal(await page.locator('.recipe-advanced,.recipe-json,.recipe-secondary .structured').count(),0);
    assert.equal(await page.locator('.recipe-inspection,.recipe-automation').count(),0);
    await page.getByRole('button',{name:'Customize',exact:true}).click();
    assert.equal(await page.locator('.editor-group .recipe-task-group[open]').count(),0);
    await page.getByRole('button',{name:'Advanced',exact:true}).click();
    assert.ok(await page.locator('.editor-group .recipe-task-group:not([open])').count() >= 2);
    await page.getByRole('button',{name:'Expert',exact:true}).click();
    assert.equal(await page.locator('.recipe-json').evaluate(node => node.open),false);
    assert.equal(await page.locator('.recipe-inspection,.recipe-automation').count(),0);
    const add = page.locator('.recipe-add').first();
    await add.locator('xpath=ancestor::details[1]').locator('summary').click();
    await add.waitFor();
    assert.ok((await add.boundingBox()).width < 400);
    await page.goto(`${base}/#/runs/ui-04-build-failed`);
    await page.locator('.run-main').waitFor();
    await page.locator('.run-diagnosis').waitFor();
    assert.equal(await page.locator('.run-technical .structured').count(),0);
    assert.equal(await page.locator('.log-options').count(),0);
    const rawLogs = page.waitForResponse(response => response.url().includes('/api/executions/ui-04-build-failed/logs?verbosity=raw'));
    await page.getByRole('combobox',{name:'Log detail'}).selectOption('raw');
    await rawLogs;
    assert.ok(logUrls.some(url => url.includes('verbosity=raw') && url.includes('after=0')));
    await page.goto(`${base}/#/system`);
    await page.locator('.system-grid').waitFor();
    assert.equal(await page.locator('.system-grid .structured,.health-repository .structured').count(),0);
    await page.getByRole('button',{name:'Developer',exact:true}).click();
    await page.locator('.developer-payload').waitFor();
    assert.equal(await page.locator('.developer-payload').evaluate(node => node.open),false);
    await page.goto(`${base}/#/settings`);
    await page.locator('.settings-content .panel').first().waitFor();
    await page.getByLabel('Theme').selectOption('dark');
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await page.getByLabel('Theme').selectOption('light');
    await page.getByRole('button',{name:'Repository',exact:true}).click();
    assert.ok(await page.locator('.settings-edit-fields label').count() >= 4);
    assert.equal(await page.locator('.settings-content input[readonly],.settings-content input[disabled],.settings-content select[disabled]').count(),0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.deepEqual(writes,[]);
    assert.deepEqual(errors,[]);
    await page.close();
  }
  console.log('Operator semantics desktop/mobile checks passed');
} finally {await browser.close();}
