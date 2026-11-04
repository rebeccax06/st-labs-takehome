"""A bounded read-only AST. It contains no SQL, table names, or organization scope."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

from app.domain import validate_endpoints
from app.querying.registry import FIELDS, canonical_uuid, validate_value
from app.schemas.normalized import Kind, Relation

MAX_HOPS = 2
PlanText = Annotated[str, StringConstraints(min_length=1, max_length=1024, pattern=r"\S")]
FieldName = Literal["ci_id", "hostname", "os_family", "status", "encrypted", "name", "email", "team", "mfa_enabled", "app_type", "owner_team"]
Scalar = PlanText | StrictBool


class QueryContract(BaseModel):
    # Tuples below make the validated AST immutable, not just its top-level fields.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Equal(QueryContract):
    field: FieldName
    operator: Literal["eq"]
    value: Scalar

    @field_validator("value")
    @classmethod
    def normalize_uuid(cls, value, info):
        return canonical_uuid(value) if info.data.get("field") == "ci_id" and isinstance(value, str) else value


class InValues(QueryContract):
    field: FieldName
    operator: Literal["in"]
    value: Annotated[tuple[Scalar, ...], Field(min_length=1, max_length=20)]

    @field_validator("value")
    @classmethod
    def normalize_uuids(cls, values, info):
        if info.data.get("field") == "ci_id":
            return tuple(canonical_uuid(v) if isinstance(v, str) else v for v in values)
        return values


class IsNull(QueryContract):
    field: FieldName
    operator: Literal["is_null"]


Predicate = Annotated[Equal | InValues | IsNull, Field(discriminator="operator")]
Predicates = Annotated[tuple[Predicate, ...], Field(max_length=8)]


class Related(QueryContract):
    relation: Relation
    direction: Literal["out", "in"]
    target_kind: Kind
    where: Predicates = ()
    exists: Annotated[tuple["Related", ...], Field(max_length=4)] = ()


def validate_node(kind, where, exists, depth=0):
    for predicate in where:
        if predicate.field not in FIELDS[kind]:
            raise ValueError(f"Field {predicate.field} is not queryable on {kind}")
        if isinstance(predicate, Equal):
            validate_value(kind, predicate.field, predicate.value)
        elif isinstance(predicate, InValues):
            for value in predicate.value:
                validate_value(kind, predicate.field, value)
    if exists and depth >= MAX_HOPS:
        raise ValueError("Query plans support at most two relationship hops")
    for edge in exists:
        source, target = (kind, edge.target_kind) if edge.direction == "out" else (edge.target_kind, kind)
        validate_endpoints(edge.relation, source, target)
        validate_node(edge.target_kind, edge.where, edge.exists, depth + 1)


class QueryPlan(QueryContract):
    version: Literal[1] = 1
    operation: Literal["list", "count"]
    root: Kind
    where: Predicates = ()
    exists: Annotated[tuple[Related, ...], Field(max_length=4)] = ()
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0, le=10000)] = 0

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Plan version must be an integer")
        return value

    @field_validator("exists")
    @classmethod
    def validate_semantics(cls, exists, info):
        kind = info.data.get("root")
        where = info.data.get("where", ())
        if kind is not None:
            validate_node(kind, where, exists)
        return exists

    @field_validator("where")
    @classmethod
    def validate_root_predicates(cls, where, info):
        if info.data.get("root") is not None:
            validate_node(info.data["root"], where, ())
        return where


class AskPlanRequest(QueryContract):
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    offset: Annotated[int, Field(ge=0, le=10000)] = 0

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Question must not be blank")
        return value


class Planned(QueryContract):
    status: Literal["planned"]
    plan: QueryPlan


Suggestion = Literal["Which users don't have MFA?", "Which devices run macOS?", "How many active devices are there?"]


class Unsupported(QueryContract):
    status: Literal["unsupported"]
    reason: Annotated[str, Field(min_length=1, max_length=512)]
    suggested_questions: Annotated[tuple[Suggestion, ...], Field(min_length=1, max_length=3)] = (
        "Which users don't have MFA?", "Which devices run macOS?", "How many active devices are there?",
    )


class ClarificationRequired(QueryContract):
    status: Literal["clarification_required"]
    question: Annotated[str, Field(min_length=1, max_length=512)]


Decision = Annotated[Planned | Unsupported | ClarificationRequired, Field(discriminator="status")]


class PlanningResponse(QueryContract):
    # Object root keeps the structured-output schema portable across model providers.
    result: Decision


class PlanPreview(QueryContract):
    result: Decision
    executed: Literal[False] = False
    planner_version: str
