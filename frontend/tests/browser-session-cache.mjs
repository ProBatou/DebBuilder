import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
const browser = await chromium.launch({headless:true});
let dashboardCalls = 0;
let packageCalls = 0;
let releaseInitial, releaseRevisit;
const initial = new Promise(resolve => {releaseInitial = resolve;});
const revisit = new Promise(resolve => {releaseRevisit = resolve;});
let releasePackageRevisit;
const packageRevisit = new Promise(resolve => {releasePackageRevisit = resolve;});
let failRefresh = false;
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/api/**',async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'});
    if (path === '/api/dashboard') {
      dashboardCalls++;
      if (dashboardCalls === 1) await initial;
      if (dashboardCalls === 2) await revisit;
      if (failRefresh) return respond({error:{code:'failed',message:'Refresh failed'}},500);
      return respond({dashboard:{packages:dashboardCalls,ready_to_publish:0,package_rows:[],latest_operations:[]}});
    }
    if (path === '/api/system/diagnostics') return respond({checks:[]});
    if (path === '/api/packages') {
      packageCalls++;
      if (packageCalls === 2) await packageRevisit;
      const name = packageCalls === 1 ? 'before-refresh' : 'after-refresh';
      return respond({packages:[{name,recipe:'',status:'recipe_missing',build:{},source:{type:'manual'},version:{},allowed_actions:{}}]});
    }
    if (path.startsWith('/api/packages/')) {
      const name = decodeURIComponent(path.slice('/api/packages/'.length));
      return respond({package:{name,recipe:'',status:'recipe_missing',build:{},source:{type:'manual'},version:{},allowed_actions:{}}});
    }
    if (path === '/api/recipes') return respond({recipes:[]});
    if (path === '/api/workflows') return respond({workflows:[],errors:[]});
    return respond({error:{code:'unexpected',message:path}},404);
  });
  await page.goto(`${base}/#/overview`);
  await page.locator('#main').getByText('Loading…',{exact:true}).waitFor();
  releaseInitial();
  await page.locator('.overview-grid').waitFor();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Recipes'}).click();
  await page.locator('.recipe-layout').waitFor();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Overview'}).click();
  await page.locator('.overview-grid').waitFor();
  assert.equal(await page.locator('#main').getByText('Loading…',{exact:true}).count(),0);
  assert.equal(await page.locator('.compact-stats').innerText().then(text => text.includes('1')),true);
  releaseRevisit();
  await page.waitForFunction(() => document.querySelector('.compact-stats')?.textContent?.includes('2'));
  assert.equal(dashboardCalls,2);
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Packages'}).click();
  await page.locator('.package-row').getByText('before-refresh').waitFor();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Recipes'}).click();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Packages'}).click();
  await page.locator('.package-row').getByText('before-refresh').waitFor();
  assert.equal(await page.locator('#main').getByText('Loading…',{exact:true}).count(),0);
  releasePackageRevisit();
  await page.locator('.package-row').getByText('after-refresh').waitFor();
  assert.equal(packageCalls,2);
  failRefresh = true;
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Recipes'}).click();
  await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name:'Overview'}).click();
  await page.getByText('Refresh failed').waitFor();
  assert.equal(await page.locator('.overview-grid').count(),1);
  assert.equal(await page.locator('#main').getByText('Loading…',{exact:true}).count(),0);
  assert.deepEqual(errors,[]);
  console.log('Session cache navigation checks passed');
} finally {await browser.close();}
