"""Tests for health and startup-proof endpoints."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from con_pilot.health.router import router


class _ServiceReady:
    is_available = True
    _client = object()
    _conductor_session = object()


class TestStartupProofEndpoint:
    def test_returns_true_flags_when_service_started(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.state.copilot_service = _ServiceReady()
        app.state.copilot_startup_complete = True
        app.state.copilot_startup_error = None

        with TestClient(app) as client:
            response = client.get("/startup-proof")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["copilot_service_present"] is True
        assert data["copilot_sdk_import_available"] is True
        assert data["copilot_client_started"] is True
        assert data["conductor_session_started"] is True
        assert data["copilot_startup_complete"] is True

    def test_returns_false_flags_when_startup_failed(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.state.copilot_service = None
        app.state.copilot_startup_complete = False
        app.state.copilot_startup_error = "startup failed"

        with TestClient(app) as client:
            response = client.get("/startup-proof")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["copilot_service_present"] is False
        assert data["copilot_client_started"] is False
        assert data["conductor_session_started"] is False
        assert data["copilot_startup_complete"] is False
        assert data["copilot_startup_error"] == "startup failed"
