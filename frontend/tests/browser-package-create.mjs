import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5183';
const template = JSON.parse(readFileSync(new URL('../../tests/fixtures/recipes/seerr.json',import.meta.url)));
const browser = await chromium.launch({headless:true});
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    const page = await browser.newPage({viewport});
    const writes = [], errors = [];
    let stored = null;
    page.on('pageerror',error => errors.push(error.message));
    await page.route('**/api/**',route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      if (!path.startsWith('/api/')) return route.continue();
      const respond = body => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(body)});
      if (request.method() === 'POST') {
        writes.push({path,body:request.postDataJSON()});
        if (path === '/api/recipes/draft') {
          const {name,repository} = request.postDataJSON();
          return respond({recipe:{...template,name,package:{...template.package,name},source:{...template.source,repository}}});
        }
        if (path === '/api/recipes/validate') return respond({ok:true,recipe:request.postDataJSON().recipe,collision:null});
        if (path === '/api/workflows/new-package') {
          stored = request.postDataJSON().workflow;
          return route.fulfill({status:200,contentType:'application/json',headers:{ETag:`"${'0'.repeat(64)}"`},body:JSON.stringify({recipe:stored})});
        }
      }
      if (path === '/api/auth/status') return respond({ok:true,auth_mode:'local'});
      if (path === '/api/status') return respond({ok:true});
      const packageRow = stored && {name:stored.package.name,recipe:stored.name,status:'build_required',source:{type:'github',repository:stored.source.repository},version:{},build:{},allowed_actions:{}};
      if (path === '/api/packages') return respond({packages:packageRow?[packageRow]:[]});
      if (path === '/api/packages/new-package') return respond({package:packageRow});
      if (path === '/api/recipes') return respond({recipes:stored?[{id:stored.name,package:stored.package.name,repository:stored.source.repository}]:[]});
      if (path === '/api/workflows') return respond({workflows:stored?[{id:stored.name,writable:true}]:[],errors:[]});
      return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:{code:'unexpected',message:path}})});
    });

    await page.goto(`${base}/#/recipes`);
    assert.equal(await page.getByRole('button',{name:'New Recipe'}).count(),0);
    await page.goto(`${base}/#/packages`);
    await page.getByRole('button',{name:'Add package'}).click();
    const dialog = page.getByRole('dialog',{name:'Add package'});
    await dialog.waitFor();
    assert.equal(new URL(page.url()).hash,'#/packages');
    assert.equal(await dialog.getByLabel('Package name').evaluate(node => document.activeElement === node),true);
    assert.deepEqual(writes,[]);
    await dialog.getByRole('button',{name:'Cancel'}).click();
    assert.equal(await dialog.isVisible(),false);
    assert.deepEqual(writes,[]);

    await page.getByRole('button',{name:'Add package'}).click();
    await dialog.getByLabel('Package name').fill('new-package');
    await dialog.getByLabel('GitHub repository').fill('example/new-package');
    await dialog.getByRole('button',{name:'Create package'}).click();
    await page.waitForURL('**/#/packages/new-package');
    await page.locator('.package-detail').getByRole('heading',{name:'new-package'}).waitFor();
    assert.equal(await page.locator('.package-row').getByText('new-package',{exact:true}).count(),1);
    assert.equal(stored.package.name,'new-package');
    assert.deepEqual(writes.map(entry => entry.path),['/api/recipes/draft','/api/recipes/validate','/api/workflows/new-package']);
    assert.deepEqual(writes[0].body,{name:'new-package',repository:'example/new-package'});
    assert.equal(writes[2].body.create_only,true);
    await page.goto(`${base}/#/packages`);
    await page.getByRole('button',{name:'Add package'}).click();
    await dialog.getByLabel('Package name').fill('new-package');
    await dialog.getByLabel('GitHub repository').fill('example/other');
    await dialog.getByRole('button',{name:'Create package'}).click();
    await dialog.getByText('A package with this name already exists.').waitFor();
    assert.equal(writes.length,3);
    assert.deepEqual(errors,[]);
    await page.close();
  }
  console.log('Package creation modal and Recipe association checks passed');
} finally {await browser.close();}
