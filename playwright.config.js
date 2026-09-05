const {defineConfig} = require('@playwright/test');

const baseURL = process.env.DEBBUILDER_UI_BASE_URL;
if (!baseURL) throw new Error('DEBBUILDER_UI_BASE_URL must be set by tests/ui/run-ui-tests.js');

module.exports = defineConfig({
  testDir: './tests/ui',
  testMatch: 'debbuilder.spec.js',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: {timeout: 8_000},
  outputDir: '.ui-artifacts/test-results',
  reporter: [
    ['line'],
    ['html', {outputFolder: '.ui-artifacts/report', open: 'never'}],
  ],
  use: {
    baseURL,
    browserName: 'chromium',
    locale: 'en-US',
    timezoneId: 'Europe/Paris',
    colorScheme: 'dark',
    reducedMotion: 'reduce',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'python3 server.py',
    url: `${baseURL}/api/status`,
    reuseExistingServer: false,
    timeout: 20_000,
    env: {
      ...process.env,
      DEBBUILDER_DATA_DIR: process.env.DEBBUILDER_DATA_DIR,
      DEBBUILDER_REPO_ROOT: process.env.DEBBUILDER_REPO_ROOT,
      DEBBUILDER_REPO_URL: 'https://repo.example.invalid/ui-showcase',
      DEBBUILDER_HOST: '127.0.0.1',
      DEBBUILDER_PORT: process.env.DEBBUILDER_PORT,
      DEBBUILDER_AUTH_MODE: 'none',
      DEBBUILDER_COOKIE_SECRET: '',
      DEBBUILDER_OIDC_CLIENT_SECRET: '',
      DEBBUILDER_GITHUB_TOKEN: '',
      GITHUB_TOKEN: '',
      DEBBUILDER_NTFY_TOKEN: '',
    },
  },
  projects: [
    {name: 'desktop', use: {viewport: {width: 1440, height: 1000}}},
    {name: 'mobile', use: {viewport: {width: 390, height: 844}, isMobile: true, hasTouch: true}},
  ],
});
