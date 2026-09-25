import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {mkdir,readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {chromium} from '@playwright/test';
import {publicRepositoryFixture as publicRepo} from '../src/lib/publicRepositoryFixture.js';
import {recipeProfiles,toV5Fixture,fromV5Fixture} from '../src/lib/recipeFixtures.js';

for(const [id,profile] of Object.entries(recipeProfiles)){const document=toV5Fixture(profile);assert.equal(document.schema_version,5);assert.equal(document.name,id);assert.deepEqual(toV5Fixture(fromV5Fixture(document)),document);}
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const captures=path.resolve(root,'../../docs/design/references');
const publicOnly=process.argv.includes('--capture-public');
const recipeOnly=process.argv.includes('--capture-recipes');
const capture=process.argv.includes('--capture')||publicOnly||recipeOnly;
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
async function shot(page,name){if(capture&&(!publicOnly||name.startsWith('public-repository-'))&&(!recipeOnly||name.includes('recipes')||name==='desktop-system-managed-self-build'))await page.screenshot({path:path.join(captures,`${name}.png`),fullPage:name!=='desktop-recipes-advanced-editor',style:'.prototype-tools,.tool-reopen{visibility:hidden!important}'});}
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
  assert.equal(await page.locator('.segmented-tabs').count(),0);await page.getByRole('button',{name:'Hide prototype controls'}).click();await page.getByRole('button',{name:/View repository inventory/}).click();assert.ok(await page.getByRole('heading',{name:'Published packages'}).isVisible());assert.ok(await page.getByText('stalwart').isVisible());assert.equal(await page.getByText('maintainerr').count(),0);await shot(page,'desktop-packages-repository-inventory');assert.equal(await page.locator('.inventory-view .copy-button,.inventory-view .install-steps').count(),0);assert.ok(await page.getByRole('link',{name:/Open public repository/}).isVisible());await page.getByRole('button',{name:'Back to Packages'}).click();await page.getByRole('button',{name:'Show prototype controls'}).click();
  await nav(page,'recipes');assert.ok(await page.locator('.recipe-picker .selection-list button').count()>=6);await page.locator('.recipe-picker .selection-list button').nth(1).click();assert.equal(await page.locator('.editor-head h2').textContent(),'pocket-id');
  await page.locator('#scenario').selectOption('blocker');await page.locator('.recipe-picker .selection-list button').first().click();assert.ok(await page.getByText('Working directory needs a declaration.').isVisible());await page.getByRole('button',{name:'Confirm directory'}).click();await page.getByRole('button',{name:'Test plan →'}).click();assert.ok(await page.getByRole('dialog').isVisible());await page.keyboard.press('Escape');assert.equal(await page.getByRole('dialog').isVisible(),false);
  await nav(page,'runs');assert.ok(await page.locator('.run-selection button').count()>=8);await page.locator('#scenario').selectOption('manyRuns');assert.ok(await page.locator('.run-selection button').count()>=30);await shot(page,'desktop-runs-many');
  await page.locator('#scenario').selectOption('running');await page.locator('.run-selection button').filter({hasText:'pocket-id'}).click();assert.equal(await page.locator('[data-run-poller]').count(),1);await page.waitForFunction(()=>Number(document.querySelector('[data-run-poller]')?.dataset.pollCount)>0);await page.getByRole('button',{name:'Request cancellation'}).click();assert.ok(await page.getByRole('dialog').isVisible());await page.keyboard.press('Escape');await nav(page,'overview');assert.equal(await page.locator('[data-run-poller]').count(),0);
  await nav(page,'settings');await page.getByRole('button',{name:'General',exact:true}).click();await page.getByLabel('Theme').selectOption('dark');await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');await page.getByLabel('Language').selectOption('fr');await page.waitForFunction(()=>document.documentElement.lang==='fr');assert.equal(await page.locator('main h1').textContent(),'Paramètres');const persisted=await page.context().newPage();await persisted.goto(`${base}?view=settings&clean=1`);await persisted.waitForFunction(()=>document.documentElement.dataset.theme==='dark'&&document.documentElement.lang==='fr');await persisted.close();
  await fit(page,viewport,'theme-locale-persisted');assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors,viewport}=await open('overview','desktop');const collapse=page.getByRole('button',{name:'Collapse sidebar'});assert.equal(await page.locator('.sidebar nav svg').count(),6);assert.equal(await page.locator('.brand-mark svg').count(),1);await collapse.click();assert.ok(await page.locator('.shell.sidebar-collapsed').count());assert.equal(await page.locator('.sidebar nav button[aria-label]').count(),6);await page.reload();assert.ok(await page.locator('.shell.sidebar-collapsed').count());await page.getByRole('button',{name:'Expand sidebar'}).click();
  const menu=await open('overview','mobile');await menu.page.getByRole('button',{name:'Open navigation'}).click();await menu.page.keyboard.press('Escape');assert.equal(await menu.page.getByRole('button',{name:'Open navigation'}).getAttribute('aria-expanded'),'false');await menu.page.close();assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors}=await open('system');
  assert.ok(await page.getByText('Signing status').isVisible());
  await page.getByRole('button',{name:'Maintenance',exact:true}).click();assert.ok(await page.getByText('Clear execution history').isVisible());
  await page.getByRole('button',{name:'Developer',exact:true}).click();assert.ok(await page.getByText('Raw OpenAPI contract').isVisible());
  await nav(page,'settings');
  for(const [tab,label] of [['Repository','Public repository URL'],['GitHub','GitHub token'],['Authentication','Issuer URL'],['Notifications','ntfy server'],['Automation','Auto validate after successful build'],['Advanced','Failed workspaces to retain']]){
   await page.getByRole('button',{name:tab,exact:true}).last().click();assert.ok((await page.locator('.settings-content').textContent()).includes(label),`${tab} settings inventory`);
  }
  assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors}=await open('recipes','desktop','light','en','long');
  await page.locator('.recipe-picker .selection-list button').filter({hasText:'maintainerr'}).click();
  await page.locator('.recipe-levels button').nth(2).click();
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
 {
  const {page,errors}=await open('settings');
  const name=page.getByLabel(/Application name/);await name.fill('Local fixture');assert.ok(await page.getByText('Unsaved changes').isVisible());await page.getByRole('button',{name:'Cancel changes',exact:true}).click();assert.equal(await name.inputValue(),'DebBuilder');
  await name.fill('Local fixture');await page.getByRole('button',{name:'Save changes'}).click();assert.equal(await name.inputValue(),'Local fixture');assert.ok(await page.getByText('Saved',{exact:true}).isVisible());
  await page.getByRole('button',{name:'Repository',exact:true}).click();await page.getByLabel(/Public repository URL/).fill('bad url');await page.getByRole('button',{name:'Save changes'}).click();assert.ok(await page.getByText('Enter a valid HTTP or HTTPS URL').isVisible());await shot(page,'desktop-settings-validation-error');
  await page.getByRole('button',{name:'Cancel changes',exact:true}).click();await page.getByRole('button',{name:'Advanced',exact:true}).click();await page.getByLabel(/Failed workspaces to retain/).fill('1001');await page.getByRole('button',{name:'Save changes'}).click();assert.ok(await page.getByText('Enter a whole number from 0 to 1000.').isVisible());await page.getByRole('button',{name:'Cancel changes',exact:true}).click();
  await page.getByRole('button',{name:'Authentication',exact:true}).click();const secret=page.getByLabel(/Client secret/);assert.equal(await secret.inputValue(),'');assert.ok(await page.locator('.settings-content').getByText(/Configured/).count()>0);await secret.fill('new-fixture-secret');await page.getByRole('button',{name:'Save changes'}).click();assert.equal(await secret.inputValue(),'');assert.equal(await page.locator('input[value="new-fixture-secret"]').count(),0);assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors,viewport}=await open('recipes');
  const levels=page.locator('.recipe-levels');
  await shot(page,'desktop-recipes-plan-audited');
  await levels.getByRole('button',{name:'Customize'}).click();
  const nativeSelect=page.locator('.recipe-form-grid select').first();
  assert.equal(await nativeSelect.evaluate(node=>getComputedStyle(node).appearance),'none');
  assert.ok((await nativeSelect.evaluate(node=>getComputedStyle(node).backgroundImage)).includes('svg'));
  await nativeSelect.focus();assert.equal(await nativeSelect.evaluate(node=>document.activeElement===node),true);
  await levels.getByRole('button',{name:'Plan'}).click();
  assert.ok(await page.locator('.recipe-plan .inline-note').isVisible());
  assert.ok(await page.getByText('Working directory is not provisioned').isVisible());
  await levels.getByRole('button',{name:'Advanced'}).click();
  assert.equal(await page.getByText('Automatic ELF dependency detection').count(),0,'mapping-only Zoraxy hides ELF');
  assert.equal(await page.getByText('Build operations').count(),0,'prebuilt hides source-build controls');
  await shot(page,'desktop-recipes-advanced-editor');
  await levels.getByRole('button',{name:'Customize'}).click();
  assert.equal(await page.getByText('Build and output').count(),0);
  await page.locator('.recipe-picker .selection-list button').filter({hasText:'maintainerr'}).click();
  await levels.getByRole('button',{name:'Customize'}).click();
  assert.ok(await page.getByText('Build and output').isVisible());
  assert.ok(await page.getByRole('button',{name:'Configure service'}).isVisible());
  const commandList=page.locator('.recipe-list-editor').filter({hasText:'Build commands'});
  assert.equal(await commandList.locator('.recipe-list-row').count(),2);
  await commandList.getByRole('button',{name:'Move down 1'}).click();
  assert.equal(await commandList.locator('input').first().inputValue(),'npm run build');
  assert.ok(await page.getByText('Unsaved changes').isVisible());
  await page.getByRole('button',{name:'Cancel changes'}).click();
  assert.equal(await commandList.locator('input').first().inputValue(),'npm ci');
  await commandList.getByRole('button',{name:'Add row'}).click();
  await commandList.locator('input').last().fill('npm run test');
  await page.getByRole('button',{name:'Save changes'}).click();
  await levels.getByRole('button',{name:'Plan'}).click();
  assert.ok(await page.getByText('No matching', {exact:false}).count()===0);
  await page.locator('.recipe-picker .selection-list button').filter({hasText:'pocket-id'}).click();
  await levels.getByRole('button',{name:'Advanced'}).click();
  assert.ok(await page.getByText('Installed files owned by').isVisible());
  assert.ok(await page.getByText('Account provisioning').isVisible());
  await shot(page,'desktop-recipes-pocket-id-advanced');
  assert.ok(await page.getByText('Only package-specific /etc, /var/lib and /var/log paths. /opt runtime directories are not supported.').isVisible());
  await levels.getByRole('button',{name:'Customize'}).click();
  assert.ok(await page.getByText('systemd User').isVisible());
  await page.locator('.recipe-picker .selection-list button').filter({hasText:'seerr'}).click();
  await levels.getByRole('button',{name:'Plan'}).click();
  assert.ok(await page.getByText('postinst',{exact:false}).count()>0);
  await levels.getByRole('button',{name:'Expert'}).click();
  assert.equal(await page.locator('.recipe-hook-grid textarea').count(),4);
  await shot(page,'desktop-recipes-seerr-expert');
  assert.ok((await page.locator('.recipe-hook-grid textarea').nth(1).inputValue()).includes('install -d'));
  await page.locator('.recipe-picker .selection-list button').filter({hasText:'archive-agent'}).click();
  await levels.getByRole('button',{name:'Advanced'}).click();
  assert.ok(await page.getByText('Automatic ELF dependency detection').isVisible());
  assert.equal(await page.locator('.recipe-elf input[type=checkbox]').isChecked(),false);
  await page.locator('.recipe-elf input[type=checkbox]').check();
  await levels.getByRole('button',{name:'Plan'}).click();
  assert.ok(await page.getByText('Run evidence is stale').isVisible());
  assert.ok(await page.getByText('inspect copied payload at Build',{exact:false}).count()>0);
  await page.getByRole('button',{name:'Cancel changes'}).click();
  assert.ok(await page.locator('.recipe-plan .inline-note').isVisible());
  await levels.getByRole('button',{name:'Customize'}).click();
  await page.getByLabel('Description').fill('Changed fixture');
  await page.getByRole('button',{name:'Save changes'}).click();
  await levels.getByRole('button',{name:'Plan'}).click();
  assert.ok(await page.getByText('Run evidence is stale').isVisible());
  await page.getByRole('button',{name:'Test plan →'}).click();
  await page.keyboard.press('Escape');
  assert.ok(await page.locator('.recipe-plan .inline-note').isVisible());
  await page.getByRole('button',{name:'Create Recipe'}).click();
  assert.ok(await page.getByText('Test source').isVisible());
  await shot(page,'desktop-recipes-create-source');
  await levels.getByRole('button',{name:'Expert'}).click();
  assert.ok(await page.getByText('Lifecycle scripts — None').isVisible());
  assert.ok(await page.getByText('Raw Recipe fixture').isVisible());
  assert.ok((await page.locator('#recipe-json').inputValue()).includes('\"schema_version\": 5'));
  assert.equal(await page.locator('.capability-drawer').count(),0);
  await levels.getByRole('button',{name:'Customize'}).click();
  assert.equal(await page.getByText('Build and output').count(),0,'Create starts with source only');
  await page.getByLabel('GitHub repository').fill('example/new-recipe');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.ok(await page.getByText('Exact source and outputs remain unknown until Test or Build.').isVisible());
  await page.getByRole('button',{name:'Test plan →'}).click();await page.keyboard.press('Escape');
  assert.ok(await page.getByRole('button',{name:/Continue to Customize/}).isVisible());
  await shot(page,'desktop-recipes-create-plan');
  await page.getByRole('button',{name:/Continue to Customize/}).click();
  assert.ok(await page.getByText('Build and output').isVisible());
  await fit(page,viewport,'recipe-structured');assert.deepEqual(errors,[]);await page.close();
  const mobile=await open('recipes','mobile','dark','de');
  await mobile.page.locator('.recipe-picker .selection-list button').first().click();
  assert.ok(await mobile.page.getByRole('button',{name:/Zurück/}).count()>0);
  await mobile.page.locator('.recipe-levels button').nth(2).click();
  await fit(mobile.page,mobile.viewport,'recipe-mobile-de');
  await shot(mobile.page,'mobile-recipes-advanced-dark-de');
  await mobile.page.locator('.recipe-advanced-menu button').first().click();
  await fit(mobile.page,mobile.viewport,'recipe-mobile-de-source');
  assert.ok(await mobile.page.getByRole('button',{name:/Zurück zu Erweitert/}).isVisible());
  await shot(mobile.page,'mobile-recipes-source-dark-de');
  await mobile.page.getByRole('button',{name:/Zurück zu Erweitert/}).click();
  assert.ok(await mobile.page.locator('.recipe-advanced-menu').isVisible());
  await mobile.page.locator('.recipe-editor > .back-button').click();
  assert.ok(await mobile.page.locator('.recipe-picker').isVisible());
  assert.deepEqual(mobile.errors,[]);await mobile.page.close();
 }
 {
  const {page,errors}=await open('system');
  await page.getByRole('button',{name:/System-managed self-build/}).last().click();
  assert.ok(await page.getByText('Authorized operator overrides').isVisible());
  await shot(page,'desktop-system-managed-self-build');
  assert.equal(await page.getByText('Installed files owned by').count(),0);
  await page.getByLabel('Package maintainer').fill('Operator');
  assert.ok(await page.getByText('Unsaved changes').isVisible());
  await page.getByRole('button',{name:'Cancel changes'}).click();
  assert.equal(await page.getByLabel('Package maintainer').inputValue(),'DebBuilder maintainers');
  assert.deepEqual(errors,[]);await page.close();
 }
 {
  const {page,errors,viewport}=await open('runs','desktop','light','fr','manyRuns');const list=page.locator('.run-selection');assert.ok(await list.count());assert.equal(await page.locator('.run-filters label').count(),2);await page.locator('.run-filters input').fill('archive-agent');assert.equal(await list.locator('button').count(),1);await page.locator('.run-filters input').fill('');await page.locator('.run-filters select').selectOption('cancelled');assert.equal(await list.locator('button').count(),1);assert.ok(await list.locator('.status-chip.neutral').count());await page.locator('.run-filters select').selectOption('all');await list.evaluate(node=>node.scrollTop=node.scrollHeight);assert.ok(await list.evaluate(node=>node.scrollTop>0));assert.ok(await page.locator('.run-filters').isVisible());await fit(page,viewport,'runs-fr-many');assert.deepEqual(errors,[]);await page.close();
  const run=await open('runs','desktop','light','en','running');await run.page.locator('.log-options summary').click();await run.page.getByRole('combobox',{name:/Log detail/}).selectOption('raw');assert.equal(await run.page.getByRole('combobox',{name:/Log detail/}).inputValue(),'raw');await run.page.getByRole('button',{name:'Pause'}).click();assert.ok(await run.page.getByText('Live output paused').isVisible());await run.page.getByRole('button',{name:'Resume'}).click();assert.ok(await run.page.getByText('Following live output').isVisible());await shot(run.page,'desktop-runs-running');await run.page.close();
  const fail=await open('runs','desktop','light','en','failed');await fail.page.getByRole('button',{name:'View diagnosis'}).click();assert.equal(await fail.page.locator('main h1').textContent(),'Runs');assert.ok(await fail.page.getByText('Failure diagnosis').isVisible());assert.ok(await fail.page.getByText('libexample.so.1').count()>0);await shot(fail.page,'desktop-runs-failed-diagnosis');await fail.page.getByRole('button',{name:/Review Recipe/}).click();assert.equal(await fail.page.locator('main h1').textContent(),'Recipes');await fail.page.close();
 }
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
  assert.equal(await page.locator('.public-page').count(),1);assert.equal(await page.locator('.public-mark svg').count(),1);
  await fit(page,viewport,`public-${size}-${scheme}-${locale}`);
  assert.equal(await page.locator('.public-disclosures details[open]').count(),0);await page.locator('.public-disclosures details').nth(2).evaluate(node=>node.open=true);assert.equal(await page.getByRole('link',{name:/InRelease/}).getAttribute('href'),`${publicRepo.baseUrl}/dists/Luminous/InRelease`);
  assert.equal(await page.getByRole('link',{name:/Public signing key|Öffentlicher Signaturschlüssel|Clé de signature publique|Clave pública de firma/}).getAttribute('href'),`${publicRepo.baseUrl}/repository.gpg`);
  if(size==='desktop'&&locale==='en')publicColors[scheme]=await page.evaluate(()=>getComputedStyle(document.documentElement).backgroundColor);
  await page.locator('.public-disclosures details').nth(1).locator('summary').click();assert.equal(await page.locator('.public-disclosures .package-table .table-row').count(),publicRepo.packages.length);await page.locator('.public-disclosures details').nth(1).locator('summary').click();await page.locator('.public-disclosures details').nth(2).evaluate(node=>node.open=false);assert.ok(await page.locator('.install-panel .command-line').isVisible());if(captureName)await shot(page,captureName);if(size==='desktop'&&scheme==='light'&&locale==='en'){await page.locator('.public-disclosures details').first().locator('summary').click();await shot(page,'public-repository-manual-open');await page.locator('.public-disclosures details').first().locator('summary').click();}
  if(locale==='en'&&scheme==='light'){
   await page.getByRole('button',{name:'Copy Generated installer'}).click();
   assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),`curl -fsSL ${publicRepo.baseUrl}/install.sh | sudo bash`);
   await page.locator('.public-disclosures details').first().evaluate(node=>node.open=true);await page.getByRole('button',{name:'Copy Add repository'}).click();
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
