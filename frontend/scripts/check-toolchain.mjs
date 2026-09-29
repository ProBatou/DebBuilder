import {execFileSync} from 'node:child_process';

const node = process.versions.node;
const npm = execFileSync('npm', ['--version'], {encoding: 'utf8'}).trim();
if (!node.startsWith('24.') || !npm.startsWith('11.')) {
  console.error(`DebBuilder frontend build requires Node 24.x and npm 11.x; found Node ${node} and npm ${npm}`);
  process.exit(1);
}
