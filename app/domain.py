"""Shared vocabulary; no source-specific mapping or resolution policy."""

from enum import StrEnum


class CIKind(StrEnum):
    DEVICE = "device"
    USER = "user"
    APPLICATION = "application"


class RelationType(StrEnum):
    ASSIGNED_TO = "assigned_to"
    USES = "uses"
    DEPENDS_ON = "depends_on"
    INTEGRATES_WITH = "integrates_with"
    OWNED_BY = "owned_by"


RELATION_ENDPOINTS: dict[RelationType, frozenset[tuple[CIKind, CIKind]]] = {
    RelationType.ASSIGNED_TO: frozenset({(CIKind.DEVICE, CIKind.USER)}),
    RelationType.USES: frozenset({(CIKind.USER, CIKind.APPLICATION)}),
    RelationType.DEPENDS_ON: frozenset(
        (source, target)
        for source in (CIKind.DEVICE, CIKind.APPLICATION)
        for target in (CIKind.DEVICE, CIKind.APPLICATION)
    ),
    RelationType.INTEGRATES_WITH: frozenset({(CIKind.APPLICATION, CIKind.APPLICATION)}),
    RelationType.OWNED_BY: frozenset({(CIKind.APPLICATION, CIKind.USER)}),
}


def validate_endpoints(relation: str, source: str, target: str) -> None:
    """Validate direction as well as endpoint types."""
    try:
        pair = (CIKind(source), CIKind(target))
        allowed = RELATION_ENDPOINTS[RelationType(relation)]
    except ValueError as exc:
        raise ValueError("Unknown CI or relationship type") from exc
    if pair not in allowed:
        raise ValueError(f"Invalid endpoints for {relation}: {source} -> {target}")
