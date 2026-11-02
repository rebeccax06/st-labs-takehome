import json

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event, func, select

from app.ai.client import AIError, OpenRouterClient
from app.ai.extraction import Extractor
from app.config import Settings
from app.db.models import Device, IngestionRun, Observation
from app.db.session import session_factory
from app.main import create_app
from app.schemas.ingest import SourceConfig

TEXT = 'Device D-7 has hostname demo-laptop and runs macOS. Its status is active.'


def extracted():
    return {"records": [{"ci_type": "device", "attributes": {
        "hostname": "demo-laptop", "os_family": "macos", "status": "active"},
        "attribute_evidence": {"hostname": {"quote": "demo-laptop"}, "os_family": {"quote": "macOS"},
                               "status": {"quote": "status is active"}},
        "identifiers": [{"identifier_type": "device_id", "value": "D-7", "evidence": {"quote": "D-7"}}],
        "evidence": {"quote": TEXT}}]}


class FakeClient:
    def __init__(self, outputs):
        self.settings = Settings()
        self.outputs = list(outputs)
        self.calls = 0
        self.hook = None

    def complete(self, **kwargs):
        if self.hook:
            self.hook()
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        value = self.outputs[index]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)


def application(engine, fake):
    source = SourceConfig(organization_id="org-a", source_id="uploads", adapter="auto")
    return create_app(settings=Settings(organization_id="org-a"), sources=[source], engine=engine, extractor=Extractor(fake))


def test_text_ai_extraction_is_grounded_marked_and_replayed_without_provider_call(engine):
    fake = FakeClient([extracted()])
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cur, statement, params, ctx, many: statements.append(statement))
    def assert_outside_write():
        assert "BEGIN IMMEDIATE" not in statements
    fake.hook = assert_outside_write
    with TestClient(application(engine, fake)) as client:
        response = client.post("/ingest/raw", content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == 200, response.text
        assert response.json()["entities"]["created"] == 1
        replay = client.post("/ingest", files={"file": ("notes.txt", TEXT)})
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["run_id"] == response.json()["run_id"]
        assert fake.calls == 1
        devices = client.get("/devices").json()["data"]
        assert devices[0]["attributes"]["hostname"] == "demo-laptop"
        detail = client.get('/ci/' + devices[0]["id"]).json()
        assert detail["provenance"]["data"][0]["origin"] == "ai"
    with session_factory(engine)() as db:
        observation = db.scalar(select(Observation))
        assert observation.normalized_record["attribute_evidence"]["hostname"]["quote"] == "demo-laptop"
        assert observation.normalized_record["identifiers"][0]["evidence"]["quote"] == "D-7"
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 1


def test_unknown_csv_layout_uses_json_pointers_and_keeps_false(engine):
    content = 'Account,Email,MFA\nU-17,amy@example.com,false\n'
    output = {"records": [{"ci_type": "user", "attributes": {"email": "amy@example.com", "mfa_enabled": False},
                           "attribute_evidence": {"email": {"pointer": "/rows/0/Email"}, "mfa_enabled": {"pointer": "/rows/0/MFA"}},
                           "identifiers": [{"identifier_type": "user_id", "value": "U-17", "evidence": {"pointer": "/rows/0/Account"}}],
                           "evidence": {"pointer": "/rows/0"}}]}
    fake = FakeClient([output])
    with TestClient(application(engine, fake)) as client:
        response = client.post("/ingest", files={"file": ("export.csv", content)})
        assert response.status_code == 200, response.text
        assert client.get("/users?mfa_enabled=false").json()["total"] == 1


@pytest.mark.parametrize("fault", ["invented_id", "missing_evidence", "invented_quote", "bad_attribute", "fake_scope"])
def test_invalid_ai_output_retries_once_and_saves_nothing(engine, fault):
    bad = extracted()
    row = bad["records"][0]
    if fault == "invented_id":
        row["identifiers"][0]["value"] = "D-999"
    elif fault == "missing_evidence":
        row["attribute_evidence"] = {}
    elif fault == "invented_quote":
        row["evidence"] = {"quote": "Invented evidence"}
    elif fault == "bad_attribute":
        row["attributes"]["execute_sql"] = "DROP TABLE users"
        row["attribute_evidence"]["execute_sql"] = {"quote": "D-7"}
    else:
        row["organization_id"] = "attacker-org"
    fake = FakeClient([bad])
    with TestClient(application(engine, fake)) as client:
        response = client.post("/ingest/raw", content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == 502, response.text
        assert fake.calls == 2
    with session_factory(engine)() as db:
        for model in (Device, IngestionRun, Observation):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_corrected_response_passes_after_one_schema_retry(engine):
    fake = FakeClient(['not JSON', extracted()])
    with TestClient(application(engine, fake)) as client:
        response = client.post("/ingest/raw", content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == 200, response.text
        assert fake.calls == 2


def test_null_mfa_cannot_be_invented_as_false(engine):
    data = {"accounts": [{"id": "U-17", "mfa": None}]}
    output = {"records": [{"ci_type": "user", "attributes": {"mfa_enabled": False},
                           "attribute_evidence": {"mfa_enabled": {"pointer": "/accounts/0/mfa"}},
                           "identifiers": [{"identifier_type": "user_id", "value": "U-17", "evidence": {"pointer": "/accounts/0/id"}}],
                           "evidence": {"pointer": "/accounts/0"}}]}
    with TestClient(application(engine, FakeClient([output]))) as client:
        response = client.post("/ingest/raw?format=json", content=json.dumps(data), headers={"Content-Type": "text/plain"})
        assert response.status_code == 502
        assert client.get("/users").json()["total"] == 0


def test_unsupported_and_oversized_unknown_input_have_helpful_errors(engine):
    fake = FakeClient([{"records": [], "unsupported_reason": "Not inventory data"}])
    with TestClient(application(engine, fake)) as client:
        response = client.post("/ingest/raw", content="A recipe for cake", headers={"Content-Type": "text/plain"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported_layout"
        response = client.post("/ingest/raw", content="a" * (64 * 1024 + 1), headers={"Content-Type": "text/plain"})
        assert response.status_code == 413
        assert fake.calls == 1


@pytest.mark.parametrize("error,status", [(AIError("Provider unavailable",code="ai_unavailable",status_code=503),503),
                                         (AIError("Timed out",code="ai_timeout",status_code=504),504)])
def test_upstream_failures_return_consistent_errors(engine, error, status):
    fake = FakeClient([error])
    with TestClient(application(engine, fake)) as client:
        response = client.post("/ingest/raw", content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == status
        assert response.json()["error"]["request_id"]
        assert fake.calls == 1


def settings(tmp_path, **kwargs):
    token = tmp_path / "token.txt"
    token.write_text("test-key-not-real")
    return Settings(openrouter_token_path=str(token), **kwargs)


def test_client_uses_token_file_and_structured_output_without_exposing_key(tmp_path):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key-not-real"
        body = json.loads(request.content)
        assert "test-key-not-real" not in json.dumps(body)
        assert body["response_format"]["type"] == "json_schema"
        assert body["provider"]["require_parameters"] is True
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})
    client = OpenRouterClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    assert client.complete(messages=[], schema={"type": "object"}) == "{}"


@pytest.mark.parametrize("status", [401,402,403,429,500])
def test_client_does_not_return_provider_error_bodies_or_credentials(tmp_path, status):
    client = OpenRouterClient(settings(tmp_path), transport=httpx.MockTransport(lambda request:
        httpx.Response(status, json={"error": "Private upstream details test-key-not-real"})))
    with pytest.raises(AIError) as error:
        client.complete(messages=[], schema={})
    assert error.value.status_code == 503
    assert "test-key-not-real" not in str(error.value)
    assert "Private" not in str(error.value)


def test_client_timeouts_and_truncation_are_explicit(tmp_path):
    def timeout(request):
        raise httpx.ReadTimeout("sensitive internal error")
    client = OpenRouterClient(settings(tmp_path), transport=httpx.MockTransport(timeout))
    with pytest.raises(AIError) as error:
        client.complete(messages=[], schema={})
    assert error.value.status_code == 504
    client.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": "length", "message": {"content": "{"}}]}))
    with pytest.raises(AIError, match="incomplete"):
        client.complete(messages=[], schema={})


def test_missing_credentials_and_env_secret_repr_are_safe(tmp_path, monkeypatch):
    client = OpenRouterClient(Settings(openrouter_token_path=str(tmp_path / "absent")))
    with pytest.raises(AIError) as error:
        client.complete(messages=[], schema={})
    assert error.value.status_code == 503
    monkeypatch.setenv("CMDB_OPENROUTER_API_KEY", "secret-test-value")
    assert "secret-test-value" not in repr(Settings.from_env())


@pytest.mark.parametrize("filename,content", [
    ("assets.json", '{"machines":[{"asset":"D-7","node":"demo-laptop"}]}'),
    ("assets.yaml", 'machines:\n  - asset: D-7\n    node: demo-laptop\n'),
])
def test_unfamiliar_structured_layouts_are_extracted_and_validated(engine, filename, content):
    output = {"records": [{"ci_type": "device", "attributes": {"hostname": "demo-laptop"},
                           "attribute_evidence": {"hostname": {"pointer": "/machines/0/node"}},
                           "identifiers": [{"identifier_type": "device_id", "value": "D-7", "evidence": {"pointer": "/machines/0/asset"}}],
                           "evidence": {"pointer": "/machines/0"}}]}
    fake = FakeClient([output])
    with TestClient(application(engine, fake)) as client:
        result = client.post("/ingest", files={"file": (filename, content)})
        assert result.status_code == 200, result.text
        assert fake.calls == 1
        device = client.get("/devices").json()["data"][0]
        detail = client.get('/ci/' + device["id"]).json()
        assert detail["field_state"]["hostname"]["evidence"][0]["extraction_evidence"]["pointer"] == "/machines/0/node"


def test_applications_mapping_is_unfamiliar_not_misclassified_as_known_array(engine):
    content = '{"applications":{"catalog":{"id":"APP-7","title":"Inventory App"}}}'
    output = {"records": [{"ci_type": "application", "attributes": {"name": "Inventory App"},
                           "attribute_evidence": {"name": {"pointer": "/applications/catalog/title"}},
                           "identifiers": [{"identifier_type": "app_id", "value": "APP-7", "evidence": {"pointer": "/applications/catalog/id"}}],
                           "evidence": {"pointer": "/applications/catalog"}}]}
    with TestClient(application(engine, FakeClient([output]))) as client:
        result = client.post("/ingest/raw", content=content, headers={"Content-Type": "text/plain"})
        assert result.status_code == 200, result.text
        assert client.get("/apps").json()["total"] == 1


def test_ai_attributes_use_the_shared_deterministic_normalizers(engine):
    output = extracted()
    output["records"][0]["attributes"]["os_family"] = "macOS"
    output["records"][0]["attributes"]["status"] = "ACTIVE"
    output["records"][0]["attributes"]["hostname"] = " demo-laptop "
    with TestClient(application(engine, FakeClient([output]))) as client:
        response = client.post("/ingest/raw", content=TEXT, headers={"Content-Type": "text/plain"})
        assert response.status_code == 200, response.text
        device = client.get("/devices?os_family=macos&status=active").json()["data"][0]
        assert device["attributes"]["hostname"] == "demo-laptop"
