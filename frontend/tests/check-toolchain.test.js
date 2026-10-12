import assert from 'node:assert/strict';
import {chmodSync, mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

import {
  ToolchainCheckError,
  evaluateToolchain,
  loadToolchainRequirements,
} from '../scripts/check-toolchain.mjs';

const requirements = {node: '>=24 <25', npm: '>=11 <12', npmExact: '11.20.0'};
const script = resolve(dirname(fileURLToPath(import.meta.url)), '../scripts/check-toolchain.mjs');

function manifest(document) {
  const directory = mkdtempSync(join(tmpdir(), 'debbuilder-toolchain-test-'));
  const path = join(directory, 'package.json');
  writeFileSync(path, typeof document === 'string' ? document : JSON.stringify(document));
  return {path, cleanup: () => rmSync(directory, {recursive: true, force: true})};
}

function validManifest(overrides = {}) {
  return {
    packageManager: 'npm@11.20.0',
    engines: {node: '>=24 <25', npm: '>=11 <12'},
    ...overrides,
  };
}

function runWithTemporaryNpm({version = '11.20.0', exitCode = 0, cwd} = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'debbuilder-toolchain-npm-'));
  const executable = join(directory, 'npm');
  const command = exitCode === 0
    ? `printf '%s\\n' '${version}'`
    : `printf '%s\\n' 'simulated npm failure' >&2\nexit ${exitCode}`;
  writeFileSync(executable, `#!/bin/sh\n${command}\n`);
  chmodSync(executable, 0o700);
  try {
    return spawnSync(process.execPath, [script], {
      cwd,
      encoding: 'utf8',
      env: {...process.env, PATH: directory},
    });
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

function structuredDiagnostic(result) {
  const prefix = 'DEBBUILDER_DIAGNOSTIC_V1=';
  const line = result.stderr.split(/\r?\n/).find(message => message.startsWith(prefix));
  return line ? JSON.parse(line.slice(prefix.length)) : null;
}

test('accepts compatible Node and the exact compatible npm identity', () => {
  assert.deepEqual(evaluateToolchain(requirements, '24.21.0', '11.20.0'), []);
});

test('reports an incompatible Node version with the manifest requirement', () => {
  assert.deepEqual(evaluateToolchain(requirements, '23.9.0', '11.20.0'), [
    {tool: 'Node', required: '>=24 <25', detected: '23.9.0'},
  ]);
});

test('direct execution preserves the structured diagnostic V1 contract for incompatibilities', () => {
  const result = runWithTemporaryNpm({version: '11.19.0'});
  assert.equal(result.status, 1);
  assert.deepEqual(structuredDiagnostic(result), {
    code: 'toolchain_requirement_mismatch',
    requirements: [{tool: 'npm', required: '11.20.0', detected: '11.19.0'}],
  });
});

test('rejects npm in the same major when it is not the packageManager identity', () => {
  assert.deepEqual(evaluateToolchain(requirements, '24.21.0', '11.19.0'), [
    {tool: 'npm', required: '11.20.0', detected: '11.19.0'},
  ]);
});

test('reports both the npm engine range and exact identity when npm is outside the range', () => {
  assert.deepEqual(evaluateToolchain(requirements, '24.21.0', '10.9.0'), [
    {tool: 'npm', required: '>=11 <12', detected: '10.9.0'},
    {tool: 'npm', required: '11.20.0', detected: '10.9.0'},
  ]);
  const result = runWithTemporaryNpm({version: '10.9.0'});
  assert.equal(result.status, 1);
  assert.deepEqual(structuredDiagnostic(result), {
    code: 'toolchain_requirement_mismatch',
    requirements: [
      {tool: 'npm', required: '>=11 <12', detected: '10.9.0'},
      {tool: 'npm', required: '11.20.0', detected: '10.9.0'},
    ],
  });
});

test('loads requirements from package.json and rejects an invalid packageManager', () => {
  const good = manifest(validManifest());
  const bad = manifest(validManifest({packageManager: 'npm@11'}));
  try {
    assert.deepEqual(loadToolchainRequirements(good.path), requirements);
    assert.throws(() => loadToolchainRequirements(bad.path), ToolchainCheckError);
  } finally {
    good.cleanup();
    bad.cleanup();
  }
});

test('validates every engine requirement token before evaluating compatibility', () => {
  const valid = manifest(validManifest());
  const invalid = [
    'definitely-invalid',
    '>=25 definitely-invalid',
    '>=0 definitely-invalid',
    'definitely-invalid >=24',
    '>=24 <',
  ].map(node => manifest(validManifest({engines: {node, npm: '>=11 <12'}})));
  try {
    assert.deepEqual(loadToolchainRequirements(valid.path), requirements);
    for (const fixture of invalid) {
      assert.throws(
        () => loadToolchainRequirements(fixture.path),
        error => error instanceof ToolchainCheckError && /unsupported version requirement/.test(error.message),
      );
    }
  } finally {
    valid.cleanup();
    for (const fixture of invalid) fixture.cleanup();
  }
});

test('rejects missing and unsupported engine requirements', () => {
  const missing = manifest(validManifest({engines: {node: '>=24 <25'}}));
  const unsupported = manifest(validManifest({engines: {node: '^24.0.0', npm: '>=11 <12'}}));
  try {
    assert.throws(() => loadToolchainRequirements(missing.path), /engines\.npm/);
    assert.throws(() => loadToolchainRequirements(unsupported.path), /unsupported version requirement/);
  } finally {
    missing.cleanup();
    unsupported.cleanup();
  }
});

test('rejects malformed JSON and contradictory packageManager requirements', () => {
  const malformed = manifest('{"engines":');
  const contradictory = manifest(validManifest({engines: {node: '>=24 <25', npm: '>=12 <13'}}));
  try {
    assert.throws(() => loadToolchainRequirements(malformed.path), /valid JSON/);
    assert.throws(() => loadToolchainRequirements(contradictory.path), /does not satisfy engines\.npm/);
  } finally {
    malformed.cleanup();
    contradictory.cleanup();
  }
});

test('direct execution resolves package.json independently of cwd', () => {
  const cwd = mkdtempSync(join(tmpdir(), 'debbuilder-toolchain-cwd-'));
  try {
    const result = runWithTemporaryNpm({cwd});
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stderr, '');
  } finally {
    rmSync(cwd, {recursive: true, force: true});
  }
});

test('npm --version non-zero exit is a controlled failure without a V1 mismatch', () => {
  const result = runWithTemporaryNpm({exitCode: 7});
  assert.equal(result.status, 1);
  assert.match(result.stderr, /npm --version failed/);
  assert.doesNotMatch(result.stderr, /\n\s+at /);
  assert.equal(structuredDiagnostic(result), null);
});

test('missing npm is a controlled failure without a Node stack trace', () => {
  const emptyPath = mkdtempSync(join(tmpdir(), 'debbuilder-toolchain-path-'));
  try {
    const result = spawnSync(process.execPath, [script], {
      encoding: 'utf8',
      env: {...process.env, PATH: emptyPath},
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /npm --version failed/);
    assert.doesNotMatch(result.stderr, /\n\s+at /);
    assert.equal(structuredDiagnostic(result), null);
  } finally {
    rmSync(emptyPath, {recursive: true, force: true});
  }
});
