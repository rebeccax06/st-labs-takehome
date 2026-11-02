"""Canonical namespace representation shared by schemas and persistence."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IdentifierScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    organization_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
    ci_type: Literal["device", "user", "application"]
    authority_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
    identifier_type: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")

    def to_namespace(self) -> str:
        return json.dumps(
            [self.organization_id, self.ci_type, self.authority_id, self.identifier_type],
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def from_namespace(cls, value: str) -> "IdentifierScope":
        try:
            parts = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Identifier namespace must be a canonical JSON tuple") from exc
        if not isinstance(parts, list) or len(parts) != 4:
            raise ValueError("Identifier namespace must contain exactly four components")
        scope = cls(
            organization_id=parts[0], ci_type=parts[1],
            authority_id=parts[2], identifier_type=parts[3],
        )
        if scope.to_namespace() != value:
            raise ValueError("Identifier namespace is not canonical")
        return scope
