from pathlib import Path
import json
from itertools import permutations

import pytest
from sqlalchemy import func, select

from app.db.models import Application, ConfigurationItem, Device, IngestionRun, Observation, Relationship, User
from app.db.session import session_factory
from app.ingestion.persistence import IngestService
from app.ingestion.service import prepare_ingestion
from app.reconciliation.policy import Policy, load_sources
from app.schemas.ingest import IngestRequest, SourceConfig

ROOT = Path(__file__).resolve().parents[2]


def source(name="inventory", adapter="hardware_json", **changes):
    return SourceConfig(organization_id="org-a", source_id=name, adapter=adapter, **changes)


def request(data, src="inventory", format="json", org="org-a"):
    return IngestRequest(organization_id=org, source_id=src, format=format,
                         content=data if isinstance(data, str) else json.dumps(data))


def test_replay_and_semantic_duplicates(engine):
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy([source()]))
    req = request([{"device_id": "D1", "hostname": "host"}])
    first = service.ingest(req)
    second = service.ingest(req)
    assert first["entities"]["created"] == 1
    assert second == first | {"replayed": True}
    service.ingest(req.model_copy(update={"content": json.dumps(json.loads(req.content), indent=2)}))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ConfigurationItem)) == 1
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 2
        assert db.scalar(select(func.count()).select_from(Observation)) == 2


def test_attribute_conflicts_preserved_and_quarantine_is_committed(engine):
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy([source()]))
    result = service.ingest(request([{"device_id": "D1", "status": "active"},
                                     {"device_id": "D1", "status": "inactive"},
                                     {"hostname": "no-stable-key"}]))
    assert result["records"] == {"received": 3, "resolved": 2, "quarantined": 1}
    with sessions() as db:
        ci = db.scalar(select(Device))
        assert ci.status is None
        assert ci.field_state["status"]["status"] == "conflict"
        assert len(ci.field_state["status"]["observation_ids"]) == 2
        assert db.scalar(select(func.count()).select_from(Observation)) == 3


def test_identity_conflict_cannot_partially_update_attributes(engine):
    sessions = session_factory(engine)
    cfg = source(identifier_authorities={"serial": "serials"})
    service = IngestService(sessions, Policy([cfg]))
    service.ingest(request([{"device_id": "D1", "serial_number": "S1", "hostname": "one"},
                            {"device_id": "D2", "serial_number": "S2", "hostname": "two"}]))
    outcome = service.ingest(request([{"device_id": "D1", "serial_number": "S2", "hostname": "incorrect"}]))
    assert outcome["records"]["quarantined"] == 1
    with sessions() as db:
        assert set(db.scalars(select(Device.hostname))) == {"one", "two"}
        conflict = db.scalar(select(Observation).where(Observation.resolution_status == "identity_conflict"))
        assert conflict.ci_id is None
        assert len(conflict.normalized_record["candidate_ids"]) == 2


def test_authority_and_freshness_are_source_configuration(engine):
    sessions = session_factory(engine)
    a = source("a", authoritative_attributes=["device.status"], identifier_authorities={"device_id": "shared"})
    b = source("b", identifier_authorities={"device_id": "shared"})
    service = IngestService(sessions, Policy([a, b]))
    service.ingest(request([{"device_id": "D", "status": "active"}], "a"))
    service.ingest(request([{"device_id": "D", "status": "inactive"}], "b"))
    service.ingest(request([{"device_id": "D", "status": None}], "a"))
    with sessions() as db:
        assert db.scalar(select(Device.status)) == "active"


def test_entire_write_rolls_back_on_failure(engine, monkeypatch):
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy([source()]))
    import app.ingestion.persistence as module
    def fail(*args):
        raise RuntimeError("Injected projection failure")
    monkeypatch.setattr(module, "project_relationships", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        service.ingest(request([{"device_id": "D1", "hostname": "host"}]))
    with sessions() as db:
        for model in (ConfigurationItem, IngestionRun, Observation, Relationship):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def sample_sources():
    sources = load_sources(ROOT / "examples/sample_sources.json")
    for src in sources:
        if src.adapter in {"okta_json", "applications_json"}:
            src.application_aliases = {"GitHub": "GitHub Enterprise", "Salesforce": "Salesforce CRM"}
    return sources


@pytest.mark.parametrize("reverse", [False, True])
def test_all_sample_files_and_order_independent_conflicts(engine, reverse):
    sessions = session_factory(engine)
    sources = sample_sources()
    service = IngestService(sessions, Policy(sources))
    manifest = json.loads((ROOT / "examples/sample_sources.json").read_text())
    for entry in manifest[::-1] if reverse else manifest:
        cfg = next(s for s in sources if s.source_id == entry["source"]["source_id"])
        formats = {"hardware_yaml": "yaml", "hardware_csv": "csv"}
        req = request((ROOT / "input_data" / entry["file"]).read_text(), cfg.source_id,
                      formats.get(cfg.adapter, "json"), cfg.organization_id)
        result = service.ingest(req)
        assert result["records"]["quarantined"] == 0
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Device)) == 3
        assert db.scalar(select(func.count()).select_from(User)) == 12
        assert db.scalar(select(func.count()).select_from(Application)) == 28
        assert db.scalar(select(func.count()).select_from(Relationship).where(Relationship.relation_type == "uses")) == 25
        assert db.scalar(select(func.count()).select_from(Relationship).where(Relationship.relation_type == "integrates_with")) == 16
        assert db.scalar(select(func.count()).select_from(Relationship).where(Relationship.relation_type == "assigned_to")) == 3
        assert db.scalar(select(func.count()).select_from(Device).where(Device.status == "active")) == 1
        assert db.scalar(select(func.count()).select_from(Device).where(Device.status.is_(None))) == 2


def test_reliable_snapshot_time_selects_latest_even_when_ingested_first(engine):
    sessions = session_factory(engine)
    cfg = source("apps", adapter="applications_json", snapshot_time_path=["inventory_metadata", "export_date"])
    # Catalog counts are attributes; only the explicitly configured export time orders observations.
    service = IngestService(sessions, Policy([cfg]))
    for exported, count in [("2025-01-02T00:00:00Z", 20), ("2025-01-01T00:00:00Z", 10)]:
        service.ingest(request({"inventory_metadata": {"export_date": exported},
                                "applications": [{"app_id": "A", "users_count": count}]}, "apps"))
    with sessions() as db:
        assert db.scalar(select(Application.reported_user_count)) == 20


def test_assignment_replacement_and_conflict_preserve_evidence(engine):
    from app.schemas.ingest import PreparedIngestion
    from app.schemas.normalized import NormalizedBatch
    sessions = session_factory(engine)
    cfg = source()
    service = IngestService(sessions, Policy([cfg]))
    def prepared(target, observed):
        scope = dict(organization_id="org-a", ci_type="device", authority_id="inventory", identifier_type="device_id")
        user_scope = scope | {"ci_type": "user", "identifier_type": "user_id"}
        return PreparedIngestion(organization_id="org-a", source_id="inventory", warning_count=0, warnings=[],
            batch=NormalizedBatch.model_validate_json(json.dumps({"records": [{
                "ci_type": "device", "organization_id": "org-a", "record_key": "/0", "attributes": {},
                "observed_at": observed, "identifiers": [{"scope": scope, "value": "D"}],
                "relationships": [{"relation_type": "assigned_to", "target": {
                    "ci_type": "user", "identifiers": [{"scope": user_scope, "value": target}]}}]
            }]})))
    service.persist(request("first"), prepared("U1", "2025-01-01T00:00:00Z"))
    result = service.persist(request("second"), prepared("U2", "2025-01-02T00:00:00Z"))
    assert result["relationships"]["removed"] == 1
    assert result["relationships"]["created"] == 1
    # An undated contradictory assertion makes the current assignment unknown.
    result = service.persist(request("third"), prepared("U1", None))
    assert result["relationships"]["removed"] == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Relationship)) == 0
        device = db.scalar(select(Device))
        assert device.field_state["assigned_to"]["status"] == "conflict"
        assert db.scalar(select(func.count()).select_from(Observation).where(
            Observation.normalized_record["record_kind"].as_string() == "relationship")) == 3


def test_suspended_users_unknown_mfa_and_access_omission(engine):
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy([source("okta", adapter="okta_json")]))
    service.ingest(request([{"user_id": "U", "status": "SUSPENDED", "mfa_enabled": False, "apps": ["Tool"]}], "okta"))
    service.ingest(request([{"user_id": "U", "mfa_enabled": None, "apps": []}], "okta"))
    with sessions() as db:
        user = db.scalar(select(User))
        assert user.mfa_enabled is False and user.status == "suspended"
        assert db.scalar(select(func.count()).select_from(Relationship)) == 1


def test_policy_change_reprocesses_same_payload_without_duplicate_entities(engine):
    sessions = session_factory(engine)
    cfg = source()
    first = IngestService(sessions, Policy([cfg])).ingest(request([{"device_id": "D", "status": "active"}]))
    cfg.authoritative_attributes = ["device.status"]
    second = IngestService(sessions, Policy([cfg])).ingest(request([{"device_id": "D", "status": "active"}]))
    assert second["replayed"] is False
    assert first["run_id"] != second["run_id"]
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Device)) == 1
        assert db.scalar(select(func.count()).select_from(IngestionRun)) == 2


def test_concurrent_exact_replay_has_one_committed_run(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.config import Settings
    from app.db.session import create_sqlite_engine, initialize_database
    engine = create_sqlite_engine(Settings(database_url=f"sqlite:///{tmp_path / 'concurrent.db'}"))
    initialize_database(engine)
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy([source()]))
    req = request([{"device_id": "D"}])
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: service.ingest(req), range(2)))
        assert {r["replayed"] for r in results} == {False, True}
        assert len({r["run_id"] for r in results}) == 1
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(IngestionRun)) == 1
            assert db.scalar(select(func.count()).select_from(Device)) == 1
    finally:
        engine.dispose()


def test_database_lock_is_a_retryable_error(tmp_path):
    from app.config import Settings
    from app.db.session import create_sqlite_engine, initialize_database
    from app.ingestion.persistence import DatabaseBusy
    engine = create_sqlite_engine(Settings(database_url=f"sqlite:///{tmp_path / 'locked.db'}", sqlite_busy_timeout_ms=1))
    initialize_database(engine)
    sessions = session_factory(engine)
    try:
        with engine.connect().execution_options(sqlite_write=True) as connection:
            transaction = connection.begin()
            with pytest.raises(DatabaseBusy):
                IngestService(sessions, Policy([source()])).ingest(request([{"device_id": "D"}]))
            transaction.rollback()
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(IngestionRun)) == 0
    finally:
        engine.dispose()
