import assert from 'node:assert/strict';
import {cached,remember,invalidateForMutation} from '../src/features/sessionCache.js';

for (const key of ['runs','overview','packages','system','recipes','settings','runs:deleted']) remember(key,{old:true});
invalidateForMutation('/api/executions/delete-logs');
for (const key of ['runs','overview','packages','system','runs:deleted']) assert.equal(cached(key),undefined);
assert.deepEqual(cached('recipes'),{old:true});
invalidateForMutation('/api/workflows/example');
assert.equal(cached('recipes'),undefined);
remember('recipes',{saved:true});
invalidateForMutation('/api/settings');
assert.equal(cached('settings'),undefined);
assert.deepEqual(cached('recipes'),{saved:true});
for (const path of ['/api/executions/run-1/cancel','/api/executions/run-1/validate','/api/executions/run-1/publish']) {
  for (const key of ['runs','overview','packages','system','runs:run-1']) remember(key,{old:true});
  invalidateForMutation(path);
  for (const key of ['runs','overview','packages','system','runs:run-1']) assert.equal(cached(key),undefined,`${path} retained ${key}`);
}
console.log('Session cache invalidation checks passed');
