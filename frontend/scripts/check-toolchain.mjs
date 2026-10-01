import {execFileSync} from 'node:child_process';

const node = process.versions.node;
const npm = execFileSync('npm', ['--version'], {encoding: 'utf8'}).trim();
if (!node.startsWith('24.') || !npm.startsWith('11.')) {
  console.error(`DEBBUILDER_DIAGNOSTIC_V1=${JSON.stringify({
    code: 'toolchain_requirement_mismatch',
    requirements: [
      {tool: 'Node', required: '24.x', detected: node},
      {tool: 'npm', required: '11.x', detected: npm},
    ],
  })}`);
  console.error(`DebBuilder frontend build requires Node 24.x and npm 11.x; found Node ${node} and npm ${npm}`);
  process.exit(1);
}
