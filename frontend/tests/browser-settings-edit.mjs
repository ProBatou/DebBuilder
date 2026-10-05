import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';

const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5182';
let settings = {
  general:{app_name:'DebBuilder',url:'https://app.example.test'},
  apt:{repository:'https://repo.example.test',distribution:'stable',component:'main',architecture:'amd64'},
  github:{token_configured:true},
  security:{auth_mode:'none',oidc_issuer:'',oidc_client_id:'',oidc_redirect_uri:'',oidc_client_secret_configured:false},
  notifications:{type:'none',server_url:'https://ntfy.sh',topic:'debbuilder',token_configured:true},
  automation:{auto_validate_after_successful_build:false,auto_publish_after_successful_validation:false,upstream_checks_enabled:true,upstream_check_interval_seconds:3600,upstream_check_concurrency:4},
  workspace_cleanup:{enabled:true,failed_workspaces_to_retain:5,pressure_minimum_free_bytes:536870912,pressure_minimum_free_percent:10,pressure_target_free_bytes:1073741824,pressure_target_free_percent:15},
  resource_limits:{memory_max_bytes:null,tasks_max:null,cpu_quota_percent:null,io_read_bandwidth_max_bytes_per_sec:null,io_write_bandwidth_max_bytes_per_sec:null},
};
const writes = [];
let failNext = false;
const browser = await chromium.launch({headless:true});
try {
  const page = await browser.newPage({viewport:{width:1440,height:900}});
  const pageErrors = [];
  page.on('pageerror',error => pageErrors.push(error.message));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const respond = (body,status=200) => route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if (path === '/api/auth/status') return respond({ok:true,auth_mode:'none'});
    if (path === '/api/status') return respond({ok:true,suite_default:'stable',component_default:'main',arch_default:'amd64'});
    if (path === '/api/settings' && request.method() === 'GET') return respond({settings});
    if (path === '/api/settings' && request.method() === 'POST') {
      const body = request.postDataJSON();
      writes.push(body);
      if (failNext) {failNext = false; return respond({error:{code:'invalid_settings_field',message:'Invalid value',path:'$.general.app_name'}},400);}
      settings = {...settings,...Object.fromEntries(Object.entries(body).map(([key,value]) => [key,{...settings[key],...Object.fromEntries(Object.entries(value).filter(([field]) => !['token','oidc_client_secret'].includes(field)))}]))};
      if (body.github?.token) settings.github.token_configured = true;
      if (body.notifications?.token) settings.notifications.token_configured = true;
      if (body.security?.oidc_client_secret) settings.security.oidc_client_secret_configured = true;
      return respond({ok:true,settings});
    }
    return respond({error:{code:'unexpected_request',message:path}},404);
  });
  await page.goto(`${base}/#/settings`);
  await page.getByLabel('Application name').waitFor();
  if (process.env.DEBBUILDER_SETTINGS_CAPTURE) await page.screenshot({path:process.env.DEBBUILDER_SETTINGS_CAPTURE,fullPage:true});
  await page.getByLabel('Application name').fill('Renamed DebBuilder');
  await page.getByRole('button',{name:'Repository',exact:true}).click();
  assert.equal(await page.getByLabel('Public repository URL').inputValue(),'https://repo.example.test');
  assert.deepEqual(writes,[]);
  await page.getByRole('button',{name:'General',exact:false}).click();
  assert.equal(await page.getByLabel('Application name').inputValue(),'Renamed DebBuilder');
  failNext = true;
  await page.getByRole('button',{name:'Save changes'}).click();
  await page.getByRole('alert').getByText('Invalid value').waitFor();
  assert.equal(await page.getByLabel('Application name').inputValue(),'Renamed DebBuilder');
  assert.deepEqual(writes[0],{general:{app_name:'Renamed DebBuilder',url:'https://app.example.test'}});
  await page.getByRole('button',{name:'Save changes'}).click();
  await page.getByRole('status').getByText('Changes saved').waitFor();
  assert.deepEqual(writes[1],writes[0]);
  await page.getByRole('button',{name:'Repository',exact:true}).click();
  await page.getByLabel('Distribution').fill('testing');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.deepEqual(Object.keys(writes[2]),['apt']);
  assert.equal(writes[2].apt.distribution,'testing');
  await page.getByRole('button',{name:'GitHub',exact:true}).click();
  assert.equal(await page.getByText('Configured.',{exact:false}).count() > 0,true);
  await page.getByLabel('GitHub token').fill('replacement-token');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.deepEqual(writes[3],{github:{token:'replacement-token'}});
  assert.equal(await page.evaluate(() => JSON.stringify({...localStorage,...sessionStorage}).includes('replacement-token')),false);
  assert.equal(await page.getByLabel('GitHub token').inputValue(),'');
  await page.getByRole('button',{name:'Authentication',exact:true}).click();
  await page.getByLabel('Authentication mode').selectOption('oidc');
  await page.getByLabel('Issuer URL').fill('https://identity.example.test');
  await page.getByLabel('Client ID').fill('debbuilder');
  await page.getByLabel('Redirect URI').fill('https://app.example.test/auth/callback');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.equal(writes.length,4);
  await page.getByRole('button',{name:'Discard changes'}).click();
  await page.getByLabel('Authentication mode').selectOption('header');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.equal(writes[4].security.auth_mode,'header');
  assert.equal('oidc_client_secret' in writes[4].security,false);
  await page.getByRole('button',{name:'Notifications',exact:true}).click();
  await page.getByLabel('Type').selectOption('ntfy');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.equal(writes[5].notifications.type,'ntfy');
  assert.equal('token' in writes[5].notifications,false);
  await page.getByRole('button',{name:'Automation',exact:true}).click();
  await page.getByLabel('Auto publish after validation').check();
  assert.equal(await page.getByLabel('Auto validate after build').isChecked(),true);
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.equal(writes[6].automation.auto_publish_after_successful_validation,true);
  assert.equal(writes[6].automation.auto_validate_after_successful_build,true);
  await page.getByRole('button',{name:'Advanced',exact:true}).click();
  assert.equal(await page.getByLabel('Pressure minimum free (%)').inputValue(),'10');
  await page.getByLabel('Pressure minimum free bytes').fill('9007199254740990');
  await page.getByLabel('Pressure target free bytes').fill('9007199254740991');
  await page.getByLabel('Pressure target free (%)').fill('18');
  await page.getByLabel('Memory limit (bytes)').fill('1048576');
  await page.getByRole('button',{name:'Save changes'}).click();
  assert.deepEqual(Object.keys(writes[7]),['workspace_cleanup','resource_limits']);
  assert.equal(writes[7].resource_limits.memory_max_bytes,1048576);
  assert.equal(writes[7].resource_limits.tasks_max,null);
  assert.equal(writes[7].workspace_cleanup.pressure_minimum_free_bytes,9007199254740990);
  assert.equal(writes[7].workspace_cleanup.pressure_target_free_bytes,Number.MAX_SAFE_INTEGER);
  assert.equal(writes[7].workspace_cleanup.pressure_target_free_percent,18);
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/#/settings`);
  await page.getByRole('button',{name:'General',exact:true}).click();
  await page.getByLabel('Application name').waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  await page.getByRole('button',{name:'Repository',exact:true}).click();
  assert.equal(await page.getByLabel('Distribution').inputValue(),'testing');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false);
  assert.deepEqual(pageErrors,[]);
  console.log('Settings editing checks passed');
} finally {await browser.close();}
