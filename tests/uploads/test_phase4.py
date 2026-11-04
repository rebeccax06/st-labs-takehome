"""Phase 4 completion: defensive provider parsing, scoped facts, and live-check CLI."""

from conftest import provider_output

from copy import deepcopy
import json

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from app.ai import __main__ as smoke
from app.ai.client import AIError, OpenRouterClient
from app.ai.evidence import evidence_value
from app.ai.extraction import Extractor
from app.config import Settings
from app.db.models import ConfigurationItem, IngestionRun, Observation
from app.db.session import session_factory
from app.main import create_app
from app.schemas.ingest import SourceConfig
from app.schemas.normalized import Evidence


class Model:
    def __init__(self, *outputs):
        self.settings = Settings()
        self.outputs = outputs
        self.messages = []

    def complete(self, *, messages, schema):
        self.messages.append(deepcopy(messages))
        output = self.outputs[min(len(self.messages) - 1, len(self.outputs) - 1)]
        if isinstance(output, Exception):
            raise output
        return output if isinstance(output, str) else json.dumps(provider_output(output))


def app(engine, model):
    source = SourceConfig(organization_id="org-a", source_id="uploads", adapter="auto")
    return create_app(settings=Settings(organization_id="org-a"), sources=[source],
                      engine=engine, extractor=Extractor(model))


def record():
    return {"ci_type": "device", "attributes": {"hostname": "alpha"},
            "attribute_evidence": {"hostname": {"pointer": "/assets/0/hostname"}},
            "identifiers": [{"identifier_type": "device_id", "value": "D7", "evidence": {"pointer": "/assets/0/id"}}],
            "evidence": {"pointer": "/assets/0"}}


DOCUMENT = {"assets": [{"id": "D7", "hostname": "alpha"}, {"id": "D8", "hostname": "beta"}]}


def assert_no_facts(engine):
    with session_factory(engine)() as db:
        for table in (ConfigurationItem, IngestionRun, Observation):
            assert db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("data", [None, [], "not an object", {}, {"choices": None}, {"choices": "invalid"},
    {"choices": [None]}, {"choices": ["invalid"]},
    {"choices": [{"finish_reason": "stop", "message": None}]},
    {"choices": [{"finish_reason": "stop", "message": "invalid"}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": []}}]},
])
def test_malformed_provider_envelopes_always_become_502(data):
    settings = Settings(openrouter_api_key="test-key-not-real")
    client = OpenRouterClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
    with pytest.raises(AIError) as error:
        client.complete(messages=[], schema={})
    assert error.value.status_code == 502


def test_null_provider_error_is_compatible_with_valid_completion():
    data = {"error": None, "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    client = OpenRouterClient(Settings(openrouter_api_key="test-key"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
    assert client.complete(messages=[], schema={}) == "{}"


def test_provider_response_size_is_bounded():
    client = OpenRouterClient(Settings(openrouter_api_key="test-key"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1))))
    with pytest.raises(AIError, match="exceeds"):
        client.complete(messages=[], schema={})


@pytest.mark.parametrize("key", ["", "two tokens", "non-ascii-\u2603"])
def test_invalid_environment_keys_fail_before_http_request(key):
    def unexpected(request):
        raise AssertionError("Invalid credentials must not reach the transport")
    client = OpenRouterClient(Settings(openrouter_api_key=key), transport=httpx.MockTransport(unexpected))
    with pytest.raises(AIError) as error:
        client.complete(messages=[], schema={})
    assert error.value.status_code == 503


@pytest.mark.parametrize("fault", ["identifier", "attribute", "display_identifier", "invalid_attribute_type"])
def test_record_evidence_cannot_be_crossed_or_promoted_to_identity(engine, fault):
    output = record()
    if fault == "identifier":
        output["identifiers"][0].update(value="D8", evidence={"pointer": "/assets/1/id"})
    elif fault == "attribute":
        output["attributes"]["hostname"] = "beta"
        output["attribute_evidence"]["hostname"] = {"pointer": "/assets/1/hostname"}
    elif fault == "display_identifier":
        output["identifiers"][0].update(value="alpha", evidence={"pointer": "/assets/0/hostname"})
    else:
        output["attributes"]["name"] = ["not", "a", "scalar"]
        output["attribute_evidence"]["name"] = {"pointer": "/assets/0/hostname"}
    model = Model({"records": [output]})
    with TestClient(app(engine, model)) as client:
        response = client.post("/ingest/raw", content=json.dumps(DOCUMENT), headers={"Content-Type": "text/plain"})
        assert response.status_code == 502, response.text
        assert len(model.messages) == 2
    assert_no_facts(engine)


def test_record_quotes_cannot_borrow_another_person_identifier(engine):
    content = "Device D7 has hostname alpha. Device D8 has hostname beta."
    output = {"records": [{"ci_type": "device", "attributes": {"hostname": "alpha"},
                           "attribute_evidence": {"hostname": {"quote": "alpha"}},
                           "identifiers": [{"identifier_type": "device_id", "value": "D8", "evidence": {"quote": "D8"}}],
                           "evidence": {"quote": "Device D7 has hostname alpha."}}]}
    with TestClient(app(engine, Model(output))) as client:
        response = client.post("/ingest/raw", content=content, headers={"Content-Type": "text/plain"})
        assert response.status_code == 502
    assert_no_facts(engine)


@pytest.mark.parametrize("observed,proposed", [("true", False), ("enabled", False), ("disabled", True),
                                              ("0", True), (1, False), ("unspecified", False)])
def test_explicit_boolean_contradictions_do_not_persist(engine, observed, proposed):
    document = {"accounts": [{"id": "U1", "mfa": observed}]}
    output = {"records": [{"ci_type": "user", "attributes": {"mfa_enabled": proposed},
                           "attribute_evidence": {"mfa_enabled": {"pointer": "/accounts/0/mfa"}},
                           "identifiers": [{"identifier_type": "user_id", "value": "U1", "evidence": {"pointer": "/accounts/0/id"}}],
                           "evidence": {"pointer": "/accounts/0"}}]}
    with TestClient(app(engine, Model(output))) as client:
        response = client.post("/ingest/raw", content=json.dumps(document), headers={"Content-Type": "text/plain"})
        assert response.status_code == 502
    assert_no_facts(engine)


def test_relationship_target_can_reference_its_own_record_outside_source(engine):
    document = {"assets": [{"id": "D7", "hostname": "alpha", "owner": "U1"}],
                "people": [{"id": "U1", "email": "amy@example.com"}]}
    device = record()
    device["relationships"] = [{"relation_type": "assigned_to", "evidence": {"pointer": "/assets/0/owner"},
        "target": {"ci_type": "user", "evidence": {"pointer": "/people/0"}, "identifiers": [
            {"identifier_type": "user_id", "value": "U1", "evidence": {"pointer": "/people/0/id"}}]}}]
    user = {"ci_type": "user", "attributes": {"email": "amy@example.com"},
            "attribute_evidence": {"email": {"pointer": "/people/0/email"}},
            "identifiers": [{"identifier_type": "user_id", "value": "U1", "evidence": {"pointer": "/people/0/id"}}],
            "evidence": {"pointer": "/people/0"}}
    with TestClient(app(engine, Model({"records": [device, user]}))) as client:
        response = client.post("/ingest/raw", content=json.dumps(document), headers={"Content-Type": "text/plain"})
        assert response.status_code == 200, response.text
        assert response.json()["entities"]["created"] == 2
        assert response.json()["relationships"]["created"] == 1
        assert client.get("/devices").json()["data"][0]["assigned_user_id"] == client.get("/users").json()["data"][0]["id"]


def test_name_only_person_stays_unresolved_but_application_name_is_source_scoped(engine):
    document = {"people": [{"name": "Alice"}], "software": [{"name": "Wiki"}]}
    outputs = {"records": [
        {"ci_type": "user", "attributes": {"name": "Alice"}, "attribute_evidence": {"name": {"pointer": "/people/0/name"}},
         "evidence": {"pointer": "/people/0"}},
        {"ci_type": "application", "attributes": {"name": "Wiki"}, "attribute_evidence": {"name": {"pointer": "/software/0/name"}},
         "identifiers": [{"identifier_type": "app_name", "value": "Wiki", "evidence": {"pointer": "/software/0/name"}}],
         "evidence": {"pointer": "/software/0"}},
    ]}
    with TestClient(app(engine, Model(outputs))) as client:
        response = client.post("/ingest/raw", content=json.dumps(document), headers={"Content-Type": "text/plain"})
        assert response.status_code == 200, response.text
        assert response.json()["records"]["quarantined"] == 1
        assert client.get("/users").json()["total"] == 0
        assert client.get("/apps").json()["total"] == 1


def test_duplicate_model_keys_get_one_actionable_repair(engine):
    valid = {"records": [record()]}
    invalid = '{"records":[],"records":' + json.dumps(valid["records"]) + '}'
    model = Model(invalid, valid)
    with TestClient(app(engine, model)) as client:
        response = client.post("/ingest/raw", content=json.dumps(DOCUMENT), headers={"Content-Type": "text/plain"})
        assert response.status_code == 200, response.text
    assert len(model.messages) == 2
    assert model.messages[1][-2] == {"role": "assistant", "content": invalid}
    assert "duplicate JSON keys" in model.messages[1][-1]["content"]


def test_pointer_escaping_is_supported_and_noncanonical_array_indices_rejected():
    document = {"a/b": {"c~d": "D7"}, "rows": ["first", "second"]}
    assert evidence_value(Evidence(pointer="/a~1b/c~0d"), document, "") == "D7"
    for pointer in ("/rows/01", "/rows/-1", "/rows/\u00b2", "/a~2b"):
        with pytest.raises(ValueError):
            evidence_value(Evidence(pointer=pointer), document, "")


def test_live_smoke_command_is_opt_in(monkeypatch, capsys):
    def forbidden(*args):
        raise AssertionError("No provider call without --live-check")
    monkeypatch.setattr(smoke, "OpenRouterClient", forbidden)
    assert smoke.main([]) == 0
    assert "--live-check" in capsys.readouterr().out


def test_live_smoke_command_runs_extraction_without_a_database(monkeypatch, capsys):
    output = {"records": [{"ci_type": "device", "attributes": {"hostname": "smoke-laptop"},
                           "attribute_evidence": {"hostname": {"quote": "smoke-laptop"}},
                           "identifiers": [{"identifier_type": "device_id", "value": "CMDB-SMOKE-17", "evidence": {"quote": "CMDB-SMOKE-17"}}],
                           "evidence": {"quote": smoke.SMOKE_TEXT}}]}
    model = Model(output)
    monkeypatch.setattr(smoke, "OpenRouterClient", lambda settings: model)
    assert smoke.main(["--live-check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok" and result["records"] == 1
    assert len(model.messages) == 1


def test_live_smoke_failure_is_a_sanitized_nonzero_exit(monkeypatch, capsys):
    model = Model(AIError("Could not connect to OpenRouter.", code="ai_unavailable", status_code=503))
    monkeypatch.setattr(smoke, "OpenRouterClient", lambda settings: model)
    assert smoke.main(["--live-check"]) == 1
    result = json.loads(capsys.readouterr().err)
    assert result == dict(status="failed", code="ai_unavailable", message="Could not connect to OpenRouter.")
