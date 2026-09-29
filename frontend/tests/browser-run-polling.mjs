import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5181';
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage();
  const errors = [], calls = {active:0,terminal:0};
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = body => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/executions') return respond({executions:[{id:'active',status:'running'},{id:'terminal',status:'success'}]});
    if (path === '/api/executions/active') {
      calls.active++;
      if (calls.active === 1) await new Promise(resolve => setTimeout(resolve,250));
      return respond({execution:{id:'active',status:'running',steps:[]}}).catch(() => {});
    }
    if (path === '/api/executions/terminal') {calls.terminal++; return respond({execution:{id:'terminal',status:'success',steps:[]}});}
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
  assert.deepEqual(errors,[]);
  console.log('Run selection ignores stale requests and terminal polling stops');
} finally {await browser.close();}
