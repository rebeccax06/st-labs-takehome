import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ingestion.errors import InputError
from app.ingestion.parsing import MAX_BYTES, parse_content
from app.ingestion.service import prepare_ingestion
from app.schemas.ingest import IngestRequest, SourceConfig
from app.schemas.normalized import DeviceRecord, NormalizedBatch, UserRecord

INPUTS = Path(__file__).resolve().parents[2] / "input_data"
LAYOUTS = {
    "sample_hardware.json": ("hardware_json", "json", None),
    "sample_hardware.csv": ("hardware_csv", "csv", None),
    "sample_hardware.yaml": ("hardware_yaml", "yaml", ["inventory", "metadata", "export_date"]),
    "sample_okta.json": ("okta_json", "json", None),
    "sample_app.json": ("applications_json", "json", ["inventory_metadata", "export_date"]),
}


def source(adapter="hardware_json", **changes):
    return SourceConfig(**({"organization_id": "org-a", "source_id": "inventory", "adapter": adapter} | changes))


def prepare(content, *, config=None, format="json"):
    config = config or source()
    if not isinstance(content, str):
        content = json.dumps(content)
    request = IngestRequest(organization_id=config.organization_id, source_id=config.source_id,
                            format=format, content=content)
    return prepare_ingestion(request, [config])


def fixture(name):
    adapter, format, snapshot = LAYOUTS[name]
    config = source(adapter, source_id=name.replace(".", "-"), snapshot_time_path=snapshot,
                    identifier_authorities={"device_id": "hardware", "serial": "serial-domain",
                                            "email": "directory", "app_name": "catalog"},
                    case_insensitive_identifiers=["app_name", "email"])
    return prepare(INPUTS.joinpath(name).read_text(), config=config, format=format)


@pytest.mark.parametrize("name,entities,edges", [
    ("sample_hardware.json", 3, 3), ("sample_hardware.csv", 3, 3),
    ("sample_hardware.yaml", 6, 3), ("sample_okta.json", 9, 25), ("sample_app.json", 6, 16),
])
def test_all_supplied_layouts_produce_valid_contracts(name, entities, edges):
    prepared = fixture(name)
    assert len(prepared.batch.records) == entities
    assert sum(len(row.relationships) for row in prepared.batch.records) == edges
    assert prepared.warning_count == 0
    assert NormalizedBatch.model_validate_json(prepared.batch.model_dump_json()) == prepared.batch


def test_flat_hardware_preserves_missing_fields_and_unresolved_people():
    rows = fixture("sample_hardware.json").batch.records
    assert [r.attributes.os_family for r in rows] == ["macos", "linux", "macos"]
    assert rows[0].attributes.last_checkin == datetime(2024, 6, 12, 14, 30, tzinfo=UTC)
    assert all(r.observed_at is None for r in rows)
    assert rows[2].attributes.ip_address is None
    assert "ip_address" not in rows[2].attributes.model_fields_set
    assert rows[0].relationships[0].target.display_name == "John D."
    assert rows[0].relationships[0].target.identifiers == []
    assert rows[0].raw_record["os"] == "macos"


def test_csv_normalizes_strings_and_retains_raw_evidence():
    rows = fixture("sample_hardware.csv").batch.records
    assert all(row.attributes.encrypted is True for row in rows)
    assert rows[0].attributes.os_version == "Ventura"
    assert rows[1].attributes.os_version == "22.04 LTS"
    assert rows[2].attributes.location == "New York HQ - Floor 15"
    assert rows[0].raw_record["encryption_status"] == "FileVault Enabled"
    assert rows[0].evidence.pointer == "/rows/0"
    assert all(row.observed_at is None for row in rows)


def test_yaml_nested_entities_and_assignment_keys():
    prepared = fixture("sample_hardware.yaml")
    devices = [r for r in prepared.batch.records if isinstance(r, DeviceRecord)]
    users = [r for r in prepared.batch.records if isinstance(r, UserRecord)]
    assert [r.attributes.status for r in devices] == ["inactive", "active", "active"]
    assert devices[0].attributes.serial_number == "LAP-JD-20230615"
    assert devices[0].attributes.os_version == "Ventura 13.5"
    assert devices[0].attributes.location == "London"
    assert users[0].attributes.team == "Platform Engineering"
    assert users[2].attributes.title == "Chief Technology Officer"
    assert all(r.attributes.mfa_enabled is None for r in users)
    assert devices[0].relationships[0].target.identifiers == users[0].identifiers
    assert users[0].record_key == "/inventory/devices/0/assigned_to"
    assert devices[0].raw_record["hardware"]["ram_gb"] == 32
    assert prepared.source_metadata["inventory_metadata"]["organization"] == "Example Corp"
    assert all(r.observed_at == datetime(2024, 7, 23, 14, tzinfo=UTC) for r in prepared.batch.records)


def test_okta_preserves_groups_false_and_suspended_accounts():
    rows = fixture("sample_okta.json").batch.records
    assert sum(r.attributes.mfa_enabled is False for r in rows) == 3
    assert rows[-1].attributes.status == "suspended"
    assert rows[1].attributes.groups == ["Engineering", "Admins"]
    assert rows[1].attributes.team is None
    assert all(r.observed_at is None for r in rows)
    assert rows[0].relationships[0].relation_type == "uses"
    assert rows[0].relationships[0].evidence.pointer == "/0/apps/0"


def test_application_metadata_is_not_an_entity_or_dependency():
    prepared = fixture("sample_app.json")
    rows = prepared.batch.records
    assert rows[0].attributes.reported_user_count == 52
    assert rows[0].attributes.owner_team == "IT Operations"
    assert rows[3].attributes.app_type == "on_prem"
    assert rows[4].attributes.deployment == "self-hosted"
    assert rows[4].raw_record["server_location"] == "AWS us-east-1"
    assert rows[0].raw_record["annual_cost_usd"] == 24000
    assert all(e.relation_type == "integrates_with" for r in rows for e in r.relationships)
    assert rows[0].relationships[0].target.identifiers[0].value == "github"  # No alias resolution here.
    assert prepared.source_metadata["summary"]["total_apps"] == 6


def test_shared_identity_scope_is_configuration_not_adapter_or_file_format():
    json_key = fixture("sample_hardware.json").batch.records[0].identifiers[0]
    csv_key = fixture("sample_hardware.csv").batch.records[0].identifiers[0]
    assert json_key == csv_key
    a = prepare([{"device_id": "42"}]).batch.records[0].identifiers[0]
    b = prepare([{"device_id": "42"}], config=source(source_id="other-system")).batch.records[0].identifiers[0]
    assert a.value == b.value
    assert a.scope != b.scope


def test_email_and_serial_cross_source_keys_are_opt_in():
    config = source("hardware_yaml")
    rows = prepare(INPUTS.joinpath("sample_hardware.yaml").read_text(), config=config, format="yaml").batch.records
    assert {i.scope.identifier_type for r in rows for i in r.identifiers} == {"device_id", "employee_id"}
    assert all(r.observed_at is None for r in rows)  # Export time not trusted without configuration.


def test_no_name_matching_deduplication_or_resolution_during_preparation():
    row = {"device_id": "42", "hostname": "host"}
    result = prepare([row, row])
    assert len(result.batch.records) == 2
    assert result.batch.records[0].record_key != result.batch.records[1].record_key
    assert result.batch.records[0].identifiers == result.batch.records[1].identifiers
    # Blank values don't create a guessed stable key.
    assert prepare([{"device_id": "", "hostname": "host"}]).batch.records[0].identifiers == []


@pytest.mark.parametrize("value,expected", [(False, False), ("false", False), ("disabled", False),
                                            (True, True), ("enabled", True), (None, None), ("", None)])
def test_false_and_unknown_are_distinct(value, expected):
    result = prepare([{"user_id": "u-1", "mfa_enabled": value}], config=source("okta_json"))
    assert result.batch.records[0].attributes.mfa_enabled is expected
    assert result.warning_count == 0


def test_unknown_descriptions_warn_without_inventing_false():
    result = prepare([{"device_id": "1", "status": "pending-review", "encryption_status": "not reported"}])
    attrs = result.batch.records[0].attributes
    assert attrs.encrypted is None
    assert attrs.status == "unknown"
    assert result.warning_count == 2
    assert {w.path for w in result.warnings} == {"/0/status", "/0/encryption_status"}


def test_warning_details_are_bounded_but_total_is_preserved():
    result = prepare([{"device_id": str(i), "status": "pending"} for i in range(60)])
    assert result.warning_count == 60
    assert len(result.warnings) == 50


@pytest.mark.parametrize("row", [
    {"device_id": "1", "ip_address": "invalid"},
    {"device_id": "1", "mac_address": "bad"},
    {"device_id": "1", "hostname": 23},
    {"device_id": "1", "last_checkin": "2024-01-01T12:00:00"},
    {"device_id": "1", "assigned_to": {"name": "Wrong layout"}},
])
def test_invalid_fields_fail_the_entire_preparation_with_path(row):
    with pytest.raises(InputError) as error:
        prepare([{"device_id": "good"}, row])
    assert error.value.path.startswith("/1/")


def test_mac_ip_and_offsets_normalize_without_touching_raw_values():
    row = {"device_id": 17, "mac_address": "aa-bb-cc-dd-ee-ff", "ip_address": "2001:0db8::1",
           "last_checkin": "2025-01-01T12:00:00+02:00"}
    normalized = prepare([row]).batch.records[0]
    assert normalized.identifiers[0].value == "17"
    assert normalized.attributes.mac_address == "AA:BB:CC:DD:EE:FF"
    assert normalized.attributes.ip_address == "2001:db8::1"
    assert normalized.attributes.last_checkin.hour == 10
    assert normalized.raw_record == row


def test_normalized_schema_failure_has_record_path_and_sanitized_message():
    with pytest.raises(InputError) as error:
        prepare([{"user_id": "u1", "email": "secret-invalid-address"}], config=source("okta_json"))
    assert error.value.path == "/0/email"
    assert "secret-invalid-address" not in str(error.value)


@pytest.mark.parametrize("content,format", [
    ('[{"device_id":"1","device_id":"2"}]', "json"),
    ('{"broken":', "json"),
    ('[{"device_id":"1","x":NaN}]', "json"),
    ('device_id,device_id\n1,2\n', "csv"),
    ('device_id,hostname\n1,host,extra\n', "csv"),
    ('device_id,hostname\n1\n', "csv"),
    ('device_id,hostname\n1,"unterminated\n', "csv"),
    ('inventory:\n  devices: []\n  devices: []\n', "yaml"),
    ('!!python/object/apply:os.system ["echo unsafe"]', "yaml"),
    ('inventory: &a {devices: []}\ncopy: *a\n', "yaml"),
    ('inventory: {devices: [}', "yaml"),
    ('inventory: {devices: [], bad: .nan}', "yaml"),
    ('? [a, b]\n: value', "yaml"),
])
def test_malformed_or_unsafe_input_rejected(content, format):
    with pytest.raises(InputError):
        parse_content(content, format)


@pytest.mark.parametrize("content,adapter,format", [
    ('{}', "hardware_json", "json"), ('[23]', "hardware_json", "json"),
    ('[{"unrecognized":1}]', "hardware_json", "json"),
    ('hostname,os\nhost,linux\n', "hardware_csv", "csv"),
    ('inventory: {devices: [{device_id: 1, network: []}]}', "hardware_yaml", "yaml"),
    ('[{"user_id":"u1","apps":"App"}]', "okta_json", "json"),
    ('[{"user_id":"u1","groups":[1]}]', "okta_json", "json"),
    ('{"applications":[{"app_id":"a","users_count":"10"}]}', "applications_json", "json"),
])
def test_known_layout_errors_do_not_fall_back_to_ai(content, adapter, format):
    with pytest.raises(InputError):
        prepare(content, config=source(adapter), format=format)


def test_size_depth_and_record_limits():
    for content in ['"' + 'x' * MAX_BYTES + '"', json.dumps([{"device_id": str(i)} for i in range(1001)])]:
        with pytest.raises(InputError) as error:
            prepare(content)
        assert error.value.code == "limit_exceeded"
    with pytest.raises(InputError, match="nesting"):
        parse_content('[' * 40 + '0' + ']' * 40, "json")
    with pytest.raises(InputError):
        parse_content('a: ' + '[' * 40 + '0' + ']' * 40, "yaml")


def test_extracted_entity_limit_includes_nested_users():
    document = {"inventory": {"devices": [
        {"device_id": str(i), "assigned_to": {"employee_id": str(i)}} for i in range(501)
    ]}}
    # JSON is valid YAML; the configured adapter still requires YAML format.
    with pytest.raises(InputError) as error:
        prepare(document, config=source("hardware_yaml"), format="yaml")
    assert error.value.code == "limit_exceeded"


def test_relationship_limit_across_multiple_records():
    with pytest.raises(InputError) as error:
        prepare([{"user_id": "1", "apps": ["App"] * 2501},
                 {"user_id": "2", "apps": ["App"] * 2500}], config=source("okta_json"))
    assert error.value.code == "limit_exceeded"


def test_timestamp_policy_activity_is_not_snapshot_freshness():
    with pytest.raises(ValidationError, match="Activity"):
        source(snapshot_time_path=["last_checkin"])
    with pytest.raises(ValidationError, match="Activity"):
        source("okta_json", snapshot_time_path=["last_login"])
    result = prepare({"inventory_metadata": {}, "applications": [{"app_id": "a"}]},
                     config=source("applications_json", snapshot_time_path=["inventory_metadata", "export_date"]))
    assert result.batch.records[0].observed_at is None
    assert result.warnings[0].code == "missing_snapshot_time"


def test_source_and_format_are_explicit():
    request = IngestRequest(organization_id="org-a", source_id="unknown", format="json", content="[]")
    with pytest.raises(InputError, match="Unknown source"):
        prepare_ingestion(request, [source()])
    with pytest.raises(InputError, match="Format"):
        prepare('device_id,hostname\n1,host\n', format="csv")
    with pytest.raises(InputError, match="No deterministic"):
        prepare('raw text', config=source("unfamiliar"), format="text")
    with pytest.raises(ValueError, match="Duplicate source"):
        prepare_ingestion(request, [source(), source()])


@pytest.mark.parametrize("changes", [{"content": " "}, {"format": "xml"}, {"unexpected": "value"}])
def test_envelope_validation(changes):
    with pytest.raises(ValidationError):
        IngestRequest(**({"organization_id": "org-a", "source_id": "inventory", "format": "json", "content": "[]"} | changes))


def test_new_adapter_can_use_same_workflow_without_modifying_registry():
    def custom_adapter(document, ctx):
        return [DeviceRecord(**ctx.base(document, "/asset"), attributes={"hostname": document["label"]})], {}
    request = IngestRequest(organization_id="org-a", source_id="inventory", format="json", content='{"label":"generic-host"}')
    result = prepare_ingestion(request, [source("custom")], adapters={"custom": ("json", custom_adapter)})
    assert result.batch.records[0].attributes.hostname == "generic-host"


def test_source_count_mismatch_is_a_warning_not_a_fabricated_entity():
    result = prepare({"inventory_metadata": {"total_applications": 99}, "applications": [{"app_id": "a"}]},
                     config=source("applications_json"))
    assert len(result.batch.records) == 1
    assert result.warnings[0].code == "count_mismatch"


def test_csv_supports_quoted_delimiters_and_multiline_values():
    content = 'device_id,hostname,location,notes\r\n1,host,"Office, West","line one\nline two"\r\n'
    row = prepare(content, config=source("hardware_csv"), format="csv").batch.records[0]
    assert row.attributes.location == "Office, West"
    assert row.raw_record["notes"] == "line one\nline two"


def test_unquoted_yaml_dates_remain_json_compatible_text():
    parsed = parse_content('inventory: {devices: [], exported: 2024-07-23}', "yaml")
    assert parsed["inventory"]["exported"] == "2024-07-23"


def test_escaped_invalid_unicode_and_infinite_number_are_rejected():
    for document in ['[{"device_id":"1","hostname":"\\ud800"}]',
                     '[{"device_id":"1","other":1e999}]']:
        with pytest.raises(InputError):
            prepare(document)


def test_empty_inventory_is_rejected_without_partial_output():
    with pytest.raises(InputError):
        prepare([])


def test_false_encryption_missing_assignee_and_unknown_fields_survive():
    row = {"device_id": "1", "encryption_status": "FileVault Disabled", "assigned_to": " ",
           "extra_vendor_metadata": {"custom": [1, True, None]}}
    result = prepare([row]).batch.records[0]
    assert result.attributes.encrypted is False
    assert result.relationships == []
    assert result.raw_record == row
