"""Schema constraints and public diagnostics for actual provider-shaped responses."""

from copy import deepcopy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from app.ai.client import OpenRouterClient, strict_schema
from app.ai.extraction import Extractor
from app.ai.schemas import extraction_contract
from app.config import Settings
from app.db.models import IngestionRun, Observation
from app.db.session import session_factory
from app.main import create_app
from app.schemas.ingest import SourceConfig


CONTENT = "Employee ID: U-1; Name: Private Person; MFA: unknown"


def output():
    return {"records": [{
        "ci_type": "user",
        "attributes": {
            "name": {"value": "Private Person", "evidence": {"quote": "Private Person"}},
            "mfa_enabled": {"value": None, "evidence": {"quote": "unknown"}},
        },
        "identifiers": [{"identifier_type": "employee_id", "value": "U-1", "evidence": {"quote": "U-1"}}],
        "relationships": [], "evidence": {"quote": CONTENT},
    }], "unsupported_reason": None}


@pytest.mark.parametrize("fault,category,path", [
    ("missing_evidence", "missing", "/attributes/name/evidence"),
    ("wrong_type", "bool_type", "/attributes/mfa_enabled/value"),
    ("wrong_kind_field", "extra_forbidden", "/attributes/hostname"),
    ("private_field", "extra_forbidden", "/attributes/field"),
    ("fabricated_quote", "invalid_evidence", "/attributes/name/evidence"),
    ("text_pointer", "missing", "/attributes/name/evidence/quote"),
    ("boolean_contradiction", "contradictory_evidence", "/attributes/mfa_enabled/value"),
    ("unknown_identifier", "identifier_not_in_source", "/identifiers/0/value"),
    ("null_with_fake_evidence", "invalid_evidence", "/attributes/mfa_enabled/evidence"),
])
def test_invalid_extraction_has_safe_details_and_no_writes(engine, fault, category, path):
    value = output()
    attrs = value["records"][0]["attributes"]
    if fault == "missing_evidence":
        del attrs["name"]["evidence"]
    elif fault == "wrong_type":
        attrs["mfa_enabled"]["value"] = "false"
    elif fault == "wrong_kind_field":
        attrs["hostname"] = attrs["name"]
    elif fault == "private_field":
        attrs["PRIVATE-SECRET-FIELD"] = attrs["name"]
    elif fault == "fabricated_quote":
        attrs["name"]["evidence"]["quote"] = "PRIVATE invented source"
    elif fault == "text_pointer":
        attrs["name"]["evidence"] = {"pointer": "/name"}
    elif fault == "boolean_contradiction":
        attrs["mfa_enabled"]["value"] = False
    elif fault == "unknown_identifier":
        value["records"][0]["identifiers"][0]["value"] = "PRIVATE invented identifier"
    else:
        attrs["mfa_enabled"]["evidence"]["quote"] = "PRIVATE invented source"
    fake = SimpleNamespace(settings=Settings(), complete=lambda **kwargs: json.dumps(value))
    source = SourceConfig(organization_id="test", source_id="notes", adapter="auto")
    app = create_app(settings=Settings(organization_id="test"), sources=[source], engine=engine,
                     extractor=Extractor(fake))
    with TestClient(app) as client:
        response = client.post("/ingest/raw?source_id=notes&format=text", content=CONTENT,
                               headers={"Content-Type": "text/plain"})
        assert response.status_code == 502
        body = response.json()["error"]
        assert body["request_id"] == response.headers["x-request-id"]
        assert any(detail["type"] == category and detail["path"].endswith(path)
                   for detail in body["details"]), body
        assert "Private Person" not in response.text
        assert "PRIVATE" not in response.text
    with session_factory(engine)() as db:
        for model in (IngestionRun, Observation):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("text_input", [True, False])
def test_provider_schema_is_closed_and_strict(text_input):
    original = extraction_contract(text_input).model_json_schema()
    before = deepcopy(original)
    schema = strict_schema(original)
    assert original == before

    def inspect(node):
        if isinstance(node, list):
            for value in node:
                inspect(value)
        elif isinstance(node, dict):
            assert "default" not in node
            assert "oneOf" not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                inspect(value)
    inspect(schema)
    if text_input:
        assert "pointer" not in json.dumps(schema)
    else:
        assert "quote" not in json.dumps(schema)


def test_client_enables_strict_mode_for_extraction_only():
    def handle(request):
        payload = json.loads(request.content)["response_format"]["json_schema"]
        assert payload["strict"] is (payload["name"] == "cmdb_extraction")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})
    client = OpenRouterClient(Settings(openrouter_api_key="test"), transport=httpx.MockTransport(handle))
    for name in ("cmdb_extraction", "cmdb_query_plan"):
        assert client.complete(messages=[], schema={"type": "object", "properties": {}}, schema_name=name) == "{}"
