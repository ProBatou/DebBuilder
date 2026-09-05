import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StaticUiTests(unittest.TestCase):
    ADMIN_SCRIPTS = (
        "static/js/pages/dashboard.js",
        "static/js/pages/packages.js",
        "static/js/pages/logs.js",
        "static/js/recipe/source_changes.js",
        "static/js/recipe/stepper.js",
        "static/js/admin.js",
    )
    STYLESHEETS = ("static/style.css", "static/css/components.css", "static/css/pages.css")

    def admin_scripts(self):
        return "\n".join(self.read(path) for path in self.ADMIN_SCRIPTS)

    def styles(self):
        return "\n".join(self.read(path) for path in self.STYLESHEETS)

    def test_package_actions_expose_canonical_validation_and_publication_endpoints(self):
        admin = self.admin_scripts()
        self.assertIn('data-admin-action="validate-package"', admin)
        self.assertIn('data-admin-action="publish-package"', admin)
        self.assertIn('id="btnRevalidateExecution"', self.read("static/index.html"))
        self.assertIn('id="btnPublishExecution"', self.read("static/index.html"))
        self.assertIn("function updateExecutionActionButtons", admin)
        self.assertRegex(admin, r"validation\.status\s*\?\s*'Revalidate'\s*:\s*'Validate'")
        self.assertIn("/validate`, {}", admin)
        self.assertIn("/publish`, {confirm: confirmation}", admin)
        self.assertIn("confirmation = `publish:${name}:${version}`", admin)
        self.assertNotIn("function renderExecutionLifecycle", admin)

    def test_generated_controls_do_not_use_inline_event_handlers(self):
        html = self.read("static/index.html")
        admin = self.admin_scripts()
        self.assertNotIn("onclick=", html)
        self.assertNotIn("onclick=", admin)
        self.assertIn("function handleAdminAction", admin)
        self.assertIn("[data-admin-action]", admin)

    def test_logs_page_has_active_selection_deletion_live_refresh_and_verbosity(self):
        html = self.read("static/index.html")
        admin = self.admin_scripts()
        css = self.styles()
        self.assertIn('role="listbox"', html)
        self.assertNotIn('id="btnDeleteSelectedLogs"', html)
        self.assertNotIn('data-select-execution', admin)
        self.assertNotIn('selectedExecutionIds', admin)
        self.assertNotIn('deleteSelectedLogs', admin)
        self.assertIn('id="btnDeleteExecutionLog"', html)
        self.assertIn('id="btnPublishExecution"', html)
        self.assertIn('id="logVerbosity"', html)
        self.assertNotIn('id="logLiveStatus"', html)
        self.assertNotIn('id="btnResumeLive"', html)
        self.assertIn('id="btnLogLiveBadge"', html)
        self.assertIn('class="log-terminal"', html)
        self.assertNotIn('id="executionLifecycle"', html)
        for value in ("compact", "normal", "verbose", "raw"):
            self.assertIn(f'<option value="{value}"', html)
        self.assertIn('aria-selected="${executionIsSelected(execution.id) ?', admin)
        self.assertIn('/logs?verbosity=', admin)
        self.assertIn('after=${adminState.logOffset}', admin)
        self.assertIn("adminState.logVerbosity === 'raw'", admin)
        self.assertIn('date.toLocaleString(undefined', admin)
        self.assertIn("second: '2-digit'", admin)
        self.assertIn('return actionPending ? 500 : executionIsLive(execution) ? 1500 : 5000', admin)
        self.assertIn('stopLogPolling();', admin)
        self.assertIn("adminState.logAutoScroll ? '● Live' : '↓ Jump to latest'", admin)
        self.assertNotIn("textContent='Done'", admin)
        self.assertIn("logAutoScroll: true", admin)
        self.assertIn("logFollowing: false", admin)
        self.assertIn("adminState.logFollowing = executionIsLive", admin)
        self.assertIn("function applyCanonicalExecution", admin)
        self.assertIn("syncExecutionListEntry(execution)", admin)
        self.assertIn("execution?.lifecycle_active === true", admin)
        self.assertIn("execution?.allowed_actions || {}", admin)
        self.assertIn("['failed', 'build_failed', 'validation_failed', 'publication_failed']", admin)
        self.assertNotIn("function executionCanValidateAgain", admin)
        self.assertIn("async function publishExecution", admin)
        self.assertIn("['Lifecycle', lifecycle]", admin)
        self.assertIn("function handleLogScroll", admin)
        self.assertIn("function resumeLiveLog", admin)
        self.assertIn("btnLogLiveBadge", admin)
        self.assertIn(".log-live-badge", css)
        self.assertIn('Delete log/history for this execution?', admin)
        self.assertIn('Package:', admin)
        self.assertIn('Run ID:', admin)
        self.assertIn('Date:', admin)
        self.assertIn('.execution-item.active', css)
        self.assertIn('[aria-selected="true"]', css)
        self.assertRegex(css, r'overflow-x:\s*hidden')

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_logs_canonical_state_refresh_updates_list_detail_and_actions(self):
        subprocess.run(
            ["node", "tests/js/test_logs_state.js"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_settings_page_has_global_safe_log_cleanup(self):
        settings = self.read("static/settings.js")
        self.assertIn('id="btnClearLogs"', settings)
        self.assertIn('Clear execution history', settings)
        self.assertIn('Remove visible execution history and detailed logs', settings)
        self.assertIn('/api/executions/delete-logs', settings)
        self.assertIn('dry_run:true', settings)
        self.assertIn('all:true', settings)
        self.assertIn('does not delete any Recipe, package, published APT entry, build artifact, manifest, validation, or publication state', settings)
        self.assertIn('expectedAbsentIds', settings)
        self.assertNotRegex(settings, r"status\.textContent\s*=\s*`?Cleared")

    def test_settings_page_exposes_lifecycle_automation(self):
        settings = self.read("static/settings.js")
        self.assertIn('id="settingAutoValidateAfterBuild"', settings)
        self.assertIn('Auto validate after successful build', settings)
        self.assertIn('id="settingAutoPublishAfterValidation"', settings)
        self.assertIn('Publish automatically after successful validation', settings)
        self.assertIn("function normalizeAutomationControls", settings)
        self.assertIn("if (changed === autoPublish && autoPublish.checked) autoValidate.checked = true", settings)
        self.assertIn("if (changed === autoValidate && !autoValidate.checked) autoPublish.checked = false", settings)
        self.assertIn('auto_validate_after_successful_build', settings)
        self.assertIn('auto_publish_after_successful_validation', settings)

    def test_recipe_serialization_preserves_advanced_pipeline_fields(self):
        script = (ROOT / "static/recipe_serialization.js").read_text()
        html = self.read("static/index.html")
        self.assertIn("output: collectBuildOutput()", script)
        self.assertIn("inactivity_timeout: value('buildInactivityTimeout') === '' ? null", script)
        self.assertIn("maximum_runtime: value('buildMaximumRuntime')", script)
        self.assertIn("Object.prototype.hasOwnProperty.call(build, 'inactivity_timeout')", script)
        self.assertNotIn("build.timeout", script)
        self.assertIn('id="buildInactivityTimeout"', html)
        self.assertIn("Inactivity timeout", html)
        self.assertIn('id="buildMaximumRuntime"', html)
        self.assertIn("Maximum runtime", html)
        self.assertIn("Unlimited", html)
        self.assertIn("advanced.version_revision || '1'", script)
        self.assertIn("advanced.service_working_directory", script)

    def test_multiple_build_output_paths_are_fully_editable(self):
        html = self.read("static/index.html")
        serialization = self.read("static/recipe_serialization.js")
        admin = self.admin_scripts()
        self.assertIn('<option value="paths">Multiple paths</option>', html)
        self.assertIn('id="buildOutputPathList"', html)
        self.assertIn('id="btnAddBuildOutputPath"', html)
        self.assertNotIn('id="buildExpectedOutput"', html)
        self.assertIn('data-remove-output-path', serialization)
        self.assertNotIn('data-move-output-path', serialization)
        self.assertNotIn('function moveBuildOutputPath', serialization)
        self.assertNotIn('value-origin configured-origin', serialization)
        self.assertIn('<span class="value-origin configured-origin">Configured</span>', html)
        self.assertIn('suggested-origin', serialization)
        self.assertIn('buildOutputIsComplete', serialization)
        self.assertIn("return {mode:'paths', paths:", serialization)
        self.assertIn("return {mode:'source'}", serialization)
        self.assertIn("paths:[...(configuredOutput.paths || [])]", serialization)
        self.assertIn('setBuildOutputMode', admin)
        self.assertIn('addBuildOutputPath', admin)

    def read(self, relative_path):
        return (ROOT / relative_path).read_text()

    def test_public_identity_is_debbuilder(self):
        html = self.read("static/index.html")
        self.assertIn("<title>DebBuilder</title>", html)
        self.assertIn("<strong>DebBuilder</strong>", html)
        self.assertNotIn("APT " + "Block" + "ly", html)

    def test_recipe_page_is_visual_mockup_without_removed_assets(self):
        html = self.read("static/index.html")
        self.assertIn("GitHub source", html)
        self.assertIn("Build", html)
        self.assertIn("Debian installation", html)
        self.assertIn("Maintainer scripts", html)
        self.assertIn("Service", html)
        self.assertIn("Advanced options", html)
        self.assertNotIn("Temporary technical output", html)
        self.assertNotIn("Generated script", html)
        self.assertNotIn("Step 5", html)
        self.assertNotIn("Publish</h2>", html)
        self.assertNotIn("block" + "ly", html.lower())

    def test_recipe_toolbar_keeps_only_simple_recipe_actions(self):
        html = self.read("static/index.html")
        for marker in ['id="workflowSelect"', 'id="btnNewRecipe"', 'id="btnDeleteRecipeTop"', 'id="btnDryRun"', 'id="btnBuildReal"', 'id="btnRuns"']:
            self.assertIn(marker, html)
        self.assertIn('id="recipeMetaActive" type="checkbox" checked hidden', html)

    def test_obsolete_technical_outputs_are_removed(self):
        html = self.read("static/index.html")
        for obsolete_id in ('id="jsonView"', 'id="script"', 'id="output"'):
            self.assertNotIn(obsolete_id, html)

    def test_recipe_form_has_no_fake_runtime_or_build_values(self):
        html = self.read("static/index.html")
        for fake in ("python3, ca-certificates", "NODE_ENV=production", "npm ci", "npm run build", "value=\"dist\""):
            self.assertNotIn(fake, html)

    def test_execution_steps_render_their_real_status(self):
        admin = self.admin_scripts()
        self.assertIn("pending: '○'", admin)
        self.assertIn("failed: '✕'", admin)
        self.assertIn("step.status || 'pending'", admin)
        self.assertNotIn('`<span class="step-chip">✓ ${esc(s.name)}</span>`', admin)

    def test_dry_run_displays_detection_proposals_without_executing_them(self):
        app = (ROOT / "static" / "app.js").read_text()
        self.assertIn("data.detection.proposed_commands", app)
        self.assertIn("detection.build_dependencies", app)
        self.assertIn("if (!(wf.build?.commands || []).length)", app)
        self.assertIn("renderBuildEnvironment(data.detection)", app)

    def test_build_environment_is_a_compact_ecosystem_summary_with_three_global_states(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        css = self.styles()
        self.assertIn('class="build-environment-summary"', html)
        self.assertIn('class="build-environment-project" id="buildDetectedProject"', html)
        self.assertIn('id="buildDependenciesSummary" hidden', html)
        self.assertIn('id="buildDetectedFiles"', html)
        self.assertNotIn('id="buildRuntimeFact"', html)
        self.assertNotIn('id="buildPackagingFact"', html)
        self.assertNotIn('class="build-environment-fact"', html)
        self.assertNotIn("$('buildDetectedRuntime')", app)
        self.assertNotIn("$('buildDetectedPackaging')", app)
        self.assertIn("buildDependencies.length === 0", app)
        self.assertIn("detectedFiles.join(' · ')", app)
        for marker in ("Detected", "Partially detected", "Not detected"):
            self.assertIn(marker, html + app)
        self.assertNotIn("detection.project_type === 'python' &&", app)
        for marker in (".detection-badge.detected", ".detection-badge.partially-detected", ".detection-badge.not-detected"):
            self.assertIn(marker, css)
        self.assertIn(".build-environment-summary", css)
        self.assertNotIn(".build-environment-facts", css)
        self.assertNotIn(".build-environment-fact{", css)

    def test_dependency_ui_distinguishes_all_four_states(self):
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()
        css = self.styles()
        serialization = (ROOT / "static" / "recipe_serialization.js").read_text()
        self.assertIn("Dependencies:", html)
        self.assertIn("Manually added", html)
        self.assertIn('id="buildDependencyPending"', html)
        self.assertIn('id="buildDependencyResults" hidden', html)
        self.assertIn('id="buildAvailableDependencies"', html)
        self.assertIn('id="buildMissingDependencies"', html)
        self.assertIn("function renderDependencyCheck(dependencies)", app)
        self.assertIn("renderDependencyCheck(data.dependencies)", app)
        self.assertIn("renderDependencyCheck();", serialization)
        self.assertIn(".dependency-check-results", css)
        self.assertNotIn(".build-dependency-state>span", css)
        self.assertIn("data-remove-dependency", serialization)

    def test_static_configured_files_mode_is_available(self):
        html = (ROOT / "static" / "index.html").read_text()
        serialization = (ROOT / "static" / "recipe_serialization.js").read_text()
        admin = self.admin_scripts()
        self.assertIn('<option value="configured_files">Custom mappings</option>', html)
        self.assertIn("Additional mappings", html)
        self.assertIn("Optionally install extra files from the selected build output", html)
        self.assertIn("configuredFiles ? 'Custom mappings' : 'Additional mappings'", self.read("static/app.js"))
        self.assertIn('id="btnAddInstallMapping"', html)
        self.assertIn('data-install-mapping-source', serialization)
        self.assertIn('data-install-mapping-destination', serialization)
        self.assertIn('data-install-mapping-policy', serialization)
        for label in ("Replace", "Preserve if existing", "Create if missing"):
            self.assertIn(label, serialization)
        self.assertIn('data-remove-install-mapping', serialization + admin)
        self.assertIn("config_files: collectInstallMappings()", serialization)
        self.assertNotIn('id="installConfigPolicy"', html)

    def test_static_mappings_only_replaces_build_output_with_intent_summary(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        self.assertIn('id="staticSourceSummary" hidden', html)
        self.assertIn("No build step is required.", html)
        self.assertIn("Source files are used directly by Debian installation mappings.", html)
        self.assertIn("dataset.value === 'static'", app)
        self.assertIn("lines(value('buildCommands')).length === 0", app)
        self.assertIn("$('buildCommandsSection').hidden = staticMappingsOnly", app)
        self.assertIn("$('buildOutputSection').hidden = staticMappingsOnly", app)
        self.assertIn('id="buildCommandsSection"', html)

    def test_static_custom_mapping_visibility_is_one_strict_combination(self):
        app = self.read("static/app.js")
        condition = "$('buildDetectedProject')?.dataset.value === 'static' && lines(value('buildCommands')).length === 0 && configuredFiles"
        self.assertIn(condition, app)
        self.assertIn("$('installDestination').closest('label').hidden = configuredFiles", app)
        self.assertIn("$('installAutomaticGroup').hidden = configuredFiles", app)
        self.assertIn("configuredFiles ? 'Custom mappings' : 'Additional mappings'", app)

    def test_upstream_archive_ui_uses_conditional_source_controls(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        serialization = self.read("static/recipe_serialization.js")
        self.assertIn('id="recipeArchiveSource"', html)
        self.assertIn('<option value="github_source">GitHub source archive</option>', html)
        self.assertIn('<option value="release_asset">Release asset</option>', html)
        self.assertIn('id="recipeAssetSelection"', html)
        self.assertIn('id="btnInspectArchive"', html)
        self.assertIn("archiveSource === 'release_asset'", app)
        self.assertIn("assetSelection === 'pattern'", app)
        self.assertIn("/api/upstream-archive/inspect", app)
        self.assertIn("artifact.archive_source = archiveSource", serialization)
        self.assertIn("artifact.asset_name = archiveSource === 'release_asset'", serialization)
        self.assertIn("artifactMode === 'upstream_deb' ? value('recipeArtifactPattern') : ''", serialization)

    def test_account_provisioning_uses_only_canonical_intent(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        serialization = self.read("static/recipe_serialization.js")
        self.assertIn("Ensure account exists", html)
        self.assertIn("Use existing account", html)
        self.assertNotIn("Custom (legacy)", html)
        self.assertNotIn("Legacy account overrides", html)
        self.assertNotIn("Create application user", html)
        self.assertNotIn("Create application group", html)
        self.assertIn("user === 'root' && group === 'root'", app)
        self.assertIn("accountProvisioning === 'ensure' && accountUser !== 'root'", serialization)
        self.assertIn('id="installAccountUser"', html)
        self.assertIn('id="installOwnerUser"', html)

    def test_service_configuration_is_derived_and_empty_state_is_compact(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        serialization = self.read("static/recipe_serialization.js")
        self.assertNotIn('id="serviceConfigured"', html)
        self.assertIn("No systemd service configured.", html)
        self.assertIn('id="btnConfigureService"', html)
        self.assertIn('id="btnRemoveService"', html)
        self.assertIn("Enable at boot", html)
        self.assertIn("window.recipeServiceVisible", app + serialization)
        self.assertIn("!!String(service.name || '').trim() && !!String(service.command || '').trim()", serialization)
        self.assertNotIn("configured: serviceConfigured", serialization)

    def test_final_ux_pass_uses_compact_shared_components(self):
        html = self.read("static/index.html")
        admin = self.admin_scripts()
        settings = self.read("static/settings.js")
        serialization = self.read("static/recipe_serialization.js")
        css = self.styles()
        self.assertEqual(html.count('<span class="nav-icon" aria-hidden="true"><svg'), 5)
        for emoji in ("📊", "📦", "🧱", "📋", "⚙️"):
            self.assertNotIn(emoji, html)
        self.assertNotIn('id="dashboardHealthState"', html)
        self.assertNotIn("function renderHealthState", admin)
        self.assertIn("dashboard-repo-summary", admin + css)
        self.assertIn("No build commands required.", html + serialization)
        self.assertRegex(css, r"--control-height:\s*38px")
        self.assertIn('class="span-2 boolean-field"', html)
        self.assertNotIn("settingAllowRealRun", settings)
        self.assertNotIn("settingAllowUnsafeBuild", settings)
        self.assertNotIn("settingBuildTempDir", settings)
        self.assertNotIn('id="btnSaveSettings"', settings)
        self.assertNotIn("settings-save-bar", settings + css)
        self.assertIn('id="settingsAutosaveStatus"', html)
        self.assertNotIn(">Saved</span>", settings)
        self.assertNotIn("Saving...", settings)
        self.assertIn("Save failed:", settings)
        self.assertIn("markSettingsDirty(isAutomation ? 0 : 500)", settings)
        self.assertIn("setTimeout(() => { saveAllSettings().catch(()=>{}); }, delay)", settings)
        self.assertIn("flushSettingsAutosave", settings)

    def test_logs_metadata_long_values_are_single_line_and_copyable(self):
        admin = self.admin_scripts()
        css = self.styles()
        self.assertIn("function metaValueHtml", admin)
        self.assertIn("middleTruncate(text, key === 'SHA-256' ? 22 : 30)", admin)
        self.assertIn('data-copy-value="${esc(text)}"', admin)
        self.assertIn("copyTextValue(button.dataset.copyValue || '')", admin)
        self.assertRegex(css, r"text-overflow:\s*ellipsis")
        self.assertIn(".meta-copy-value", css)
        self.assertNotIn(".logs-detail-card .meta-cell{min-width:0;overflow-wrap:anywhere", css)

    def test_installation_preview_is_derived_from_build_output_for_all_modes(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        admin = self.admin_scripts()
        serialization = self.read("static/recipe_serialization.js")
        self.assertIn("Which files and directories constitute the result of the build?", html)
        self.assertIn("Where should the build result be installed in the Debian package?", html)
        self.assertIn('<option value="build_output">Selected build output</option>', html)
        self.assertIn('id="installContentSummary"', html)
        self.assertNotIn("Full build output", html + app)
        self.assertIn("const output = collectBuildOutput();", app)
        self.assertIn("if (output.mode === 'paths')", app)
        self.assertIn("else if (output.mode === 'path')", app)
        self.assertIn("source:'Entire source tree'", app)
        self.assertIn("destination:`${destination}/${path.replace(/^\\.\\/+/, '')}`", app)
        self.assertIn("installMappingRowHtml(row)", app)
        self.assertIn("renderInstallContentSummary();", admin)
        self.assertIn("if (event.target.value.trim()) scheduleRecipeAutosave();", admin)
        self.assertIn("output: collectBuildOutput()", serialization)
        self.assertNotIn("installOutputPath", html + app + serialization)

    def test_automatic_and_custom_mappings_share_one_visual_component(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        serialization = self.read("static/recipe_serialization.js")
        css = self.styles()
        self.assertIn('id="installAutomaticGroup"', html)
        self.assertIn("These files are installed automatically", html)
        self.assertIn("function installMappingRowHtml", serialization)
        self.assertIn("installMappingRowHtml(row, {editable:true, index})", serialization)
        self.assertIn("installMappingRowHtml(row)", app)
        self.assertIn("install-mapping-row-automatic", serialization + css)
        self.assertNotIn("data-remove-install-mapping", serialization.split("if (!editable)", 1)[1].split("return `<article", 1)[0])
        self.assertIn("$('installAutomaticGroup').hidden = configuredFiles", app)

    def test_lifecycle_labels_and_colors_are_shared(self):
        labels = self.read("static/ui_core.js")
        admin = self.admin_scripts()
        css = self.styles()
        expected = {
            "up_to_date": "Up to date",
            "update_available": "Update available",
            "build_required": "Build needed",
            "build_success": "Validation needed",
            "validation_needed": "Validation needed",
            "publication_available": "Ready to publish",
            "published": "Published",
            "build_failed": "Build failed",
            "validating": "Validating",
            "validation_failed": "Validation failed",
            "publishing": "Publishing",
            "publication_failed": "Publication failed",
        }
        for state, label in expected.items():
            self.assertRegex(labels, rf"{state}: '{re.escape(label)}'")
            self.assertIn(f".badge.{state}", css)
        self.assertIn("STATUS_LABELS[value]", admin)
        self.assertIn("function dashboardLifecycleState(packageRow)", admin)
        self.assertIn("return lifecycleState(packageRow);", admin)
        self.assertNotIn("Success / Published", labels)

    def test_dashboard_packages_and_details_use_canonical_lifecycle_status(self):
        admin = self.admin_scripts()
        pages = self.read("static/css/pages.css")
        self.assertIn('latest-operation-row', admin)
        self.assertRegex(pages, r"\.latest-operation-row\s*\{[^}]*align-items:\s*center;")
        self.assertIn("badge(execution.lifecycle_status || execution.status)", admin)
        self.assertIn("function lifecycleState(packageRow)", admin)
        self.assertIn("Current lifecycle", admin)
        self.assertIn("Latest built version", admin)
        self.assertIn("Latest run status", admin)
        self.assertIn("remains published", admin)
        self.assertIn("const actions = packageRow.allowed_actions || {};", admin)
        self.assertIn("if (actions.validate)", admin)
        self.assertIn("if (actions.publish)", admin)
        self.assertIn("async function validatePackage", admin)

    def test_build_audit_is_available_through_verbose_logs(self):
        app = (ROOT / "static" / "app.js").read_text()
        admin = self.admin_scripts()
        self.assertNotIn("function formatBuildAudit", app)
        self.assertIn("logVerbosity", admin)
        self.assertIn("verbosity=verbose", self.read("tests/test_admin_api.py"))

    def test_sidebar_can_collapse_and_copy_install_command(self):
        html = self.read("static/index.html")
        admin_js = self.admin_scripts()
        css = self.styles()
        self.assertIn('id="btnSidebarCompact"', html)
        self.assertIn('class="sidebar-toggle"', html)
        self.assertIn('title="Collapse sidebar"', html)
        self.assertIn('aria-label="Collapse sidebar"', html)
        self.assertIn('<svg viewBox="0 0 24 24" aria-hidden="true">', html)
        self.assertIn('/style.css?v=20260905-1', html)
        self.assertIn('/css/components.css?v=20260905-3', html)
        self.assertIn('/css/pages.css?v=20260905-4', html)
        self.assertNotIn('/css/logs.css', html)
        for script in ("/js/pages/dashboard.js", "/js/pages/packages.js", "/js/pages/logs.js", "/js/recipe/source_changes.js", "/js/admin.js"):
            self.assertIn(script, html)
        self.assertIn('/ui_core.js?v=20260905-2', html)
        self.assertIn('/settings.js?v=20260905-4', html)
        self.assertIn('/js/pages/dashboard.js?v=20260905-2', html)
        self.assertIn('/js/pages/logs.js?v=20260905-6', html)
        self.assertIn('/js/admin.js?v=20260905-4', html)
        self.assertIn("debBuilderSidebarCompact", admin_js)
        self.assertIn("Expand sidebar", admin_js)
        self.assertIn("Collapse sidebar", admin_js)
        self.assertNotIn('>←</button>', html)
        self.assertNotIn("button.textContent = compact", admin_js)
        self.assertRegex(css, r"body\.sidebar-collapsed \.sidebar-toggle svg\s*\{[^}]*transform:\s*scaleX\(-1\)")
        self.assertIn("button:focus-visible", css)
        self.assertIn("copyInstallCommand", admin_js)
        self.assertIn("copied", css)
        self.assertNotIn("cop" + "ié", css)

    def test_stylesheets_follow_the_shared_component_architecture(self):
        html = self.read("static/index.html")
        pages = self.read("static/css/pages.css")
        self.assertIn('/style.css?v=20260905-1', html)
        self.assertIn('/css/components.css?v=20260905-3', html)
        self.assertIn('/css/pages.css?v=20260905-4', html)
        self.assertNotIn('/css/logs.css', html)
        self.assertFalse((ROOT / "static" / "css" / "logs.css").exists())
        self.assertNotRegex(self.styles(), r"nth-(?:child|of-type)\s*\(")
        self.assertIn(".mobile-log-open .logs-list-card", pages)
        self.assertIn(".mobile-log-open .logs-detail-card", pages)

    def test_stabilized_layouts_use_intrinsic_rows_and_explicit_groups(self):
        html = self.read("static/index.html")
        components = self.read("static/css/components.css")
        pages = self.read("static/css/pages.css")
        settings = self.read("static/settings.js")
        self.assertRegex(pages, r"#view-packages\s*\{[^}]*grid-auto-rows:\s*max-content;[^}]*align-content:\s*start;")
        self.assertRegex(pages, r"\.logs-list-card \.list\s*\{[^}]*grid-auto-rows:\s*max-content;[^}]*align-content:\s*start;")
        self.assertRegex(components, r"\.data-list,\s*\.list\s*\{[^}]*grid-auto-rows:\s*max-content;[^}]*align-content:\s*start;")
        for marker in (
            "install-package-fields", "install-destination-fields", "install-ownership-fields",
            "install-persistent-fields", "service-environment-fields", "service-dependency-fields",
            "service-lifecycle-fields", "service-command-hook-fields", "service-output-fields",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("grid-area: auto", pages)
        self.assertRegex(pages, r"\.recipe-form-group \.recipe-step-fields > label\s*\{[^}]*grid-row:\s*auto;")
        self.assertRegex(html, r'class="span-12"><span>Persistent directories</span><textarea id="installDirectories"')
        self.assertRegex(pages, r"\.service-environment-fields\s*\{[^}]*grid-template-columns:[^;}]*1fr[^;}]*2fr[^;}]*;")
        self.assertIn(".service-command-hook-fields", pages)
        self.assertIn("minmax(220px, 1fr)", pages)
        for row in ("settings-layout-row--general", "settings-layout-row--integrations", "settings-layout-row--automation"):
            self.assertIn(row, settings)
        self.assertRegex(pages, r"\.settings-editable-area\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;")
        self.assertRegex(pages, r"\.settings-layout-row\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);[^}]*align-items:\s*stretch;")
        self.assertIn('class="settings-section settings-card card maintenance-settings-card"', settings)

    def test_shared_feedback_replaces_native_browser_dialogs(self):
        html = self.read("static/index.html")
        core = self.read("static/ui_core.js")
        components = self.read("static/css/components.css")
        application_js = "\n".join(path.read_text() for path in (ROOT / "static").rglob("*.js"))
        self.assertIsNone(re.search(r"\b(?:alert|confirm|prompt)\s*\(", application_js))
        for element_id in ("toastRegion", "appDialog", "appDialogTitle", "appDialogDescription", "appDialogCancel", "appDialogConfirm"):
            self.assertIn(f'id="{element_id}"', html)
        for primitive in ("function showToast", "function showConfirm", "function showPrompt", "function settleAppDialog"):
            self.assertIn(primitive, core)
        self.assertIn("appDialogPreviousFocus", core)
        self.assertIn("dialog.addEventListener('cancel'", core)
        self.assertIn("event.target === dialog", core)
        self.assertIn(".toast-region", components)
        self.assertIn(".app-dialog", components)

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_logs_delete_clears_client_selection_without_reopening_the_run(self):
        subprocess.run(
            ["node", "tests/js/test_logs_delete.js"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_recipe_stepper_tracks_the_section_nearest_the_sticky_offset(self):
        subprocess.run(
            ["node", "tests/js/test_recipe_stepper.js"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_recipe_stepper_and_autosave_status_are_explicit(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        pages = self.read("static/css/pages.css")
        stepper = self.read("static/js/recipe/stepper.js")
        self.assertIn('class="recipe-stepper"', html)
        for step in ("source", "build", "install", "service"):
            self.assertIn(f'id="recipe-step-{step}"', html)
        self.assertIn('id="recipeAutosaveStatus"', html)
        self.assertIn("function setRecipeAutosaveState", app)
        for state in ("pending", "saving", "saved", "error"):
            self.assertIn(f"'{state}'", app)
        self.assertIn(".recipe-build-card.not-applicable", pages)
        self.assertIn(".recipe-install-card.not-applicable", pages)
        self.assertIn(".recipe-service-card.not-applicable", pages)
        self.assertIn("function updateActiveRecipeStep", stepper)
        self.assertIn("recipeStickyOffset()", stepper)
        self.assertIn("aria-current", stepper)
        self.assertIn('.recipe-stepper a.active', pages)

    def test_hidden_states_remain_authoritative(self):
        html = self.read("static/index.html")
        css = self.styles()
        self.assertRegex(css, r"\[hidden\]\s*\{\s*display:\s*none\s*!important;")
        self.assertRegex(css, r"\.hidden\s*\{\s*display:\s*none\s*!important;")
        for element_id in (
            "mobileNavBackdrop",
            "executionMoreDetails",
            "buildDependenciesSummary",
            "recipeArchiveInspectionField",
            "staticSourceSummary",
        ):
            self.assertRegex(html, rf'id="{element_id}"[^>]*\bhidden\b')

    def test_settings_page_is_english_and_single_language(self):
        html = self.read("static/index.html")
        settings_js = self.read("static/settings.js")
        self.assertIn("<h2>Settings</h2>", html)
        self.assertIn('id="settingsAutosaveStatus"', html)
        self.assertIn("OIDC authentication", settings_js)
        self.assertIn("ntfy token (optional)", settings_js)

    def test_javascript_files_do_not_reference_removed_visual_runtime(self):
        for path in ["static/app.js", "static/recipe_serialization.js", "static/ui_core.js", "static/settings.js", *self.ADMIN_SCRIPTS]:
            text = self.read(path)
            self.assertNotIn("Block" + "ly", text)
            self.assertNotIn("block" + "ly.", text.lower())

    def test_css_has_recipe_layout_sections(self):
        css = self.styles()
        for marker in [
            ".recipe-simple-toolbar",
            ".recipe-source-card",
            ".recipe-install-card",
            ".recipe-service-card",
            ".systemd-advanced",
            ".maintainer-script-grid",
            "body.sidebar-collapsed",
        ]:
            self.assertIn(marker, css)

    def test_recipe_mockup_is_english_and_has_consistent_visual_groups(self):
        html = self.read("static/index.html")
        admin_js = self.admin_scripts()
        css = self.styles()
        for marker in [
            "Build environment",
            "Source changes",
            "General service settings",
            'class="recipe-form-group service-command-group"',
            "Add source change",
        ]:
            self.assertIn(marker, html)
        self.assertIn("Result preview", admin_js)
        self.assertIn(".recipe-source-card .recipe-metadata", css)
        self.assertIn(".service-primary-groups", css)
        for french_text in ["Ajouter une modification", "Environnement de build", "Aperçu du résultat", "Modifier les commandes"]:
            self.assertNotIn(french_text, html + admin_js)

    def test_public_files_have_no_old_package_import_path(self):
        combined = "\n".join(self.read(path) for path in [
            "server.py",
            "static/index.html",
            "static/app.js",
            *self.ADMIN_SCRIPTS,
            "static/settings.js",
            "static/recipe_serialization.js",
        ])
        self.assertNotIn("apt_" + "block" + "ly", combined)
        self.assertNotIn("apt-" + "block" + "ly", combined)


if __name__ == "__main__":
    unittest.main()
