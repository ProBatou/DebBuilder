import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {mkdir,readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {chromium} from '@playwright/test';
import {publicRepositoryFixture as publicRepo} from '../src/lib/publicRepositoryFixture.js';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const captures=path.resolve(root,'../../docs/design/references');
const publicOnly=process.argv.includes('--capture-public');
const capture=process.argv.includes('--capture')||publicOnly;
const views=['overview','packages','recipes','runs','system','settings'];
const labels={overview:'Overview',packages:'Packages',recipes:'Recipes',runs:'Runs',system:'System',settings:'Settings'};
async function freePort(){const server=createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port;await new Promise(resolve=>server.close(resolve));return port;}
async function ready(url){for(let i=0;i<80;i++){try{if((await fetch(url)).ok)return;}catch{}await new Promise(resolve=>setTimeout(resolve,100));}throw Error('Vite preview did not start');}
const port=await freePort();
const base=`http://127.0.0.1:${port}/`;
const server=spawn(path.join(root,'node_modules/.bin/vite'),['preview','--host','127.0.0.1','--port',String(port),'--strictPort'],{cwd:root,stdio:'ignore'});
let browser;
async function open(view='overview',size='desktop',theme='light',locale='en',scenario='normal'){
  const viewport=size==='mobile'?{width:390,height:844}:{width:1440,height:1000};
  const context=await browser.newContext({viewport,deviceScaleFactor:1,reducedMotion:'reduce'});const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(({theme,locale})=>{localStorage.setItem('debBuilder24Theme',theme);localStorage.setItem('debBuilder24Locale',locale);},{theme,locale});
  await page.goto(`${base}?view=${view}&scenario=${scenario}&clean=1`);
  await page.waitForFunction(({theme,locale})=>document.documentElement.dataset.theme===theme&&document.documentElement.lang===locale,{theme,locale});
  await page.waitForFunction(()=>{const root=getComputedStyle(document.documentElement).color;return getComputedStyle(document.querySelector('main h1')).color===root&&[...document.querySelectorAll('.panel h2')].every(node=>getComputedStyle(node).color===root);});
  return {page,errors,viewport};
}
async function openPublic(size='desktop',scheme='light',locale='en'){
  const viewport=size==='mobile'?{width:390,height:844}:{width:1440,height:1000};
  const context=await browser.newContext({viewport,deviceScaleFactor:1,reducedMotion:'reduce',colorScheme:scheme,locale:{en:'en-US',fr:'fr-FR',de:'de-DE',es:'es-ES'}[locale]});
  await context.grantPermissions(['clipboard-read','clipboard-write'],{origin:base.slice(0,-1)});
  const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const response=await page.goto(`${base}repository-public.html?clean=1`);
  assert.equal(response?.status(),200,'public HTML entry is served');
  await page.waitForFunction(expected=>document.documentElement.lang===expected,locale);
  await page.waitForFunction(()=>document.querySelector('.public-page')&&document.querySelector('main h1'));
  return {page,errors,viewport};
}
async function fit(page,viewport,name){const width=await page.evaluate(()=>document.documentElement.scrollWidth);assert.ok(width<=viewport.width,`${name} overflows by ${width-viewport.width}px`);}
async function shot(page,name){if(capture&&(!publicOnly||name.startsWith('public-repository-')))await page.screenshot({path:path.join(captures,`${name}.png`),fullPage:true,style:'.prototype-tools,.tool-reopen{visibility:hidden!important}'});}
async function nav(page,name,mobile=false){if(mobile)await page.getByRole('button',{name:'Open navigation'}).click();await page.locator('.sidebar nav button').filter({hasText:labels[name]}).click();assert.equal(await page.locator('main h1').textContent(),labels[name]);}
try{
 await ready(base);browser=await chromium.launch({headless:true});if(capture)await mkdir(captures,{recursive:true});
 for(const size of ['desktop','mobile'])for(const view of views){const {page,errors,viewport}=await open(view,size);await fit(page,viewport,`${size}-${view}`);await shot(page,`${size}-${view}-light-en`);assert.deepEqual(errors,[]);await page.close();}
 for(const [size,view,theme,locale,scenario] of [
  ['desktop','overview','dark','en','normal'],['desktop','recipes','dark','de','blocker'],['desktop','runs','dark','en','failed'],['desktop','system','dark','fr','recovery'],['desktop','settings','dark','es','normal'],
  ['mobile','overview','dark','fr','manyActions'],['mobile','recipes','dark','de','blocker'],['mobile','runs','dark','es','running'],['desktop','overview','light','fr','one'],['mobile','packages','light','es','manyPackages']
 ]){const {page,errors,viewport}=await open(view,size,theme,locale,scenario);if(size==='mobile'&&['recipes','runs','packages'].includes(view))await page.locator(view==='recipes'?'.recipe-picker .selection-list button':view==='runs'?'.run-list .selection-list button':'.package-list .package-row').first().click();await fit(page,viewport,`${size}-${view}-${theme}-${locale}-${scenario}`);await shot(page,`${size}-${view}-${theme}-${locale}-${scenario}`);assert.deepEqual(errors,[]);await page.close();}
 {const {page,errors,viewport}=await open('packages','mobile');await page.getByRole('button',{name:/View repository inventory/}).click();await fit(page,viewport,'mobile-repository-inventory');await shot(page,'mobile-packages-repository-inventory');assert.deepEqual(errors,[]);await page.close();}
 {
  const {page,errors,viewport}=await open();
  await page.goto(`${base}?clean=0`);
  for(const [scenario,expected] of [['empty',0],['one',1],['three',3],['manyActions',5]]){await page.locator('#scenario').selectOption(scenario);assert.equal(await page.locator('.overview-main .panel').first().locator('.click-row').count(),expected);}
  await shot(page,'desktop-overview-many-actions');
  await page.locator('#scenario').selectOption('normal');await page.locator('.overview-main .panel').first().locator('.click-row').first().click();assert.equal(await page.locator('main h1').textContent(),'Packages');
  await page.locator('#scenario').selectOption('manyPackages');assert.ok(await page.locator('.package-row').count()>=20);await page.locator('.package-row').nth(4).click();assert.equal(await page.locator('.package-detail h2').textContent(),'maintainerr');await shot(page,'desktop-packages-many');
  assert.equal(await page.locator('.segmented-tabs').count(),0);await page.getByRole('button',{name:'Hide prototype controls'}).click();await page.getByRole('button',{name:/View repository inventory/}).click();assert.ok(await page.getByText('Published packages').isVisible());assert.ok(await page.getByText('stalwart').isVisible());assert.equal(await page.getByText('maintainerr').count(),0);await shot(page,'desktop-packages-repository-inventory');await page.context().grantPermissions(['clipboard-read','clipboard-write'],{origin:base.slice(0,-1)});await page.getByRole('button',{name:'Copy Add signing key'}).click();assert.ok((await page.evaluate(()=>navigator.clipboard.readText())).includes('repository.gpg'));await page.getByRole('button',{name:'Back to Packages'}).click();await page.getByRole('button',{name:'Show prototype controls'}).click();
  await nav(page,'recipes');assert.ok(await page.locator('.recipe-picker .selection-list button').count()>=6);await page.locator('.recipe-picker .selection-list button').nth(1).click();assert.equal(await page.locator('.editor-head h2').textContent(),'pocket-id');
  await page.locator('#scenario').selectOption('blocker');await page.locator('.recipe-picker .selection-list button').first().click();assert.ok(await page.getByText('Working directory needs a declaration.').isVisible());await page.getByRole('button',{name:'Confirm directory'}).click();await page.getByRole('button',{name:'Test plan →'}).click();assert.ok(await page.getByRole('dialog').isVisible());await page.keyboard.press('Escape');assert.equal(await page.getByRole('dialog').isVisible(),false);
  await nav(page,'runs');assert.ok(await page.locator('.run-selection button').count()>=8);await page.locator('#scenario').selectOption('manyRuns');assert.ok(await page.locator('.run-selection button').count()>=30);await shot(page,'desktop-runs-many');
  await page.locator('#scenario').selectOption('running');await page.locator('.run-selection button').filter({hasText:'pocket-id'}).click();assert.equal(await page.locator('[data-run-poller]').count(),1);await page.waitForFunction(()=>Number(document.querySelector('[data-run-poller]')?.dataset.pollCount)>0);await page.getByRole('button',{name:'Request cancellation'}).click();assert.ok(await page.getByRole('dialog').isVisible());await page.keyboard.press('Escape');await nav(page,'overview');assert.equal(await page.locator('[data-run-poller]').count(),0);
  await nav(page,'settings');await page.getByRole('button',{name:'General',exact:true}).click();await page.getByLabel('Theme').selectOption('dark');await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');await page.getByLabel('Language').selectOption('fr');await page.waitForFunction(()=>document.documentElement.lang==='fr');assert.equal(await page.locator('main h1').textContent(),'Paramètres');const persisted=await page.context().newPage();await persisted.goto(`${base}?view=settings&clean=1`);await persisted.waitForFunction(()=>document.documentElement.dataset.theme==='dark'&&document.documentElement.lang==='fr');await persisted.close();
  await fit(page,viewport,'theme-locale-persisted');assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors,viewport}=await open('overview','desktop');const collapse=page.getByRole('button',{name:'Collapse sidebar'});await collapse.click();assert.ok(await page.locator('.shell.sidebar-collapsed').count());await page.reload();assert.ok(await page.locator('.shell.sidebar-collapsed').count());await page.getByRole('button',{name:'Expand sidebar'}).click();
  const menu=await open('overview','mobile');await menu.page.getByRole('button',{name:'Open navigation'}).click();await menu.page.keyboard.press('Escape');assert.equal(await menu.page.getByRole('button',{name:'Open navigation'}).getAttribute('aria-expanded'),'false');await menu.page.close();assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors}=await open('system');
  assert.ok(await page.getByText('Signing status').isVisible());
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();assert.ok(await page.getByText('Clear execution history').isVisible());
  await page.getByRole('button',{name:'Developer',exact:true}).click();assert.ok(await page.getByText('Raw OpenAPI contract').isVisible());
  await nav(page,'settings');
  for(const [tab,label] of [['Repository','Public repository URL'],['GitHub','GitHub token'],['Authentication','Issuer URL'],['Notifications','ntfy server'],['Automation','Auto validate after successful build'],['Advanced','No additional editable settings']]){
   await page.getByRole('button',{name:tab,exact:true}).last().click();assert.ok((await page.locator('.settings-content').textContent()).includes(label),`${tab} settings inventory`);
  }
  assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors}=await open('recipes','desktop','light','en','long');
  const anchors=['.brand-mark','.sidebar nav button:first-child','.repo-indicator','.sidebar .version'];
  for(const collapsed of [false,true]){
   if(collapsed)await page.getByRole('button',{name:'Collapse sidebar'}).click();
   await page.evaluate(()=>scrollTo(0,0));
   const before=await Promise.all(anchors.map(selector=>page.locator(selector).boundingBox()));
   await page.evaluate(()=>scrollTo(0,document.documentElement.scrollHeight));
   await page.waitForFunction(()=>scrollY>350);
   const after=await Promise.all(anchors.map(selector=>page.locator(selector).boundingBox()));
   for(let i=0;i<anchors.length;i++){
    assert.ok(before[i]&&after[i],`${anchors[i]} missing`);
    assert.ok(Math.abs(before[i].y-after[i].y)<=1,`${anchors[i]} moved while scrolling (${collapsed?'collapsed':'expanded'})`);
    assert.ok(after[i].y>=0&&after[i].y+after[i].height<=1000,`${anchors[i]} outside viewport`);
   }
  }
  assert.deepEqual(errors,[]);await page.close();
 }
 for(const locale of ['en','fr','de','es'])for(const size of ['desktop','mobile']){const {page,errors,viewport}=await open('settings',size,'light',locale,'long');for(const view of views){if(view!=='settings'){if(size==='mobile')await page.locator('.mobile-top .icon-button').click();await page.locator('.sidebar nav button').nth(views.indexOf(view)).click();}await fit(page,viewport,`${locale}-${size}-${view}`);}assert.deepEqual(errors,[]);await page.close();}
 const installerTemplate=await readFile(path.resolve(root,'../../debbuilder/repository_templates/install.sh'),'utf8');
 assert.ok(installerTemplate.includes('Signed-By: /etc/apt/keyrings/debbuilder.gpg'));
 assert.ok(installerTemplate.includes('apt-get update'));
 assert.equal(publicRepo.baseUrl,'https://repo.probatou.com');
 const publicColors={};
 for(const [size,scheme,locale,captureName] of [
  ['desktop','light','en','public-repository-desktop-light-en'],
  ['desktop','dark','en','public-repository-desktop-dark-en'],
  ['mobile','light','en','public-repository-mobile-light-en'],
  ['mobile','dark','de','public-repository-mobile-dark-de'],
  ['mobile','light','fr',null],['desktop','light','es',null],
 ]){
  const {page,errors,viewport}=await openPublic(size,scheme,locale);
  assert.equal(await page.locator('.sidebar,.workspace,.page-title,.prototype-tools').count(),0,'public page contains no admin shell');
  assert.equal(await page.getByRole('heading',{name:'Packages',exact:true}).count(),0);
  assert.equal(await page.locator('.public-page').count(),1);
  await fit(page,viewport,`public-${size}-${scheme}-${locale}`);
  assert.equal(await page.getByRole('link',{name:/InRelease/}).getAttribute('href'),`${publicRepo.baseUrl}/dists/Luminous/InRelease`);
  assert.equal(await page.getByRole('link',{name:/Public signing key|Öffentlicher Signaturschlüssel|Clé de signature publique|Clave pública de firma/}).getAttribute('href'),`${publicRepo.baseUrl}/repository.gpg`);
  if(size==='desktop'&&locale==='en')publicColors[scheme]=await page.evaluate(()=>getComputedStyle(document.documentElement).backgroundColor);
  if(captureName)await shot(page,captureName);
  if(locale==='en'&&scheme==='light'){
   await page.getByRole('button',{name:'Copy Generated installer'}).click();
   assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),`curl -fsSL ${publicRepo.baseUrl}/install.sh | sudo bash`);
   await page.getByRole('button',{name:'Copy Add repository'}).click();
   assert.ok((await page.evaluate(()=>navigator.clipboard.readText())).includes('Signed-By: /etc/apt/keyrings/debbuilder.gpg'));
   await page.getByRole('button',{name:'Copy Update package lists'}).focus();await page.keyboard.press('Enter');
   assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'sudo apt-get update');
  }
  if(locale==='de'){
   assert.equal(await page.locator('main h1').textContent(),'DebBuilder-Repository');
   await page.getByRole('combobox',{name:'Sprache'}).selectOption('fr');
   assert.equal(await page.locator('main h1').textContent(),'Dépôt DebBuilder');
   await page.reload();await page.waitForFunction(()=>document.documentElement.lang==='fr');
  }
  assert.deepEqual(errors,[],`public ${size} ${scheme} ${locale} page errors`);
  await page.close();
 }
 assert.notEqual(publicColors.light,publicColors.dark,'public landing follows prefers-color-scheme');
 console.log('Browser reference checks passed: admin screens and independent public landing, Light/Dark, four locales, copy, links, responsive overflow and keyboard paths.');
}finally{await browser?.close();server.kill('SIGTERM');}
