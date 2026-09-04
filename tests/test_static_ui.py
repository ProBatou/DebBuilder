import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StaticUiTests(unittest.TestCase):
    def test_execution_lifecycle_actions_cover_all_run_states(self):
        core = self.read("static/ui_core.js")
        scenarios = [
            {"name": "validation_needed", "run": {"mode": "build", "status": "success", "artifact": {"path": "demo.deb"}}},
            {"name": "validation_running", "pending": "validation", "run": {"mode": "build", "status": "success", "artifact": {"path": "demo.deb"}}},
            {"name": "ready_to_publish", "run": {"mode": "build", "status": "success", "artifact": {"path": "demo.deb"}, "validations": [{"status": "success"}]}},
            {"name": "validation_failed", "run": {"mode": "build", "status": "success", "artifact": {"path": "demo.deb"}, "validations": [{"status": "failed"}]}},
            {"name": "published", "run": {"mode": "build", "status": "success", "artifact": {"path": "demo.deb"}, "validations": [{"status": "success"}], "publications": [{"status": "success"}]}},
            {"name": "build_failed", "run": {"mode": "build", "status": "failed", "artifact": {"path": "demo.deb"}}},
            {"name": "dry_run", "run": {"mode": "dry_run", "status": "prepared", "artifact": {}}},
        ]
        script = "global.window={};\n" + core + "\nconst scenarios=" + json.dumps(scenarios) + "; process.stdout.write(JSON.stringify(Object.fromEntries(scenarios.map(row=>[row.name,executionLifecycleModel(row.run,row.pending||'')]))));"
        models = json.loads(subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True).stdout)
        self.assertTrue(models["validation_needed"]["canValidate"])
        self.assertFalse(models["validation_needed"]["canPublish"])
        self.assertEqual(models["validation_running"]["validationStatus"], "running")
        self.assertTrue(models["validation_running"]["validationDisabled"])
        self.assertTrue(models["ready_to_publish"]["canPublish"])
        self.assertEqual(models["validation_failed"]["validationStatus"], "failed")
        self.assertTrue(models["validation_failed"]["canValidate"])
        self.assertFalse(models["published"]["canValidate"])
        self.assertFalse(models["published"]["canPublish"])
        self.assertFalse(models["build_failed"]["canValidate"])
        self.assertFalse(models["dry_run"]["canValidate"])
        self.assertFalse(models["dry_run"]["canPublish"])

    def test_execution_detail_exposes_existing_validation_and_publication_endpoints(self):
        html = self.read("static/index.html")
        admin = self.read("static/admin.js")
        self.assertIn('id="executionLifecycle"', html)
        self.assertIn('data-execution-action="validate"', admin)
        self.assertIn('data-execution-action="publish"', admin)
        self.assertIn("/validate`,{}", admin)
        self.assertIn("/publish`,{confirm:confirmation}", admin)
        self.assertIn("confirmation=`publish:${packageName}:${packageVersion}`", admin)
        self.assertIn("model.validationStatus==='failed'?lifecycleFailureDetails", admin)

    def test_recipe_serialization_preserves_advanced_pipeline_fields(self):
        script = (ROOT / "static/recipe_serialization.js").read_text()
        self.assertIn("output: collectBuildOutput()", script)
        self.assertIn("advanced.timeout || 120", script)
        self.assertIn("advanced.version_revision || '1'", script)
        self.assertIn("advanced.service_working_directory", script)

    def test_multiple_build_output_paths_are_fully_editable(self):
        html = self.read("static/index.html")
        serialization = self.read("static/recipe_serialization.js")
        admin = self.read("static/admin.js")
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
        admin = (ROOT / "static" / "admin.js").read_text()
        self.assertIn("pending:'○'", admin)
        self.assertIn("failed:'✕'", admin)
        self.assertIn("s.status||'pending'", admin)
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
        css = self.read("static/style.css")
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
        css = self.read("static/style.css")
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
        admin = self.read("static/admin.js")
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

    def test_account_provisioning_uses_explicit_intent_and_legacy_override(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        serialization = self.read("static/recipe_serialization.js")
        self.assertIn("Ensure account exists", html)
        self.assertIn("Use existing account", html)
        self.assertIn("Custom (legacy)", html)
        self.assertNotIn("Create application user", html)
        self.assertNotIn("Create application group", html)
        self.assertIn("user === 'root' && group === 'root'", app)
        self.assertIn("accountProvisioning === 'ensure' ? accountUser !== 'root'", serialization)
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
        admin = self.read("static/admin.js")
        settings = self.read("static/settings.js")
        serialization = self.read("static/recipe_serialization.js")
        css = self.read("static/style.css")
        self.assertEqual(html.count('<span class="nav-icon" aria-hidden="true"><svg'), 5)
        for emoji in ("📊", "📦", "🧱", "📋", "⚙️"):
            self.assertNotIn(emoji, html)
        self.assertNotIn('id="dashboardHealthState"', html)
        self.assertNotIn("function renderHealthState", admin)
        self.assertIn("dashboard-repo-summary", admin + css)
        self.assertIn("No build commands required.", html + serialization)
        self.assertIn("--control-height:38px", css)
        self.assertIn('class="span-2 boolean-field"', html)
        self.assertNotIn("settingAllowRealRun", settings)
        self.assertNotIn("settingAllowUnsafeBuild", settings)
        self.assertNotIn("settingBuildTempDir", settings)
        self.assertIn('id="btnSaveSettings"', settings)

    def test_installation_preview_is_derived_from_build_output_for_all_modes(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        admin = self.read("static/admin.js")
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
        self.assertIn("renderInstallContentSummary();if(event.target.value.trim())", admin)
        self.assertIn("output: collectBuildOutput()", serialization)
        self.assertNotIn("installOutputPath", html + app + serialization)

    def test_automatic_and_custom_mappings_share_one_visual_component(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        serialization = self.read("static/recipe_serialization.js")
        css = self.read("static/style.css")
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
        admin = self.read("static/admin.js")
        css = self.read("static/style.css")
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
        self.assertIn("function dashboardLifecycleState(p){\n  return lifecycleState(p);\n}", admin)
        self.assertNotIn("Success / Published", labels)

    def test_dashboard_packages_and_details_use_canonical_lifecycle_status(self):
        admin = self.read("static/admin.js")
        self.assertIn("badge(e.lifecycle_status||e.status)", admin)
        self.assertIn("function lifecycleState(p){ return p.lifecycle_display_status", admin)
        self.assertIn("Current lifecycle", admin)
        self.assertIn("Latest built version", admin)
        self.assertIn("Latest run status", admin)
        self.assertIn("remains published", admin)
        self.assertIn("state==='validation_needed'||state==='validation_failed'", admin)
        self.assertIn("state==='ready_to_publish'||state==='publication_failed'", admin)
        self.assertIn("async function validatePackage", admin)

    def test_build_audit_displays_effective_command_and_working_directory(self):
        app = (ROOT / "static" / "app.js").read_text()
        admin = (ROOT / "static" / "admin.js").read_text()
        self.assertIn("function formatBuildAudit", app)
        self.assertIn("row.command", app)
        self.assertIn("row.working_directory", app)
        self.assertIn("formatBuildAudit(buildStep?.details)", admin)

    def test_sidebar_can_collapse_and_copy_install_command(self):
        html = self.read("static/index.html")
        admin_js = self.read("static/admin.js")
        css = self.read("static/style.css")
        self.assertIn('id="btnSidebarCompact"', html)
        self.assertIn("debBuilderSidebarCompact", admin_js)
        self.assertIn("copyInstallCommand", admin_js)
        self.assertIn("copied", css)
        self.assertNotIn("cop" + "ié", css)

    def test_settings_page_is_english_and_single_language(self):
        html = self.read("static/index.html")
        settings_js = self.read("static/settings.js")
        self.assertIn("Editable settings", html)
        self.assertIn("OIDC authentication", settings_js)
        self.assertIn("ntfy token (optional)", settings_js)

    def test_javascript_files_do_not_reference_removed_visual_runtime(self):
        for path in ["static/app.js", "static/admin.js", "static/recipe_serialization.js", "static/ui_core.js", "static/settings.js"]:
            text = self.read(path)
            self.assertNotIn("Block" + "ly", text)
            self.assertNotIn("block" + "ly.", text.lower())

    def test_css_has_recipe_layout_sections(self):
        css = self.read("static/style.css")
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
        admin_js = self.read("static/admin.js")
        css = self.read("static/style.css")
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
            "static/admin.js",
            "static/settings.js",
            "static/recipe_serialization.js",
        ])
        self.assertNotIn("apt_" + "block" + "ly", combined)
        self.assertNotIn("apt-" + "block" + "ly", combined)


if __name__ == "__main__":
    unittest.main()
