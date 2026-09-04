function dashboardLifecycleState(packageRow) {
  return lifecycleState(packageRow);
}

function metricCard(value, label, detail = '', tone = '') {
  return `<div class="metric dashboard-metric ${tone}"><strong>${esc(value)}</strong><span>${esc(label)}</span>${detail ? `<em>${esc(detail)}</em>` : ''}</div>`;
}

function compactPackageRow(packageRow) {
  const version = packageRow.version || {};
  const state = dashboardLifecycleState(packageRow);
  return `<div class="dashboard-package-row" role="button" tabindex="0" data-admin-action="open-dashboard-package" data-package-name="${esc(packageRow.name)}"><div class="dashboard-package-identity"><strong>${esc(packageRow.name)}</strong><span>${esc(sourceLabel(packageRow))}</span></div><div class="dashboard-package-version"><span>Published</span><strong>${esc(version.published || packageRow.apt_version || '—')}</strong></div><div class="dashboard-package-version"><span>Source</span><strong>${esc(version.source || packageRow.upstream_version || '—')}</strong></div><div class="dashboard-package-state">${badge(state)}</div></div>`;
}

function renderRepoState(settings, packages) {
  const apt = (settings || {}).apt || {};
  const architectures = [...new Set(packages.map(packageRow => packageRow.architecture || 'all'))].sort();
  const configured = !!(apt.repository && apt.distribution && apt.component);
  const repository = String(apt.repository || 'Not configured').replace(/^https?:\/\//, '').replace(/\/$/, '');
  return `<div class="dashboard-repo-summary"><h3>APT repository</h3><span>${esc(repository)} · ${esc(apt.distribution || '—')} · ${esc(apt.component || '—')} · ${esc(architectures.join(', ') || apt.architecture || '—')}</span>${statusBadge(configured ? 'CONFIGURED' : 'CHECK', configured ? 'active' : 'warning')}</div>`;
}

async function loadDashboard() {
  const [dashboardData, settingsData] = await Promise.all([getJson('/api/dashboard'), getJson('/api/settings')]);
  const dashboard = dashboardData.dashboard || {};
  const packages = dashboard.package_rows || [];
  adminState.packages = packages;
  renderPackageOptions();
  const counts = {
    total: dashboard.packages || 0,
    updateAvailable: dashboard.updates || 0,
    publicationAvailable: dashboard.ready_to_publish || 0,
    failed: dashboard.package_errors || 0,
    github: dashboard.github_sources || 0,
    local: dashboard.local_sources || 0,
  };
  $('dashboardMetrics').innerHTML = [
    metricCard(counts.total, 'Tracked packages', `${counts.github} GitHub · ${counts.local} local`),
    metricCard(counts.updateAvailable, 'Source updates', 'source > published', counts.updateAvailable ? 'warning' : ''),
    metricCard(counts.publicationAvailable, 'Ready to publish', 'verified build not published', counts.publicationAvailable ? 'success' : ''),
    metricCard(counts.failed + (dashboard.errors || 0), 'Alerts / errors', 'packages or executions', counts.failed || dashboard.errors ? 'danger' : ''),
  ].join('');
  if ($('dashboardRepoState')) $('dashboardRepoState').innerHTML = renderRepoState(settingsData.settings, packages);
  const rank = {build_failed: 0, validation_failed: 0, publication_failed: 0, failed: 0, validation_needed: 1, validating: 1, ready_to_publish: 2, publishing: 2, update_available: 3, publication_available: 3, build_available: 4, not_published: 4, recipe_missing: 5, unknown: 8, published: 9, up_to_date: 9, ready: 9};
  const priority = packages.slice().sort((left, right) =>
    (rank[dashboardLifecycleState(left)] ?? 7) - (rank[dashboardLifecycleState(right)] ?? 7)
      || left.name.localeCompare(right.name)
  ).slice(0, 12);
  if ($('dashboardPackageFlow')) $('dashboardPackageFlow').innerHTML = priority.map(compactPackageRow).join('') || '<p class="muted">No tracked packages.</p>';
  $('latestOperations').classList.add('latest-ops');
  $('latestOperations').innerHTML = (dashboard.latest_operations || []).map(execution => `<div class="item" role="button" tabindex="0" data-admin-action="open-dashboard-execution" data-execution-id="${esc(execution.id)}"><div class="item-title"><span>${esc(packageLabelForExecution(execution))} · ${esc(execution.action || 'build')}</span>${badge(execution.lifecycle_status || execution.status)}</div><div class="item-meta">Build ${esc(execution.build_status || execution.status)} · ${esc(STATUS_LABELS[execution.lifecycle_status] || execution.lifecycle_status || execution.status)} · ${esc(execution.id)} · ${fmtTime(execution.updated)}</div></div>`).join('') || '<p class="muted">No recent operation.</p>';
}
