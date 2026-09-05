const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const {spawn, spawnSync} = require('child_process');

const root = path.resolve(__dirname, '..', '..');
const artifacts = path.join(root, '.ui-artifacts');
const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'debbuilder-ui-'));
const dataDir = path.join(runtimeRoot, 'data');
const repoRoot = path.join(runtimeRoot, 'repository');

function reservePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.unref();
    socket.on('error', reject);
    socket.listen(0, '127.0.0.1', () => {
      const port = socket.address().port;
      socket.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function main() {
  fs.rmSync(artifacts, {recursive: true, force: true});
  fs.mkdirSync(artifacts, {recursive: true});
  const seeded = spawnSync('python3', ['-m', 'tests.ui.showcase', '--data-dir', dataDir, '--repo-root', repoRoot], {
    cwd: root,
    encoding: 'utf8',
  });
  if (seeded.stdout) process.stdout.write(seeded.stdout);
  if (seeded.stderr) process.stderr.write(seeded.stderr);
  if (seeded.status !== 0) process.exitCode = seeded.status || 1;
  if (process.exitCode) return;

  const port = await reservePort();
  const env = {
    ...process.env,
    DEBBUILDER_DATA_DIR: dataDir,
    DEBBUILDER_REPO_ROOT: repoRoot,
    DEBBUILDER_HOST: '127.0.0.1',
    DEBBUILDER_PORT: String(port),
    DEBBUILDER_AUTH_MODE: 'none',
    DEBBUILDER_UI_BASE_URL: `http://127.0.0.1:${port}`,
  };
  const playwright = path.join(root, 'node_modules', '@playwright', 'test', 'cli.js');
  const child = spawn(process.execPath, [playwright, 'test', '--config', 'playwright.config.js', ...process.argv.slice(2)], {
    cwd: root,
    env,
    stdio: 'inherit',
  });
  const forwardSignal = signal => {
    if (!child.killed) child.kill(signal);
  };
  process.once('SIGINT', () => forwardSignal('SIGINT'));
  process.once('SIGTERM', () => forwardSignal('SIGTERM'));
  const code = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', value => resolve(value ?? 1));
  });
  process.exitCode = code;
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
}).finally(() => {
  fs.rmSync(runtimeRoot, {recursive: true, force: true});
});
