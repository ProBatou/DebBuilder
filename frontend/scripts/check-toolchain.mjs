import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';

const MANIFEST_URL = new URL('../package.json', import.meta.url);
const VERSION = '(0|[1-9][0-9]*)';
const EXACT_VERSION = new RegExp(`^${VERSION}\\.${VERSION}\\.${VERSION}$`);
const COMPARATOR = new RegExp(`^(<=|>=|<|>|=)?${VERSION}(?:\\.${VERSION})?(?:\\.${VERSION})?$`);

export class ToolchainCheckError extends Error {}

function numericVersion(value, label, {partial = false} = {}) {
  const match = (partial ? COMPARATOR : EXACT_VERSION).exec(value);
  if (!match) {
    throw new ToolchainCheckError(`${label} uses an unsupported version expression: ${value}`);
  }
  const offset = partial ? 2 : 1;
  return [match[offset], match[offset + 1] ?? '0', match[offset + 2] ?? '0'].map(Number);
}

function compareVersions(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) return left[index] < right[index] ? -1 : 1;
  }
  return 0;
}

function parseRequirement(requirement) {
  const tokens = requirement.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) {
    throw new ToolchainCheckError('version requirement must not be empty');
  }
  return tokens.map(token => {
    const match = COMPARATOR.exec(token);
    if (!match) {
      throw new ToolchainCheckError(`unsupported version requirement: ${requirement}`);
    }
    return {
      operator: match[1] ?? '',
      version: numericVersion(token, 'version requirement', {partial: true}),
    };
  });
}

export function versionSatisfies(version, requirement) {
  const detected = numericVersion(version, 'detected tool version');
  const comparators = parseRequirement(requirement);
  return comparators.every(({operator, version: required}) => {
    const comparison = compareVersions(detected, required);
    return {'<': comparison < 0, '<=': comparison <= 0, '>': comparison > 0,
      '>=': comparison >= 0, '=': comparison === 0, '': comparison === 0}[operator];
  });
}

function requireEngine(engines, tool) {
  const requirement = engines?.[tool];
  if (typeof requirement !== 'string' || requirement.trim() === '') {
    throw new ToolchainCheckError(`package.json must declare engines.${tool}`);
  }
  const normalized = requirement.trim();
  // Parse once up front so unknown expressions are rejected even when the
  // currently executing version would otherwise appear compatible.
  versionSatisfies('0.0.0', normalized);
  return normalized;
}

export function loadToolchainRequirements(manifestUrl = MANIFEST_URL) {
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestUrl, 'utf8'));
  } catch (error) {
    throw new ToolchainCheckError(`frontend package.json could not be read as valid JSON: ${error.message}`);
  }
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new ToolchainCheckError('frontend package.json root must be an object');
  }
  if (!manifest.engines || typeof manifest.engines !== 'object' || Array.isArray(manifest.engines)) {
    throw new ToolchainCheckError('package.json must declare engines.node and engines.npm');
  }
  const node = requireEngine(manifest.engines, 'node');
  const npm = requireEngine(manifest.engines, 'npm');
  if (typeof manifest.packageManager !== 'string') {
    throw new ToolchainCheckError('package.json must declare an exact npm packageManager version');
  }
  const packageManager = /^npm@(.+)$/.exec(manifest.packageManager);
  if (!packageManager || !EXACT_VERSION.test(packageManager[1])) {
    throw new ToolchainCheckError('package.json packageManager must be npm with an exact numeric version');
  }
  const npmExact = packageManager[1];
  if (!versionSatisfies(npmExact, npm)) {
    throw new ToolchainCheckError('package.json packageManager version does not satisfy engines.npm');
  }
  return {node, npm, npmExact};
}

export function evaluateToolchain(requirements, nodeVersion, npmVersion) {
  // Validate detected output separately to turn malformed command output into
  // a controlled failure instead of emitting an unusable diagnostic.
  numericVersion(nodeVersion, 'detected Node version');
  numericVersion(npmVersion, 'detected npm version');
  const mismatches = [];
  if (!versionSatisfies(nodeVersion, requirements.node)) {
    mismatches.push({tool: 'Node', required: requirements.node, detected: nodeVersion});
  }
  if (!versionSatisfies(npmVersion, requirements.npm)) {
    mismatches.push({tool: 'npm', required: requirements.npm, detected: npmVersion});
  }
  if (npmVersion !== requirements.npmExact) {
    mismatches.push({tool: 'npm', required: requirements.npmExact, detected: npmVersion});
  }
  return mismatches;
}

function installedNpmVersion() {
  try {
    const version = execFileSync('npm', ['--version'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
    if (!version) throw new Error('npm --version returned no version');
    return version;
  } catch (error) {
    throw new ToolchainCheckError(`npm --version failed: ${error.message}`);
  }
}

export function main() {
  try {
    const requirements = loadToolchainRequirements();
    const nodeVersion = process.versions.node;
    const npmVersion = installedNpmVersion();
    const mismatches = evaluateToolchain(requirements, nodeVersion, npmVersion);
    if (mismatches.length === 0) return 0;
    console.error(`DEBBUILDER_DIAGNOSTIC_V1=${JSON.stringify({
      code: 'toolchain_requirement_mismatch',
      requirements: mismatches,
    })}`);
    console.error(`DebBuilder frontend toolchain is incompatible; found Node ${nodeVersion} and npm ${npmVersion}`);
    return 1;
  } catch (error) {
    const message = error instanceof ToolchainCheckError ? error.message : 'unexpected toolchain validation failure';
    console.error(`DebBuilder frontend toolchain check failed: ${message}`);
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
