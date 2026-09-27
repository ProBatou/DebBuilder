import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRevision, loadRecipe, saveExistingRecipe, createRecipe} from '../src/features/recipes/persistence.js';

const revision = 'a'.repeat(64);
const canonical = {schema_version:5,name:'example'};
const response = (body, etag = `"${revision}"`) => new Response(JSON.stringify(body), {status:200,headers:{'Content-Type':'application/json','ETag':etag}});

test('revision parser fails closed on weak, malformed, and absent ETags', () => {
  assert.equal(parseRevision(`"${revision}"`),revision);
  for (const value of [null, revision, `W/"${revision}"`, '"ABC"', '"' + 'A'.repeat(64) + '"'])
    assert.throws(() => parseRevision(value),/revision unavailable/);
});

test('loaded and saved Recipes carry server revisions and guarded request bodies', async () => {
  const calls = [];
  const fetchImpl = async (path, opts) => {
    calls.push({path,method:opts.method,body:opts.body && JSON.parse(opts.body)});
    return response(path === '/api/workflows/example' && opts.method === 'GET' ? canonical : {recipe:canonical});
  };
  assert.deepEqual(await loadRecipe('example',{fetchImpl}),{recipe:canonical,revision});
  assert.deepEqual(await saveExistingRecipe('example',canonical,revision,{fetchImpl}),{recipe:canonical,revision});
  assert.deepEqual(await createRecipe('example',canonical,{fetchImpl}),{recipe:canonical,revision});
  assert.deepEqual(calls.map(call => call.body),[undefined,{workflow:canonical,expected_revision:revision},{workflow:canonical,create_only:true}]);
  await assert.rejects(saveExistingRecipe('example',canonical,'bad',{fetchImpl}),/revision unavailable/);
  assert.equal(calls.length,3);
});
