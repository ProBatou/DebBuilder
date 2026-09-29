import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
let recoveryBlocked = false;
let historyCount = 3;
let failMutation = false;
const writes = [];
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'});
    if (path === '/api/system/diagnostics') return respond({schema_version:1,status:recoveryBlocked?'warning':'ok',checks:[{id:'execution.admission',status:recoveryBlocked?'warning':'ok',message:recoveryBlocked?'Build/Test admission is closed':'Build/Test admission is open',details:{open:!recoveryBlocked,recovery_blocked:recoveryBlocked}}]});
    if (path === '/api/storage') return respond({storage:{state:'ready',measured_at:'2026-09-28T12:00:00Z',bytes:{managed_total:128597,repository:65536},categories:{disposable:8192},runs:{count:historyCount,artifact_bytes:4096},retention_policy:{enabled:true,failed_workspaces_to_retain:5,periodic_destructive_cleanup:true}}});
    if (path === '/api/executions/delete-logs') {
      const body = request.postDataJSON();
      writes.push(body);
      if (body.dry_run === true) return respond({count:historyCount?2:0,ids:historyCount?['finished-1','finished-2']:[]});
      if (failMutation) return respond({error:{code:'history_changed',message:'History changed'}},409);
      assert.deepEqual(body,{ids:['finished-1','finished-2']});
      historyCount = 1;
      return respond({deleted:['finished-1','finished-2'].map(id => ({id,history_deleted:true,visible:false})),errors:[]});
    }
    return respond({error:{code:'unexpected_request',message:path}},404);
  });
  await page.goto(`${base}/#/system`);
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByRole('heading',{name:'Storage'}).waitFor();
  assert.equal(await page.getByText('3',{exact:true}).count(),1);
  assert.equal(await page.getByText('125.6 KiB').count(),1);
  assert.equal(await page.getByText('Automatic workspace cleanup').count(),1);
  assert.equal(await page.getByText('No recovery block reported for new work.').count(),1);
  assert.equal(await page.locator('.maintenance-card').count(),3);
  assert.equal(await page.getByText('Available in a later migration').count(),0);
  if (process.env.DEBBUILDER_MAINTENANCE_CAPTURE) await page.screenshot({path:process.env.DEBBUILDER_MAINTENANCE_CAPTURE,fullPage:true});

  await page.getByRole('button',{name:'Clear execution history'}).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('Completed Runs selected for removal').waitFor();
  assert.deepEqual(writes,[{all:true,dry_run:true}]);
  await dialog.getByRole('button',{name:'Cancel'}).click();
  await dialog.waitFor({state:'hidden'});
  assert.equal(writes.length,1);

  await page.getByRole('button',{name:'Clear execution history'}).click();
  await dialog.getByRole('button',{name:'Confirm removal'}).click();
  await dialog.getByText('Execution history cleared.').waitFor();
  assert.deepEqual(writes.slice(1),[{all:true,dry_run:true},{ids:['finished-1','finished-2']}]);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  assert.equal(await page.getByText('1',{exact:true}).count(),1);
  historyCount = 0;
  await page.getByRole('button',{name:'Clear execution history'}).click();
  await page.locator('.maintenance-card').getByText('No completed execution history to clear.').waitFor();
  assert.equal(await dialog.isVisible(),false);

  historyCount = 2;
  failMutation = true;
  await page.getByRole('button',{name:'Clear execution history'}).click();
  await dialog.getByRole('button',{name:'Confirm removal'}).click();
  await dialog.getByText('History changed').waitFor();
  assert.equal(await dialog.getByRole('button',{name:'Confirm removal'}).count(),0);
  await dialog.getByRole('button',{name:'Cancel'}).click();

  recoveryBlocked = true;
  await page.reload();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByText('Recovery is blocking new Build and Test work.').waitFor();
  assert.equal(await page.locator('.maintenance-card .status-chip.warning').count(),1);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('System maintenance checks passed');
} finally {await browser.close();}
