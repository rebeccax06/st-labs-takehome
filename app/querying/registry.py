"""Allowed query vocabulary, independent of SQLAlchemy and database contents."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class QueryField:
    value_type: Literal["string", "boolean", "uuid"]
    description: str
    choices: tuple[str, ...] = ()
    reference: bool = False


CI_ID = QueryField("uuid", "Internal CMDB UUID; never infer from an external ID or name.", reference=True)
FIELDS: dict[str, dict[str, QueryField]] = {
    "device": {
        "ci_id": CI_ID,
        "hostname": QueryField("string", "Exact hostname; not globally unique.", reference=True),
        "os_family": QueryField("string", "Normalized OS family, e.g. macos, linux, windows. No OS-version or distribution filter."),
        "status": QueryField("string", "Observed operational status.", ("active", "inactive", "unknown")),
        "encrypted": QueryField("boolean", "Explicit encryption state; unknown is null."),
    },
    "user": {
        "ci_id": CI_ID,
        "name": QueryField("string", "Exact display name; may match multiple users.", reference=True),
        "email": QueryField("string", "Exact email, not an address guessed from a person's name.", reference=True),
        "team": QueryField("string", "Observed team label; not inferred from group membership."),
        "status": QueryField("string", "Observed account status.", ("active", "inactive", "suspended", "unknown")),
        "mfa_enabled": QueryField("boolean", "False means explicitly disabled; null means missing or conflicted."),
    },
    "application": {
        "ci_id": CI_ID,
        "name": QueryField("string", "Literal application name from the question. Scoped aliases resolve later.", reference=True),
        "app_type": QueryField("string", "Normalized application type.", ("saas", "on_prem", "unknown")),
        "owner_team": QueryField("string", "Team-owner label; human owners use owned_by edges."),
    },
}


def canonical_uuid(value: str) -> str:
    if len(value) != 36:
        raise ValueError("ci_id must be a hyphenated internal UUID")
    parsed = str(UUID(value))
    if value.casefold() != parsed:
        raise ValueError("ci_id must be a hyphenated internal UUID")
    return parsed


def validate_value(kind: str, field: str, value: str | bool) -> None:
    spec = FIELDS[kind].get(field)
    if spec is None:
        raise ValueError(f"Field {field} is not queryable on {kind}")
    if spec.value_type == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{kind}.{field} requires a JSON boolean")
    elif type(value) is not str:
        raise ValueError(f"{kind}.{field} requires a string")
    elif spec.value_type == "uuid":
        canonical_uuid(value)
    if spec.choices and value not in spec.choices:
        raise ValueError(f"Invalid enum value for {kind}.{field}")


def catalog() -> dict:
    return {kind: {field: dict(type=spec.value_type, description=spec.description,
                              **({"values": list(spec.choices)} if spec.choices else {}))
                   for field, spec in fields.items()} for kind, fields in FIELDS.items()}
