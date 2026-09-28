import {chromium} from '@playwright/test';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';

const prototype = process.env.DEBBUILDER_PROTOTYPE_URL || 'http://127.0.0.1:5173';
const real = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const output = process.env.DEBBUILDER_VISUAL_DIR || '/tmp/debbuilder-c31-visual-final';
const browser = await chromium.launch({headless:true});
const cases = [
  {name:'overview',realRoute:'overview',ready:'.overview-grid',metrics:[
    ['sidebar','.sidebar',['x','width'],2],['content','.content',['x'],2],['title','.page-title',['x','y','width'],3],
    ['grid','.overview-grid',['x','width'],10],['main','.overview-main',['x','width'],10],['secondary','.overview-side',['x','width'],10],
    ['first panel','.overview-main .panel',['x','y','width'],12],['right panel','.overview-side .panel',['x','y','width'],12],
  ]},
  {name:'packages',realRoute:'packages',ready:'.package-detail',metrics:[
    ['layout','.package-layout',['x','y','width'],12],['list','.package-list',['x','y','width'],12],['detail','.package-detail',['x','y','width'],12],
  ]},
  {name:'recipes',realRoute:'recipes/seerr',ready:'.recipe-plan',metrics:[
    ['layout','.recipe-layout',['x','y','width'],12],['selector','.recipe-picker',['x','y','width'],12],['editor','.recipe-editor',['x','y','width'],12],
    ['levels','.recipe-levels',['x','width'],12],['levels top','.recipe-levels',['y'],30],['plan','.recipe-plan',['x','width'],12],['plan top','.recipe-plan',['y'],50],
    ['actions','.recipe-next',['x','width'],12],
  ]},
  {name:'runs',realRoute:'runs/ui-04-build-failed',ready:'.log-output',metrics:[
    ['layout','.run-layout',['x','y','width'],12],['selector','.run-list',['x','y','width'],12],['detail','.run-detail',['x','y','width'],12],
    ['stages','.stage-list',['x','width'],12],['stages top','.stage-list',['y'],150],['logs','.log-output',['x','width'],12],
  ]},
  {name:'system',realRoute:'system',ready:'.system-grid',metrics:[
    ['grid','.system-grid',['x','y','width'],16],['diagnostics','.system-grid .panel',['x','y','width'],16],['repository','.health-repository',['x','width'],16],
  ]},
  {name:'settings',realRoute:'settings',ready:'.settings-content .panel',metrics:[
    ['layout','.settings-layout',['x','y','width'],12],['groups','.settings-nav',['x','y','width'],12],['content','.settings-content',['x','y','width'],12],
  ]},
];
const reports = [];
function round(value) {return Math.round(value*10)/10;}
async function open(kind,item,size='desktop') {
  const viewport=size==='mobile'?{width:390,height:844}:{width:1440,height:900};
  const page=await browser.newPage({viewport,deviceScaleFactor:1,reducedMotion:'reduce'});
  await page.addInitScript(() => localStorage.setItem('debBuilder24Theme','light'));
  await page.goto(kind==='b6'?`${prototype}/?view=${item.name}&scenario=${item.name==='runs'?'failed':'normal'}&clean=1`:`${real}/#/${item.realRoute}`);
  const ready = size==='mobile' && kind==='b6' && item.name==='recipes' ? '.recipe-picker' : size==='mobile' && kind==='b6' && item.name==='runs' ? '.run-list' : item.ready;
  await page.locator(ready).first().waitFor({timeout:15000});
  if(size==='mobile' && kind==='b6' && item.name==='recipes')await page.locator('.recipe-picker .selection-list button').first().click();
  if(size==='mobile' && kind==='b6' && item.name==='runs')await page.locator('.run-selection button').first().click();
  await page.waitForTimeout(250);
  return page;
}
try {
  for (const item of cases) {
    const b6=await open('b6',item),current=await open('real',item);
    const compare=[];
    for (const [label,selector,fields,tolerance] of item.metrics) {
      const read=page=>page.locator(selector).first().evaluate(node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
      const reference=await read(b6),actual=await read(current);
      for(const field of fields){const delta=round(actual[field]-reference[field]);compare.push({label,selector,field,b6:round(reference[field]),real:round(actual[field]),difference:delta,tolerance,pass:Math.abs(delta)<=tolerance});}
    }
    reports.push({surface:item.name,size:'desktop',metrics:compare,pass:compare.every(row=>row.pass)});
    await b6.close();await current.close();
  }
  for (const item of cases.filter(row=>['overview','recipes','runs','settings'].includes(row.name))) {
    const b6=await open('b6',item,'mobile'),current=await open('real',item,'mobile');
    if(item.name==='overview'){
      await b6.getByRole('button',{name:'Open navigation'}).click();
      await current.getByRole('button',{name:'Menu'}).click();
    }
    const selector=item.name==='overview'?'.sidebar':item.name==='recipes'?'.recipe-editor':item.name==='runs'?'.run-detail':'.settings-nav';
    const read=page=>page.locator(selector).first().evaluate(node=>{const r=node.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width};});
    const reference=await read(b6),actual=await read(current);
    const fields=item.name==='overview'?['x','width']:['x','width'];
    const metrics=fields.map(field=>({label:selector,field,b6:round(reference[field]),real:round(actual[field]),difference:round(actual[field]-reference[field]),tolerance:16,pass:Math.abs(actual[field]-reference[field])<=16}));
    const overflow=await current.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
    reports.push({surface:item.name,size:'mobile',metrics,overflow,pass:metrics.every(row=>row.pass)&&overflow===0});
    await b6.close();await current.close();
  }
  await mkdir(output,{recursive:true});
  await writeFile(path.join(output,'geometry.json'),JSON.stringify({viewport:{desktop:[1440,900],mobile:[390,844]},reports},null,2));
  const failures=reports.flatMap(row=>row.metrics.filter(metric=>!metric.pass).map(metric=>({surface:row.surface,size:row.size,...metric}))).concat(reports.filter(row=>row.overflow).map(row=>({surface:row.surface,size:row.size,overflow:row.overflow})));
  console.log(JSON.stringify({output,surfaces:reports.length,assertions:reports.reduce((n,row)=>n+row.metrics.length,0),failures},null,2));
  if(failures.length)process.exitCode=1;
} finally {await browser.close();}
