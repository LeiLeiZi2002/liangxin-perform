from importlib import import_module

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def load_application() -> FastAPI:
    try:
        application_module = import_module("app.main")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Backend application is not implemented: {exc}")

    application = getattr(application_module, "app", None)
    if not isinstance(application, FastAPI):
        pytest.fail("app.main must expose a FastAPI instance named 'app'")

    return application


def test_health_endpoint_reports_ready_service() -> None:
    response = TestClient(load_application()).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "psych-assessment-demo",
    }


def test_cors_allows_only_configured_frontend_origin() -> None:
    client = TestClient(load_application())
    allowed_response = client.options(
        "/api/health",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    blocked_response = client.options(
        "/api/health",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed_response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-origin" not in blocked_response.headers
