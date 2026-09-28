import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const expected = process.env.DEBBUILDER_EXPECT_INVENTORY;
if (!['empty','error'].includes(expected)) throw new Error('Set DEBBUILDER_EXPECT_INVENTORY=empty or error');
const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const mutations = [];
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/') && request.method() !== 'GET') mutations.push(request.url());});
  await page.goto(base);
  await page.getByRole('button',{name:'Packages',exact:true}).click();
  await page.getByRole('button',{name:'View repository inventory'}).click();
  if (expected === 'empty') {
    await page.getByRole('heading',{name:'Published entries'}).waitFor();
    assert.equal(await page.locator('.table-wrap tbody tr').count(),0);
    await page.getByText('No items').last().waitFor();
  } else {
    await page.getByText('Repository inventory is unavailable').waitFor();
    assert.equal(await page.locator('.table-wrap tbody tr').count(),0);
    await page.getByRole('button',{name:'Retry'}).click();
    await page.getByText('Repository inventory is unavailable').waitFor();
  }
  assert.deepEqual(mutations,[]);
  console.log(`Behavior Lab ${expected} inventory browser check passed`);
} finally {await browser.close();}
