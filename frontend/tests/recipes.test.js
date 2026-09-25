import assert from 'node:assert/strict';
import test from 'node:test';
import {isManaged, recipePlan} from '../src/features/recipes/model.js';

test('managed identity depends on canonical metadata', () => {
  assert.equal(isManaged({name:'debbuilder'}), false);
  assert.equal(isManaged({management:{owner:'application',builtin_id:'debbuilder'}}), true);
});

test('Plan keeps authored values configured and does not invent Run evidence', () => {
  const plan = recipePlan({source:{repository:'example/app',tracking:'manual'},artifact:{mode:'source_build'},build:{commands:['make'],output:{mode:'single',path:'out'}},package:{name:'app',architecture:'amd64',runtime_dependency_detection:{enabled:false,overrides:[{soname:'libx.so'}]}},install:{maintainer_scripts:{postinst:'true'}},service:{enabled:false}});
  assert.equal(plan.mode,'source_build');
  assert.equal(plan.summary.find(([name]) => name === 'Source')[2],'Configured');
  assert.deepEqual(plan.hooks,['postinst']);
  assert.equal(plan.sections.some(([name]) => name === 'Service'),false);
  assert.equal(plan.sections.some(([name]) => name === 'Build'),true);
});

test('ELF policy appears only for eligible Release archives while existing overrides remain in canonical data', () => {
  const base = {artifact:{mode:'upstream_archive',archive_source:'release_asset'},package:{architecture:'amd64',runtime_dependency_detection:{enabled:false,overrides:[{soname:'libx.so',action:'ignore'}]}},install:{},service:{enabled:false}};
  const eligible = recipePlan(base);
  assert.equal(eligible.elfEligible,true);
  assert.equal(eligible.summary.find(([name]) => name === 'ELF detection')[1],'Disabled');
  const ineligible = recipePlan({...base,artifact:{mode:'upstream_deb'}});
  assert.equal(ineligible.elfEligible,false);
  assert.equal(ineligible.summary.some(([name]) => name === 'ELF detection'),false);
  assert.equal(ineligible.pkg.runtime_dependency_detection.overrides.length,1);
});
