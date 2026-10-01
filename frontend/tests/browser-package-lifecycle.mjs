import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5184';
let phase = 'building';
let exists = true;
let removeAttempts = 0;
let packageReads = 0;
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  const packageRow = () => ({
    name:'ssh-notify', recipe:'ssh-notify-recipe', source:{type:'github',repository:'owner/ssh-notify'},
    lifecycle_display_status:phase === 'building' ? 'building' : phase === 'validating' ? 'validating' : 'validation_failed',
    version:{candidate:phase === 'building' ? '6.1.1-1' : '6.1.2-1',published:'6.1.1-1'},
    build:{latest_run_id:'run-b',active_run_id:['building','validating'].includes(phase)?'run-b':''},
    allowed_actions:{},
  });
  await page.route('**/api/**',async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if (request.method() === 'DELETE' && path === '/api/packages/ssh-notify') {
      removeAttempts++;
      if (removeAttempts === 1) return respond({ok:false,error:{code:'reprepro_remove_failed',message:'Repository removal failed',details:{}}},422);
      exists = false;
      return respond({ok:true,id:'ssh-notify',removal:{publication_removed:true,recipe_preserved:true,runs_preserved:true}});
    }
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'local'});
    if (path === '/api/status') return respond({ok:true});
    if (path === '/api/packages') {
      packageReads++;
      if (packageReads >= 2 && phase === 'building') phase = 'validating';
      return respond({packages:exists?[packageRow()]:[]});
    }
    if (path === '/api/packages/ssh-notify' && exists) return respond({package:packageRow()});
    if (path === '/api/recipes') return respond({recipes:[]});
    if (path === '/api/workflows') return respond({workflows:[],errors:[]});
    return respond({error:{code:'unexpected',message:path,details:{}}},404);
  });

  await page.goto(`${base}/#/packages/ssh-notify`);
  const row = page.locator('.package-row',{hasText:'ssh-notify'});
  await row.getByText('6.1.1-1',{exact:true}).first().waitFor();
  await page.waitForFunction(() => document.querySelector('.package-row')?.textContent?.includes('Validating'),null,{timeout:5000});
  const versions = row.locator('.version-cell');
  assert.equal(await versions.nth(0).innerText(),'6.1.2-1');
  assert.equal(await versions.nth(1).innerText(),'6.1.1-1');

  phase = 'failed';
  await page.waitForFunction(() => document.querySelector('.package-row')?.textContent?.includes('Validation failed'),null,{timeout:5000});
  assert.equal(await versions.nth(0).innerText(),'6.1.2-1');
  assert.equal(await versions.nth(1).innerText(),'6.1.1-1');

  const detail = page.locator('.package-detail');
  await detail.getByRole('button',{name:'Remove package'}).click();
  const dialog = page.getByRole('dialog',{name:'Remove package'});
  await dialog.getByText('The Recipe will remain.').waitFor();
  await dialog.getByText('Historical Runs will remain.').waitFor();
  const confirm = dialog.getByRole('button',{name:'Remove package',exact:true});
  assert.equal(await confirm.isDisabled(),true);
  await dialog.getByRole('checkbox',{name:/understand/}).check();
  await confirm.click();
  await dialog.getByText('Repository removal failed').waitFor();
  assert.equal(await page.locator('.package-row',{hasText:'ssh-notify'}).count(),1);
  await confirm.click();
  await dialog.waitFor({state:'hidden'});
  await page.getByText('No items',{exact:true}).waitFor();
  assert.equal(removeAttempts,2);
  assert.deepEqual(errors,[]);
  console.log('Live package lifecycle and safe removal checks passed');
} finally {
  await browser.close();
}
