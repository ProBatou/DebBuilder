import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
let recoveryBlocked = false;
let historyCount = 3;
let failMutation = false;
let storagePending = false;
let storageState = 'ready';
let cleanupProjection = {
  entries: [
    {run_id:'retention-run',reason:'retention',cleaned_at:'2026-09-08T12:00:00Z',removed:['source','staging']},
    {run_id:'pressure-run',reason:'storage_pressure',cleaned_at:'2026-09-07T12:00:00Z',removed:['downloads']},
    {run_id:'terminal-run',reason:'terminal_run',cleaned_at:'2026-09-06T12:00:00Z',removed:['toolchain']},
  ],
  total_marked:5,
  omitted:2,
};
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
    if (path === '/api/dashboard') return respond({dashboard:{packages:0,ready_to_publish:0,package_rows:[],latest_operations:historyCount === 3 ? [{id:'finished-1',package:'old',status:'failed',updated:'2026-09-28T12:00:00Z'}] : []}});
    if (path === '/api/executions') return respond({executions:historyCount === 3 ? [{id:'finished-1',package:'old',status:'failed',steps:[]}] : []});
    if (path === '/api/executions/finished-1') return respond({execution:{id:'finished-1',package:'old',status:'failed',steps:[]}});
    if (path === '/api/executions/finished-1/logs') return respond({log:{text:'',offset:0}});
    if (path === '/api/storage') return respond({storage:storagePending ? {state:'collecting',measured_at:'2026-09-28T12:00:00Z',bytes:{managed_total:null,repository:null},runs:{count:null,artifact_bytes:null},recent_workspace_cleanups:cleanupProjection} : {state:storageState,measured_at:historyCount === 1 ? '2026-09-28T12:01:00Z' : '2026-09-28T12:00:00Z',bytes:{managed_total:historyCount === 1 ? 124501 : 128597,repository:65536},categories:{disposable:8192},runs:{count:historyCount,artifact_bytes:historyCount === 1 ? 0 : 4096},recent_workspace_cleanups:cleanupProjection,retention_policy:{enabled:true,failed_workspaces_to_retain:5,periodic_destructive_cleanup:true}}});
    if (path === '/api/executions/delete-logs') {
      const body = request.postDataJSON();
      writes.push(body);
      if (body.dry_run === true) return respond({count:historyCount?2:0,ids:historyCount?['finished-1','finished-2']:[]});
      if (failMutation) return respond({error:{code:'history_changed',message:'History changed'}},409);
      assert.deepEqual(body,{ids:['finished-1','finished-2']});
      historyCount = 1;
      storagePending = true;
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
  assert.equal(await page.locator('.maintenance-card').count(),4);
  const cleanupPanel = page.getByTestId('workspace-cleanups');
  await cleanupPanel.getByText('retention-run').waitFor();
  assert.equal(await cleanupPanel.getByText('Retention policy cleanup').count(),1);
  assert.equal(await cleanupPanel.getByText('Storage pressure relief').count(),1);
  assert.equal(await cleanupPanel.getByText('Temporary Run toolchain disposal').count(),1);
  assert.equal(await cleanupPanel.getByText(/Sep.*2026|2026/).count() > 0,true);
  assert.equal(await cleanupPanel.getByText('source, staging').count(),1);
  assert.equal(await cleanupPanel.getByText(/additional valid markers omitted: 2/).count(),1);
  assert.equal(await page.locator('dialog:visible').count(),0);
  assert.deepEqual(writes,[]);
  assert.equal(await page.getByText('Available in a later migration').count(),0);
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Overview'}).click();
  await page.getByText('finished-1').waitFor();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Runs'}).click();
  await page.locator('.run-list').getByText('finished-1').first().waitFor();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'System'}).click();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
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
  await page.getByText('Refreshing storage…').first().waitFor();
  assert.equal(await page.getByText('3',{exact:true}).count(),0);
  storagePending = false;
  assert.deepEqual(writes.slice(1),[{all:true,dry_run:true},{ids:['finished-1','finished-2']}]);
  await dialog.getByRole('button',{name:'Close'}).last().click();
  await page.getByText('1',{exact:true}).waitFor();
  assert.equal(await page.getByText('121.6 KiB').count(),1);
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Overview'}).click();
  await page.getByText('No items').first().waitFor();
  assert.equal(await page.getByText('finished-1').count(),0);
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Runs'}).click();
  await page.locator('.run-list').waitFor();
  await page.waitForFunction(() => !document.querySelector('.run-list')?.textContent?.includes('finished-1'));
  assert.equal(await page.getByText('finished-1').count(),0);
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'System'}).click();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
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

  recoveryBlocked = false;
  cleanupProjection = {entries:[],total_marked:0,omitted:0};
  storageState = 'partial';
  await page.reload();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByTestId('workspace-cleanups-unavailable').waitFor();
  assert.equal(await page.getByTestId('workspace-cleanups-empty').count(),0);
  storageState = 'stale';
  await page.reload();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByTestId('workspace-cleanups-unavailable').waitFor();

  storageState = 'error';
  await page.reload();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByTestId('workspace-cleanups-unavailable').waitFor();

  storageState = 'ready';
  await page.reload();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByTestId('workspace-cleanups-empty').waitFor();

  cleanupProjection = {entries:[{run_id:'future-run',reason:'future_reason',cleaned_at:'invalid',removed:['source']}],total_marked:1,omitted:0};
  await page.reload();
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();
  await page.getByText('Unknown cleanup reason').waitFor();
  assert.equal(await page.getByText('Unknown date').count(),1);
  assert.deepEqual(errors,[]);
  assert.equal(await page.locator('dialog:visible').count(),0);

  cleanupProjection = {entries:[{run_id:'translated-run',reason:'retention',cleaned_at:'2026-09-08T12:00:00Z',removed:['source']}],total_marked:1,omitted:0};
  for (const [language,label] of [
    ['fr','Nettoyage selon la politique de rétention'],
    ['de','Bereinigung gemäß Aufbewahrungsrichtlinie'],
    ['es','Limpieza según la política de retención'],
    ['en','Retention policy cleanup'],
  ]) {
    await page.evaluate(value => localStorage.setItem('debBuilder24Locale',value),language);
    await page.reload();
    await page.locator('.system-tabs button').nth(1).click();
    await page.getByText(label).waitFor();
  }
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.equal(writes.length,6);
  assert.deepEqual(errors,[]);
  console.log('System maintenance checks passed');
} finally {await browser.close();}
