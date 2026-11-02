"""The model proposes facts and evidence, never namespaces or internal CI IDs."""

from pydantic import Field

from app.schemas.normalized import Contract, Evidence, Kind, Relation, Text
from typing import Literal


class ExtractedIdentifier(Contract):
    identifier_type: Literal["device_id", "user_id", "app_id", "external_id", "employee_id", "email", "serial", "app_name"]
    value: Text
    evidence: Evidence


class ExtractedTarget(Contract):
    ci_type: Kind
    display_name: Text | None = None
    identifiers: list[ExtractedIdentifier] = Field(default_factory=list, max_length=20)
    evidence: Evidence


class ExtractedRelationship(Contract):
    relation_type: Relation
    target: ExtractedTarget
    evidence: Evidence


AttributeValue = str | bool | int | list[str] | None


class ExtractedRecord(Contract):
    ci_type: Kind
    attributes: dict[str, AttributeValue]
    attribute_evidence: dict[str, Evidence]
    identifiers: list[ExtractedIdentifier] = Field(default_factory=list, max_length=20)
    relationships: list[ExtractedRelationship] = Field(default_factory=list, max_length=5000)
    evidence: Evidence


class Extraction(Contract):
    records: list[ExtractedRecord] = Field(default_factory=list, max_length=1000)
    unsupported_reason: Text | None = None
