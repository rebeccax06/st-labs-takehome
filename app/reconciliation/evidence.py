"""Authority and comparable observation times; arrival order is never a tiebreaker."""

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any


@dataclass(frozen=True)
class Candidate:
    value: Any
    observation_id: str
    source_id: str
    observed_at: datetime | None
    reference_only: bool = False


@dataclass(frozen=True)
class Selection:
    value: Any
    status: str
    observation_ids: tuple[str, ...]


def choose(candidates: list[Candidate], authority: str | None = None) -> Selection | None:
    eligible = [c for c in candidates if c.value is not None and c.value != ""]
    if not eligible:
        return None
    # Inventory facts supersede labels used only to identify reference placeholders.
    inventory = [c for c in eligible if not c.reference_only]
    if inventory:
        eligible = inventory
    authoritative = [c for c in eligible if c.source_id == authority]
    if authoritative:
        eligible = authoritative
    distinct = {json.dumps(c.value, sort_keys=True) for c in eligible}
    if len(distinct) > 1 and all(c.observed_at is not None for c in eligible):
        latest = max(c.observed_at for c in eligible)
        eligible = [c for c in eligible if c.observed_at == latest]
        distinct = {json.dumps(c.value, sort_keys=True) for c in eligible}
    status = "selected" if len(distinct) == 1 else "conflict"
    return Selection(eligible[0].value if status == "selected" else None, status,
                     tuple(sorted(c.observation_id for c in eligible)))
