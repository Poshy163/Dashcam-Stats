"""The application imports, wires up, and produces a valid schema.

This exists because a real failure got all the way to a running container: FastAPI
infers the response model from a handler's return annotation, and a bare ``-> None`` on a
204 route becomes ``NoneType`` -- a class object, so truthy -- which trips its "204 must
not have a response body" assertion **at import time**. The app died before serving a
single request, and nothing in the unit tests noticed because they never imported it.

Importing the app and building its OpenAPI schema is cheap and catches that entire class
of wiring error (bad status codes, unresolvable response models, duplicate operation ids)
in the fast backend job rather than in a container smoke test minutes later.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def application():
    from app.main import app

    return app


class TestImport:
    def test_app_imports_and_builds(self, application):
        assert application.title == "Dashcam Analyser"

    def test_openapi_schema_generates(self, application):
        # Response-model resolution happens here, so a model FastAPI cannot build fails
        # this even when the route registered cleanly.
        schema = application.openapi()
        assert schema["openapi"].startswith("3.")
        assert len(schema["paths"]) > 20

    def test_core_endpoints_are_registered(self, application):
        paths = set(application.openapi()["paths"])
        for required in (
            "/health",
            "/api/status",
            "/api/recordings",
            "/api/journeys",
            "/api/plates",
            "/api/jobs",
            "/api/settings",
            "/api/retention/plan",
            "/api/search",
            "/stream/{recording_id}",
        ):
            assert required in paths, f"{required} is not registered"


class TestStatusCodes:
    def test_bodyless_responses_declare_no_content(self, application):
        """A 204 route must not carry a response schema.

        FastAPI asserts this during route construction, so a violation is an import-time
        crash rather than a bad response at runtime.
        """
        schema = application.openapi()
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                for code, response in operation.get("responses", {}).items():
                    if code in ("204", "304"):
                        assert "content" not in response, (
                            f"{method.upper()} {path} declares a body for {code}"
                        )

    def test_operation_ids_are_unique(self, application):
        """Duplicates silently break generated clients and the docs page."""
        seen: dict[str, str] = {}
        for path, operations in application.openapi()["paths"].items():
            for method, operation in operations.items():
                op_id = operation.get("operationId")
                if not op_id:
                    continue
                assert op_id not in seen, (
                    f"duplicate operationId {op_id!r}: {seen[op_id]} and {method.upper()} {path}"
                )
                seen[op_id] = f"{method.upper()} {path}"


class TestHealthContract:
    def test_health_declares_the_documented_fields(self, application):
        """The Docker HEALTHCHECK depends on this shape."""
        from app.api.schemas import HealthOut

        assert {"status", "database", "scanner", "worker", "version"} <= set(HealthOut.model_fields)
