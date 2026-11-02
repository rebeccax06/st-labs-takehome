"""Source-independent records, not raw input adapters or API ingestion logic.

Missing identifiers are representable so later ingestion can quarantine evidence.
Strict booleans preserve unknown versus false; canonical values are supplied by
adapters, not coerced or guessed by these contracts.
"""

from ipaddress import ip_address
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints,
    field_validator, model_validator,
)

from app.domain import validate_endpoints
from app.identity.namespaces import IdentifierScope

Text = Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"\S")]
Kind = Literal["device", "user", "application"]
Relation = Literal["assigned_to", "uses", "depends_on", "integrates_with", "owned_by"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Contract):
    pointer: Text | None = None
    quote: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def has_evidence(self) -> Self:
        if self.pointer is None and self.quote is None:
            raise ValueError("Evidence requires a source pointer or quote")
        return self


class Identifier(Contract):
    scope: IdentifierScope
    value: Text
    evidence: Evidence | None = None


class DeviceAttributes(Contract):
    hostname: Text | None = None
    ip_address: Text | None = None
    mac_address: Annotated[str, StringConstraints(pattern=r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")] | None = None
    serial_number: Text | None = None
    os_family: Text | None = None
    os_version: Text | None = None
    location: Text | None = None
    status: Literal["active", "inactive", "unknown"] | None = None
    encrypted: bool | None = None
    device_type: Text | None = None
    department: Text | None = None
    last_checkin: AwareDatetime | None = None

    @field_validator("ip_address")
    @classmethod
    def valid_ip(cls, value: str | None) -> str | None:
        if value is not None:
            ip_address(value)
        return value


class UserAttributes(Contract):
    name: Text | None = None
    email: Text | None = None
    team: Text | None = None
    department: Text | None = None
    title: Text | None = None
    status: Literal["active", "inactive", "suspended", "unknown"] | None = None
    mfa_enabled: bool | None = None
    last_login: AwareDatetime | None = None
    groups: list[Text] = Field(default_factory=list, max_length=100)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        # A deliberately modest inventory-address check; no delivery verification.
        if value is not None:
            if value.count("@") != 1 or any(c.isspace() for c in value):
                raise ValueError("Expected an email address")
            local, domain = value.split("@")
            if not local or not domain or domain.startswith(".") or domain.endswith("."):
                raise ValueError("Expected an email address")
        return value


class ApplicationAttributes(Contract):
    name: Text | None = None
    app_type: Literal["saas", "on_prem", "unknown"] | None = None
    vendor: Text | None = None
    category: Text | None = None
    deployment: Text | None = None
    owner_team: Text | None = None
    reported_user_count: Annotated[int, Field(ge=0)] | None = None
    sso_enabled: bool | None = None


class EntityReference(Contract):
    ci_type: Kind
    identifiers: list[Identifier] = Field(default_factory=list, max_length=20)
    display_name: Text | None = None
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def identifier_types_match(self) -> Self:
        if any(i.scope.ci_type != self.ci_type for i in self.identifiers):
            raise ValueError("Reference identifiers must match the target CI type")
        if not self.identifiers and self.display_name is None:
            raise ValueError("Reference requires identifiers or an unresolved display name")
        return self


class RelationshipAssertion(Contract):
    relation_type: Relation
    target: EntityReference
    evidence: Evidence | None = None


class Record(Contract):
    ci_type: Kind
    record_key: Text
    organization_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]
    display_name: Text | None = None
    identifiers: list[Identifier] = Field(default_factory=list, max_length=20)
    observed_at: AwareDatetime | None = None
    relationships: list[RelationshipAssertion] = Field(default_factory=list, max_length=5000)
    evidence: Evidence | None = None
    raw_record: JsonValue = None
    attribute_evidence: dict[str, Evidence] = Field(default_factory=dict)

    @model_validator(mode="after")
    def scopes_and_edges_match(self) -> Self:
        for identifier in self.identifiers:
            if (identifier.scope.organization_id, identifier.scope.ci_type) != (
                self.organization_id, self.ci_type
            ):
                raise ValueError("Identifier scope must match the record organization and type")
        for edge in self.relationships:
            validate_endpoints(edge.relation_type, self.ci_type, edge.target.ci_type)
            for identifier in edge.target.identifiers:
                if identifier.scope.organization_id != self.organization_id:
                    raise ValueError("Relationship targets must share the record organization")
        return self


class DeviceRecord(Record):
    ci_type: Literal["device"] = "device"
    attributes: DeviceAttributes


class UserRecord(Record):
    ci_type: Literal["user"] = "user"
    attributes: UserAttributes


class ApplicationRecord(Record):
    ci_type: Literal["application"] = "application"
    attributes: ApplicationAttributes


NormalizedRecord = Annotated[
    DeviceRecord | UserRecord | ApplicationRecord, Field(discriminator="ci_type")
]


class NormalizedBatch(Contract):
    records: list[NormalizedRecord] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def bounded_unique_records(self) -> Self:
        keys = [record.record_key for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("Record paths must be unique within a batch")
        if sum(len(record.relationships) for record in self.records) > 5000:
            raise ValueError("At most 5000 relationship assertions are allowed per batch")
        if len({record.organization_id for record in self.records}) != 1:
            raise ValueError("One batch must belong to one organization")
        return self
