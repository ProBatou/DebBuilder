import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {chromium} from '@playwright/test';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const captures = path.resolve(root, '../../docs/design/references');
const shouldCapture = process.argv.includes('--capture');
const pages = ['overview','packages','recipes','runs','system','settings'];
const labels = {overview:'Overview',packages:'Packages',recipes:'Recipes',runs:'Runs',system:'System',settings:'Settings'};

async function freePort() {
  const server = createServer();
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}
async function ready(url) {
  for (let i=0;i<80;i++) {
    try {const response = await fetch(url); if (response.ok) return; } catch {}
    await new Promise(resolve => setTimeout(resolve,100));
  }
  throw new Error('Vite preview did not start');
}
async function navigate(page, name, mobile) {
  if (mobile) await page.getByRole('button',{name:'Open navigation'}).click();
  await page.locator('.sidebar nav button').filter({hasText:labels[name]}).click();
  await assert.equal(await page.locator('main h1').textContent(), labels[name]);
}
async function screenshot(page,name) {
  if (shouldCapture) await page.screenshot({path:path.join(captures, name+'.png'), fullPage:true});
}

const port = await freePort();
const server = spawn(path.join(root,'node_modules/.bin/vite'),['preview','--host','127.0.0.1','--port',String(port),'--strictPort'],{cwd:root,stdio:'ignore'});
let browser;
try {
  await ready(`http://127.0.0.1:${port}/`);
  browser = await chromium.launch({headless:true});
  if (shouldCapture) await mkdir(captures,{recursive:true});
  for (const [size,viewport] of [['desktop',{width:1440,height:1000}],['mobile',{width:390,height:844}]]) {
    const mobile = size === 'mobile';
    const page = await browser.newPage({viewport,deviceScaleFactor:1,reducedMotion:'reduce'});
    const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.goto(`http://127.0.0.1:${port}/`);
    if (mobile) {
      const menu = page.getByRole('button',{name:'Open navigation'});
      await menu.click();
      assert.equal(await menu.getAttribute('aria-expanded'),'true');
      await page.keyboard.press('Escape');
      assert.equal(await menu.getAttribute('aria-expanded'),'false');
      assert.equal(await menu.evaluate(node=>document.activeElement === node),true);
    }
    for (const name of pages) {
      if (name !== 'overview') await navigate(page,name,mobile);
      await page.waitForTimeout(75);
      const width = await page.evaluate(()=>document.documentElement.scrollWidth);
      assert.ok(width <= viewport.width, `${size} ${name} overflows by ${width-viewport.width}px`);
      await screenshot(page,`${size}-${name}-normal`);
    }
    await navigate(page,'recipes',mobile);
    await page.getByRole('button',{name:'Review detailed controls'}).click();
    assert.equal(await page.getByRole('button',{name:'Review detailed controls'}).getAttribute('aria-expanded'),'true');
    await page.locator('#scenario').selectOption('blocker');
    assert.equal(await page.getByRole('button',{name:'Test plan →'}).first().isDisabled(),true);
    await screenshot(page,`${size}-recipes-blocker`);
    await page.getByRole('button',{name:'Confirm directory'}).click();
    assert.equal(await page.getByRole('button',{name:'Test plan →'}).first().isEnabled(),true);
    const testTrigger = page.getByRole('button',{name:'Test plan →'}).first();
    await testTrigger.click();
    assert.equal(await page.getByRole('dialog').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('aria-label')),'Close dialog');
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').isVisible(),false);
    assert.equal(await testTrigger.evaluate(node=>document.activeElement === node),true);
    await navigate(page,'runs',mobile);
    await page.locator('#scenario').selectOption('running');
    assert.equal(await page.locator('[data-run-poller]').count(),1);
    await page.waitForTimeout(1400);
    const pollCount = Number(await page.locator('.run-header').getAttribute('data-poll-count'));
    assert.ok(pollCount > 0,'Run poller should update while mounted');
    await screenshot(page,`${size}-runs-running`);
    await page.getByRole('button',{name:'Pause follow'}).click();
    assert.ok((await page.locator('.log-help').textContent()).includes('paused'));
    await page.locator('#scenario').selectOption('failed');
    assert.ok((await page.getByRole('alert').textContent()).includes('Offline service validation failed'));
    await screenshot(page,`${size}-runs-failed`);
    await navigate(page,'overview',mobile);
    assert.equal(await page.locator('[data-run-poller]').count(),0);
    await page.locator('#scenario').selectOption('empty');
    await screenshot(page,`${size}-overview-empty`);
    await navigate(page,'system',mobile);
    await page.locator('#scenario').selectOption('recovery');
    assert.ok((await page.locator('.hero').textContent()).includes('Build admission blocked'));
    await screenshot(page,`${size}-system-recovery`);
    assert.deepEqual(errors,[],`${size} page errors`);
    await page.close();
  }
  console.log(`Browser reference checks passed: 6 screens × 2 viewports, blocker/running/failed/empty/recovery, keyboard dialog, mounted Run polling${shouldCapture ? '; screenshots saved' : ''}.`);
} finally {
  await browser?.close();
  server.kill('SIGTERM');
}
