import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5175';
const api = process.env.DEBBUILDER_DEV_API || 'http://127.0.0.1:8875';
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const admissions = [];
  page.on('response',async response => {
    if (new URL(response.url()).pathname === '/api/run' && response.request().method() === 'POST' && response.status() === 202)
      admissions.push(await response.json());
  });
  for (const [action,mode,terminal] of [['Test','dry_run','prepared'],['Build','build','success']]) {
    await page.goto(`${base}/#/recipes/seerr`);
    await page.getByRole('heading',{name:'seerr'}).waitFor();
    await page.getByRole('button',{name:'Customize',exact:true}).click();
    const group = page.locator('[data-recipe-path="package.description"]').locator('xpath=ancestor::details[1]');
    if (!await group.evaluate(node => node.open)) await group.locator('summary').click();
    const description = page.locator('[data-recipe-path="package.description"] textarea');
    const local = `${await description.inputValue()} ${action} real admission`;
    await description.fill(local);
    await page.getByRole('button',{name:action,exact:true}).click();
    await page.getByText(`${action} queued`,{exact:true}).waitFor();
    const id = admissions.at(-1)?.run_id;
    assert.ok(id);
    await page.getByRole('button',{name:'View Run'}).click();
    await page.getByRole('dialog').getByRole('button',{name:'Discard draft'}).click();
    assert.ok(page.url().endsWith(`#/runs/${encodeURIComponent(id)}`));
    await page.getByText('Stages',{exact:true}).waitFor();
    await page.waitForFunction(async ({id,terminal}) => {
      const response = await fetch(`/api/executions/${encodeURIComponent(id)}`);
      return (await response.json()).execution?.status === terminal;
    },{id,terminal});
    const run = (await (await page.request.get(`${api}/api/executions/${id}`)).json()).execution;
    assert.equal(run.mode,mode);
    assert.equal(run.status,terminal);
    assert.equal(run.recipe_id,'seerr');
  }
  await page.close();
  console.log('Real Behavior Lab Test prepared and Build success admission journeys passed');
} finally {await browser.close();}
