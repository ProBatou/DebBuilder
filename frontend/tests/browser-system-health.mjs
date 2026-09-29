import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
const ids = [
  'application.runtime','settings.documents','repository.publication',
  'validation.oci','execution.admission','execution.containment',
  'application.mutations','automation.scheduler','automation.orchestrator',
];
const statuses = ['ok','ok','warning','unknown','ok','failed','ok','ok','ok'];
const checks = ids.map((id,index) => ({
  id,status:statuses[index],message:`Diagnostic message ${index + 1}`,
  details:id==='repository.publication' ? {signed_release_present:true,last_publication_at:'2026-09-28'} : {},
}));
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const errors = [], writes = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    if (request.method() !== 'GET') writes.push(`${request.method()} ${path}`);
    const body = path==='/api/auth/status' ? {ok:true,auth_mode:'none'}
      : path==='/api/status' ? {ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'}
      : path==='/api/system/diagnostics' ? {schema_version:1,status:'warning',checks}
      : path==='/api/storage' ? {storage:{state:'ready',bytes:{managed_total:0},runs:{count:0}}}
      : {error:{code:'unexpected_request',message:path}};
    return route.fulfill({status:body.error?404:200,contentType:'application/json',body:JSON.stringify(body)});
  });
  await page.goto(`${base}/#/system`);
  await page.locator('.system-check-card').first().waitFor();
  assert.equal(await page.locator('.system-check-card').count(),9);
  assert.equal(await page.locator('.system-managed-card').count(),0);
  assert.equal(await page.getByRole('button',{name:'System-managed self-build',exact:true}).count(),1);
  assert.equal(await page.getByText('More checks').count(),0);
  assert.equal(await page.locator('.system-extra').count(),0);
  const card = name => page.locator('.system-check-card').filter({has:page.getByRole('heading',{name,exact:true})});
  assert.ok(await card('Application runtime').locator('.status-chip.ready').count());
  assert.equal(await card('Application runtime').locator('.status-chip').textContent(),'●OK');
  assert.ok(await card('Repository publication').locator('.status-chip.warning').count());
  assert.ok(await card('Validation capability').locator('.status-chip.neutral').count());
  assert.ok(await card('Command containment').locator('.status-chip.danger').count());
  assert.equal(await card('Repository publication').getByText('Signed metadata: Available').count(),1);
  assert.equal(await page.locator('.system-health>.section-head .status-chip').count(),0);
  if (process.env.DEBBUILDER_SYSTEM_CAPTURE) {
    await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
    await page.screenshot({path:process.env.DEBBUILDER_SYSTEM_CAPTURE,fullPage:true});
  }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('.system-check-card').count(),9);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.deepEqual(writes,[]);
  assert.deepEqual(errors,[]);
  console.log('System health cards checks passed');
} finally {await browser.close();}
