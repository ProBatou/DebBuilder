import test from 'node:test';
import assert from 'node:assert/strict';
import {parseLocation} from '../src/navigation/location.js';
import {t,when,number} from '../src/i18n/i18n.js';

test('exact Run and Package IDs survive hash navigation', () => {
  assert.deepEqual(parseLocation('#/runs/run%2F42'),{page:'runs',id:'run/42'});
  assert.deepEqual(parseLocation('#/packages/lib%2Bfoo'),{page:'packages',id:'lib+foo'});
  assert.equal(parseLocation('#/unknown').page,'overview');
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
