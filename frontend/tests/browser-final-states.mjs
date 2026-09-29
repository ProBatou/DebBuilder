import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5181';
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:390,height:844}});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  let diagnostics = 'unavailable';
  let storageAvailable = false;
  await page.route('**/api/**',route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/dashboard') return respond({dashboard:{packages:0,ready_to_publish:0,package_rows:[],latest_operations:[]}});
    if (path === '/api/system/diagnostics') return diagnostics === 'empty'
      ? respond({checks:[]}) : respond({error:{code:'diagnostics_unavailable',message:'Diagnostics unavailable'}},503);
    if (path === '/api/storage') return storageAvailable
      ? respond({storage:{state:'ready',runs:{count:0},bytes:{managed_total:0}}})
      : respond({error:{code:'storage_unavailable',message:'Storage unavailable'}},503);
    if (path === '/api/settings') return respond({error:{code:'settings_unavailable',message:'Settings unavailable'}},503);
    return respond({error:{code:'unexpected_request',message:path}},404);
  });

  await page.goto(`${base}/#/system`);
  await page.getByText('Diagnostics unavailable').waitFor();
  assert.equal(await page.locator('.system-check-grid').getByText('Loading…').count(),0);
  await page.getByRole('button',{name:'Maintenance'}).click();
  assert.equal(await page.locator('.maintenance-card').getByText('Loading…').count(),0);

  diagnostics = 'empty'; storageAvailable = true;
  await page.getByRole('button',{name:'Health'}).click();
  await page.getByRole('button',{name:'Retry'}).click();
  await page.locator('.system-check-grid').getByText('No items').waitFor();

  diagnostics = 'unavailable';
  await page.goto(`${base}/#/overview`);
  await page.locator('.overview-grid').waitFor();
  await page.getByText('Diagnostics unavailable').waitFor();

  await page.goto(`${base}/#/settings`);
  await page.getByText('Settings unavailable').waitFor();
  await page.getByLabel('Theme').selectOption('dark');
  assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  await page.getByLabel('Language').selectOption('fr');
  await page.getByRole('heading',{name:'Paramètres',exact:true}).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('System and Settings failure/empty states passed');
} finally {await browser.close();}
