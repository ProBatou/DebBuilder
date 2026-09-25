import test from 'node:test';
import assert from 'node:assert/strict';
import {api, ApiError, request} from '../src/api/client.js';

const response = (status, payload, type = 'application/json') => ({ok:status >= 200 && status < 300,status,headers:{get:()=>type},json:async()=>payload,text:async()=>payload});

test('authenticated bootstrap preserves the backend auth mode and user', async () => {
  const value = await request('/api/auth/status',{fetchImpl:async(path, opts) => {
    assert.equal(path,'/api/auth/status'); assert.equal(opts.credentials,'same-origin'); assert.equal(opts.method,'GET');
    return response(200,{ok:true,auth_mode:'oidc',user:'operator'});
  }});
  assert.equal(value.user,'operator'); assert.equal(value.auth_mode,'oidc');
});

for (const [status,code] of [[400,'invalid_recipe_json'],[401,'authentication_required'],[403,'forbidden'],[404,'build_run_not_found'],[409,'execution_active'],[503,'settings_unavailable']]) {
  test(`preserves ${status} canonical error`, async () => {
    await assert.rejects(request('/api/executions/x',{fetchImpl:async()=>response(status,{error:{code,message:'Backend message',details:{path:'$.field',classification:'validation'}}})}), error => {
      assert.ok(error instanceof ApiError); assert.equal(error.status,status); assert.equal(error.code,code); assert.equal(error.message,'Backend message'); assert.equal(error.details.path,'$.field'); assert.equal(error.retryable,status >= 500); return true;
    });
  });
}

test('non-JSON success, network failure, abort and timeout remain distinct', async () => {
  assert.equal(await request('/api/x',{fetchImpl:async()=>response(200,'plain','text/plain')}),'plain');
  await assert.rejects(request('/api/x',{fetchImpl:async()=>{throw Error('offline');}}), {code:'network_unavailable'});
  const controller = new AbortController(); controller.abort();
  await assert.rejects(request('/api/x',{signal:controller.signal,fetchImpl:async(_path,{signal})=>{if(signal.aborted) throw new DOMException('aborted','AbortError');}}), {name:'AbortError'});
  await assert.rejects(request('/api/x',{timeout:1,fetchImpl:async(_path,{signal})=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))))}), {code:'request_timeout'});
});

test('GET route identity is escaped and log cursor is explicit', async () => {
  const original = globalThis.fetch; const paths=[];
  globalThis.fetch = async path => {paths.push(path); return response(200,{log:{text:'',offset:8}});};
  try {await api.logs('run/a','raw',8); assert.equal(paths[0],'/api/executions/run%2Fa/logs?verbosity=raw&after=8');}
  finally {globalThis.fetch=original;}
});
