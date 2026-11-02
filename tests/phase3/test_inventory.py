import json
from pathlib import Path

from sqlalchemy import select

from app.api.inventory_service import InventoryService
from app.db.models import Device, Relationship
from app.db.session import session_factory
from app.ingestion.persistence import IngestService
from app.ingestion.service import ADAPTERS
from app.reconciliation.policy import Policy, load_sources
from app.schemas.api import CIDetail, CIView, Page
from app.schemas.ingest import IngestRequest, PreparedIngestion, SourceConfig
from app.schemas.normalized import NormalizedBatch

ROOT = Path(__file__).resolve().parents[2]


def test_inventory_serialization_and_bounded_attribution(engine):
    sources = load_sources(ROOT / "examples/sample_sources.json")
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy(sources))
    for entry in json.loads((ROOT / "examples/sample_sources.json").read_text()):
        cfg = entry["source"]
        service.ingest(IngestRequest(organization_id=cfg["organization_id"], source_id=cfg["source_id"],
                                     format=ADAPTERS[cfg["adapter"]][0],
                                     content=(ROOT / "input_data" / entry["file"]).read_text()))
    with sessions() as session:
        inventory = InventoryService(session, "example-corp")
        users = Page[CIView].model_validate(inventory.list("user", {"mfa_enabled": False}, 100, 0))
        assert users.total == 3
        devices = Page[CIView].model_validate(inventory.list("device", {}, 100, 0))
        assert devices.total == 3
        device = next(row for row in devices.data if row.attributes["hostname"] == "laptop-jdoe")
        detail = CIDetail.model_validate(inventory.detail(device.id, 1, 0))
        assert detail.relationships.total == 1
        assert detail.attributes["status"] is None
        assert {e["value"] for e in detail.field_state["status"]["evidence"]} == {"active", "inactive"}
        assert any(w["code"] == "unresolved_references" for w in detail.warnings)
        apps = Page[CIView].model_validate(inventory.list("application", {}, 100, 0))
        assert apps.total == 28
        github = next(row for row in apps.data if row.attributes["name"] == "GitHub Enterprise")
        assert github.observed_assignment_count == 5
        assert github.attributes["reported_user_count"] == 28


def test_all_relationship_types_persist_from_normalized_contract(engine):
    cfg = SourceConfig(organization_id="org-a", source_id="test", adapter="hardware_json")
    sessions = session_factory(engine)
    service = IngestService(sessions, Policy([cfg]))
    def key(kind, value):
        return {"scope": {"organization_id": "org-a", "ci_type": kind, "authority_id": "inventory",
                          "identifier_type": "external_id"}, "value": value}
    def edge(relation, kind, value):
        return {"relation_type": relation, "target": {"ci_type": kind, "identifiers": [key(kind, value)]}}
    records = []
    for kind, value, edges in [
        ("device", "D", [edge("assigned_to", "user", "U")]),
        ("user", "U", [edge("uses", "application", "A")]),
        ("application", "A", [edge("owned_by", "user", "U"), edge("depends_on", "application", "B"),
                               edge("integrates_with", "application", "B")]),
        ("application", "B", []),
    ]:
        records.append(dict(ci_type=kind, organization_id="org-a", record_key=f"/{value}",
                            identifiers=[key(kind, value)], attributes={}, relationships=edges))
    prepared = PreparedIngestion(organization_id="org-a", source_id="test", warning_count=0, warnings=[],
                                 batch=NormalizedBatch.model_validate_json(json.dumps({"records": records})))
    result = service.persist(IngestRequest(organization_id="org-a", source_id="test", format="json", content="synthetic-contract"), prepared)
    assert result["entities"]["created"] == 4
    assert result["relationships"]["created"] == 5
    with sessions() as session:
        assert set(session.scalars(select(Relationship.relation_type))) == {
            "assigned_to", "uses", "owned_by", "depends_on", "integrates_with"}
        inventory = InventoryService(session, "org-a")
        apps = inventory.list("application", {}, 100, 0)
        assert sum(len(app["owner_user_ids"]) for app in apps["data"]) == 1
