"""One immutable policy snapshot per service/deployment."""

import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter

from app.identity.aliases import Aliases
from app.schemas.ingest import SourceConfig
from app.schemas.normalized import ApplicationAttributes, DeviceAttributes, UserAttributes

ATTRIBUTES = {"device": DeviceAttributes, "user": UserAttributes, "application": ApplicationAttributes}
PIPELINE_VERSION = "phase4-v1"


def load_sources(path: str | Path) -> list[SourceConfig]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Source configuration must be an array")
    # Accept the existing sample manifest as well as a plain list of source definitions.
    return TypeAdapter(list[SourceConfig]).validate_python([
        entry["source"] if isinstance(entry, dict) and "source" in entry else entry for entry in data
    ])


class Policy:
    def __init__(self, sources: list[SourceConfig]):
        self.sources = [s.model_copy(deep=True) for s in sources]
        if len({(s.organization_id, s.source_id) for s in sources}) != len(sources):
            raise ValueError("Duplicate source configuration")
        self.aliases = Aliases(self.sources)
        self.authorities: dict[tuple[str, str, str], str] = {}
        for source in self.sources:
            for attribute in source.authoritative_attributes:
                kind, separator, field = attribute.partition(".")
                if not separator or kind not in ATTRIBUTES or (
                    field not in ATTRIBUTES[kind].model_fields and (kind, field) != ("device", "assigned_to")
                ) or field == "groups":
                    raise ValueError(f"Unsupported authoritative attribute: {attribute}")
                key = source.organization_id, kind, field
                if key in self.authorities and self.authorities[key] != source.source_id:
                    raise ValueError("Only one source can be authoritative for an attribute")
                self.authorities[key] = source.source_id
        content = [s.model_dump(mode="json") for s in sorted(self.sources, key=lambda s: (s.organization_id, s.source_id))]
        self.version = hashlib.sha256(json.dumps(
            {"pipeline": PIPELINE_VERSION, "sources": content}, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def authority(self, organization: str, kind: str, field: str) -> str | None:
        return self.authorities.get((organization, kind, field))
