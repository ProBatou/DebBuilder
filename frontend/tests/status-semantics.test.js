import assert from 'node:assert/strict';
import test from 'node:test';
import {statusSemantics, operatorStatusSemantics} from '../src/components/statusSemantics.js';

test('status icons reserve the checkmark for completed success', () => {
  for (const state of ['failed','publication_failed','validation_failed','blocked'])
    assert.deepEqual(statusSemantics(state),{tone:'danger',icon:'!'});
  for (const state of ['validation_needed','ready_to_validate','ready_to_publish','publication_available','update_available','warning'])
    assert.equal(statusSemantics(state).tone,'warning');
  for (const state of ['running','cancelling'])
    assert.deepEqual(statusSemantics(state),{tone:'info',icon:'◌'});
  for (const state of ['prepared','ok','healthy','available','up_to_date'])
    assert.deepEqual(statusSemantics(state),{tone:'ready',icon:'●'});
  for (const state of ['success','validated','published','completed'])
    assert.deepEqual(statusSemantics(state),{tone:'success',icon:'✓'});
  for (const state of ['cancelled','skipped','disabled','unknown'])
    assert.deepEqual(statusSemantics(state),{tone:'neutral',icon:'●'});
});

test('operator lifecycle statuses retain the same action and completion meaning', () => {
  for (const state of ['validation_needed','ready_to_validate','ready_to_publish','publication_available'])
    assert.deepEqual(operatorStatusSemantics(state), statusSemantics(state));
  for (const state of ['building','validating','publishing'])
    assert.deepEqual(operatorStatusSemantics(state), {tone:'info',icon:'◌'});
  assert.deepEqual(operatorStatusSemantics('up_to_date'), {tone:'success',icon:'●'});
});
