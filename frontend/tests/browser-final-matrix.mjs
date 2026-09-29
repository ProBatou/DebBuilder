import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {t} from '../src/i18n/i18n.js';

// Run against an isolated showcase Behavior Lab through Vite.
const base = process.env.DEBBUILDER_FRONTEND_URL || 'http://127.0.0.1:5174';
const routes = [
  ['overview','overview','.overview-grid'],
  ['packages','packages/bashrc','.package-detail'],
  ['recipes','recipes/seerr','.recipe-identity'],
  ['runs','runs/ui-04-build-failed','.run-main'],
  ['system','system','.system-check-grid'],
  ['settings','settings','.settings-content .panel'],
];
const browser = await chromium.launch({headless:true});
let checked = 0;
try {
  for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
    for (const language of ['en','fr','de','es']) {
      for (const theme of ['system','light','dark']) {
        const page = await browser.newPage({viewport,colorScheme:'light',reducedMotion:'reduce'});
        const errors = [];
        page.on('pageerror',error => errors.push(error.message));
        await page.addInitScript(({language,theme}) => {
          localStorage.setItem('debBuilder24Locale',language);
          localStorage.setItem('debBuilder24Theme',theme);
        },{language,theme});
        for (const [name,route,ready] of routes) {
          await page.goto(`${base}/#/${route}`);
          await page.locator(ready).first().waitFor();
          assert.equal(await page.getByRole('heading',{level:1}).count(),1,`${name} h1`);
          assert.equal(await page.getByRole('heading',{level:1}).textContent(),t(name,language),`${name} heading in ${language}`);
          assert.equal(await page.locator('html').getAttribute('lang'),language);
          assert.equal(await page.locator('html').getAttribute('data-theme'),theme === 'dark' ? 'dark' : 'light');
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,`${name} overflow at ${viewport.width}px, ${theme}, ${language}`);
          if (name === 'recipes') {
            await page.getByRole('button',{name:t('customize',language),exact:true}).click();
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,`Recipe editing overflow at ${viewport.width}px, ${theme}, ${language}`);
          }
          checked++;
        }
        if (viewport.width === 390) {
          await page.getByRole('button',{name:t('menu',language)}).click();
          await page.locator('.sidebar.mobile-open').waitFor();
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,'mobile drawer overflow');
          await page.keyboard.press('Escape');
          await page.locator('.sidebar.mobile-open').waitFor({state:'hidden'});
        }
        assert.deepEqual(errors,[],`browser errors at ${viewport.width}px, ${theme}, ${language}`);
        await page.close();
      }
    }
  }
  console.log(`Desktop/mobile, System/Light/Dark, EN/FR/DE/ES matrix passed: ${checked} page states`);
} finally {await browser.close();}
