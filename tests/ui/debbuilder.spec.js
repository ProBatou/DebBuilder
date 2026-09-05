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

async function expectFullyInViewport(page, locator) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize().height + 1);
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

test('Recipe JSON stays canonical across view, edit, apply, export, and import', async ({page}, testInfo) => {
  const editedDescription = `Edited through canonical JSON on ${testInfo.project.name}`;
  const importedId = `json-import-${testInfo.project.name}`;
  await page.evaluate(() => {
    window.__recipeJsonClipboard = '';
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {writeText: async value => { window.__recipeJsonClipboard = value; }},
    });
  });
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await expect(page.locator('#recipeTitle')).toHaveText('debbuilder');

  await page.locator('#recipePackageVersionRevision').fill('1+b1');
  await page.locator('#btnRecipeJson').click();
  const editor = page.locator('#recipeJsonEditor');
  await expect(page.locator('#recipeJsonDialog')).toBeVisible();
  await expect(editor).toHaveAttribute('readonly', '');
  const viewed = JSON.parse(await editor.inputValue());
  expect(viewed.package.version_revision).toBe('1+b1');
  expect(viewed.service).not.toHaveProperty('configured');
  expect(viewed.build.output).not.toHaveProperty('path');
  await page.locator('#btnCopyRecipeJson').click();
  await expect.poll(() => page.evaluate(() => window.__recipeJsonClipboard)).toContain('"version_revision": "1+b1"');
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#btnExportRecipeJson').click();
  expect((await downloadPromise).suggestedFilename()).toBe('debbuilder.json');
  await expectFullyInViewport(page, page.locator('#btnCancelRecipeJson'));
  await capture(page, testInfo, 'recipe-json-view', {fullPage: false});

  await page.locator('#btnEditRecipeJson').click();
  viewed.package.description = editedDescription;
  await editor.fill(JSON.stringify(viewed, null, 2));
  await expect(page.locator('#btnApplyRecipeJson')).toBeDisabled();
  await page.locator('#btnValidateRecipeJson').click();
  await expect(page.locator('#recipeJsonPreview')).toBeVisible();
  await expect(page.locator('#recipeJsonChangeSummary')).toContainText('$.package.description');
  await expect(page.locator('#btnApplyRecipeJson')).toBeEnabled();
  await expectFullyInViewport(page, page.locator('#btnApplyRecipeJson'));
  await capture(page, testInfo, 'recipe-json-edit-preview', {fullPage: false});

  await page.locator('#btnApplyRecipeJson').click();
  await expect(page.locator('#appDialog')).toBeVisible();
  await expect(page.locator('#appDialogDescription')).toContainText('form will be refreshed');
  await page.locator('#appDialogConfirm').click();
  await expect(page.locator('#recipeJsonDialog')).not.toBeVisible();
  await expect(page.locator('#packageDescription')).toHaveValue(editedDescription);

  await page.locator('#btnRecipeJson').click();
  await expect(editor).toHaveValue(new RegExp(editedDescription));
  await page.locator('#btnCancelRecipeJson').click();

  const imported = structuredClone(viewed);
  imported.name = importedId;
  imported.package.name = importedId;
  imported.package.description = 'Imported canonical Recipe';
  imported.source.repository = `example/${importedId}`;
  imported.install.destination = `/opt/${importedId}`;
  imported.install.owner = {user: importedId, group: importedId, create_user: false, create_group: false};
  imported.install.account = {user: importedId, group: importedId, create_user: false, create_group: false};
  await page.locator('#recipeImportFile').setInputFiles({
    name: 'unsafe-client-name.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(imported)),
  });
  await expect(page.locator('#recipeJsonDialog')).toBeVisible();
  await expect(page.locator('#recipeJsonDescription')).toContainText('create a new Recipe');
  await expect(page.locator('#btnApplyRecipeJson')).toBeEnabled();
  await expectFullyInViewport(page, page.locator('#btnApplyRecipeJson'));
  await capture(page, testInfo, 'recipe-json-import', {fullPage: false});
  await page.locator('#btnApplyRecipeJson').click();
  await expect(page.locator('#appDialogTitle')).toHaveText(`Create Recipe “${importedId}”?`);
  await page.locator('#appDialogConfirm').click();
  await expect(page.locator('#workflowSelect')).toHaveValue(importedId);
  await expect(page.locator('#packageDescription')).toHaveValue('Imported canonical Recipe');

  await page.locator('#recipeImportFile').setInputFiles({
    name: 'collision.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(imported)),
  });
  await expect(page.locator('#recipeJsonDescription')).toContainText('replace the existing user Recipe');
  await page.locator('#btnApplyRecipeJson').click();
  await expect(page.locator('#appDialogTitle')).toHaveText(`Replace Recipe “${importedId}”?`);
  await page.locator('#appDialogCancel').click();
  await expect(page.locator('#recipeJsonDialog')).toBeVisible();
  await page.locator('#btnCancelRecipeJson').click();
  const cleanup = await page.request.delete(`/api/workflows/${importedId}`);
  expect(cleanup.ok()).toBe(true);
  const packageCleanup = await page.request.delete(`/api/packages/${importedId}`);
  expect(packageCleanup.ok()).toBe(true);
});

test('Recipe JSON Apply drains an older autosave before persisting JSON', async ({page}, testInfo) => {
  const workflowUrl = '**/api/workflows/debbuilder';
  const originalResponse = await page.request.get('/api/workflows/debbuilder');
  expect(originalResponse.ok()).toBe(true);
  const original = await originalResponse.json();
  let releaseAutosave;
  let autosaveStarted;
  const autosaveGate = new Promise(resolve => { releaseAutosave = resolve; });
  const firstWriteStarted = new Promise(resolve => { autosaveStarted = resolve; });
  const writes = [];
  await page.route(workflowUrl, async route => {
    if (route.request().method() !== 'POST') return route.continue();
    writes.push(route.request().postDataJSON());
    if (writes.length === 1) {
      autosaveStarted();
      await autosaveGate;
    }
    return route.continue();
  });

  try {
    await openView(page, 'recipes');
    await page.locator('#workflowSelect').selectOption('debbuilder');
    await page.locator('#packageDescription').fill(`Delayed form autosave on ${testInfo.project.name}`);
    await firstWriteStarted;

    await page.locator('#btnRecipeJson').click();
    const editor = page.locator('#recipeJsonEditor');
    const applied = JSON.parse(await editor.inputValue());
    applied.package.description = `JSON wins after delayed autosave on ${testInfo.project.name}`;
    await page.locator('#btnEditRecipeJson').click();
    await editor.fill(JSON.stringify(applied, null, 2));
    await page.locator('#btnValidateRecipeJson').click();
    await expect(page.locator('#btnApplyRecipeJson')).toBeEnabled();
    await page.locator('#btnApplyRecipeJson').click();
    await page.locator('#appDialogConfirm').click();

    await page.waitForTimeout(150);
    expect(writes).toHaveLength(1);
    releaseAutosave();
    await expect(page.locator('#recipeJsonDialog')).not.toBeVisible();
    expect(writes).toHaveLength(2);

    const persistedResponse = await page.request.get('/api/workflows/debbuilder');
    expect(persistedResponse.ok()).toBe(true);
    const persisted = await persistedResponse.json();
    expect(persisted.package.description).toBe(applied.package.description);
  } finally {
    releaseAutosave();
    await page.unroute(workflowUrl);
    const restored = await page.request.post('/api/workflows/debbuilder', {data: {workflow: original, previous_id: 'debbuilder'}});
    expect(restored.ok()).toBe(true);
  }
});

test('Recipe JSON Apply failure stays visible and can be retried', async ({page}, testInfo) => {
  const workflowUrl = '**/api/workflows/debbuilder';
  const originalResponse = await page.request.get('/api/workflows/debbuilder');
  expect(originalResponse.ok()).toBe(true);
  const original = await originalResponse.json();
  let failNextWrite = true;
  await page.route(workflowUrl, async route => {
    if (route.request().method() === 'POST' && failNextWrite) {
      failNextWrite = false;
      return route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({error: 'Forced Recipe save failure'}),
      });
    }
    return route.continue();
  });

  try {
    await openView(page, 'recipes');
    await page.locator('#workflowSelect').selectOption('debbuilder');
    const previousDescription = await page.locator('#packageDescription').inputValue();
    await page.locator('#btnRecipeJson').click();
    const editor = page.locator('#recipeJsonEditor');
    const applied = JSON.parse(await editor.inputValue());
    applied.package.description = `Retryable JSON change on ${testInfo.project.name}`;
    await page.locator('#btnEditRecipeJson').click();
    await editor.fill(JSON.stringify(applied, null, 2));
    await page.locator('#btnValidateRecipeJson').click();
    await page.locator('#btnApplyRecipeJson').click();
    await page.locator('#appDialogConfirm').click();

    await expect(page.locator('#recipeJsonDialog')).toBeVisible();
    await expect(page.locator('#recipeJsonError')).toContainText('Forced Recipe save failure');
    await expect(page.locator('#recipeAutosaveStatus')).toHaveAttribute('data-state', 'error');
    await expect(page.locator('#recipeAutosaveStatus')).toContainText('not saved');
    await expect(page.locator('#packageDescription')).toHaveValue(previousDescription);
    await expect(page.locator('#btnApplyRecipeJson')).toBeEnabled();
    await expect.poll(() => page.uiErrors.filter(message => message.includes('status of 500')).length).toBe(1);
    page.uiErrors = page.uiErrors.filter(message => !message.includes('status of 500'));

    await page.locator('#btnApplyRecipeJson').click();
    await page.locator('#appDialogConfirm').click();
    await expect(page.locator('#recipeJsonDialog')).not.toBeVisible();
    await expect(page.locator('#packageDescription')).toHaveValue(applied.package.description);
    await expect(page.locator('#recipeAutosaveStatus')).toHaveAttribute('data-state', 'saved');
    const persisted = await (await page.request.get('/api/workflows/debbuilder')).json();
    expect(persisted.package.description).toBe(applied.package.description);
  } finally {
    await page.unroute(workflowUrl);
    const restored = await page.request.post('/api/workflows/debbuilder', {data: {workflow: original, previous_id: 'debbuilder'}});
    expect(restored.ok()).toBe(true);
  }
});

test('Read-only Recipe JSON remains viewable, copyable, and exportable', async ({page}) => {
  await page.evaluate(() => {
    window.__recipeJsonClipboard = '';
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {writeText: async value => { window.__recipeJsonClipboard = value; }},
    });
  });
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await page.locator('#workflowSelect option:checked').evaluate(option => { option.dataset.writable = 'false'; });
  await page.locator('#btnRecipeJson').click();

  await expect(page.locator('#recipeJsonDialog')).toBeVisible();
  await expect(page.locator('#recipeJsonEditor')).toHaveAttribute('readonly', '');
  await expect(page.locator('#btnEditRecipeJson')).toBeDisabled();
  await expect(page.locator('#btnApplyRecipeJson')).toBeHidden();
  await page.locator('#btnCopyRecipeJson').click();
  await expect.poll(() => page.evaluate(() => window.__recipeJsonClipboard)).toContain('"name": "debbuilder"');
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#btnExportRecipeJson').click();
  expect((await downloadPromise).suggestedFilename()).toBe('debbuilder.json');
  await page.locator('#btnCancelRecipeJson').click();
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
