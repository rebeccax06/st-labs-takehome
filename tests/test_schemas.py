import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain import validate_endpoints
from app.identity.namespaces import IdentifierScope
from app.schemas.normalized import (
    ApplicationAttributes, DeviceAttributes, DeviceRecord, Evidence, Identifier,
    NormalizedBatch, UserAttributes,
)


def scope(**changes):
    return IdentifierScope(**({
        "organization_id": "org-a", "ci_type": "device",
        "authority_id": "inventory", "identifier_type": "device_id",
    } | changes))


def record(**changes):
    return {"ci_type": "device", "record_key": "/0", "organization_id": "org-a",
            "attributes": {"hostname": "host-17"}} | changes


def test_canonical_namespace_round_trip_and_scoping():
    namespace = scope().to_namespace()
    assert namespace == '["org-a","device","inventory","device_id"]'
    assert IdentifierScope.from_namespace(namespace) == scope()
    assert len({scope(**c).to_namespace() for c in [
        {}, {"organization_id": "org-b"}, {"ci_type": "user"},
        {"authority_id": "other"}, {"identifier_type": "serial"},
    ]}) == 5


@pytest.mark.parametrize("namespace", [
    '["org-a", "device", "inventory", "device_id"]',
    '["org-a","device","inventory"]', 'not-json', '{}',
    '["org-a","invalid","inventory","device_id"]',
    '["org-a","device","","device_id"]', '[1,"device","inventory","device_id"]',
])
def test_invalid_or_noncanonical_namespace_rejected(namespace):
    with pytest.raises(ValueError):
        IdentifierScope.from_namespace(namespace)


def test_normalized_batch_supports_each_ci_type_and_json_round_trip():
    raw = {"records": [
        record(observed_at="2025-01-02T12:00:00Z", attributes={"ip_address": "2001:db8::1"}),
        record(ci_type="user", record_key="/1", attributes={"mfa_enabled": False, "groups": ["IT"]}),
        record(ci_type="application", record_key="/2", attributes={"reported_user_count": 0}),
    ]}
    batch = NormalizedBatch.model_validate_json(json.dumps(raw))
    assert batch.records[0].observed_at == datetime(2025, 1, 2, 12, tzinfo=UTC)
    assert batch.records[1].attributes.mfa_enabled is False
    assert NormalizedBatch.model_validate_json(batch.model_dump_json()) == batch


def test_keyless_records_are_representable_for_later_quarantine():
    item = DeviceRecord.model_validate(record())
    assert item.identifiers == []
    assert item.observed_at is None  # Activity does not populate observation time.


def test_strict_unknown_false_true_and_field_omission():
    assert UserAttributes().mfa_enabled is None
    assert UserAttributes(mfa_enabled=False).mfa_enabled is False
    assert UserAttributes(mfa_enabled=True).mfa_enabled is True
    assert "mfa_enabled" not in UserAttributes().model_fields_set
    assert "mfa_enabled" in UserAttributes(mfa_enabled=None).model_fields_set


@pytest.mark.parametrize("value", ["false", "true", "0", 0, 1])
def test_boolean_coercion_rejected(value):
    with pytest.raises(ValidationError):
        UserAttributes(mfa_enabled=value)


@pytest.mark.parametrize("values", [
    {"reported_user_count": -1}, {"reported_user_count": "10"},
    {"reported_user_count": True}, {"app_type": "on-premise"}, {"made_up": "value"},
])
def test_application_attributes_require_normalized_values(values):
    with pytest.raises(ValidationError):
        ApplicationAttributes(**values)


@pytest.mark.parametrize("values", [
    {"ip_address": "not-an-ip"}, {"mac_address": "AA-BB-CC-DD-EE-FF"},
    {"status": "deactivated"}, {"hostname": " "},
    {"last_checkin": datetime(2025, 1, 1)}, {"hostname": 123},
])
def test_invalid_device_attributes(values):
    with pytest.raises(ValidationError):
        DeviceAttributes(**values)


@pytest.mark.parametrize("email", ["not-an-email", "@example.com", "a@", "a b@example.com", "a@b@c"])
def test_invalid_user_addresses(email):
    with pytest.raises(ValidationError):
        UserAttributes(email=email)


def test_identifier_scope_matches_record():
    invalid = Identifier(scope=scope(ci_type="user"), value="U-17")
    with pytest.raises(ValidationError, match="scope must match"):
        DeviceRecord.model_validate(record(identifiers=[invalid]))


def test_unresolved_name_reference_is_preserved_without_invented_identifier():
    edge = {"relation_type": "assigned_to", "target": {"ci_type": "user", "display_name": "A. Person"}}
    item = DeviceRecord.model_validate(record(relationships=[edge]))
    assert item.relationships[0].target.identifiers == []


@pytest.mark.parametrize("edge", [
    {"relation_type": "uses", "target": {"ci_type": "application", "display_name": "App"}},
    {"relation_type": "assigned_to", "target": {"ci_type": "device", "display_name": "Host"}},
    {"relation_type": "invented", "target": {"ci_type": "user", "display_name": "User"}},
    {"relation_type": "assigned_to", "target": {"ci_type": "user"}},
])
def test_invalid_relationship_assertions(edge):
    with pytest.raises(ValidationError):
        DeviceRecord.model_validate(record(relationships=[edge]))


def test_cross_organization_target_identifier_rejected():
    edge = {"relation_type": "assigned_to", "target": {"ci_type": "user", "identifiers": [
        Identifier(scope=scope(organization_id="org-b", ci_type="user"), value="U-17")
    ]}}
    with pytest.raises(ValidationError, match="share the record organization"):
        DeviceRecord.model_validate(record(relationships=[edge]))


def test_batch_limits_and_duplicate_paths():
    for records in [[], [record(), record()], [record(record_key=f"/{i}") for i in range(1001)]]:
        with pytest.raises(ValidationError):
            NormalizedBatch.model_validate({"records": records})
    with pytest.raises(ValidationError, match="one organization"):
        NormalizedBatch.model_validate({"records": [record(), record(record_key="/1", organization_id="org-b")]})


def test_batch_edge_limit_is_global():
    edge = {"relation_type": "assigned_to", "target": {"ci_type": "user", "display_name": "User"}}
    with pytest.raises(ValidationError, match="5000"):
        NormalizedBatch.model_validate({"records": [record(relationships=[edge] * 2501),
                                                    record(record_key="/1", relationships=[edge] * 2500)]})


def test_extra_fields_and_naive_observation_time_rejected():
    for changes in [{"invented": 1}, {"observed_at": datetime(2025, 1, 1)}]:
        with pytest.raises(ValidationError):
            DeviceRecord.model_validate(record(**changes))


def test_evidence_requires_pointer_or_quote():
    with pytest.raises(ValidationError):
        Evidence()
    assert Evidence(pointer="/0/asset_id").pointer is not None
    assert Evidence(quote="Asset A-17").quote is not None


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("CMDB_DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("CMDB_SQLITE_BUSY_TIMEOUT_MS", "1234")
    settings = Settings.from_env()
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.sqlite_busy_timeout_ms == 1234
    monkeypatch.setenv("CMDB_SQLITE_BUSY_TIMEOUT_MS", "-1")
    with pytest.raises(ValidationError):
        Settings.from_env()
