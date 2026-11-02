"""Adapters for flat JSON, CSV, and the nested hardware inventory layout."""

from app.ingestion.adapters.common import mapping, records
from app.ingestion.errors import InputError
from app.ingestion.normalization import boolean, ip, mac, nonmissing, operating_system, status, text, timestamp
from app.schemas.normalized import (
    DeviceAttributes, DeviceRecord, EntityReference, Evidence,
    RelationshipAssertion, UserAttributes, UserRecord,
)


def _device_keys(ctx, row, path, serial=None):
    return ctx.identifiers(
        ctx.identifier("device", "device_id", row.get("device_id"), path + "/device_id"),
        ctx.identifier("device", "serial", serial, path + "/serial_number", trusted_only=True),
    )


def _flat_device(ctx, row, path):
    ctx.record_path = path
    if not {"device_id", "hostname"}.intersection(row):
        raise InputError("Device object requires device_id or hostname field", path=path)
    os_family, version = operating_system(row.get("os"), path + "/os")
    attributes = DeviceAttributes(**nonmissing(dict(
        hostname=text(row.get("hostname"), path + "/hostname"),
        ip_address=ip(row.get("ip_address"), path + "/ip_address"),
        mac_address=mac(row.get("mac_address"), path + "/mac_address"),
        serial_number=text(row.get("serial_number"), path + "/serial_number"),
        os_family=os_family, os_version=version,
        location=text(row.get("location"), path + "/location"),
        status=status(row.get("status"), path + "/status", ctx.warn),
        encrypted=boolean(row.get("encryption_status"), path + "/encryption_status", ctx.warn),
        device_type=text(row.get("device_type"), path + "/device_type"),
        department=text(row.get("department"), path + "/department"),
        last_checkin=timestamp(row.get("last_checkin"), path + "/last_checkin"),
    )))
    edges = []
    assignee = text(row.get("assigned_to"), path + "/assigned_to")
    if assignee is not None:
        edges.append(RelationshipAssertion(
            relation_type="assigned_to", target=EntityReference(ci_type="user", display_name=assignee),
            evidence=Evidence(pointer=path + "/assigned_to"),
        ))
    return DeviceRecord(**ctx.base(row, path), display_name=attributes.hostname,
                        identifiers=_device_keys(ctx, row, path, attributes.serial_number),
                        attributes=attributes, relationships=edges)


def hardware_json(document, ctx):
    rows = records(document, "")
    return [_flat_device(ctx, row, f"/{i}") for i, row in enumerate(rows)], {}


def hardware_csv(document, ctx):
    if not {"device_id", "hostname"}.issubset(document["headers"]):
        raise InputError("Hardware CSV requires device_id and hostname headers")
    return [_flat_device(ctx, row, f"/rows/{i}") for i, row in enumerate(document["rows"])], {}


def hardware_yaml(document, ctx):
    root = mapping(document, "/")
    inventory = mapping(root.get("inventory"), "/inventory")
    rows = records(inventory.get("devices"), "/inventory/devices")
    metadata = mapping(inventory.get("metadata", {}), "/inventory/metadata")
    ctx.check_count(metadata.get("total_devices"), len(rows), "/inventory/metadata/total_devices")
    result = []
    for i, row in enumerate(rows):
        path = f"/inventory/devices/{i}"
        ctx.record_path = path
        if not {"device_id", "hostname"}.intersection(row):
            raise InputError("Device object requires device_id or hostname field", path=path)
        nested = {key: mapping(row.get(key, {}), path + "/" + key) for key in (
            "hardware", "network", "operating_system", "security", "location", "status"
        )}
        hardware = nested["hardware"]
        network = nested["network"]
        os = nested["operating_system"]
        security = nested["security"]
        location = nested["location"]
        state = nested["status"]
        os_family, embedded_version = operating_system(os.get("name"), path + "/operating_system/name")
        attributes = DeviceAttributes(**nonmissing(dict(
            hostname=text(row.get("hostname"), path + "/hostname"),
            ip_address=ip(network.get("primary_ip"), path + "/network/primary_ip"),
            mac_address=mac(network.get("mac_address"), path + "/network/mac_address"),
            serial_number=text(hardware.get("serial_number"), path + "/hardware/serial_number"),
            os_family=os_family,
            os_version=text(os.get("version"), path + "/operating_system/version") or embedded_version,
            location=text(location.get("site"), path + "/location/site"),
            status=status(state.get("operational_status"), path + "/status/operational_status", ctx.warn),
            encrypted=boolean(security.get("encryption_enabled"), path + "/security/encryption_enabled", ctx.warn),
            device_type=text(row.get("type"), path + "/type"),
            last_checkin=timestamp(state.get("last_checkin"), path + "/status/last_checkin"),
        )))
        edges = []
        person = row.get("assigned_to")
        if person is not None:
            person_path = path + "/assigned_to"
            ctx.record_path = person_path
            person = mapping(person, person_path)
            user_attributes = UserAttributes(**nonmissing({
                key: text(person.get(key), person_path + "/" + key)
                for key in ("name", "email", "department", "team", "title")
            }))
            keys = ctx.identifiers(
                ctx.identifier("user", "employee_id", person.get("employee_id"), person_path + "/employee_id"),
                ctx.identifier("user", "email", user_attributes.email, person_path + "/email", trusted_only=True),
            )
            display = user_attributes.name or user_attributes.email
            if keys or display:
                result.append(UserRecord(**ctx.base(person, person_path), display_name=display,
                                         identifiers=keys, attributes=user_attributes))
                edges.append(RelationshipAssertion(
                    relation_type="assigned_to", target=EntityReference(
                        ci_type="user", identifiers=keys, display_name=display,
                        evidence=Evidence(pointer=person_path),
                    ), evidence=Evidence(pointer=person_path),
                ))
        ctx.record_path = path
        result.append(DeviceRecord(**ctx.base(row, path), display_name=attributes.hostname,
                                   identifiers=_device_keys(ctx, row, path, attributes.serial_number),
                                   attributes=attributes, relationships=edges))
    return result, {"inventory_metadata": metadata}
