import {chromium} from '@playwright/test';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const output = process.env.DEBBUILDER_VISUAL_DIR || '/tmp/debbuilder-c32-operator-final';
const browser = await chromium.launch({headless:true});
const states = [
  ['overview','overview'],['runs-normal','runs/ui-06-ready-to-publish'],['runs-failed','runs/ui-04-build-failed'],
  ['system-health','system'],['system-developer','system',page=>page.getByRole('button',{name:'Developer',exact:true}).click()],
  ['settings-general','settings'],['settings-repository','settings',page=>page.getByRole('button',{name:'Repository',exact:true}).click()],
  ['recipe-plan','recipes/seerr'],
  ['recipe-customize','recipes/seerr',page=>page.getByRole('button',{name:'Customize',exact:true}).click()],
  ['recipe-advanced','recipes/seerr',page=>page.getByRole('button',{name:'Advanced',exact:true}).click()],
  ['recipe-expert','recipes/seerr',page=>page.getByRole('button',{name:'Expert',exact:true}).click()],
  ['recipe-expert-json','recipes/seerr',async page=>{await page.getByRole('button',{name:'Expert',exact:true}).click();await page.locator('.recipe-json summary').click();}],
  ['elf-inapplicable','recipes/vendor-cli',page=>page.getByRole('button',{name:'Advanced',exact:true}).click()],
  ['elf-eligible','recipes/archive-agent',page=>page.getByRole('button',{name:'Advanced',exact:true}).click()],
];
const results=[];
await mkdir(output,{recursive:true});
try {
  for (const [name,route,action] of states) {
    const page=await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'reduce'});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`${base}/#/${route}`);
    await page.getByRole('heading',{level:1}).waitFor();
    if(route.startsWith('recipes/')){await page.locator('.recipe-identity').waitFor();await page.getByRole('button',{name:'Edit',exact:true}).waitFor();}
    if(route.startsWith('runs/'))await page.locator('.run-main').waitFor();
    if(route==='system')await page.locator('.system-grid').waitFor();
    if(route==='settings')await page.locator('.settings-content .panel').first().waitFor();
    if(route==='overview')await page.locator('.overview-grid').waitFor();
    await action?.(page);
    await page.waitForTimeout(250);
    await page.screenshot({path:path.join(output,`${name}.png`),fullPage:true});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
    results.push({name,route,overflow,errors});await page.close();
  }
  for(const [name,route] of [['overview','overview'],['runs-failed','runs/ui-04-build-failed'],['recipe-plan','recipes/seerr'],['recipe-customize','recipes/seerr'],['recipe-expert','recipes/seerr'],['settings-repository','settings']]) {
    const page=await browser.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`${base}/#/${route}`);
    if(route.startsWith('recipes/')){await page.locator('.recipe-identity').waitFor();await page.getByRole('button',{name:'Edit',exact:true}).waitFor();}
    else if(route.startsWith('runs/'))await page.locator('.run-main').waitFor();
    else if(route==='overview')await page.locator('.overview-grid').waitFor();
    else await page.locator('.settings-content .panel').first().waitFor();
    if(name==='recipe-customize')await page.getByRole('button',{name:'Customize',exact:true}).click();
    if(name==='recipe-expert')await page.getByRole('button',{name:'Expert',exact:true}).click();
    if(name==='settings-repository')await page.getByRole('button',{name:'Repository',exact:true}).click();
    await page.waitForTimeout(250);
    await page.screenshot({path:path.join(output,`mobile-${name}.png`),fullPage:true});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
    results.push({name:`mobile-${name}`,route,overflow,errors});await page.close();
  }
  await writeFile(path.join(output,'manifest.json'),JSON.stringify(results,null,2));
  const failures=results.filter(row=>row.errors.length||row.overflow);
  console.log(JSON.stringify({output,captures:results.length,failures},null,2));
  if(failures.length)process.exitCode=1;
} finally {await browser.close();}
