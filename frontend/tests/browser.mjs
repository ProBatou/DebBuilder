import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

// Start Behavior Lab on 8765 and Vite on 5174 before this isolated integration check.
const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({headless:true});
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    const failures=[];
    page.on('pageerror', error => failures.push(error.message));
    await page.goto(base);
    await page.getByRole('heading',{name:'Overview'}).waitFor();
    async function openNavigation(name) {
      if (viewport.width < 600) await page.getByRole('button',{name:'Menu'}).click();
      await page.getByRole('button',{name,exact:true}).first().click();
    }
    await openNavigation('Packages');
    await page.locator('.list .row').first().click();
    const packageId = await page.locator('.detail h2').textContent();
    assert.ok(packageId);
    assert.ok(page.url().includes(encodeURIComponent(packageId)));
    await openNavigation('Runs');
    await page.locator('.list .row').first().click();
    const runId = await page.locator('.detail code').first().textContent();
    assert.ok(runId);
    assert.ok(page.url().includes(encodeURIComponent(runId)));
    await page.getByText('Stages',{exact:true}).waitFor();
    await page.getByText('Logs',{exact:true}).waitFor();
    await page.getByRole('button',{name:'Options'}).click();
    await page.getByRole('button',{name:'Raw',exact:true}).click();
    await page.getByRole('button',{name:'Options'}).click();
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('button',{name:'Raw',exact:true}).count(),0);
    let runRequests = 0;
    page.on('request', request => {if (request.url().includes(`/api/executions/${encodeURIComponent(runId)}`)) runRequests++;});
    await openNavigation('System');
    await page.getByText('application.runtime').waitFor();
    const stoppedCount = runRequests;
    await page.waitForTimeout(1800);
    assert.equal(runRequests,stoppedCount);
    assert.ok(await page.locator('.grid .panel').count() >= 1);
    const theme = page.getByLabel('Theme');
    await theme.selectOption('dark');
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    await theme.selectOption('light');
    assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
    const language = page.locator('.preferences select').nth(1);
    for (const [code,label] of [['fr','Système'],['de','System'],['es','Sistema'],['en','System']]) {
      await language.selectOption(code);
      await page.getByRole('heading',{name:label,exact:true}).waitFor();
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
    assert.deepEqual(failures,[]);
    await page.close();
  }
  console.log('Behavior Lab desktop/mobile API browser checks passed');
} finally {await browser.close();}
