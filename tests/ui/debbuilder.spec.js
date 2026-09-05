const path = require('path');
const {test, expect} = require('@playwright/test');

const artifactRoot = path.resolve(__dirname, '..', '..', '.ui-artifacts');

function artifactPath(testInfo, name) {
  return path.join(artifactRoot, testInfo.project.name, `${name}.png`);
}

async function openView(page, name) {
  if (await page.locator('#btnMobileMenu').isVisible()) {
    await page.locator('#btnMobileMenu').click();
  }
  await page.locator(`.sidebar .nav-link[data-view="${name}"]`).click();
  await expect(page.locator(`#view-${name}`)).toHaveClass(/active/);
}

async function expectNoHorizontalOverflow(page) {
  const overflow = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll('button, input, select, textarea, table, .card')]
      .filter(node => {
        if (!node.closest('.view.active, dialog[open], .mobile-topbar') || node.closest('[aria-hidden="true"]')) return false;
        const style = getComputedStyle(node);
        const rect = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0
          && (rect.left < -1 || rect.right > window.innerWidth + 1);
      })
      .slice(0, 8)
      .map(node => ({tag: node.tagName, id: node.id, className: node.className, rect: node.getBoundingClientRect().toJSON()})),
  }));
  expect(overflow.document, JSON.stringify(overflow)).toBeLessThanOrEqual(overflow.viewport + 1);
  expect(overflow.offenders, JSON.stringify(overflow)).toEqual([]);
}

async function capture(page, testInfo, name, {fullPage = true} = {}) {
  await expectNoHorizontalOverflow(page);
  await page.screenshot({path: artifactPath(testInfo, name), fullPage});
}

test.beforeEach(async ({page}) => {
  page.uiErrors = [];
  page.on('pageerror', error => page.uiErrors.push(`pageerror: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') page.uiErrors.push(`console: ${message.text()}`);
  });
  await page.goto('/');
  await expect(page.locator('#view-dashboard')).toHaveClass(/active/);
  await expect(page.locator('#dashboardMetrics .metric')).toHaveCount(4);
});

test.afterEach(async ({page}) => {
  expect(page.uiErrors).toEqual([]);
});

test('Dashboard loads its canonical package and lifecycle projections', async ({page}, testInfo) => {
  await expect(page.getByRole('heading', {name: 'Dashboard'})).toBeVisible();
  await expect(page.locator('#dashboardPackageFlow .dashboard-package-row')).toHaveCount(8);
  await expect(page.locator('#latestOperations .latest-operation-row')).toHaveCount(7);
  await expect(page.locator('#dashboardRepoState')).toContainText('repo.example.invalid/ui-showcase');
  await capture(page, testInfo, 'dashboard');
});

test('Packages supports search, status filtering, and details', async ({page}, testInfo) => {
  await openView(page, 'packages');
  await expect(page.locator('#packageList .package-table-row')).toHaveCount(8);
  await page.locator('#packageSearch').fill('debbuilder');
  await expect(page.locator('#packageList .package-table-row')).toHaveCount(1);
  await expect(page.locator('#packageList')).toContainText('Update available');
  await page.locator('#packageSearch').fill('');
  await page.locator('#packageFilter').selectOption('update_available');
  await expect(page.locator('#packageList .package-table-row')).toHaveCount(1);
  await page.locator('#packageFilter').selectOption('all');
  await capture(page, testInfo, 'packages');

  await page.locator('[data-package-name="vendor-cli"][data-admin-action="open-package"]').click();
  await expect(page.locator('#packageDrawer')).toHaveClass(/open/);
  await expect(page.locator('#packageDetail')).toContainText('Ready to publish');
  await capture(page, testInfo, 'package-detail', {fullPage: false});
  await page.locator('#btnClosePackageDrawer').click();
  await expect(page.locator('#packageDrawer')).not.toHaveClass(/open/);
});

test('Recipes selects a showcase Recipe, changes step, and closes a safe modal', async ({page}, testInfo) => {
  await openView(page, 'recipes');
  await expect(page.locator('#workflowSelect option')).toHaveCount(8);
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await expect(page.locator('#recipeTitle')).toHaveText('debbuilder');
  await page.locator('[data-recipe-step="build"]').click();
  await expect(page.locator('[data-recipe-step="build"]')).toHaveAttribute('aria-current', 'step');
  await expect(page.locator('#recipe-step-build')).toBeInViewport();
  await capture(page, testInfo, 'recipes');

  await page.locator('#btnAddSourceChange').click();
  await expect(page.locator('#sourceChangeDialog')).toBeVisible();
  await expect(page.locator('#sourceChangeDialog')).toContainText('Add source change');
  await capture(page, testInfo, 'recipe-modal', {fullPage: false});
  await page.locator('#btnCancelSourceChange').click();
  await expect(page.locator('#sourceChangeDialog')).not.toBeVisible();
});

test('Logs selects an execution and renders lifecycle, steps, and output', async ({page}, testInfo) => {
  await openView(page, 'logs');
  await expect(page.locator('#executionList .execution-item')).toHaveCount(7);
  await capture(page, testInfo, 'logs');
  await page.locator('#executionList [data-execution-id="ui-06-ready-to-publish"]').click();
  await expect(page.locator('#executionMeta')).toContainText('Ready to publish');
  await expect(page.locator('#executionSteps .step-chip')).toHaveCount(10);
  await expect(page.locator('#executionDetail')).toContainText('artifact: success');
  if (testInfo.project.name === 'mobile') {
    await expect(page.locator('.logs-detail-card')).toBeVisible();
    await expect(page.locator('.logs-list-card')).toBeHidden();
  }
  await capture(page, testInfo, 'log-detail', {fullPage: false});
});

test('Settings renders every section without performing actions', async ({page}, testInfo) => {
  await openView(page, 'settings');
  for (const heading of ['General', 'APT repository', 'GitHub integration', 'Notifications', 'OIDC authentication', 'Automation', 'Maintenance']) {
    await expect(page.getByRole('heading', {name: heading, exact: true})).toBeVisible();
  }
  await expect(page.locator('#settingRepoUrl')).toHaveValue('https://repo.example.invalid/ui-showcase');
  await expect(page.locator('#btnClearLogs')).toBeVisible();
  await capture(page, testInfo, 'settings');
});
