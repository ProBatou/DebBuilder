import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5181';
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const errors = [], calls = {active:0,terminal:0,list:0};
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = body => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/executions') {calls.list++; return respond({executions:[{id:'active',status:'running'},{id:'terminal',status:'success'}]});}
    if (path === '/api/dashboard') return respond({dashboard:{packages:0,ready_to_publish:0,package_rows:[],latest_operations:[]}});
    if (path === '/api/system/diagnostics') return respond({checks:[]});
    if (path === '/api/executions/active') {
      calls.active++;
      if (calls.active === 1) await new Promise(resolve => setTimeout(resolve,250));
      return respond({execution:{id:'active',status:'running',steps:[]}}).catch(() => {});
    }
    if (path === '/api/executions/terminal') {calls.terminal++; return respond({execution:{id:'terminal',status:'success',steps:[]}});}
    if (path === '/api/executions/archived') return respond({execution:{id:'archived',status:'success',steps:[]}});
    if (path === '/api/executions/missing') return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:{code:'build_run_not_found',message:'Build Run was not found'}})});
    if (path.endsWith('/logs')) return respond({log:{text:'',offset:0}});
    return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:{code:'unexpected_request',message:path}})});
  });

  await page.goto(`${base}/#/runs/active`);
  await page.waitForFunction(() => document.querySelector('.run-list'));
  await page.waitForTimeout(50);
  await page.goto(`${base}/#/runs/terminal`);
  await page.locator('.run-main .detail-breadcrumb strong').getByText('terminal').waitFor();
  await page.waitForTimeout(1800);
  assert.equal(await page.locator('.run-main .detail-breadcrumb strong').textContent(),'terminal');
  assert.equal(calls.terminal,1,'terminal Run must stop detail polling');
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Overview'}).click();
  await page.locator('.overview-grid').waitFor();
  const stoppedAt = calls.list;
  await page.waitForTimeout(5300);
  assert.equal(calls.list,stoppedAt,'leaving Runs must stop list polling');
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Runs'}).click();
  await page.locator('.run-list').waitFor();
  await page.waitForTimeout(5300);
  assert.ok(calls.list - stoppedAt <= 2,'returning to Runs must use one list poller');
  await page.goto(`${base}/#/runs/archived`);
  await page.locator('.run-main .detail-breadcrumb strong').getByText('archived').waitFor();
  await page.goto(`${base}/#/runs/missing`);
  await page.waitForURL('**/#/runs');
  assert.deepEqual(errors,[]);
  console.log('Run selection ignores stale requests and terminal polling stops');
} finally {await browser.close();}
