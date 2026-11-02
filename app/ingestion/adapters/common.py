"""Adapter context, explicit source scopes, and source-path validation."""

from dataclasses import dataclass, field
from datetime import datetime

from app.identity.namespaces import IdentifierScope
from app.ingestion.errors import InputError
from app.ingestion.normalization import text, timestamp
from app.schemas.ingest import IngestWarning, SourceConfig
from app.schemas.normalized import EntityReference, Evidence, Identifier, RelationshipAssertion


def mapping(value, path):
    if not isinstance(value, dict):
        raise InputError("Expected an object", path=path)
    return value


def records(value, path):
    if not isinstance(value, list):
        raise InputError("Expected an array", path=path)
    if len(value) > 1000:
        raise InputError("More than 1000 records", path=path, code="limit_exceeded")
    return [mapping(row, f"{path}/{i}") for i, row in enumerate(value)]


def string_list(value, path):
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError("Expected an array of strings", path=path)
    if len(value) > 5000:
        raise InputError("More than 5000 list entries", path=path, code="limit_exceeded")
    result = []
    for i, item in enumerate(value):
        cleaned = text(item, f"{path}/{i}")
        if cleaned is None:
            raise InputError("References and group names must not be blank", path=f"{path}/{i}")
        result.append(cleaned)
    return result


@dataclass
class Context:
    source: SourceConfig
    observed_at: datetime | None = None
    warnings: list[IngestWarning] = field(default_factory=list)
    warning_count: int = 0
    record_path: str = "/"
    edge_count: int = 0

    def warn(self, code, path, message):
        self.warning_count += 1
        if len(self.warnings) < 50:
            self.warnings.append(IngestWarning(code=code, path=path, message=message))

    def identifier(self, kind, identifier_type, value, path, *, trusted_only=False):
        if trusted_only and identifier_type not in self.source.identifier_authorities:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        value = text(value, path)
        if value is None:
            return None
        if identifier_type in self.source.case_insensitive_identifiers:
            value = value.casefold()
        if identifier_type == "app_name":
            value = " ".join(value.split())
        return Identifier(scope=IdentifierScope(
            organization_id=self.source.organization_id, ci_type=kind,
            authority_id=self.source.identifier_authorities.get(identifier_type, self.source.source_id),
            identifier_type=identifier_type,
        ), value=value)

    def identifiers(self, *values):
        return [value for value in values if value is not None]

    def base(self, row, path):
        return dict(organization_id=self.source.organization_id, record_key=path,
                    raw_record=row, observed_at=self.observed_at, evidence=Evidence(pointer=path))

    def app_edge(self, relation, name, path):
        self.edge_count += 1
        if self.edge_count > 5000:
            raise InputError("More than 5000 relationship assertions", path=path, code="limit_exceeded")
        identifier = self.identifier("application", "app_name", name, path)
        return RelationshipAssertion(relation_type=relation, target=EntityReference(
            ci_type="application", display_name=name, identifiers=[identifier],
            evidence=Evidence(pointer=path),
        ), evidence=Evidence(pointer=path))

    def snapshot(self, document):
        if self.source.snapshot_time_path is None:
            return
        value = document
        for part in self.source.snapshot_time_path:
            if not isinstance(value, dict) or part not in value:
                self.warn("missing_snapshot_time", "/", "Configured snapshot timestamp is absent")
                return
            value = value[part]
        self.observed_at = timestamp(value, "/" + "/".join(self.source.snapshot_time_path))

    def check_count(self, value, actual, path):
        if value is not None and (type(value) is not int or value != actual):
            self.warn("count_mismatch", path, "Declared record count differs from parsed inventory")
