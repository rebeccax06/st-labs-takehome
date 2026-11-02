"""Ingestion envelope and trusted source configuration (not an API route)."""

from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from app.identity.namespaces import IdentifierScope
from app.schemas.normalized import Contract, NormalizedBatch, Text


class IngestRequest(Contract):
    organization_id: Text
    source_id: Text
    format: Literal["json", "csv", "yaml", "text"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Content must not be blank")
        return value


class SourceConfig(Contract):
    organization_id: Text
    source_id: Text
    adapter: Text
    # Unconfigured external IDs are source-local. Serial/email keys are opt-in.
    identifier_authorities: dict[str, str] = Field(default_factory=dict)
    case_insensitive_identifiers: list[str] = Field(default_factory=lambda: ["app_name"])
    application_aliases: dict[Text, Text] = Field(default_factory=dict)
    authoritative_attributes: list[Text] = Field(default_factory=list)
    snapshot_time_path: list[Text] | None = Field(default=None, min_length=1, max_length=16)

    @model_validator(mode="after")
    def valid_scopes_and_snapshot(self) -> Self:
        IdentifierScope(organization_id=self.organization_id, ci_type="device",
                        authority_id=self.source_id, identifier_type="external_id")
        for key, authority in self.identifier_authorities.items():
            IdentifierScope(organization_id=self.organization_id, ci_type="device",
                            authority_id=authority, identifier_type=key)
        if self.snapshot_time_path and any(
            part.casefold() in {"last_login", "last_checkin"} for part in self.snapshot_time_path
        ):
            raise ValueError("Activity timestamps cannot be configured as snapshot times")
        return self


class IngestWarning(Contract):
    code: str
    path: str
    message: str


class PreparedIngestion(Contract):
    organization_id: str
    source_id: str
    origin: Literal["adapter", "ai"] = "adapter"
    batch: NormalizedBatch
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    warning_count: int
    warnings: list[IngestWarning]
