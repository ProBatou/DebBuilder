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

async function captureElement(page, testInfo, name, locator) {
  await expectNoHorizontalOverflow(page);
  if (testInfo.project.name === 'desktop') {
    await page.evaluate(() => {
      document.body.style.overflow = 'visible';
      document.querySelector('.app-shell').style.height = 'auto';
      document.querySelector('.content').style.overflow = 'visible';
    });
  }
  await locator.screenshot({path: artifactPath(testInfo, name)});
}

async function expectFullyInViewport(page, locator) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize().height + 1);
}

async function expectWrappedExecutionSteps(page) {
  const layout = await page.locator('#executionSteps').evaluate(node => {
    const bounds = node.getBoundingClientRect();
    const chips = [...node.children];
    return {
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
      overflowX: getComputedStyle(node).overflowX,
      rows: new Set(chips.map(chip => chip.offsetTop)).size,
      clipped: chips.some(chip => {
        const box = chip.getBoundingClientRect();
        return box.left < bounds.left - 1 || box.right > bounds.right + 1;
      }),
    };
  });
  expect(layout.overflowX).not.toMatch(/auto|scroll/);
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth);
  expect(layout.rows).toBeGreaterThan(1);
  expect(layout.clipped).toBe(false);
}

const archiveFiles = [
  '.github/workflows/release.yml',
  'README.md',
  'bin/archive-agent',
  'debbuilder/app.py',
  'debbuilder/runtime.py',
  'debbuilder/services/execution_service.py',
  'debbuilder/services/package_service.py',
  'debbuilder/services/a-very-long-service-module-name-that-must-wrap-safely.py',
  'server.py',
  'share/defaults.yml',
  'static/css/pages.css',
  'static/index.html',
  'static/js/app.js',
  'tests/test_app.py',
  'tests/test_runtime.py',
];

function archiveInventory(files) {
  const directories = new Set();
  files.forEach(file => {
    const parts = file.split('/');
    for (let index = 1; index < parts.length; index += 1) directories.add(`${parts.slice(0, index).join('/')}/`);
  });
  const entries = [
    ...[...directories].map(directory => ({
      path: directory,
      kind: 'directory',
      descendant_files: files.filter(file => file.startsWith(directory)).length,
    })),
    ...files.map((file, index) => ({path: file, kind: 'file', size: 128 + index, mode: file === 'bin/archive-agent' ? '0755' : '0644'})),
  ].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return {
    entries,
    file_count: files.length,
    directory_count: directories.size,
    entry_count: entries.length,
    complete: true,
  };
}

async function routeArchiveInspection(page, inventory = archiveInventory(archiveFiles)) {
  await page.route('**/api/upstream-archive/inspect', async route => {
    const workflow = route.request().postDataJSON().workflow;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({inspection: {
        source: {name: 'archive-agent-linux-amd64.tar.gz', source: 'release_asset', archive_format: 'tar.gz'},
        release: {repository: workflow.source.repository, ref: 'v5.0.0', tag: 'v5.0.0', upstream_version: '5.0.0', debian_version: '5.0.0-3'},
        extraction: {files: inventory.file_count, stripped_root: 'archive-agent-5.0.0'},
        inventory,
        payload: null,
        selection_error: null,
      }}),
    });
  });
}

async function openArchiveRecipe(page) {
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('archive-agent');
  await expect(page.locator('#recipeTitle')).toHaveText('archive-agent');
  await expect(page.locator('#recipeArchivePayloadField')).toBeVisible();
}

function archiveAction(page, action, pathValue) {
  return page.locator(`[data-archive-action="${action}"][data-archive-path="${pathValue}"]`);
}

async function persistedArchivePayload(page) {
  const response = await page.request.get('/api/workflows/archive-agent');
  expect(response.ok()).toBe(true);
  return (await response.json()).artifact.payload;
}

async function expectPersistedArchivePayload(page, expected) {
  await expect.poll(() => persistedArchivePayload(page)).toEqual(expected);
}

async function restoreArchiveRecipe(page, original) {
  await page.evaluate(async () => {
    clearTimeout(autosaveTimer);
    autosaveDirty = false;
    autosaveRevision += 1;
    recipeMutationPaused = true;
    await waitForAutosaveIdle();
  });
  const response = await page.request.post('/api/workflows/archive-agent', {
    data: {workflow: original, previous_id: 'archive-agent'},
  });
  expect(response.ok()).toBe(true);
}

async function pauseForManualArchiveReview(page) {
  if (process.env.DEBBUILDER_MANUAL_ARCHIVE_REVIEW === '1') await page.pause();
}

async function routeArchivePreparedTest(page, facts) {
  const response = await page.request.get('/api/executions/ui-01-prepared');
  expect(response.ok()).toBe(true);
  const execution = structuredClone((await response.json()).execution);
  execution.id = 'ui-archive-prepared';
  execution.recipe_id = 'archive-agent';
  execution.recipe = 'archive-agent';
  const step = name => execution.steps.find(row => row.name === name);
  step('source').details = {
    repository: 'example/archive-agent', strategy: 'latest_release', ref: 'v5.0.0', tag: 'v5.0.0',
    upstream_version: '5.0.0', debian_version: '5.0.0-3', archive_payload: facts,
    asset: {name: 'archive-agent-linux-amd64.tar.gz', source: 'release_asset'},
  };
  step('detection').details = {
    project_type: 'upstream_archive', display_name: 'Upstream release artifact · no source build',
    detected_files: facts.mode === 'entire_archive' ? ['Entire archive'] : facts.include,
    build_tools: [], system_build_dependencies: [], proposed_commands: [], warnings: [], archive_payload: facts,
  };
  step('dependencies').details = {
    detected: [], manually_added: [], requested: [], available: [], missing: [],
    tools: [], detected_tools: [], available_tools: [], missing_tools: [], reason: 'upstream_archive',
  };
  step('build').details = {
    executed: false, reason: 'upstream_archive', commands: [],
    plan: {commands: [], working_directory: '.', environment: {}, output: {mode: 'archive_payload', payload: facts}},
    output: {mode: 'archive_payload', payload: facts},
  };
  step('staging').details = {
    preview: true, version: '5.0.0-3', install_destination: '/opt/archive-agent', include_output: true,
    content_available: true, content_file_count: facts.selected_files, configurations: [], directories: [],
    control: 'Package: archive-agent\nVersion: 5.0.0-3\nArchitecture: amd64\n', maintainer_scripts: {},
  };
  await page.route('**/api/run', route => route.fulfill({
    status: 202, contentType: 'application/json', body: JSON.stringify({run_id: 'ui-archive-prepared', status: 'queued'}),
  }));
  await page.route('**/api/executions/ui-archive-prepared', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({execution}),
  }));
}

async function routeCancellationJourney(page, {
  runId,
  mode = 'dry_run',
  initialStatus = 'running',
  cancelStatus = 202,
  terminalStatus = 'cancelled',
} = {}) {
  const executionResponse = await page.request.get('/api/executions/ui-01-prepared');
  expect(executionResponse.ok()).toBe(true);
  const baseExecution = structuredClone((await executionResponse.json()).execution);
  const executionsResponse = await page.request.get('/api/executions');
  expect(executionsResponse.ok()).toBe(true);
  const baseExecutions = (await executionsResponse.json()).executions;
  const packagesResponse = await page.request.get('/api/packages');
  expect(packagesResponse.ok()).toBe(true);
  const basePackages = (await packagesResponse.json()).packages;
  const packageResponse = await page.request.get('/api/packages/debbuilder');
  expect(packageResponse.ok()).toBe(true);
  const basePackage = (await packageResponse.json()).package;
  let status = initialStatus;
  let cancelCalls = 0;
  let detailCalls = 0;
  let logCalls = 0;
  let terminalReleased = false;
  let cancelResponseReleased = cancelStatus !== 202;
  let releaseCancelResponse;
  const cancelResponseGate = new Promise(resolve => { releaseCancelResponse = resolve; });

  const cancellation = () => ({
    code: 'execution_cancelled', reason: 'user_requested', phase: 'pipeline', stage: 'build',
    requested_at: '2026-09-07T10:00:00+00:00',
    ...(status === 'cancelled' ? {completed_at: '2026-09-07T10:00:01+00:00'} : {}),
    kind: status === 'cancelled' ? 'cancelled' : 'cancelling',
  });
  const execution = () => {
    const row = structuredClone(baseExecution);
    row.id = runId;
    row.run_id = runId;
    row.recipe_id = 'debbuilder';
    row.recipe = 'debbuilder';
    row.mode = mode;
    row.action = mode === 'dry_run' ? 'dry-run' : 'build';
    row.status = status;
    row.build_status = status;
    row.lifecycle_status = status === 'running' ? 'building' : status;
    row.lifecycle_active = ['queued', 'running', 'cancelling'].includes(status);
    row.ready_for_build = false;
    row.allowed_actions = {validate: false, publish: false};
    row.error = null;
    row.artifact = null;
    if (['cancelling', 'cancelled'].includes(status)) row.cancellation = cancellation();
    else row.cancellation = null;
    const source = row.steps.find(step => step.name === 'source');
    const build = row.steps.find(step => step.name === 'build');
    row.steps.forEach(step => {
      step.status = 'pending';
      step.summary = '';
      step.error = null;
    });
    if (status !== 'queued') {
      source.status = 'success';
      source.summary = 'Fetched deterministic cancellation fixture';
      build.status = status === 'cancelled' ? 'cancelled' : 'running';
      build.summary = status === 'cancelled' ? 'Build cancelled by user' : 'Long-running command active';
      build.details = {commands: [{
        index: 1, status: status === 'cancelled' ? 'cancelled' : 'running',
        command: 'python3 cancellation_worker.py', arguments: ['python3', 'cancellation_worker.py'],
        stdout: 'worker-ready\ntick-1\ntick-2\n', stderr: 'child-ready\n', cancelled: status === 'cancelled',
      }]};
    }
    return row;
  };
  const packageProjection = () => ({
    ...structuredClone(basePackage),
    lifecycle_state: 'cancelled', lifecycle_display_status: 'cancelled', status: 'cancelled',
    allowed_actions: {test: true, build: true, validate: false, publish: false},
    build: {...basePackage.build, latest_status: 'cancelled', latest_run_id: runId},
  });

  await page.route('**/api/run', route => route.fulfill({
    status: 202, contentType: 'application/json', body: JSON.stringify({run_id: runId, status: 'queued'}),
  }));
  await page.route(`**/api/executions/${runId}/cancel`, async route => {
    cancelCalls += 1;
    if (cancelStatus === 409) {
      status = terminalStatus;
      await route.fulfill({
        status: 409, contentType: 'application/json',
        body: JSON.stringify({error: {code: 'execution_not_cancellable', message: 'Execution is already terminal', details: {status}}}),
      });
      return;
    }
    if (cancelStatus === 202) {
      await cancelResponseGate;
      cancelResponseReleased = true;
      status = 'cancelling';
    } else {
      status = 'cancelled';
    }
    await route.fulfill({
      status: cancelStatus, contentType: 'application/json',
      body: JSON.stringify({run_id: runId, status, cancellation: cancellation()}),
    });
  });
  await page.route(`**/api/executions/${runId}`, route => {
    detailCalls += 1;
    if (status === 'cancelling' && terminalReleased) status = 'cancelled';
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({execution: execution()})});
  });
  await page.route(`**/api/executions/${runId}/logs?**`, route => {
    logCalls += 1;
    const text = status === 'queued' ? '' : 'worker-ready\ntick-1\ntick-2\nchild-ready\n';
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({log: {text, offset: text.length, size: text.length, complete: status === 'cancelled', verbosity: 'normal'}}),
    });
  });
  await page.route('**/api/executions', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({executions: [execution(), ...baseExecutions.filter(row => row.id !== runId)]}),
  }));
  await page.route('**/api/packages', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({packages: basePackages.map(row => row.name === 'debbuilder' && status === 'cancelled' ? packageProjection() : row)}),
  }));
  await page.route('**/api/packages/debbuilder', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({package: status === 'cancelled' ? packageProjection() : basePackage}),
  }));

  return {
    releaseCancelResponse() { releaseCancelResponse(); },
    releaseTerminal() { terminalReleased = true; },
    setStatus(value) { status = value; },
    snapshot() { return {status, cancelCalls, detailCalls, logCalls, cancelResponseReleased, terminalReleased}; },
  };
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
  await expect(page.locator('#latestOperations .latest-operation-row')).toHaveCount(8);
  await expect(page.locator('#dashboardRepoState')).toContainText('repo.example.invalid/ui-showcase');
  await capture(page, testInfo, 'dashboard');
});

test('System shows diagnostics, distinguishes degraded states, and downloads the server ZIP', async ({page}) => {
  await openView(page, 'system');
  await expect(page.locator('#systemChecks .system-check')).toHaveCount(9);
  await expectNoHorizontalOverflow(page);
  await expect(page.locator('#systemRuntime')).toContainText('DebBuilder version');
  await expect(page.locator('#systemRuntime')).toContainText('Recipe schema');
  await expect(page.locator('#systemRuntime')).toContainText('Python version');
  await expect(page.locator('#systemOverallStatus')).toContainText(/OK|Warning|Failed|Unknown/);
  await expect(page.locator('#view-system a[href="/api/openapi.json"]')).toHaveAttribute('target', '_blank');
  const original = await (await page.request.get('/api/system/diagnostics')).json();
  const mocked = {...original, status: 'warning', checks: original.checks.map((check, index) => ({
    ...check,
    status: index === 0 ? 'warning' : index === 1 ? 'unknown' : index === 2 ? 'failed' : 'ok',
    message: index === 0 ? '<img src=x onerror=alert(1)>' : check.message,
    details: index === 0 ? {...check.details, unexpected_secret: 'DO_NOT_RENDER'} : check.details,
  }))};
  await page.route('**/api/system/diagnostics', route => route.fulfill({contentType: 'application/json', body: JSON.stringify(mocked)}));
  await page.locator('#btnRefreshDiagnostics').click();
  await expect(page.locator('#systemOverallStatus')).toContainText('Warning');
  await expect(page.locator('#systemChecks .system-status--warning')).toHaveCount(1);
  await expect(page.locator('#systemChecks .system-status--unknown')).toHaveCount(1);
  await expect(page.locator('#systemChecks .system-status--failed')).toHaveCount(1);
  await expect(page.locator('#systemChecks .system-status--ok')).toHaveCount(6);
  await expect(page.locator('#systemChecks')).toContainText('<img src=x onerror=alert(1)>');
  await expect(page.locator('#systemChecks img')).toHaveCount(0);
  await expect(page.locator('#systemChecks')).not.toContainText('DO_NOT_RENDER');
  await page.unroute('**/api/system/diagnostics');
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#btnSystemSupportBundle').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('debbuilder-support.zip');
  await expect(page.locator('#btnSystemSupportBundle')).toBeEnabled();
});

test('Recipe and Run inspectors load on demand and contextual support downloads use selected IDs', async ({page}) => {
  const requests = [];
  page.on('request', request => { if (request.url().includes('/inspect')) requests.push(request.url()); });
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('seerr');
  await expect(page.locator('#recipeTitle')).toHaveText('seerr');
  expect(requests).toHaveLength(0);
  await page.locator('#btnInspectRecipe').click();
  await expect(page.locator('#systemInspectorDialog')).toBeVisible();
  await expect(page.locator('#systemInspectorContent')).toContainText('Source summary');
  await expect(page.locator('#systemInspectorContent')).toContainText('Build command count');
  await expectNoHorizontalOverflow(page);
  await expect(page.locator('#systemInspectorContent')).not.toContainText('build.environment');
  expect(requests.some(url => url.includes('/api/recipes/seerr/inspect'))).toBe(true);
  await page.locator('#btnCloseSystemInspector').click();
  const recipeDownload = page.waitForEvent('download');
  const recipeRequest = page.waitForRequest(request => request.url().includes('/api/support-bundle?recipe_id=seerr'));
  await page.locator('#btnRecipeSupportBundle').click();
  await recipeRequest;
  expect((await recipeDownload).suggestedFilename()).toBe('debbuilder-support.zip');

  await openView(page, 'logs');
  await page.locator('#executionList [data-execution-id="ui-06-ready-to-publish"]').click();
  await expect(page.locator('#btnInspectRun')).toBeEnabled();
  await page.locator('#btnInspectRun').click();
  await expect(page.locator('#systemInspectorContent')).toContainText('Recipe fingerprint SHA-256');
  await expect(page.locator('#systemInspectorContent')).toContainText('Recovery and containment');
  await expectNoHorizontalOverflow(page);
  expect(requests.some(url => url.includes('/api/executions/ui-06-ready-to-publish/inspect'))).toBe(true);
  await page.locator('#btnCloseSystemInspector').click();
  const runDownload = page.waitForEvent('download');
  const runRequest = page.waitForRequest(request => request.url().includes('/api/support-bundle?run_id=ui-06-ready-to-publish'));
  await page.locator('#btnRunSupportBundle').click();
  await runRequest;
  expect((await runDownload).suggestedFilename()).toBe('debbuilder-support.zip');
});

test('System and support errors show canonical messages without rendering response markup', async ({page}) => {
  await page.route('**/api/system/diagnostics', route => route.fulfill({
    status: 503, contentType: 'application/json',
    body: JSON.stringify({ok: false, error: {code: 'settings_unavailable', message: '<safe settings unavailable>', details: {}}}),
  }));
  await openView(page, 'system');
  await expect(page.locator('#systemFeedback')).toHaveText('<safe settings unavailable>');
  await expect(page.locator('#view-system safe')).toHaveCount(0);
  await page.unroute('**/api/system/diagnostics');
  await page.locator('#btnRefreshDiagnostics').click();
  await expect(page.locator('#systemChecks .system-check')).toHaveCount(9);
  await page.route('**/api/support-bundle', route => route.fulfill({
    status: 409, contentType: 'application/json',
    body: JSON.stringify({ok: false, error: {code: 'run_inspection_unavailable', message: '<bundle unavailable>', details: {}}}),
  }));
  await page.locator('#btnSystemSupportBundle').click();
  await expect(page.locator('.toast-message')).toContainText('<bundle unavailable>');
  await expect(page.locator('.toast-message bundle')).toHaveCount(0);
  await expect(page.locator('#btnSystemSupportBundle')).toBeEnabled();
  page.uiErrors = page.uiErrors.filter(message => !message.includes('status of 503') && !message.includes('status of 409'));
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
  await page.locator('#workflowSelect').selectOption('seerr');
  await expect(page.locator('#recipeTitle')).toHaveText('seerr');
  await expect(page.locator('#recipeMetaActive')).toBeChecked();
  await page.locator('#recipeMetaActive').uncheck();
  await expect(page.locator('#btnDryRun')).toBeDisabled();
  await expect(page.locator('#btnBuildReal')).toBeDisabled();
  await page.locator('#recipeMetaActive').check();
  await expect(page.locator('#btnDryRun')).toBeEnabled();
  await expect(page.locator('#packageDescription')).toHaveAttribute('rows', '1');
  await capture(page, testInfo, 'recipe-source');
  await page.locator('[data-recipe-step="build"]').click();
  await expect(page.locator('[data-recipe-step="build"]')).toHaveAttribute('aria-current', 'step');
  await expect(page.locator('#recipe-step-build')).toBeInViewport();
  await capture(page, testInfo, 'recipes');

  await page.locator('[data-recipe-step="install"]').click();
  await expect(page.locator('#recipe-step-install')).toBeInViewport();
  await capture(page, testInfo, 'recipe-install');

  await page.locator('[data-recipe-step="service"]').click();
  await expect(page.locator('#recipe-step-service')).toBeInViewport();
  await page.locator('.systemd-advanced').evaluate(node => { node.open = true; });
  await expect(page.locator('#serviceDescription')).toBeVisible();
  await expect(page.locator('#serviceWorkingDirectory')).toBeVisible();
  await capture(page, testInfo, 'recipe-service-advanced');

  await page.locator('#btnAddSourceChange').click();
  await expect(page.locator('#sourceChangeDialog')).toBeVisible();
  await expect(page.locator('#sourceChangeDialog')).toContainText('Add source change');
  await capture(page, testInfo, 'recipe-modal', {fullPage: false});
  await page.locator('#btnCancelSourceChange').click();
  await expect(page.locator('#sourceChangeDialog')).not.toBeVisible();
});

test('Recipe automation config, Check now, Retry, polling, and reload follow server state', async ({page}) => {
  const originalResponse = await page.request.get('/api/workflows/bashrc');
  expect(originalResponse.ok()).toBe(true);
  let configured = await originalResponse.json();
  let automationState = 'manual';
  let generation = null;
  let revision = 'manual';
  let checkCalls = 0;
  let retryCalls = 0;
  const projection = () => ({
    recipe_id: 'bashrc', recipe_active: true,
    automation: configured.automation || {enabled: false, policy: 'manual'},
    eligible: configured.automation?.enabled === true && configured.automation?.policy !== 'manual',
    state: automationState, stage: automationState === 'failed' ? 'terminal' : 'detection',
    result: automationState === 'failed' ? 'failed' : automationState === 'manual' ? 'disabled' : 'active',
    last_check_at: automationState === 'manual' ? null : '2026-09-15T12:00:00+00:00',
    detected: {version: automationState === 'manual' ? '' : '5.0.0', ref: automationState === 'manual' ? '' : 'v5.0.0'},
    attempt_policy: configured.automation?.policy || null,
    run: null, validation: null, publication: null,
    retry: {scheduled: false, not_before: null},
    blocked: null, diagnostic_code: automationState === 'failed' ? 'run_failed' : null,
    can_check_now: automationState === 'watching',
    can_retry: automationState === 'failed',
    generation, revision,
    state_active: automationState === 'checking',
    scheduler: {
      state: 'running', checks_enabled: true, admission_open: true,
      last_pass_started: null, last_pass_finished: null,
      next_scheduled_check: '2026-09-15T13:00:00+00:00', active_recipe_checks: automationState === 'checking' ? 1 : 0,
      blocker: null,
    },
  });

  await page.route('**/api/workflows/bashrc', async route => {
    if (route.request().method() === 'GET') {
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(configured)});
    }
    configured = route.request().postDataJSON().workflow;
    automationState = configured.automation.enabled && configured.automation.policy !== 'manual' ? 'watching' : 'manual';
    revision = `configured-${configured.automation.policy}`;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, id: 'bashrc'})});
  });
  await page.route('**/api/recipes/bashrc/automation', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({automation: projection()}),
  }));
  await page.route('**/api/recipes/bashrc/automation/check', route => {
    checkCalls += 1;
    automationState = 'checking';
    generation = 0;
    revision = 'checking-0';
    return route.fulfill({status: 202, contentType: 'application/json', body: JSON.stringify({
      ok: true, automation: {accepted: true, created: true, recipe_id: 'bashrc', status: projection()},
    })});
  });
  await page.route('**/api/recipes/bashrc/automation/retry', route => {
    retryCalls += 1;
    expect(route.request().postDataJSON()).toEqual({generation: 0, revision: 'failed-0'});
    automationState = 'checking';
    generation = 1;
    revision = 'retry-1';
    return route.fulfill({status: 202, contentType: 'application/json', body: JSON.stringify({
      ok: true, automation: {accepted: true, created: true, generation: 1, status: projection()},
    })});
  });

  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('bashrc');
  await expect(page.locator('#recipeAutomationStatus')).toContainText('Manual');
  await expect(page.locator('#btnDryRun')).toBeVisible();
  await expect(page.locator('#btnBuildReal')).toBeVisible();

  await page.locator('#recipeAutomationEnabled').check();
  await page.locator('#recipeAutomationPolicy').selectOption('full');
  await expect(page.locator('#recipeAutosaveStatus')).toHaveAttribute('data-state', 'saved', {timeout: 3000});
  await expect(page.locator('#recipeAutomationStatus')).toContainText('Watching');
  expect(configured.automation).toEqual({enabled: true, policy: 'full'});

  await page.locator('#btnAutomationCheckNow').click();
  await expect(page.locator('#recipeAutomationStatus')).toContainText('Checking');
  expect(checkCalls).toBe(1);
  automationState = 'failed';
  generation = 0;
  revision = 'failed-0';
  await expect(page.locator('#recipeAutomationStatus')).toContainText('Failed', {timeout: 4000});
  await expect(page.locator('#btnAutomationRetry')).toBeVisible();
  await page.locator('#btnAutomationRetry').click();
  await expect(page.locator('#recipeAutomationStatus')).toContainText('Checking');
  expect(retryCalls).toBe(1);

  await page.reload();
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('bashrc');
  await expect(page.locator('#recipeAutomationStatus')).toContainText('Checking');
  await expect(page.locator('#recipeAutomationEnabled')).toBeChecked();
  await expect(page.locator('#recipeAutomationPolicy')).toHaveValue('full');
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
  await page.locator('#workflowSelect').selectOption('seerr');
  await expect(page.locator('#recipeTitle')).toHaveText('seerr');

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
  expect((await downloadPromise).suggestedFilename()).toBe('seerr.json');
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
  imported.install.directories = imported.install.directories.map(directory => ({
    ...directory,
    path: directory.path.replace('/seerr', `/${importedId}`),
  }));
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

test('Recipe form and JSON preserve enabled, Debian description, service description, and WorkingDirectory', async ({page}, testInfo) => {
  const id = `recipe-roundtrip-${testInfo.project.name}`;
  const imported = {
    schema_version: 5, name: id, active: false,
    package: {name: id, description: 'Imported Debian description\nLong text: café & <package>'},
    source: {provider: 'github', repository: `example/${id}`, tracking: 'latest_release', ref: '', version: {source: 'tag', expression: ''}},
    service: {name: `${id}.service`, command: `/opt/${id}/bin/serve`, description: 'Imported service', working_directory: `/opt/${id}`},
  };
  try {
    await openView(page, 'recipes');
    await page.locator('#recipeImportFile').setInputFiles({
      name: `${id}.json`, mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(imported)),
    });
    await page.locator('#btnApplyRecipeJson').click();
    await page.locator('#appDialogConfirm').click();
    await expect(page.locator('#workflowSelect')).toHaveValue(id);
    await expect(page.locator('#recipeMetaActive')).not.toBeChecked();
    await expect(page.locator('#btnDryRun')).toBeDisabled();
    await expect(page.locator('#packageDescription')).toHaveValue(imported.package.description);
    await page.locator('.systemd-advanced').evaluate(node => { node.open = true; });
    await expect(page.locator('#serviceDescription')).toHaveValue(imported.service.description);
    await expect(page.locator('#serviceWorkingDirectory')).toHaveValue(imported.service.working_directory);

    await page.locator('#btnRecipeJson').click();
    let json = JSON.parse(await page.locator('#recipeJsonEditor').inputValue());
    expect(json.active).toBe(false);
    expect(json.package.description).toBe(imported.package.description);
    expect(json.service.description).toBe(imported.service.description);
    expect(json.service.working_directory).toBe(imported.service.working_directory);
    await page.locator('#btnCancelRecipeJson').click();

    const editedDescription = ' Edited Debian description\nKeeps spaces & symbols: € < > ';
    await page.locator('#recipeMetaActive').check();
    await page.locator('#packageDescription').fill(editedDescription);
    await page.locator('#serviceDescription').fill(' Edited service description ');
    await page.locator('#serviceWorkingDirectory').fill(`/opt/${id}/runtime`);
    await expect.poll(async () => {
      const response = await page.request.get(`/api/workflows/${id}`);
      const persisted = await response.json();
      return {
        active: persisted.active,
        description: persisted.package.description,
        serviceDescription: persisted.service.description,
        workingDirectory: persisted.service.working_directory,
      };
    }).toEqual({
      active: true,
      description: editedDescription,
      serviceDescription: ' Edited service description ',
      workingDirectory: `/opt/${id}/runtime`,
    });
    await page.locator('#btnRecipeJson').click();
    json = JSON.parse(await page.locator('#recipeJsonEditor').inputValue());
    expect(json.active).toBe(true);
    expect(json.package.description).toBe(editedDescription);
    expect(json.service.description).toBe(' Edited service description ');
    expect(json.service.working_directory).toBe(`/opt/${id}/runtime`);
    await page.locator('#btnCancelRecipeJson').click();
  } finally {
    await page.request.delete(`/api/workflows/${id}`);
    await page.request.delete(`/api/packages/${id}`);
  }
});

test('Recipe JSON Apply drains an older autosave before persisting JSON', async ({page}, testInfo) => {
  const workflowUrl = '**/api/workflows/seerr';
  const originalResponse = await page.request.get('/api/workflows/seerr');
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
    await page.locator('#workflowSelect').selectOption('seerr');
    await page.locator('#packageDescription').fill(`Delayed form autosave on ${testInfo.project.name}`);
    await firstWriteStarted;

    await page.locator('#btnRecipeJson').click();
    const editor = page.locator('#recipeJsonEditor');
    await expect(page.locator('#recipeJsonDialog')).toBeVisible();
    await expect(editor).not.toHaveValue('');
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

    const persistedResponse = await page.request.get('/api/workflows/seerr');
    expect(persistedResponse.ok()).toBe(true);
    const persisted = await persistedResponse.json();
    expect(persisted.package.description).toBe(applied.package.description);
  } finally {
    releaseAutosave();
    await page.unroute(workflowUrl);
    const restored = await page.request.post('/api/workflows/seerr', {data: {workflow: original, previous_id: 'seerr'}});
    expect(restored.ok()).toBe(true);
  }
});

test('Recipe JSON Apply failure stays visible and can be retried', async ({page}, testInfo) => {
  const workflowUrl = '**/api/workflows/seerr';
  const originalResponse = await page.request.get('/api/workflows/seerr');
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
    await page.locator('#workflowSelect').selectOption('seerr');
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
    const persisted = await (await page.request.get('/api/workflows/seerr')).json();
    expect(persisted.package.description).toBe(applied.package.description);
  } finally {
    await page.unroute(workflowUrl);
    const restored = await page.request.post('/api/workflows/seerr', {data: {workflow: original, previous_id: 'seerr'}});
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
  await expect(page.locator('#workflowSelect option:checked')).toHaveAttribute('data-managed', 'true');
  await expect(page.locator('#recipeMetaActive')).toBeEnabled();
  await expect(page.locator('#recipePackageVersionRevision')).toBeDisabled();
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
  await expect(page.locator('#executionList .execution-item')).toHaveCount(8);
  await capture(page, testInfo, 'logs');
  await page.locator('#executionList [data-execution-id="ui-06-ready-to-publish"]').click();
  await expect(page.locator('#executionMeta')).toContainText('Ready to publish');
  await expect(page.locator('#executionSteps .step-chip')).toHaveCount(10);
  await expect(page.locator('#executionDetail')).toContainText('artifact: success');
  await expectWrappedExecutionSteps(page);
  if (testInfo.project.name === 'mobile') {
    await expect(page.locator('.logs-detail-card')).toBeVisible();
    await expect(page.locator('.logs-list-card')).toBeHidden();
  }
  await capture(page, testInfo, 'log-detail', {fullPage: false});
});

test('Validation admission, polling, stale responses, reload, failure, and cancellation stay canonical', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'One focused desktop lifecycle journey is sufficient');
  const runId = 'ui-03-validation-needed';
  const baseResponse = await page.request.get(`/api/executions/${runId}`);
  expect(baseResponse.ok()).toBe(true);
  const base = (await baseResponse.json()).execution;
  let mode = 'idle';
  let submissions = 0;
  let cancellations = 0;
  let staleReads = 0;
  let releaseQueuedPoll;
  let releaseStalePoll;
  const queuedPollGate = new Promise(resolve => { releaseQueuedPoll = resolve; });
  const stalePollGate = new Promise(resolve => { releaseStalePoll = resolve; });

  const validation = status => ({
    id: 'validation-ui-attempt', attempt_id: 'validation-ui-attempt', build_run_id: runId,
    status, phase: status === 'queued' ? 'queued' : 'lifecycle', cancellable: ['queued', 'running', 'cancelling'].includes(status),
    created_at: '2026-09-14T10:00:00+00:00', started_at: status === 'queued' ? null : '2026-09-14T10:00:01+00:00',
    finished_at: ['success', 'failed', 'cancelled'].includes(status) ? '2026-09-14T10:00:02+00:00' : null,
    profile: {name: 'bookworm'}, checks: status === 'failed' ? [{name: 'package_install', status: 'failed', error: 'Validation check failed'}] : [],
    error: status === 'failed' ? {code: 'package_install_failed', message: 'Offline lifecycle validation failed'} : null,
  });
  const projected = status => ({
    ...base,
    validation_status: status,
    lifecycle_status: ['queued', 'running', 'cancelling'].includes(status) ? 'validating'
      : status === 'success' ? 'ready_to_publish' : status === 'failed' ? 'validation_failed' : 'validation_cancelled',
    lifecycle_active: ['queued', 'running', 'cancelling'].includes(status),
    allowed_actions: {validate: !['queued', 'running', 'cancelling'].includes(status), publish: status === 'success'},
    validations: [validation(status)],
    diagnostic: status === 'failed' ? {
      title: 'Package validation failed', code: 'package_install_failed', reason: 'Offline lifecycle validation failed',
      where: [{label: 'Profile', value: 'bookworm'}], facts: [{label: 'Failed checks', value: 'package_install'}],
      next_action: 'Review the package lifecycle failure.',
    } : null,
  });
  const fulfill = (route, body, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});

  await page.route(`**/api/executions/${runId}**`, async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && pathname.endsWith('/validate')) {
      submissions += 1;
      mode = 'queued';
      return fulfill(route, {validation: validation('queued')}, 202);
    }
    if (request.method() === 'POST' && pathname.endsWith('/validations/validation-ui-attempt/cancel')) {
      cancellations += 1;
      mode = 'cancelled';
      return fulfill(route, {ok: true, accepted: true, validation: validation('cancelling')}, 202);
    }
    if (request.method() === 'GET' && pathname === `/api/executions/${runId}`) {
      if (mode === 'idle') return fulfill(route, {execution: base});
      if (mode === 'queued') {
        await queuedPollGate;
        mode = 'success';
        return fulfill(route, {execution: projected('running')});
      }
      if (mode === 'stale') {
        staleReads += 1;
        if (staleReads === 1) {
          await stalePollGate;
          return fulfill(route, {execution: projected('running')});
        }
        return fulfill(route, {execution: projected('success')});
      }
      if (mode === 'recovery') {
        const blocked = validation('cancelling');
        blocked.recovery_blocker = {
          code: 'validation_container_recovery_required',
          message: 'Operator attention required',
        };
        return fulfill(route, {execution: {...projected('cancelling'), validations: [blocked]}});
      }
      return fulfill(route, {execution: projected(mode)});
    }
    return route.fallback();
  });

  await openView(page, 'logs');
  await page.locator(`#executionList [data-execution-id="${runId}"]`).click();
  await expect(page.locator('#btnRevalidateExecution')).toHaveText('Validate');
  await page.locator('#btnRevalidateExecution').evaluate(button => { button.click(); button.click(); });
  await expect.poll(() => submissions).toBe(1);
  await expect(page.locator('#btnRevalidateExecution')).toHaveText('Queued');
  await expect(page.locator('#btnRevalidateExecution')).toBeDisabled();
  releaseQueuedPoll();
  await expect(page.locator('#btnRevalidateExecution')).toHaveText('Revalidate');
  await expect(page.locator('#executionMeta')).toContainText('Ready to publish');
  expect(submissions).toBe(1);

  mode = 'running';
  await page.evaluate(() => pollOpenExecution());
  await page.evaluate(() => stopLogPolling());
  mode = 'stale';
  await page.evaluate(() => { pollOpenExecution(); });
  await expect.poll(() => staleReads).toBe(1);
  await page.evaluate(() => pollOpenExecution());
  releaseStalePoll();
  await expect(page.locator('#btnRevalidateExecution')).toHaveText('Revalidate');
  await expect(page.locator('#executionMeta')).toContainText('Ready to publish');

  mode = 'running';
  await page.reload();
  await openView(page, 'logs');
  await page.locator(`#executionList [data-execution-id="${runId}"]`).click();
  await expect(page.locator('#btnRevalidateExecution')).toHaveText('Running');
  await expect(page.locator('#btnCancelExecution')).toHaveText('Cancel validation');
  await page.locator('#btnCancelExecution').click();
  await expect.poll(() => cancellations).toBe(1);
  await expect(page.locator('#executionCancellationSummary')).toContainText('Validation cancelled');

  mode = 'failed';
  await page.evaluate(() => pollOpenExecution());
  await expect(page.locator('#executionMeta')).toContainText('Validation failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('package_install');
  mode = 'recovery';
  await page.evaluate(() => pollOpenExecution());
  await expect(page.locator('#executionMeta')).toContainText('Validation recovery');
  await expect(page.locator('#executionMeta')).toContainText('Operator attention');
  await expect(page.locator('body')).not.toContainText('BEGIN PGP PRIVATE KEY');
  await expect(page.locator('body')).not.toContainText('/tmp/debbuilder-validation');
});

test('Logs explains build, validation, and publication failures', async ({page}, testInfo) => {
  await openView(page, 'logs');
  const openFailure = async id => {
    if (testInfo.project.name === 'mobile' && await page.locator('.logs-list-card').isHidden()) {
      await page.locator('#btnCloseLogDetail').click();
    }
    await page.locator(`#executionList [data-execution-id="${id}"]`).click();
    await expect(page.locator('#executionDiagnostic')).toBeVisible();
    await expectWrappedExecutionSteps(page);
  };

  await openFailure('ui-04-build-failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('Build command failed');
  await expect(page.locator('#executionDiagnostic [data-diagnostic-toggle]')).toHaveText('Show details');
  await expect(page.locator('#executionDiagnostic .diagnostic-details')).toBeHidden();
  if (testInfo.project.name === 'desktop') {
    const [diagnostic, logs] = await Promise.all([
      page.locator('#executionDiagnostic').boundingBox(),
      page.locator('#executionDetail').boundingBox(),
    ]);
    expect(diagnostic).not.toBeNull();
    expect(logs).not.toBeNull();
    expect(logs.height).toBeGreaterThan(250);
    expect(logs.height).toBeGreaterThan(diagnostic.height);
  }
  await page.locator('#executionDiagnostic [data-diagnostic-toggle]').click();
  await expect(page.locator('#executionDiagnostic [data-diagnostic-toggle]')).toHaveText('Hide details');
  await expect(page.locator('#executionDiagnostic .diagnostic-details')).toBeVisible();
  await expect(page.locator('#executionDiagnostic')).toContainText('pnpm build --filter');
  await expect(page.locator('#executionDiagnostic')).toContainText('Exit code');
  await expect(page.locator('#executionDiagnostic')).toContainText('What to do next');
  await capture(page, testInfo, 'log-build-diagnostic', {fullPage:false});
  await page.locator('#executionDiagnostic [data-diagnostic-recipe]').click();
  await expect(page.locator('#view-recipes')).toHaveClass(/active/);
  await expect(page.locator('#workflowSelect')).toHaveValue('seerr');
  await expect(page.locator('#recipe-step-build')).toBeInViewport();
  await openView(page, 'logs');

  await openFailure('ui-05-validation-failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('Package validation failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('bookworm');
  await expect(page.locator('#executionDiagnostic')).toContainText('validation_checks_failed');
  await capture(page, testInfo, 'log-validation-diagnostic', {fullPage:false});

  await openFailure('ui-00-publication-failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('APT publication failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('reprepro_include_failed');
  await expect(page.locator('#executionDiagnostic')).toContainText('No matching signing key');
  await capture(page, testInfo, 'log-publication-diagnostic', {fullPage:false});
});

test('Archive Selected paths persists compact recursive selectors and presents them in Test', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop archive journey');
  const original = await (await page.request.get('/api/workflows/archive-agent')).json();
  await routeArchiveInspection(page);
  try {
    await openArchiveRecipe(page);
    await page.locator('.archive-payload-modes label').filter({hasText: 'Entire archive'}).click();
    await expect(page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]')).toBeChecked();
    await page.locator('.archive-payload-modes label').filter({hasText: 'Selected paths'}).click();
    await expect(page.locator('input[name="recipeArchivePayloadMode"][value="paths"]')).toBeChecked();
    await page.locator('#btnInspectArchive').click();
    await expect(page.locator('#recipeArchiveInspectionStatus')).toHaveText('Inspection is current.');
    await expect(page.locator('.archive-tree-row')).toHaveCount(8);
    await expect(archiveAction(page, 'toggle', 'debbuilder/')).toHaveAttribute('aria-expanded', 'false');
    await captureElement(page, testInfo, 'archive-selected-initial', page.locator('#recipeArchivePayloadField'));
    await pauseForManualArchiveReview(page);

    await archiveAction(page, 'toggle', 'debbuilder/').click();
    await expect(archiveAction(page, 'toggle', 'debbuilder/')).toHaveAttribute('aria-expanded', 'true');
    await archiveAction(page, 'toggle', 'debbuilder/services/').click();
    await expect(page.locator('.archive-tree')).toContainText('app.py');
    await expect(page.locator('.archive-tree')).toContainText('runtime.py');
    await expect(page.locator('.archive-tree')).toContainText('execution_service.py');
    await archiveAction(page, 'toggle', 'debbuilder/services/').click();
    await expect(archiveAction(page, 'toggle', 'debbuilder/services/')).toHaveAttribute('aria-expanded', 'false');
    await archiveAction(page, 'toggle', 'debbuilder/services/').click();

    await archiveAction(page, 'include', 'debbuilder/').click();
    await archiveAction(page, 'include', 'static/').click();
    await archiveAction(page, 'include', 'server.py').click();
    await archiveAction(page, 'exclude', 'debbuilder/services/').click();
    const expected = {
      mode: 'paths',
      include: ['debbuilder/', 'server.py', 'static/'],
      exclude: ['debbuilder/services/'],
    };
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('2 directories · 1 explicit file · 6 resolved files');
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('debbuilder/services/');
    await expect(page.locator('.archive-tree')).not.toContainText('Inside included directory');
    await expectPersistedArchivePayload(page, expected);
    await captureElement(page, testInfo, 'archive-selected-expanded', page.locator('#recipeArchivePayloadField'));
    await captureElement(page, testInfo, 'archive-selected-summary', page.locator('#recipeArchivePayloadSummary'));

    await page.locator('#btnRecipeJson').click();
    const recipeJson = JSON.parse(await page.locator('#recipeJsonEditor').inputValue());
    expect(recipeJson.artifact.payload).toEqual(expected);
    expect(JSON.stringify(recipeJson.artifact.payload)).not.toContain('execution_service.py');
    expect(recipeJson.artifact).not.toHaveProperty('selected_files');
    await page.locator('#btnCancelRecipeJson').click();

    await page.reload();
    await openArchiveRecipe(page);
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('2 directories · 1 explicit file');
    await expect(page.locator('#recipeArchiveInspectionStatus')).toHaveText('Inspect to browse archive contents.');
    await expect(page.locator('.archive-tree-row')).toHaveCount(0);
    await page.locator('#btnInspectArchive').click();
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('6 resolved files');
    await expect(archiveAction(page, 'toggle', 'debbuilder/')).toBeEnabled();

    await routeArchivePreparedTest(page, {
      ...expected,
      selected_directories: 2,
      explicit_files: 1,
      selected_files: 6,
      excluded_directories: 1,
      excluded_files: 0,
      excluded_resolved_files: 3,
    });
    await page.locator('#btnDryRun').click();
    await expect(page.locator('#testRunState')).toHaveText('Prepared');
    await expect(page.locator('#testRunPreflightContent')).toContainText('2 directories · 1 explicit file');
    await expect(page.locator('#testRunPreflightContent')).toContainText('6 resolved files · 1 exclusion');
    await captureElement(page, testInfo, 'archive-selected-preflight', page.locator('#testRunDialog'));
    await page.locator('#btnTestRunClose').click();
  } finally {
    await restoreArchiveRecipe(page, original);
  }
});

test('Archive Entire archive persists exclusions and remains compact', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop archive journey');
  const original = await (await page.request.get('/api/workflows/archive-agent')).json();
  await routeArchiveInspection(page);
  try {
    await openArchiveRecipe(page);
    await page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]').check();
    expect(await page.evaluate(() => window.recipeArchiveState.payload.include)).toEqual([]);
    await page.locator('#btnInspectArchive').click();
    await archiveAction(page, 'exclude', '.github/').click();
    await archiveAction(page, 'exclude', 'tests/').click();
    const expected = {mode: 'entire_archive', include: [], exclude: ['.github/', 'tests/']};
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('Entire archive · 12 files');
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('2 directories · 0 files');
    await expect(page.locator('.archive-tree')).not.toContainText('Included by entire archive');
    await expectPersistedArchivePayload(page, expected);
    await captureElement(page, testInfo, 'archive-entire-exclusions', page.locator('#recipeArchivePayloadField'));

    await page.reload();
    await openArchiveRecipe(page);
    await expect(page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]')).toBeChecked();
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('.github/');
    await page.locator('#btnInspectArchive').click();
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('Entire archive · 12 files');

    await routeArchivePreparedTest(page, {
      ...expected,
      selected_directories: 0,
      explicit_files: 0,
      selected_files: 12,
      excluded_directories: 2,
      excluded_files: 0,
      excluded_resolved_files: 3,
    });
    await page.locator('#btnDryRun').click();
    await expect(page.locator('#testRunState')).toHaveText('Prepared');
    await expect(page.locator('#testRunPreflightContent')).toContainText('Entire archive');
    await expect(page.locator('#testRunPreflightContent')).toContainText('12 resolved files · 2 exclusions');
    await captureElement(page, testInfo, 'archive-entire-preflight', page.locator('#testRunDialog'));
    await page.locator('#btnTestRunClose').click();
  } finally {
    await restoreArchiveRecipe(page, original);
  }
});

test('Archive selector remains usable without overflow on mobile', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile archive journey');
  const original = await (await page.request.get('/api/workflows/archive-agent')).json();
  await routeArchiveInspection(page);
  try {
    await openArchiveRecipe(page);
    await page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]').check();
    await page.locator('input[name="recipeArchivePayloadMode"][value="paths"]').check();
    await page.locator('#btnInspectArchive').click();
    await capture(page, testInfo, 'archive-mobile-inspected');
    await pauseForManualArchiveReview(page);
    await archiveAction(page, 'toggle', 'debbuilder/').click();
    await archiveAction(page, 'toggle', 'debbuilder/services/').click();
    await expect(page.locator('.archive-tree')).toContainText('a-very-long-service-module-name-that-must-wrap-safely.py');
    await archiveAction(page, 'include', 'debbuilder/').click();
    await archiveAction(page, 'exclude', 'debbuilder/services/').click();
    await expect(archiveAction(page, 'remove-include', 'debbuilder/')).toBeVisible();
    await expect(archiveAction(page, 'remove-exclude', 'debbuilder/services/')).toBeVisible();
    await capture(page, testInfo, 'archive-mobile-expanded');
    await captureElement(page, testInfo, 'archive-mobile-summary', page.locator('#recipeArchivePayloadSummary'));
    await expectNoHorizontalOverflow(page);
    await page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]').check();
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('Entire archive');
    await expect(archiveAction(page, 'exclude', 'tests/')).toBeVisible();
    await captureElement(page, testInfo, 'archive-mobile-entire', page.locator('#recipeArchivePayloadField'));
  } finally {
    await restoreArchiveRecipe(page, original);
  }
});

test('Archive large inventory renders only roots and one expanded branch', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop structural inventory journey');
  const original = await (await page.request.get('/api/workflows/archive-agent')).json();
  const largeFiles = Array.from({length: 30}, (_, branch) =>
    Array.from({length: 100}, (__, file) => `segment-${String(branch).padStart(2, '0')}/file-${String(file).padStart(3, '0')}.dat`)
  ).flat();
  await routeArchiveInspection(page, archiveInventory(largeFiles));
  try {
    await openArchiveRecipe(page);
    await page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]').check();
    await page.locator('input[name="recipeArchivePayloadMode"][value="paths"]').check();
    await page.locator('#btnInspectArchive').click();
    await expect(page.locator('.archive-tree-row')).toHaveCount(30);
    await expect(page.locator('#recipeArchiveInspection')).toContainText('3000 files');
    await archiveAction(page, 'toggle', 'segment-00/').click();
    await expect(page.locator('.archive-tree-row')).toHaveCount(130);
    await archiveAction(page, 'include', 'segment-00/').click();
    await expect(page.locator('#recipeArchivePayloadSummary .archive-selector-row')).toHaveCount(1);
    const payload = await page.evaluate(() => collectWorkflow().artifact.payload);
    expect(payload).toEqual({mode: 'paths', include: ['segment-00/'], exclude: []});
    expect(JSON.stringify(payload)).not.toContain('file-000.dat');
  } finally {
    await restoreArchiveRecipe(page, original);
  }
});

test('Archive inspection becomes stale only for source-affecting edits', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop stale lifecycle journey');
  const original = await (await page.request.get('/api/workflows/archive-agent')).json();
  await routeArchiveInspection(page);
  try {
    await openArchiveRecipe(page);
    await page.locator('input[name="recipeArchivePayloadMode"][value="entire_archive"]').check();
    await page.locator('input[name="recipeArchivePayloadMode"][value="paths"]').check();
    await page.locator('#btnInspectArchive').click();
    await archiveAction(page, 'include', 'debbuilder/').click();
    await page.locator('#recipeMetaGithub').fill('example/archive-agent-next');
    await expect(page.locator('#recipeArchiveInspectionStatus')).toHaveText('Inspection is stale. Inspect again to enable tree actions.');
    await expect(page.locator('#recipeArchivePayloadSummary')).toContainText('debbuilder/');
    await expect(page.locator('.archive-tree-toggle').first()).toBeDisabled();
    await expect(page.locator('[data-archive-action="include"], [data-archive-action="exclude"]')).toHaveCount(0);
    await expect.poll(() => persistedArchivePayload(page)).toEqual({mode: 'paths', include: ['debbuilder/'], exclude: []});

    await page.locator('#btnInspectArchive').click();
    await expect(page.locator('#recipeArchiveInspectionStatus')).toHaveText('Inspection is current.');
    await page.locator('#packageDescription').fill('A non-source archive description edit');
    await expect(page.locator('#recipeArchiveInspectionStatus')).toHaveText('Inspection is current.');
    await expect(archiveAction(page, 'toggle', 'debbuilder/')).toBeEnabled();
  } finally {
    await restoreArchiveRecipe(page, original);
  }
});

test('Test accepts HTTP 202 and follows the returned Run in a Recipe modal', async ({page}, testInfo) => {
  await page.route('**/api/run', route => route.fulfill({status:202, contentType:'application/json', body:JSON.stringify({run_id:'ui-01-prepared', status:'queued'})}));
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await expect(page.locator('#buildCommands')).toHaveValue('');
  await page.locator('#btnDryRun').click();
  await expect(page.locator('#view-recipes')).toHaveClass(/active/);
  await expect(page.locator('#testRunDialog')).toBeVisible();
  await expect(page.locator('#testRunKind')).toHaveText('Test recipe');
  await expect(page.locator('#testRunState')).toHaveText('Prepared');
  await expect(page.locator('#testRunPrepared')).toBeVisible();
  await expect(page.locator('#testRunPreflightContent')).toContainText('Source & project');
  const serviceSection = page.locator('#testRunPreflightContent .insight-section--service');
  await expect(serviceSection).toContainText('Systemd service');
  await expect(serviceSection).toContainText('ExecStart');
  await expect(serviceSection).toContainText('/opt/debbuilder/server.py');
  await expect(page.locator('#btnTestRunBuild')).toBeVisible();
  await expect(page.locator('.toast-region')).toContainText('Test queued: ui-01-prepared');
  await capture(page, testInfo, 'recipe-test-followed-run', {fullPage:false});
  await page.locator('#btnTestRunLogs').click();
  await expect(page.locator('#view-logs')).toHaveClass(/active/);
  await expect(page.locator('#executionMeta')).toContainText('#ui-01-prepared');
  await expect(page.locator('#executionMeta')).toContainText('Prepared');
});

test('Package Test follows the same modal without opening Logs', async ({page}, testInfo) => {
  await page.route('**/api/run', route => route.fulfill({status:202, contentType:'application/json', body:JSON.stringify({run_id:'ui-01-prepared', status:'queued'})}));
  await openView(page, 'packages');
  await page.locator('[data-package-name="debbuilder"][data-admin-action="open-package"]').click();
  await expect(page.locator('#packageDrawer')).toHaveClass(/open/);
  await page.locator('#packageDetail [data-admin-action="build-package"][data-dry-run="true"]').click();
  await expect(page.locator('#view-packages')).toHaveClass(/active/);
  await expect(page.locator('#testRunDialog')).toBeVisible();
  await expect(page.locator('#testRunKind')).toHaveText('Test package');
  await expect(page.locator('#testRunState')).toHaveText('Prepared');
  await expect(page.locator('#view-logs')).not.toHaveClass(/active/);
  await expect(page.locator('#packageDrawer')).not.toHaveClass(/open/);
  await expect(page.locator('#packageDrawer')).toHaveAttribute('aria-hidden', 'true');
  const dialogBox = await page.locator('#testRunDialog').boundingBox();
  const preflightBox = await page.locator('#testRunPreflightContent').boundingBox();
  expect(dialogBox?.width).toBeGreaterThan(testInfo.project.name === 'desktop' ? 900 : 360);
  expect(preflightBox?.width).toBeGreaterThan(testInfo.project.name === 'desktop' ? 850 : 340);
  await capture(page, testInfo, 'package-test-followed-run', {fullPage:false});
  await page.locator('#btnTestRunLogs').click();
  await expect(page.locator('#view-logs')).toHaveClass(/active/);
  await expect(page.locator('#executionMeta')).toContainText('#ui-01-prepared');
});

test('Recipe Test modal keeps running progress compact', async ({page}, testInfo) => {
  const response = await page.request.get('/api/executions/ui-02-worker');
  expect(response.ok()).toBe(true);
  const execution = (await response.json()).execution;
  execution.id = 'ui-02-running';
  execution.run_id = 'ui-02-running';
  execution.status = 'running';
  execution.lifecycle_status = 'building';
  execution.lifecycle_active = true;
  execution.steps.find(step => step.name === 'dependencies').status = 'running';
  await page.route('**/api/executions/ui-02-running', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({execution}),
  }));
  await page.route('**/api/executions/ui-02-running/logs?**', route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({log: {text: 'Checking build dependencies\n', offset: 28, size: 28, complete: false, verbosity: 'normal'}}),
  }));
  await page.route('**/api/run', route => route.fulfill({status:202, contentType:'application/json', body:JSON.stringify({run_id:'ui-02-running', status:'queued'})}));
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('worker-agent');
  await page.locator('#btnDryRun').click();
  await expect(page.locator('#testRunDialog')).toBeVisible();
  await expect(page.locator('#testRunState')).toHaveText('Running');
  await expect(page.locator('#testRunLiveLog')).toBeVisible();
  await capture(page, testInfo, 'recipe-test-running', {fullPage:false});
  await page.locator('#btnTestRunClose').click();
  await expect(page.locator('#testRunDialog')).not.toBeVisible();
});

test('queued Test cancellation is immediate, terminal, and retained in Logs', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop queued cancellation journey');
  const journey = await routeCancellationJourney(page, {
    runId: 'ui-cancel-queued-test', initialStatus: 'queued', cancelStatus: 200,
  });
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await page.locator('#btnDryRun').click();
  await expect(page.locator('#testRunDialog')).toBeVisible();
  await expect(page.locator('#testRunState')).toHaveText('Queued');
  await expect(page.locator('#btnTestRunCancel')).toBeVisible();
  await expect(page.locator('#btnTestRunBuild')).toBeHidden();
  await capture(page, testInfo, 'cancellation-desktop-queued', {fullPage: false});

  await page.locator('#btnTestRunCancel').click();
  await expect(page.locator('#testRunState')).toHaveText('Cancelled');
  await expect(page.locator('#btnTestRunCancel')).toBeHidden();
  await expect(page.locator('#btnTestRunBuild')).toBeHidden();
  expect(journey.snapshot().cancelCalls).toBe(1);
  expect(journey.snapshot().status).toBe('cancelled');
  await capture(page, testInfo, 'cancellation-desktop-queued-cancelled', {fullPage: false});

  await page.locator('#btnTestRunLogs').click();
  await expect(page.locator('#view-logs')).toHaveClass(/active/);
  await expect(page.locator('#executionMeta')).toContainText('#ui-cancel-queued-test');
  await expect(page.locator('#executionMeta')).toContainText('Cancelled');
  await expect(page.locator('#executionCancellationSummary')).toContainText('Cancelled');
  await expect(page.locator('#executionList [data-execution-id="ui-cancel-queued-test"]')).toContainText('Cancelled');
  await expect(page.locator('#executionList [data-execution-id="ui-02-worker"]')).toContainText('Validation needed');
});

test('running Test cancellation protects repeated clicks and converges canonically', async ({page}, testInfo) => {
  const journey = await routeCancellationJourney(page, {runId: 'ui-cancel-running-test'});
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await page.locator('#btnDryRun').click();
  await expect(page.locator('#testRunDialog')).toBeVisible();
  await expect(page.locator('#testRunState')).toHaveText('Running');
  await expect(page.locator('#testRunLiveLog')).toContainText('tick-2');
  await expect(page.locator('#btnTestRunCancel')).toBeEnabled();
  await expect(page.locator('#btnTestRunBuild')).toBeHidden();
  await capture(page, testInfo, `cancellation-${testInfo.project.name}-test-running`, {fullPage: false});

  const click = page.locator('#btnTestRunCancel').click();
  await expect(page.locator('#btnTestRunCancel')).toBeDisabled();
  await expect(page.locator('#btnTestRunCancel')).toHaveText('Cancelling…');
  await page.locator('#btnTestRunCancel').dispatchEvent('click');
  await page.locator('#btnTestRunCancel').dispatchEvent('click');
  expect(journey.snapshot().cancelCalls).toBe(1);

  journey.releaseCancelResponse();
  await click;
  await expect(page.locator('#testRunState')).toHaveText('Cancelling…');
  expect(journey.snapshot().cancelResponseReleased).toBe(true);
  await capture(page, testInfo, `cancellation-${testInfo.project.name}-test-cancelling`, {fullPage: false});
  if (testInfo.project.name === 'desktop') {
    await page.locator('#btnTestRunClose').click();
    await expect(page.locator('#testRunDialog')).not.toBeVisible();
    expect(journey.snapshot().status).toBe('cancelling');
    journey.releaseTerminal();
    await page.evaluate(async runId => {
      switchView('logs');
      await openExecution(runId);
    }, 'ui-cancel-running-test');
    await expect(page.locator('#executionMeta')).toContainText('Cancelled');
    await expect(page.locator('#executionDetail')).toContainText('tick-2');
    await expect(page.locator('#executionCancellationSummary')).toContainText('Requested by user during Build');
    await expect(page.locator('#btnCancelExecution')).toBeHidden();
    await expect(page.locator('#btnRevalidateExecution')).toBeHidden();
    await expect(page.locator('#btnPublishExecution')).toBeHidden();
    await expectWrappedExecutionSteps(page);
    await capture(page, testInfo, 'cancellation-desktop-logs-cancelled-detail', {fullPage: false});
  } else {
    journey.releaseTerminal();
    await expect(page.locator('#testRunState')).toHaveText('Cancelled');
    await expect(page.locator('#testRunLiveLog')).toBeHidden();
    await expect(page.locator('#btnTestRunCancel')).toBeHidden();
    await expect(page.locator('#btnTestRunBuild')).toBeHidden();
    await capture(page, testInfo, 'cancellation-mobile-test-cancelled', {fullPage: false});
    await page.locator('#btnTestRunLogs').click();
    await expect(page.locator('#view-logs')).toHaveClass(/active/);
    await expectWrappedExecutionSteps(page);
  }
});

test('running Build cancellation returns retry actions without validation or publication', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop real-Build UI journey');
  const journey = await routeCancellationJourney(page, {runId: 'ui-cancel-running-build', mode: 'build'});
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('debbuilder');
  await page.locator('#btnBuildReal').click();
  await expect(page.locator('#appDialog')).toBeVisible();
  await page.locator('#appDialogConfirm').click();
  await expect(page.locator('#view-logs')).toHaveClass(/active/);
  await expect(page.locator('#executionMeta')).toContainText('Running');
  await expect(page.locator('#btnCancelExecution')).toBeEnabled();
  await capture(page, testInfo, 'cancellation-desktop-build-running', {fullPage: false});

  const click = page.locator('#btnCancelExecution').click();
  await expect(page.locator('#btnCancelExecution')).toBeDisabled();
  await expect(page.locator('#btnCancelExecution')).toHaveText('Cancelling…');
  journey.releaseCancelResponse();
  await click;
  await expect(page.locator('#executionMeta')).toContainText('Cancelling');
  await capture(page, testInfo, 'cancellation-desktop-build-cancelling', {fullPage: false});
  journey.releaseTerminal();
  await expect(page.locator('#executionMeta')).toContainText('Cancelled');
  await expect(page.locator('#executionDetail')).toContainText('worker-ready');
  await expect(page.locator('#btnCancelExecution')).toBeHidden();

  await openView(page, 'packages');
  await page.locator('[data-package-name="debbuilder"][data-admin-action="open-package"]').click();
  const actions = page.locator('#packageDetail .package-action-bar');
  await expect(actions.getByRole('button', {name: 'Test'})).toBeVisible();
  await expect(actions.getByRole('button', {name: 'Build'})).toBeVisible();
  await expect(actions.getByRole('button', {name: 'Validate'})).toHaveCount(0);
  await expect(actions.getByRole('button', {name: 'Publish'})).toHaveCount(0);
});

test('terminal race refreshes canonical state after an informational 409', async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'One shared Logs surface covers the terminal race');
  const journey = await routeCancellationJourney(page, {
    runId: 'ui-cancel-terminal-race', mode: 'build', cancelStatus: 409, terminalStatus: 'success',
  });
  await page.evaluate(async runId => {
    switchView('logs');
    await openExecution(runId);
  }, 'ui-cancel-terminal-race');
  await expect(page.locator('#executionMeta')).toContainText('Running');
  await page.locator('#btnCancelExecution').click();
  await expect(page.locator('.toast-region')).toContainText('Run already finished.');
  await expect(page.locator('.toast-region')).not.toContainText('Cancellation failed');
  await expect(page.locator('#executionMeta')).toContainText('Success');
  await expect(page.locator('#btnCancelExecution')).toBeHidden();
  expect(journey.snapshot().cancelCalls).toBe(1);
  page.uiErrors = page.uiErrors.filter(message => !message.includes('status of 409 (Conflict)'));
});

test('Recipe Test modal presents a failed diagnostic', async ({page}, testInfo) => {
  await page.route('**/api/run', route => route.fulfill({status:202, contentType:'application/json', body:JSON.stringify({run_id:'ui-04-build-failed', status:'queued'})}));
  await openView(page, 'recipes');
  await page.locator('#workflowSelect').selectOption('seerr');
  await page.locator('#btnDryRun').click();
  await expect(page.locator('#testRunDialog')).toBeVisible();
  await expect(page.locator('#testRunState')).toHaveText('Test failed');
  await expect(page.locator('#testRunFailed')).toContainText('Build command failed');
  await capture(page, testInfo, 'recipe-test-failed', {fullPage:false});
  await page.locator('#btnTestRunClose').click();
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
