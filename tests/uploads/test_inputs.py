import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import Device, IngestionRun, Observation
from app.db.session import session_factory
from app.main import create_app
from app.schemas.ingest import SourceConfig

ROOT = Path(__file__).resolve().parents[2]


class NoAI:
    version = "test-no-ai"
    def prepare(self, *args):
        raise AssertionError("Known input must not call AI")


def application(engine):
    sources = [SourceConfig(organization_id="org-a", source_id="hardware", adapter="hardware_json"),
               SourceConfig(organization_id="org-a", source_id="csv", adapter="hardware_csv"),
               SourceConfig(organization_id="org-a", source_id="yaml", adapter="hardware_yaml")]
    return create_app(settings=Settings(organization_id="org-a"), sources=sources, engine=engine, extractor=NoAI())


def test_openapi_has_file_picker_source_choices_and_raw_editor(engine):
    with TestClient(application(engine)) as client:
        schema = client.get("/openapi.json").json()
        upload = schema["paths"]["/ingest"]["post"]["requestBody"]["content"]
        assert upload["multipart/form-data"]["schema"]["properties"]["file"]["format"] == "binary"
        assert "application/json" in upload
        source = upload["multipart/form-data"]["schema"]["properties"]["source_id"]
        assert source["enum"] == ["hardware", "csv", "yaml", "uploaded-data"]
        raw = schema["paths"]["/ingest/raw"]["post"]
        assert "text/plain" in raw["requestBody"]["content"]
        assert {p["name"] for p in raw["parameters"]} == {"source_id", "format"}
        menu = client.get("/ingest/sources").json()
        assert menu["default_source_id"] == "uploaded-data"
        assert next(s for s in menu["sources"] if s["source_id"] == "yaml")["formats"] == ["yaml"]


def test_file_raw_and_existing_json_envelope_share_replay(engine):
    content = json.dumps([{"device_id": "D17", "hostname": 'A "quoted" host'}])
    with TestClient(application(engine)) as client:
        first = client.post("/ingest", files={"file": ("assets.json", content, "application/json")}, data={"source_id": "hardware"})
        assert first.status_code == 200, first.text
        assert first.json()["replayed"] is False
        second = client.post("/ingest/raw?source_id=hardware", content=content, headers={"Content-Type": "text/plain"})
        assert second.status_code == 200, second.text
        third = client.post("/ingest", json=dict(organization_id="org-a", source_id="hardware", format="json", content=content))
        assert third.status_code == 200
        assert first.json()["run_id"] == second.json()["run_id"] == third.json()["run_id"]
        assert second.json()["replayed"] and third.json()["replayed"]
        assert client.get("/devices").json()["data"][0]["attributes"]["hostname"] == 'A "quoted" host'
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 1
        assert db.scalar(select(func.count()).select_from(Observation)) == 1


@pytest.mark.parametrize("filename,source", [("sample_hardware.json", "hardware"),
                                              ("sample_hardware.csv", "csv"),
                                              ("sample_hardware.yaml", "yaml")])
def test_named_sources_accept_upload_and_paste_without_format_fields(engine, filename, source):
    raw = (ROOT / "input_data" / filename).read_bytes()
    with TestClient(application(engine)) as client:
        result = client.post("/ingest", files={"file": (filename, raw)}, data={"source_id": source})
        assert result.status_code == 200, result.text
        replay = client.post(f"/ingest/raw?source_id={source}", content=raw, headers={"Content-Type": "text/plain"})
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True


@pytest.mark.parametrize("filename", ["sample_hardware.json", "sample_hardware.csv", "sample_hardware.yaml", "sample_okta.json", "sample_app.json"])
def test_automatic_source_detects_known_layouts_without_ai(engine, filename):
    with TestClient(application(engine)) as client:
        result = client.post("/ingest", files={"file": (filename, (ROOT / "input_data" / filename).read_bytes())})
        assert result.status_code == 200, result.text
        assert result.json()["records"]["resolved"] > 0


def test_canonical_ingest_accepts_bare_json_and_raw_text_bodies(engine):
    with TestClient(application(engine)) as client:
        result = client.post("/ingest?source_id=hardware", json=[{"device_id": "D1"}])
        assert result.status_code == 200, result.text
        result = client.post("/ingest?source_id=csv", content="device_id,hostname\nD2,host\n",
                             headers={"Content-Type": "text/csv"})
        assert result.status_code == 200, result.text


@pytest.mark.parametrize("data,status", [(b"", 422), (b"\xff\xfe\x00", 415), (b"%PDF-1.7", 415),
                                         (b"a" * (1024 * 1024 + 1), 413)])
def test_bad_file_errors_have_no_side_effects(engine, data, status):
    with TestClient(application(engine)) as client:
        response = client.post("/ingest", files={"file": ("input.dat", data)})
        assert response.status_code == status, response.text
        assert response.json()["error"]["request_id"]
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 0


def test_bad_multipart_unknown_source_and_conflicting_format(engine):
    with TestClient(application(engine)) as client:
        assert client.post("/ingest", data={"source_id": "hardware"}).status_code == 422
        assert client.post("/ingest", files={"file": ("a.json", "[]")}, data={"source_id": "missing"}).status_code == 422
        response = client.post("/ingest", files={"file": ("a.json", "[]")}, data={"source_id": "hardware", "format": "csv"})
        assert response.status_code == 422
        assert "expects json" in response.text
        assert client.post("/ingest/raw?source_id=missing", content="[]").status_code == 422
        assert client.post("/ingest", files=[("file", ("a.json", "[]")), ("file", ("b.json", "[]"))]).status_code == 422


def test_chunked_body_limits_do_not_depend_on_content_length(engine):
    def chunks():
        yield b"a" * (1024 * 1024)
        yield b"too large"
    with TestClient(application(engine)) as client:
        response = client.post("/ingest/raw?source_id=hardware", content=chunks(), headers={"Content-Type": "text/plain"})
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "limit_exceeded"


def test_malformed_known_layout_never_falls_back_to_ai(engine):
    with TestClient(application(engine)) as client:
        response = client.post("/ingest", files={"file": ("bad.json", b'{"not finished":')})
        assert response.status_code == 422
        assert "Malformed" in response.text


def test_excessively_nested_canonical_json_is_a_helpful_client_error(engine):
    with TestClient(application(engine)) as client:
        response = client.post("/ingest", content='[' * 1500 + '0' + ']' * 1500,
                               headers={"Content-Type": "application/json"})
        assert response.status_code in {413, 422}
        assert response.json()["error"]["code"] in {"invalid_input", "limit_exceeded"}


@pytest.mark.parametrize("content", ['[]', '{}', 'null'])
def test_empty_structured_upload_does_not_call_ai(engine, content):
    with TestClient(application(engine)) as client:
        response = client.post("/ingest/raw?format=json", content=content,
                               headers={"Content-Type": "text/plain"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported_layout"
