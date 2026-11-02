"""Response contracts for the deterministic CMDB API."""

from typing import Generic, Literal, TypeVar

from pydantic import AwareDatetime, Field, JsonValue

from app.schemas.normalized import Contract, Kind


class RecordCounts(Contract):
    received: int
    resolved: int
    quarantined: int


class EntityCounts(Contract):
    created: int
    updated: int
    unchanged: int


class RelationshipCounts(Contract):
    created: int
    removed: int
    unchanged: int


class IngestResponse(Contract):
    run_id: str
    replayed: bool
    records: RecordCounts
    entities: EntityCounts
    relationships: RelationshipCounts
    warning_count: int
    warnings: list[dict[str, JsonValue]]
    unresolved_reference_count: int


class CIView(Contract):
    id: str
    kind: Kind
    display_name: str
    is_reference: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime
    attributes: dict[str, JsonValue]
    has_conflicts: bool
    assigned_user_id: str | None = None
    observed_assignment_count: int | None = None
    owner_user_ids: list[str] = Field(default_factory=list)


T = TypeVar("T")


class Page(Contract, Generic[T]):
    data: list[T]
    total: int
    limit: int
    offset: int


class EdgeView(Contract):
    id: str
    direction: Literal["in", "out"]
    relation_type: str
    other_ci_id: str


class ObservationSummary(Contract):
    observation_id: str
    source_id: str
    run_id: str
    record_key: str
    observed_at: AwareDatetime | None
    resolution_status: str
    origin: str
    attributes: dict[str, JsonValue]
    relation_type: str | None
    target_ci_id: str | None


class ProvenanceSummary(Contract):
    total: int
    data: list[ObservationSummary]


class CIDetail(CIView):
    identifiers: list[dict[str, str]]
    field_state: dict[str, JsonValue]
    relationships: Page[EdgeView]
    provenance: ProvenanceSummary
    warnings: list[dict[str, JsonValue]]
