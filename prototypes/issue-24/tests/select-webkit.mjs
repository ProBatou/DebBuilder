import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {webkit} from '@playwright/test';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const probe=createServer();
await new Promise(resolve=>probe.listen(0,'127.0.0.1',resolve));
const port=probe.address().port;
await new Promise(resolve=>probe.close(resolve));
const base=`http://127.0.0.1:${port}/?view=recipes&clean=1`;
const server=spawn(path.join(root,'node_modules/.bin/vite'),['preview','--host','127.0.0.1','--port',String(port),'--strictPort'],{cwd:root,stdio:'ignore'});
let browser;
try{
  for(let i=0;i<80;i++){try{if((await fetch(base)).ok)break;}catch{}await new Promise(resolve=>setTimeout(resolve,100));}
  browser=await webkit.launch({headless:true});
  for(const theme of ['light','dark']){
    const context=await browser.newContext({viewport:{width:1440,height:900}});
    await context.addInitScript(theme=>localStorage.setItem('debBuilder24Theme',theme),theme);
    const page=await context.newPage();
    await page.goto(base);
    await page.getByRole('button',{name:'Customize',exact:true}).click();
    const select=page.locator('.recipe-form-grid select').first();
    assert.equal(await select.evaluate(node=>getComputedStyle(node).appearance),'none');
    assert.ok((await select.evaluate(node=>getComputedStyle(node).backgroundImage)).includes('svg'));
    await select.focus();
    assert.equal(await select.evaluate(node=>document.activeElement===node),true);
    await page.keyboard.press('ArrowDown');
    assert.ok(await select.inputValue());
    await context.close();
  }
  console.log('WebKit select appearance, keyboard and Light/Dark checks passed.');
}finally{await browser?.close();server.kill();}
