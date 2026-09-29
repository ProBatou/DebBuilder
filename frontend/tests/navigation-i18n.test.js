import test from 'node:test';
import assert from 'node:assert/strict';
import {parseLocation} from '../src/navigation/location.js';
import {t,when,number,statusLabel} from '../src/i18n/i18n.js';

test('exact Run and Package IDs survive hash navigation', () => {
  assert.deepEqual(parseLocation('#/runs/run%2F42'),{page:'runs',id:'run/42'});
  assert.deepEqual(parseLocation('#/packages/lib%2Bfoo'),{page:'packages',id:'lib+foo'});
  assert.equal(parseLocation('#/unknown').page,'overview');
  assert.deepEqual(parseLocation('#/recipes/%'),{page:'recipes',id:''});
});

test('all production locale catalogs contain translated navigation and Intl formats', () => {
  for (const language of ['en','fr','de','es']) {
    assert.notEqual(t('packages',language),'packages');
    assert.match(when('2026-09-25T12:00:00Z',language),/2026/);
    assert.match(when(1790337600,language),/2026/);
    assert.ok(number(1000,language));
    assert.equal(t('unknown-key',language),'unknown-key');
  }
  assert.equal(t('overview','xx'),t('overview','en'));
});

test('Recipe editor chrome has explicit French, German and Spanish translations', () => {
  for (const language of ['fr','de','es']) for (const key of [
    'edit','validate','cancel','reviewChanges','loadedBaseline','currentDraft',
    'discardChanges','keepEditing','add','remove','move','up','down','key','value',
  ]) assert.notEqual(t(key,language),t(key,'en'),`${language}.${key}`);
});

test('Recipe persistence chrome has explicit translations in every locale', () => {
  for (const language of ['fr','de','es']) for (const key of [
    'save','saving','saved','newRecipe','recipeId','githubRepository','continue',
    'recipeConflict','conflictPreserved','recipeAlreadyExists','latestServer',
    'changedBoth','changedLocally','changedServer','discardReload','changeIdentity',
    'revisionUnavailable',
  ]) assert.notEqual(t(key,language),t(key,'en'),`${language}.${key}`);
});

test('Recipe Run admission chrome has explicit translations in every locale', () => {
  for (const language of ['fr','de','es']) for (const key of [
    'test','buildAction','startingTest','startingBuild','testHelp',
    'testHelpPrebuilt','buildHelp','buildHelpPrebuilt','saveBeforeRun','enableBeforeRun',
    'testQueued','buildQueued','runNotStarted','admissionUnknown','viewRuns',
  ]) assert.notEqual(t(key,language),t(key,'en'),`${language}.${key}`);
});

test('operator action and active lifecycle labels are translated in every locale', () => {
  for (const language of ['fr','de','es']) for (const state of [
    'ready_to_validate','publication_available','validation_cancelled',
    'building','validating','publishing','build_required','recipe_missing',
  ]) assert.notEqual(statusLabel(state,language),statusLabel(state,'en'),`${language}.${state}`);
});
