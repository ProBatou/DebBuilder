const adminState = {
  packages: [],
  executions: [],
  selectedPackage: null,
  selectedExecution: null,
  executionAction: null,
  logPollTimer: null,
  logOffset: 0,
  logVerbosity: 'normal',
  logAutoScroll: true,
  logFollowing: false,
};

function badge(status) {
  const value = status || 'unknown';
  return `<span class="badge ${esc(value)}">${esc(STATUS_LABELS[value] || value)}</span>`;
}

function packageLabelForExecution(execution) {
  if (execution.package) return execution.package;
  const match = adminState.packages.find(packageRow => execution.id && execution.id.includes(packageRow.name));
  return match ? match.name : execution.action || 'Operation';
}

function switchView(name) {
  closeMobileNav();
  closeLogDetail();
  if (name !== 'settings' && typeof flushSettingsAutosave === 'function') flushSettingsAutosave().catch(() => {});
  if (name !== 'logs') stopLogPolling();
  document.querySelectorAll('.nav-link').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === 'view-' + name));
  if (name === 'packages') loadPackages();
  if (name === 'logs') loadExecutions();
  if (name === 'settings') loadSettings();
  if (name === 'dashboard') loadDashboard();
}

function isMobileViewport() {
  return window.matchMedia('(max-width: 899px)').matches;
}

function setMobileNav(open) {
  document.body.classList.toggle('mobile-nav-open', !!open);
  const button = $('btnMobileMenu');
  if (button) button.setAttribute('aria-expanded', open ? 'true' : 'false');
  const backdrop = $('mobileNavBackdrop');
  if (backdrop) backdrop.hidden = !open;
}

function closeMobileNav() {
  setMobileNav(false);
}

function closeLogDetail() {
  document.body.classList.remove('mobile-log-open');
}

function handleResponsiveResize() {
  closeMobileNav();
  closeLogDetail();
}

function setSidebarCompact(compact) {
  document.body.classList.toggle('sidebar-collapsed', !!compact);
  const button = $('btnSidebarCompact');
  if (!button) return;
  button.setAttribute('aria-pressed', compact ? 'true' : 'false');
  button.setAttribute('aria-label', compact ? 'Expand sidebar' : 'Collapse sidebar');
  button.title = compact ? 'Expand sidebar' : 'Collapse sidebar';
}

function toggleSidebarCompact() {
  const compact = !document.body.classList.contains('sidebar-collapsed');
  localStorage.setItem('debBuilderSidebarCompact', compact ? '1' : '0');
  setSidebarCompact(compact);
}

function restoreSidebarPreference() {
  setSidebarCompact(localStorage.getItem('debBuilderSidebarCompact') === '1');
}

async function copyInstallCommand() {
  const status = $('status');
  const command = status?.textContent?.trim();
  if (!status || !command || !command.startsWith('curl ')) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(command);
  } else {
    const input = document.createElement('textarea');
    input.value = command;
    input.style.position = 'fixed';
    input.style.opacity = '0';
    document.body.appendChild(input);
    input.select();
    document.execCommand('copy');
    input.remove();
  }
  status.classList.add('copied');
  setTimeout(() => status.classList.remove('copied'), 1200);
}

async function handleAdminAction(element) {
  const action = element.dataset.adminAction;
  const packageName = element.dataset.packageName;
  const executionId = element.dataset.executionId;
  if (action === 'open-dashboard-package') {
    switchView('packages');
    await openPackage(packageName);
  } else if (action === 'open-dashboard-execution') {
    switchView('logs');
    await openExecution(executionId);
  } else if (action === 'open-package') await openPackage(packageName);
  else if (action === 'open-recipe') await openLinkedRecipe(element.dataset.recipeId);
  else if (action === 'create-recipe') await createRecipeForPackage(packageName);
  else if (action === 'build-package') await buildPackage(packageName, element.dataset.dryRun === 'true');
  else if (action === 'validate-package') await validatePackage(packageName);
  else if (action === 'publish-package') await publishPackage(packageName);
  else if (action === 'delete-package') await deletePackageUi(packageName);
  else if (action === 'open-history-execution') {
    closePackageDrawer();
    switchView('logs');
    await openExecution(executionId);
  } else if (action === 'open-execution') await openExecution(executionId);
}

function wireAdmin() {
  restoreSidebarPreference();
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  document.addEventListener('click', event => {
    const element = event.target.closest('[data-admin-action]');
    if (!element) return;
    event.stopPropagation();
    handleAdminAction(element).catch(error => alert(error.message));
  });
  $('packageSearch')?.addEventListener('input', renderPackages);
  $('packageFilter')?.addEventListener('change', renderPackages);
  $('logSearch')?.addEventListener('input', renderExecutions);
  $('logStatus')?.addEventListener('change', renderExecutions);
  $('logVerbosity')?.addEventListener('change', event => changeLogVerbosity(event.target.value));
  $('btnDeleteExecutionLog')?.addEventListener('click', () => {
    if (adminState.selectedExecution) deleteExecutionLog(adminState.selectedExecution.id).catch(error => alert(error.message));
  });
  $('btnRevalidateExecution')?.addEventListener('click', () => {
    if (adminState.selectedExecution) validateExecution(adminState.selectedExecution.id).catch(error => alert(error.message));
  });
  $('btnLogLiveBadge')?.addEventListener('click', resumeLiveLog);
  $('executionDetail')?.addEventListener('scroll', handleLogScroll);
  $('executionMeta')?.addEventListener('click', event => {
    const button = event.target.closest('[data-copy-value]');
    if (!button) return;
    copyTextValue(button.dataset.copyValue || '').then(() => {
      button.classList.add('copied');
      setTimeout(() => button.classList.remove('copied'), 900);
    }).catch(() => {});
  });
  $('btnNewPackage')?.addEventListener('click', () => createPackageUi().catch(error => alert(error.message)));
  $('btnNewRecipe')?.addEventListener('click', newRecipeUi);
  $('recipeMetaName')?.addEventListener('input', event => {$('recipeTitle').textContent = event.target.value || 'Recipe';});
  $('newRecipePackage')?.addEventListener('change', event => syncNewRecipeFromPackage(event.target.value));
  $('newRecipeForm')?.addEventListener('submit', event => {
    event.preventDefault();
    if (event.currentTarget.reportValidity()) createRecipeFromDialog().catch(reportAutosaveError);
  });
  $('cancelNewRecipe')?.addEventListener('click', () => $('newRecipeDialog').close());
  $('btnClosePackageDrawer')?.addEventListener('click', closePackageDrawer);
  $('packageDrawerBackdrop')?.addEventListener('click', closePackageDrawer);
  $('btnSidebarCompact')?.addEventListener('click', toggleSidebarCompact);
  $('status')?.addEventListener('click', () => copyInstallCommand().catch(() => {}));
  $('status')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      copyInstallCommand().catch(() => {});
    }
  });
  $('btnMobileMenu')?.addEventListener('click', () => setMobileNav(!document.body.classList.contains('mobile-nav-open')));
  $('mobileNavBackdrop')?.addEventListener('click', closeMobileNav);
  $('btnCloseLogDetail')?.addEventListener('click', closeLogDetail);
  $('btnAddSourceChange')?.addEventListener('click', () => openSourceChangeDialog());
  $('sourceChangeList')?.addEventListener('click', event => {
    const edit = event.target.closest('[data-edit-change-index]');
    const remove = event.target.closest('[data-remove-change-index]');
    if (edit) openSourceChangeDialog('replace', Number(edit.dataset.editChangeIndex));
    if (remove) {
      window.recipeSourceChanges.splice(Number(remove.dataset.removeChangeIndex), 1);
      renderSourceChanges();
      scheduleRecipeAutosave();
    }
  });
  document.querySelectorAll('[data-change-type]').forEach(button => button.addEventListener('click', () => selectSourceChangeType(button.dataset.changeType)));
  ['btnCloseSourceChange', 'btnCancelSourceChange'].forEach(id => $(id)?.addEventListener('click', () => $('sourceChangeDialog').close()));
  $('btnConfirmSourceChange')?.addEventListener('click', confirmSourceChange);
  $('btnEditBuildCommands')?.addEventListener('click', () => {
    $('buildCommandPreview').classList.toggle('hidden');
    $('buildCommandEditor').classList.toggle('hidden');
    $('btnEditBuildCommands').textContent = $('buildCommandEditor').classList.contains('hidden') ? 'Edit commands' : 'View preview';
  });
  $('buildOutputMode')?.addEventListener('change', event => {
    setBuildOutputMode(event.target.value);
    const output = collectBuildOutput();
    if (output.mode === 'source' || output.path || (output.paths || []).length) scheduleRecipeAutosave();
  });
  $('buildOutputPath')?.addEventListener('input', event => {
    window.recipeBuildOutput.path = event.target.value;
    renderInstallContentSummary();
    if (event.target.value.trim()) scheduleRecipeAutosave();
  });
  $('btnAddBuildOutputPath')?.addEventListener('click', () => addBuildOutputPath());
  $('buildOutputPathList')?.addEventListener('input', event => {
    const field = event.target.closest('input[data-output-path-index]');
    if (!field) return;
    window.recipeBuildOutput.paths[Number(field.dataset.outputPathIndex)] = field.value;
    renderInstallContentSummary();
    if (field.value.trim()) scheduleRecipeAutosave();
  });
  $('buildOutputPathList')?.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-output-path]');
    if (remove) {
      removeBuildOutputPath(Number(remove.dataset.removeOutputPath));
      if (window.recipeBuildOutput.paths.length) scheduleRecipeAutosave();
    }
  });
  $('buildOutputSuggestionList')?.addEventListener('click', event => {
    const add = event.target.closest('[data-add-output-suggestion]');
    if (!add) return;
    setBuildOutputMode('paths');
    addBuildOutputPath(add.dataset.addOutputSuggestion);
    scheduleRecipeAutosave();
  });
  $('btnAddInstallMapping')?.addEventListener('click', addInstallMapping);
  $('installMappingList')?.addEventListener('input', event => {
    const keys = ['source', 'destination', 'policy', 'owner', 'group', 'mode'];
    const key = keys.find(item => event.target.closest(`[data-install-mapping-${item}]`));
    if (!key) return;
    const field = event.target.closest(`[data-install-mapping-${key}]`);
    const index = Number(field.dataset[`installMapping${key[0].toUpperCase()}${key.slice(1)}`]);
    window.recipeInstallMappings[index][key] = field.value;
    if (window.recipeInstallMappings[index].source.trim() && window.recipeInstallMappings[index].destination.trim()) scheduleRecipeAutosave();
  });
  $('installMappingList')?.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-install-mapping]');
    if (!remove) return;
    removeInstallMapping(Number(remove.dataset.removeInstallMapping));
    scheduleRecipeAutosave();
  });
  $('btnAddBuildDependency')?.addEventListener('click', () => {
    const dependency = prompt('Debian build dependency name');
    if (dependency && !window.recipeExtraDependencies.includes(dependency.trim())) {
      window.recipeExtraDependencies.push(dependency.trim());
      renderDependencyChips();
      scheduleRecipeAutosave();
    }
  });
  $('buildDependencyChips')?.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-dependency]');
    if (remove) {
      window.recipeExtraDependencies.splice(Number(remove.dataset.removeDependency), 1);
      renderDependencyChips();
      scheduleRecipeAutosave();
    }
  });
  document.addEventListener('keydown', event => {
    const action = event.target.closest('[data-admin-action]');
    if (action && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      action.click();
      return;
    }
    if (event.key === 'Escape') {
      closeMobileNav();
      closeLogDetail();
      closePackageDrawer();
    }
  });
  window.addEventListener('resize', handleResponsiveResize);
  window.addEventListener('orientationchange', () => setTimeout(handleResponsiveResize, 250));
  handleResponsiveResize();
  loadDashboard().catch(error => {
    if ($('latestOperations')) $('latestOperations').textContent = error.message;
  });
  loadExecutions().catch(() => {});
}

wireAdmin();
