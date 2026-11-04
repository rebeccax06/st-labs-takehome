"""The model proposes facts and evidence, never namespaces or internal CI IDs."""

import json
from functools import lru_cache
from typing import Annotated, Literal, Union

from pydantic import Field, create_model

from app.schemas.normalized import (
    ApplicationAttributes, Contract, DeviceAttributes, Evidence, Kind, Relation,
    Text, UserAttributes,
)


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


# Provider-facing schemas keep each typed value inseparable from its evidence.
# Internal Extraction remains the conversion boundary used by persistence.


class QuoteEvidence(Contract):
    quote: str = Field(min_length=1, max_length=4096)


class PointerEvidence(Contract):
    pointer: str = Field(min_length=1, max_length=1024)


@lru_cache(maxsize=2)
def extraction_contract(text_input: bool):
    proof = QuoteEvidence if text_input else PointerEvidence
    suffix = "Text" if text_input else "Structured"
    identifier = create_model("Identifier" + suffix, __base__=Contract,
        identifier_type=(ExtractedIdentifier.model_fields["identifier_type"].annotation, ...),
        value=(Text, ...), evidence=(proof, ...))
    target = create_model("Target" + suffix, __base__=Contract,
        ci_type=(Kind, ...), display_name=(Text | None, None),
        identifiers=(list[identifier], ...), evidence=(proof, ...))
    relationship = create_model("Relationship" + suffix, __base__=Contract,
        relation_type=(Relation, ...), target=(target, ...), evidence=(proof, ...))
    records = []
    for kind, attributes in (("device", DeviceAttributes), ("user", UserAttributes),
                             ("application", ApplicationAttributes)):
        fields = {}
        for name, field in attributes.model_fields.items():
            # Reuse attribute annotations so provider and persisted field types stay aligned.
            fact = create_model(kind.title() + name.title().replace("_", "") + "Fact" + suffix,
                                __base__=Contract, value=(field.rebuild_annotation(), ...),
                                evidence=(proof, ...))
            fields[name] = (fact | None, None)
        typed_attributes = create_model(kind.title() + "Facts" + suffix, __base__=Contract, **fields)
        records.append(create_model(kind.title() + "Extraction" + suffix, __base__=Contract,
            ci_type=(Literal[kind], ...), attributes=(typed_attributes, ...),
            identifiers=(list[identifier], ...), relationships=(list[relationship], ...),
            evidence=(proof, ...)))
    return create_model("Extraction" + suffix, __base__=Contract,
                        records=(list[Annotated[Union[tuple(records)], Field(discriminator="ci_type")]], ...),
                        unsupported_reason=(Text | None, None))


def internal_extraction(wire):
    """Separate validated facts into the existing internal attributes/evidence maps."""
    records = []
    for row in wire.model_dump(mode="json")["records"]:
        facts = row.pop("attributes")
        row["attributes"] = {name: fact["value"] for name, fact in facts.items() if fact is not None}
        row["attribute_evidence"] = {name: fact["evidence"] for name, fact in facts.items() if fact is not None}
        records.append(row)
    return Extraction.model_validate_json(json.dumps(dict(
        records=records, unsupported_reason=wire.unsupported_reason)))
