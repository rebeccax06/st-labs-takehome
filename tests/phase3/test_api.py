import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import IngestionRun
from app.db.session import session_factory
from app.ingestion.persistence import DatabaseBusy
from app.main import create_app
from app.reconciliation.policy import load_sources
from app.schemas.ingest import SourceConfig

ROOT = Path(__file__).resolve().parents[2]


def application(engine):
    config = SourceConfig(organization_id="org-a", source_id="hardware", adapter="hardware_json")
    return create_app(settings=Settings(organization_id="org-a"), sources=[config], engine=engine)


def payload(rows):
    return dict(organization_id="org-a", source_id="hardware", format="json", content=json.dumps(rows))


def test_http_ingest_replay_list_details_and_errors(engine):
    with TestClient(application(engine)) as client:
        request = payload([{"device_id": "D1", "hostname": "host", "status": "active"}])
        result = client.post("/ingest", json=request)
        assert result.status_code == 200, result.text
        assert result.json()["entities"]["created"] == 1
        assert client.post("/ingest", json=request).json()["replayed"] is True
        listed = client.get("/devices", params={"status": "active", "limit": 1})
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        ci_id = listed.json()["data"][0]["id"]
        detail = client.get(f"/ci/{ci_id}")
        assert detail.status_code == 200, detail.text
        item = detail.json()
        assert item["attributes"]["hostname"] == "host"
        assert item["provenance"]["total"] == 1
        assert item["field_state"]["status"]["evidence"][0]["source_id"] == "hardware"
        assert "raw_record" not in detail.text
        assert client.get("/devices?limit=0").status_code == 422
        assert client.get("/devices?limit=101").status_code == 422
        assert client.get("/devices?status=invalid").status_code == 422
        assert client.get("/ci/not-a-uuid").status_code == 422
        missing = client.get(f"/ci/{uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["request_id"] == missing.headers["x-request-id"]
        assert client.get("/devices", params={"os_family": "' OR 1=1 --"}).json()["total"] == 0


def test_all_samples_end_to_end_and_consistent_pagination(engine):
    sources = load_sources(ROOT / "examples/sample_sources.json")
    app = create_app(settings=Settings(organization_id="example-corp"), sources=sources, engine=engine)
    with TestClient(app) as client:
        for entry in json.loads((ROOT / "examples/sample_sources.json").read_text()):
            cfg = entry["source"]
            format = "yaml" if cfg["adapter"] == "hardware_yaml" else "csv" if cfg["adapter"] == "hardware_csv" else "json"
            response = client.post("/ingest", json=dict(
                organization_id=cfg["organization_id"], source_id=cfg["source_id"], format=format,
                content=(ROOT / "input_data" / entry["file"]).read_text(),
            ))
            assert response.status_code == 200, response.text
        assert client.get("/devices").json()["total"] == 3
        assert client.get("/users").json()["total"] == 12
        assert client.get("/apps").json()["total"] == 28
        assert client.get("/devices?status=active").json()["total"] == 1
        assert client.get("/devices?os_family=macos").json()["total"] == 2
        false_users = client.get("/users?mfa_enabled=false").json()
        assert false_users["total"] == 3
        assert any(row["attributes"]["status"] == "suspended" for row in false_users["data"])
        page1 = client.get("/devices?limit=1&offset=0").json()
        page2 = client.get("/devices?limit=1&offset=1").json()
        assert page1["total"] == page2["total"] == 3
        assert page1["data"][0]["id"] != page2["data"][0]["id"]
        apps = client.get("/apps?limit=100").json()["data"]
        github = next(row for row in apps if row["attributes"]["name"] == "GitHub Enterprise")
        assert github["observed_assignment_count"] == 5
        assert github["attributes"]["reported_user_count"] == 28
        assert github["is_reference"] is False
        assert sum(row["is_reference"] for row in apps) == 22
        details = client.get(f"/ci/{github['id']}?edge_limit=1").json()
        assert len(details["relationships"]["data"]) == 1
        assert details["relationships"]["total"] > 1
        assert details["provenance"]["total"] >= len(details["provenance"]["data"])
        device = next(row for row in client.get("/devices").json()["data"] if row["attributes"]["hostname"] == "laptop-jdoe")
        detail = client.get(f"/ci/{device['id']}").json()
        assert detail["attributes"]["status"] is None
        state = detail["field_state"]["status"]
        assert state["status"] == "conflict"
        assert {e["value"] for e in state["evidence"]} == {"active", "inactive"}
        assignee = client.get(f"/ci/{device['assigned_user_id']}").json()
        assert assignee["attributes"]["mfa_enabled"] is None
        assert detail["warnings"][0]["code"] == "unresolved_references"
        assert "/ask" in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("body,status", [
    ({"organization_id": "org-a"}, 422),
    (payload([{"device_id": "D"}]) | {"organization_id": "other-org"}, 422),
    (payload([{"device_id": "D"}]) | {"source_id": "unconfigured"}, 422),
    (payload([{"device_id": "D"}]) | {"content": "malformed-json"}, 422),
    (payload([{"device_id": "D"}]) | {"content": "x" * (1024 * 1024 + 1)}, 413),
])
def test_request_errors_have_no_database_side_effects(engine, body, status):
    with TestClient(application(engine)) as client:
        response = client.post("/ingest", json=body)
        assert response.status_code == status, response.text
        assert "request_id" in response.json()["error"]
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 0


def test_http_quarantine_commits_evidence_and_returns_warning(engine):
    with TestClient(application(engine)) as client:
        response = client.post("/ingest", json=payload([{"hostname": "ambiguous-host"}]))
        assert response.status_code == 200
        result = response.json()
        assert result["records"]["quarantined"] == 1
        assert result["warnings"][0]["code"] == "missing_identifiers"
        assert client.get("/devices").json()["total"] == 0


def test_busy_and_unexpected_errors_are_sanitized(engine, monkeypatch):
    app = application(engine)
    with TestClient(app, raise_server_exceptions=False) as client:
        def busy(_payload):
            raise DatabaseBusy("Database is busy; retry the request")
        monkeypatch.setattr(app.state.ingestion, "ingest", busy)
        assert client.post("/ingest", json=payload([{"device_id": "D"}])).status_code == 503
        def fail(_payload):
            raise RuntimeError("Private database parameters must not appear")
        monkeypatch.setattr(app.state.ingestion, "ingest", fail)
        response = client.post("/ingest", json=payload([{"device_id": "D"}]))
        assert response.status_code == 500
        assert "Private" not in response.text
        assert response.json()["error"]["code"] == "internal_error"


def test_evidence_summary_and_field_evidence_are_bounded(engine):
    with TestClient(application(engine)) as client:
        for i in range(25):
            response = client.post("/ingest", json=payload([{"device_id": "D", "hostname": "host", "unused": i}]))
            assert response.status_code == 200
        ci_id = client.get("/devices").json()["data"][0]["id"]
        detail = client.get(f"/ci/{ci_id}").json()
        assert detail["provenance"]["total"] == 25
        assert len(detail["provenance"]["data"]) == 20
        assert detail["field_state"]["hostname"]["observation_count"] == 25
        assert len(detail["field_state"]["hostname"]["evidence"]) == 20
