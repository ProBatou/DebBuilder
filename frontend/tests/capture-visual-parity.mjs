import {chromium} from '@playwright/test';
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';

const real = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const prototype = process.env.DEBBUILDER_PROTOTYPE_URL || 'http://127.0.0.1:5173';
const output = process.env.DEBBUILDER_VISUAL_DIR || '/tmp/debbuilder-c3-visual';
const browser = await chromium.launch({headless:true});
const captures = [];
await mkdir(output,{recursive:true});

async function capture(kind, surface, {size='desktop', theme='light', scenario='normal', route='', action}={}) {
  const viewport = size === 'mobile' ? {width:390,height:844} : {width:1440,height:900};
  const context = await browser.newContext({viewport,deviceScaleFactor:1,reducedMotion:'reduce'});
  await context.addInitScript(theme => localStorage.setItem('debBuilder24Theme',theme),theme);
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const url = kind === 'b6' ? `${prototype}/?view=${route || surface}&scenario=${scenario}&clean=1` : `${real}/#/${route || surface}`;
  await page.goto(url);
  const view = (route || surface).split('/')[0];
  const title = view === 'repository-inventory' ? 'Packages' : view === 'recipes-view' || view === 'recipes-edit' || view === 'recipes-advanced' || view === 'recipes-expert' ? 'Recipes' : view === 'runs-normal' || view === 'runs-failed' ? 'Runs' : view === 'drawer' ? 'Overview' : view[0].toUpperCase() + view.slice(1);
  await page.getByRole('heading',{level:1,name:title}).waitFor({timeout:10000});
  if (kind === 'real') {
    const ready = surface === 'system' ? '.system-check' : surface === 'settings' ? '.settings-appearance' : surface.startsWith('recipes') ? '.recipe-identity h2' : surface === 'overview' || surface === 'drawer' ? '.overview-grid' : null;
    if (ready) await page.locator(ready).first().waitFor({timeout:10000});
  }
  if (kind === 'b6' && surface.startsWith('recipes')) {
    const seerr = page.locator('.recipe-picker .selection-list button').filter({hasText:'seerr'}).first();
    if (await seerr.count()) await seerr.click();
  }
  if (kind === 'b6' && surface === 'runs-failed') {
    const failed = page.locator('.run-selection button').filter({hasText:'archive-agent'}).first();
    if (await failed.count()) await failed.click();
  }
  if (kind === 'real' && size === 'desktop' && surface === 'runs-normal' && !(route || '').includes('/')) {
    await page.locator('.run-list .row').first().click();
  }
  await action?.(page,kind);
  if (kind === 'real' && surface === 'packages') await page.locator('.package-detail h2').waitFor();
  if (kind === 'real' && surface === 'repository-inventory') await page.locator('.table-wrap').first().waitFor();
  if (kind === 'real' && (surface === 'runs-detail' || surface === 'runs-failed' || (surface === 'runs-normal' && size === 'desktop'))) await page.locator('.run-main h2').waitFor();
  if (kind === 'real' && surface === 'runs-normal' && size === 'mobile') await page.locator('.run-list .row').first().waitFor();
  await page.waitForTimeout(300);
  const filename = `${kind}-${size}-${surface}-${theme}.png`;
  await page.screenshot({path:path.join(output,filename),fullPage:true});
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  captures.push({kind,surface,size,theme,scenario,filename,overflow,errors});
  await context.close();
}

try {
  const surfaces = [
    ['overview',{}],
    ['packages',{}],
    ['repository-inventory',{route:'packages',action:async (page,kind) => page.getByRole('button',{name:/View repository inventory/}).click()}],
    ['recipes-view',{route:'recipes/seerr'}],
    ['recipes-edit',{route:'recipes/seerr',action:async (page,kind) => {if(kind==='real')await page.getByRole('button',{name:'Edit',exact:true}).click();else await page.getByRole('button',{name:'Customize',exact:true}).first().click();}}],
    ['recipes-advanced',{route:'recipes/seerr',action:async (page,kind) => {if(kind==='real')await page.getByRole('button',{name:'Edit',exact:true}).click();await page.getByRole('button',{name:/Advanced/}).first().click();}}],
    ['recipes-expert',{route:'recipes/seerr',action:async (page,kind) => {if(kind==='real')await page.getByRole('button',{name:'Edit',exact:true}).click();await page.getByRole('button',{name:'Expert',exact:true}).first().click();}}],
    ['runs-normal',{route:'runs'}],
    ['runs-failed',{route:'runs/ui-04-build-failed',scenario:'failed'}],
    ['system',{}],
    ['settings',{}],
  ];
  for (const [surface,options] of surfaces) for (const kind of ['b6','real']) {
    try {await capture(kind,surface,{...options,route:kind==='b6' ? options.route?.split('/')[0] : options.route});}
    catch(error) {captures.push({kind,surface,error:error.message});}
  }
  for (const surface of ['overview','recipes-view','runs-normal','runs-detail','settings','drawer']) for (const kind of ['b6','real']) {
    const route = surface === 'drawer' ? 'overview' : surface === 'recipes-view' ? kind === 'b6' ? 'recipes' : 'recipes/seerr' : surface === 'runs-normal' || surface === 'runs-detail' ? 'runs' : surface;
    const action = surface === 'drawer' ? async page => page.getByRole('button',{name:kind==='b6'?'Open navigation':'Menu'}).click() : surface === 'runs-detail' ? async page => page.locator(kind === 'b6' ? '.run-selection button' : '.run-list .row').first().click() : undefined;
    try {await capture(kind,surface,{size:'mobile',route,action});}
    catch(error) {captures.push({kind,surface,size:'mobile',error:error.message});}
  }
  for (const kind of ['b6','real']) for (const surface of ['overview','recipes-view','runs-failed']) {
    const route = surface === 'recipes-view' ? kind === 'b6' ? 'recipes' : 'recipes/seerr' : surface === 'runs-failed' ? kind === 'b6' ? 'runs' : 'runs/ui-04-build-failed' : surface;
    try {await capture(kind,surface,{theme:'dark',route,scenario:surface === 'runs-failed' ? 'failed' : 'normal'});}
    catch(error) {captures.push({kind,surface,theme:'dark',error:error.message});}
  }
  await writeFile(path.join(output,'manifest.json'),JSON.stringify(captures,null,2));
  const errors = captures.filter(row => row.error || row.errors?.length || row.overflow > 0);
  console.log(JSON.stringify({output,total:captures.length,errors},null,2));
  if (errors.length) process.exitCode = 1;
} finally {await browser.close();}
