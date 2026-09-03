import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StaticUiTests(unittest.TestCase):
    def test_recipe_serialization_preserves_advanced_pipeline_fields(self):
        script = (ROOT / "static/recipe_serialization.js").read_text()
        self.assertIn("output: collectBuildOutput()", script)
        self.assertIn("advanced.timeout || 120", script)
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
        self.assertIn('configured-origin', serialization)
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

    def test_build_environment_is_ecosystem_specific_and_has_three_global_states(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        css = self.read("static/style.css")
        self.assertIn('id="buildRuntimeFact" hidden', html)
        self.assertIn('id="buildPackagingFact" hidden', html)
        self.assertNotIn('id="buildPythonDetails"', html)
        self.assertIn("projectType === 'python'", app)
        self.assertIn("projectType === 'nodejs'", app)
        for marker in ("Detected", "Partially detected", "Not detected"):
            self.assertIn(marker, html + app)
        for marker in (".detection-badge.detected", ".detection-badge.partially-detected", ".detection-badge.not-detected"):
            self.assertIn(marker, css)

    def test_dependency_ui_distinguishes_all_four_states(self):
        html = (ROOT / "static" / "index.html").read_text()
        app = (ROOT / "static" / "app.js").read_text()
        serialization = (ROOT / "static" / "recipe_serialization.js").read_text()
        self.assertIn("Build dependencies", html)
        self.assertIn("Manually added", html)
        self.assertIn('id="buildAvailableDependencies"', html)
        self.assertIn('id="buildMissingDependencies"', html)
        self.assertIn("data.dependencies.available", app)
        self.assertIn("data.dependencies.missing", app)
        self.assertIn("data-remove-dependency", serialization)

    def test_static_configured_files_mode_is_available(self):
        html = (ROOT / "static" / "index.html").read_text()
        serialization = (ROOT / "static" / "recipe_serialization.js").read_text()
        admin = self.read("static/admin.js")
        self.assertIn('<option value="configured_files">Custom mappings</option>', html)
        self.assertIn('id="btnAddInstallMapping"', html)
        self.assertIn('data-install-mapping-source', serialization)
        self.assertIn('data-install-mapping-destination', serialization)
        self.assertIn('data-remove-install-mapping', serialization + admin)
        self.assertIn("config_files: collectInstallMappings()", serialization)

    def test_installation_explains_relative_path_preservation(self):
        html = self.read("static/index.html")
        app = self.read("static/app.js")
        self.assertIn("Which files and directories constitute the result of the build?", html)
        self.assertIn("Where should the build result be installed in the Debian package?", html)
        self.assertIn('id="installContentSummary"', html)
        self.assertIn("Relative paths are preserved below the install directory.", app)
        self.assertIn("`${path} → ${destination}/${path}`", app)

    def test_lifecycle_labels_and_colors_are_shared(self):
        labels = self.read("static/ui_core.js")
        admin = self.read("static/admin.js")
        css = self.read("static/style.css")
        expected = {
            "up_to_date": "Up to date",
            "update_available": "Update available",
            "build_required": "Build needed",
            "build_success": "Validation needed",
            "publication_available": "Ready to publish",
            "published": "Published",
            "build_failed": "Build failed",
            "validation_failed": "Validation failed",
            "publication_failed": "Publication failed",
        }
        for state, label in expected.items():
            self.assertRegex(labels, rf"{state}: '{re.escape(label)}'")
            self.assertIn(f".badge.{state}", css)
        self.assertIn("STATUS_LABELS[value]", admin)
        self.assertIn("function dashboardLifecycleState(p){\n  return lifecycleState(p);\n}", admin)
        self.assertNotIn("Success / Published", labels)

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
