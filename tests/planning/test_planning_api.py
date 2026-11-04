import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.ai.client import AIError
from app.config import Settings
from app.main import create_app
from app.querying.planner import QueryPlanner


class Model:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.output, Exception):
            raise self.output
        return json.dumps(self.output)


def application(engine, model):
    return create_app(settings=Settings(organization_id="private-org"), sources=[], engine=engine,
                      planner=QueryPlanner(model))


def test_plan_preview_never_accesses_database(engine):
    model = Model({"result": {"status": "planned", "plan": {
        "root": "user", "operation": "list", "where": [
            {"field": "mfa_enabled", "operator": "eq", "value": False}]}}})
    app = application(engine, model)
    with TestClient(app) as client:
        statements = []
        def capture(*args):
            statements.append(args[2])
        event.listen(engine, "before_cursor_execute", capture)
        try:
            result = client.post("/ask/plan", json={"question": "Which users don't have MFA?", "limit": 3})
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["executed"] is False
        assert body["result"]["plan"]["limit"] == 3
        assert "data" not in body and "answer" not in body
        assert not statements
        assert "private-org" not in json.dumps(model.calls)
        assert "/ask" in client.get("/openapi.json").json()["paths"]
        assert "/ask/plan" in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("payload", [
    {"question": " "}, {"question": "x" * 2001}, {"question": "Count devices", "limit": 0},
    {"question": "Count devices", "offset": -1}, {"question": "Count devices", "organization_id": "other"},
])
def test_invalid_requests_do_not_call_provider(engine, payload):
    model = Model({})
    with TestClient(application(engine, model)) as client:
        assert client.post("/ask/plan", json=payload).status_code == 422
    assert not model.calls


@pytest.mark.parametrize("decision", [
    {"status": "unsupported", "reason": "Historical queries are unsupported."},
    {"status": "clarification_required", "question": "Do you mean users or devices?"},
])
def test_non_plan_decisions(engine, decision):
    with TestClient(application(engine, Model({"result": decision}))) as client:
        response = client.post("/ask/plan", json={"question": "Explain historical ownership"})
        assert response.status_code == 200
        assert response.json()["result"]["status"] == decision["status"]
        assert response.json()["executed"] is False


@pytest.mark.parametrize("status", [502, 503, 504])
def test_provider_errors_use_existing_error_contract(engine, status):
    model = Model(AIError("Provider failed", status_code=status))
    with TestClient(application(engine, model)) as client:
        response = client.post("/ask/plan", json={"question": "Count devices"})
        assert response.status_code == status
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
