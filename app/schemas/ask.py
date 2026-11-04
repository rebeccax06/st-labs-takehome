"""Grounded answers and database-backed clarification contracts."""

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.api import CIView
from app.schemas.normalized import Contract, Kind
from app.schemas.query_plan import QueryPlan, Unsupported


class ResolvedReference(Contract):
    kind: Kind
    field: str
    value: str
    ci_id: str | None


class QueryWarning(Contract):
    code: str
    message: str
    path: str | None = None
    field: str | None = None
    count: int | None = None


class Candidate(Contract):
    id: str
    kind: Kind
    display_name: str


class AskClarification(Contract):
    status: Literal["clarification_required"] = "clarification_required"
    question: str
    candidates: list[Candidate] = Field(default_factory=list)
    candidate_total: int = 0


class Answered(Contract):
    status: Literal["answered"] = "answered"
    answer: str
    plan: QueryPlan
    resolved_references: list[ResolvedReference]
    data: list[CIView] | None
    total: int
    limit: int
    offset: int
    warnings: list[QueryWarning]


AskResponse = Annotated[Answered | Unsupported | AskClarification, Field(discriminator="status")]
