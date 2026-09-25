import assert from 'node:assert/strict';
import {readFile,readdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {catalogs,translate,formatCount,formatWhen,countMessage} from '../src/lib/i18n.js';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
const englishKeys=Object.keys(catalogs.en).sort();
assert.ok(englishKeys.length>=300,'English source catalog should cover the UI');
const placeholders=value=>[...value.matchAll(/\{([a-zA-Z][a-zA-Z0-9_]*)\}/g)].map(match=>match[1]).sort();
for(const locale of ['en','fr','de','es']){
 const raw=await readFile(path.join(root,'locales',`${locale}.json`),'utf8');
 const keys=[...raw.matchAll(/^  "([^"]+)":/gm)].map(match=>match[1]);
 assert.equal(keys.length,new Set(keys).size,`${locale} has duplicate keys`);
 assert.deepEqual(Object.keys(catalogs[locale]).sort(),englishKeys,`${locale} does not cover English`);
 for(const key of englishKeys){assert.ok(catalogs[locale][key]?.trim(),`${locale}:${key} is empty`);assert.deepEqual(placeholders(catalogs[locale][key]),placeholders(catalogs.en[key]),`${locale}:${key} placeholders differ`);}
}
assert.equal(translate('de','missing.key'),'missing.key');
assert.equal(translate('de','nav.overview'),catalogs.de['nav.overview']);
assert.equal(translate('zz','nav.overview'),catalogs.en['nav.overview']);
const savedFrench=catalogs.fr['nav.overview'];delete catalogs.fr['nav.overview'];
assert.equal(translate('fr','nav.overview'),catalogs.en['nav.overview']);
catalogs.fr['nav.overview']=savedFrench;
assert.equal(translate('fr','overview.activeBuild',{name:'pocket-id'}).includes('pocket-id'),true);
assert.ok(countMessage('en','common.count.one','common.count.other',1).includes('1 item'));
assert.ok(countMessage('en','common.count.one','common.count.other',2).includes('2 items'));
assert.equal(formatCount('de',1200),'1.200');
assert.ok(formatWhen('fr','2026-09-25T09:42:00Z').length>8);
const technical=['pocket-id','libssl3','/opt/zoraxy','publication_proof_invalid'];
for(const value of technical)assert.equal(translate('fr','overview.activeBuild',{name:value}).includes(value),true);
const allow=new Set(['libexample.so.1','DEPENDENCY_UNRESOLVED','· archive-agent 5.0.0-3','[09:42:04] unresolved: libexample.so.1','DebBuilder','DebBuilder Repository','/etc/apt/sources.list.d/debbuilder.sources','stable · amd64','Luminous · amd64','Luminous · main · amd64','Luminous','v1.0.0','stable · main · amd64','stable','main','amd64','amd64 · all','repository.gpg · InRelease','InRelease','v3.1.4 · zoraxy_linux_amd64','libc6, libssl3','ca-certificates','libc6, libssl3, ca-certificates','debbuilder · 1.0.0']);
const components=['App.svelte','PublicRepository.svelte',...(await readdir(path.join(root,'pages'))).filter(x=>x.endsWith('.svelte')).map(x=>'pages/'+x),...(await readdir(path.join(root,'lib'))).filter(x=>x.endsWith('.svelte')).map(x=>'lib/'+x)];
for(const name of components){let source=await readFile(path.join(root,name),'utf8');source=source.replace(/<script[\s\S]*?<\/script>/g,'').replace(/<style[\s\S]*?<\/style>/g,'');const literal=[...source.matchAll(/>([^<>{}]+)</g)].map(match=>match[1].trim()).filter(value=>/[A-Za-z]/.test(value));for(const value of literal)assert.ok(allow.has(value),`${name}: untranslated literal ${JSON.stringify(value)}`);}
for(const name of components){let source=await readFile(path.join(root,name),'utf8');source=source.replace(/<script[\s\S]*?<\/script>/g,'').replace(/<style[\s\S]*?<\/style>/g,'');for(const match of source.matchAll(/(?:aria-label|title|placeholder)="([^"{}]+)"/g)){assert.ok(allow.has(match[1]),`${name}: untranslated attribute ${JSON.stringify(match[1])}`);}}
console.log(`i18n checks passed: ${englishKeys.length} keys × 4 locales, fallback, placeholders, plural, Intl, technical values, component literal scan.`);
