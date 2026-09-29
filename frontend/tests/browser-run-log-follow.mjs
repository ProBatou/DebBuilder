import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5181';
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1000,height:500}});
  const errors = [], requests = [];
  const runs = {
    active:{id:'active',package:'example',status:'running',lifecycle_status:'building',steps:[]},
    other:{id:'other',package:'other',status:'running',lifecycle_status:'building',steps:[]},
  };
  const lines = prefix => Array.from({length:100},(_,index) => `${prefix} line ${index}\n`).join('');
  const content = {active:{normal:lines('NORMAL'),raw:lines('RAW'),compact:Array.from({length:35},(_,index) => `COMPACT line ${index}\n`).join('')},other:{normal:lines('OTHER')}};
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = body => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/executions') return respond({executions:Object.values(runs)});
    const match = path.match(/^\/api\/executions\/(active|other)(\/logs)?$/);
    if (match && !match[2]) return respond({execution:runs[match[1]]});
    if (match && match[2]) {
      const [runId] = match.slice(1);
      const level = url.searchParams.get('verbosity');
      const after = Number(url.searchParams.get('after'));
      requests.push({runId,level,after});
      const value = content[runId][level] || content[runId].normal;
      return respond({log:{text:value.slice(after),offset:value.length}});
    }
    return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:{code:'unexpected_request',message:path}})});
  });
  await page.goto(`${base}/#/runs/active`);
  const terminal = page.locator('.run-logs .log-output');
  const waitLog = value => page.waitForFunction(text => document.querySelector('.run-logs .log-output')?.textContent?.includes(text),value);
  const live = page.getByLabel('Live log following');
  const jump = page.getByRole('button',{name:'Jump to latest log output'});
  await waitLog('NORMAL line 99');
  await live.waitFor();
  const atBottom = () => terminal.evaluate(element => element.scrollHeight-element.clientHeight-element.scrollTop <= 24);
  assert.equal(await atBottom(),true);
  await page.evaluate(() => window.scrollTo(0,120));
  const pageY = await page.evaluate(() => window.scrollY);
  content.active.normal += 'NORMAL newest\n';
  await waitLog('NORMAL newest');
  assert.equal(await atBottom(),true);
  assert.equal(await page.evaluate(() => window.scrollY),pageY);
  await terminal.evaluate(element => {element.scrollTop=0;});
  await jump.waitFor();
  assert.equal(await live.count(),0);
  content.active.normal += 'NORMAL while detached\n';
  await waitLog('NORMAL while detached');
  assert.equal(await terminal.evaluate(element => element.scrollTop),0);
  await jump.click();
  await live.waitFor();
  assert.equal(await atBottom(),true);
  const verbosity = page.getByRole('combobox',{name:'Log detail'});
  const beforeRawY = await page.evaluate(() => window.scrollY);
  await verbosity.selectOption('raw');
  await waitLog('RAW line 99');
  assert.equal((await terminal.textContent()).includes('NORMAL line 99'),false);
  assert.equal(await atBottom(),true);
  assert.equal(await page.evaluate(() => window.scrollY),beforeRawY);
  await terminal.evaluate(element => {element.scrollTop=0;});
  await jump.waitFor();
  const beforeCompactY = await page.evaluate(() => window.scrollY);
  await verbosity.selectOption('compact');
  await waitLog('COMPACT line 34');
  assert.equal(await jump.isVisible(),true);
  assert.equal(await terminal.evaluate(element => element.scrollTop),0);
  assert.equal(await page.evaluate(() => window.scrollY),beforeCompactY);
  content.active.compact += 'COMPACT after detach\n';
  await waitLog('COMPACT after detach');
  assert.equal(await terminal.evaluate(element => element.scrollTop),0);
  runs.active.status='success'; runs.active.lifecycle_status='completed';
  content.active.compact += 'COMPACT final\n';
  await waitLog('COMPACT final');
  assert.equal(await live.count(),0);
  assert.equal(await terminal.evaluate(element => element.scrollTop),0);
  await page.goto(`${base}/#/runs/other`);
  await waitLog('OTHER line 99');
  await live.waitFor();
  assert.equal(await atBottom(),true);
  assert.ok(requests.some(row => row.runId === 'active' && row.level === 'raw' && row.after === 0));
  assert.ok(requests.some(row => row.runId === 'active' && row.level === 'compact' && row.after === 0));
  assert.deepEqual(errors,[]);
  console.log('Run log follow, detach, verbosity, and completion checks passed');
} finally {await browser.close();}
