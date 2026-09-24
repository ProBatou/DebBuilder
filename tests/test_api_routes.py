from dataclasses import FrozenInstanceError, replace
import unittest
import urllib.request

from debbuilder.api_routes import (
    ADMIN_API_ROUTES,
    AuthenticationPolicy,
    RouteEffect,
    match_route,
    validate_routes,
)
from debbuilder.http_handler import create_handler
from tests.admin_api_case import AdminApiCase


EXPECTED_ROUTES = {
    ("GET", "/api/status", "system.status"),
    ("GET", "/api/openapi.json", "system.openapi"),
    ("GET", "/api/auth/status", "auth.status"),
    ("GET", "/api/dashboard", "dashboard.get"),
    ("GET", "/api/packages", "packages.list"),
    ("GET", "/api/packages/{name}", "packages.get"),
    ("GET", "/api/recipes", "recipes.list"),
    ("GET", "/api/recipes/{recipe_id}/automation", "automation.get"),
    ("GET", "/api/executions", "executions.list"),
    ("GET", "/api/executions/{run_id}", "executions.get"),
    ("GET", "/api/executions/{run_id}/logs", "executions.logs.get"),
    ("GET", "/api/executions/{run_id}/validations/{attempt_id}", "validation.get"),
    ("GET", "/api/settings", "settings.get"),
    ("GET", "/api/storage", "storage.get"),
    ("GET", "/api/workflows", "workflows.list"),
    ("GET", "/api/workflows/{workflow_id}", "workflows.get"),
    ("POST", "/api/recipes/{recipe_id}/observation/refresh", "observations.refresh"),
    ("POST", "/api/recipes/{recipe_id}/automation/check", "automation.check"),
    ("POST", "/api/recipes/{recipe_id}/automation/retry", "automation.retry"),
    ("POST", "/api/executions/{run_id}/validations/{attempt_id}/cancel", "validation.cancel"),
    ("POST", "/api/executions/{run_id}/cancel", "executions.cancel"),
    ("POST", "/api/recipes/validate", "recipes.validate"),
    ("POST", "/api/recipes/import", "recipes.import"),
    ("POST", "/api/run", "executions.run"),
    ("POST", "/api/upstream-archive/inspect", "archives.inspect"),
    ("POST", "/api/executions/{run_id}/validate", "validation.start"),
    ("POST", "/api/executions/{run_id}/publish", "publication.publish"),
    ("POST", "/api/executions/{run_id}/reconcile-publication", "publication.reconcile"),
    ("POST", "/api/notifications/test", "notifications.test"),
    ("POST", "/api/settings", "settings.update"),
    ("POST", "/api/executions/delete-logs", "executions.logs.delete_many"),
    ("POST", "/api/packages", "packages.create"),
    ("POST", "/api/packages/{name}", "packages.update"),
    ("POST", "/api/workflows/{workflow_id}", "workflows.save"),
    ("DELETE", "/api/workflows/{workflow_id}", "workflows.delete"),
    ("DELETE", "/api/executions/{run_id}/logs", "executions.logs.delete"),
    ("DELETE", "/api/packages/{name}", "packages.delete"),
}


class ApiRouteRegistryTests(unittest.TestCase):
    def test_registry_exactly_matches_reviewable_admin_api_contract(self):
        actual = {(route.method, route.path_template, route.operation_id) for route in ADMIN_API_ROUTES}
        self.assertEqual(actual, EXPECTED_ROUTES)
        self.assertEqual(len(actual), len(ADMIN_API_ROUTES))

    def test_inventory_metadata_matches_current_behavior(self):
        by_method = {
            method: sum(route.method == method for route in ADMIN_API_ROUTES)
            for method in ("GET", "POST", "DELETE")
        }
        self.assertEqual(by_method, {"GET": 16, "POST": 18, "DELETE": 3})
        self.assertEqual(
            sum(route.effect is RouteEffect.DURABLE_MUTATION for route in ADMIN_API_ROUTES),
            19,
        )
        self.assertEqual(
            {route.operation_id for route in ADMIN_API_ROUTES if route.effect is RouteEffect.EPHEMERAL_ACTION},
            {"recipes.validate", "archives.inspect"},
        )
        self.assertTrue(all(
            route.effect is RouteEffect.READ_ONLY
            for route in ADMIN_API_ROUTES
            if route.method == "GET"
        ))
        self.assertTrue(all(route.error_schemas == ("ApiError",) for route in ADMIN_API_ROUTES))

    def test_all_registered_routes_keep_the_configured_admin_auth_boundary(self):
        self.assertTrue(all(
            route.authentication is AuthenticationPolicy.CONFIGURED_ADMIN
            for route in ADMIN_API_ROUTES
        ))
        with self.assertRaises(FrozenInstanceError):
            ADMIN_API_ROUTES[0].authentication = None

    def test_template_matching_decodes_variables_and_is_method_specific(self):
        matched = match_route("GET", "/api/executions/run%2Done/validations/attempt%2Done")
        self.assertEqual(matched.route.operation_id, "validation.get")
        self.assertEqual(matched.path_variables, {"run_id": "run-one", "attempt_id": "attempt-one"})
        self.assertIsNone(match_route("DELETE", "/api/settings"))
        self.assertIsNone(match_route("GET", "/api/executions/run/validations/attempt/cancel"))

    def test_registry_validation_rejects_invalid_developer_configuration(self):
        route = ADMIN_API_ROUTES[0]
        invalid = (
            (route, route),
            (route, replace(ADMIN_API_ROUTES[1], operation_id=route.operation_id)),
            (replace(route, method="PATCH"),),
            (replace(route, path_template="api/status"),),
            (replace(route, path_template="/api/{item}/{item}"),),
            (replace(route, handler=""),),
            (replace(route, effect=RouteEffect.DURABLE_MUTATION),),
        )
        for routes in invalid:
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                validate_routes(routes)

    def test_handler_identity_is_validated_during_handler_construction(self):
        handler = create_handler(__import__("debbuilder.app", fromlist=["app"]))
        validate_routes(ADMIN_API_ROUTES, handler)
        with self.assertRaisesRegex(ValueError, "handler is missing"):
            validate_routes((replace(ADMIN_API_ROUTES[0], handler="_missing"),), handler)


class LegacyHeadBehaviorTests(AdminApiCase):
    def test_authorized_head_remains_wildcard_http_layer_behavior(self):
        self.assertFalse(any(route.method == "HEAD" for route in ADMIN_API_ROUTES))
        request = urllib.request.Request(
            self.base_url + "/api/not-a-named-operation",
            method="HEAD",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")


if __name__ == "__main__":
    unittest.main()
