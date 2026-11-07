"""Question -> validated read-only plan. This module never opens a DB session."""

import json
import re
from uuid import UUID

from pydantic import ValidationError

from app.ai.client import AIError
from app.ai.json_output import decode_object
from app.domain import RELATION_ENDPOINTS
from app.identity.aliases import Aliases
from app.identity.namespaces import IdentifierScope
from app.querying.prompts import SYSTEM
from app.querying.registry import FIELDS, catalog
from app.schemas.query_plan import AskPlanRequest, Equal, InValues, Planned, PlanningResponse

PLANNER_VERSION = "query-plan-v4"
UUID_PATTERN = r"(?<![\w-])[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?![\w-])"


def literal_in_question(value, question):
    text = " ".join(question.casefold().split())
    value = " ".join(value.casefold().split())
    return re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text) is not None


def validate_references(plan, question):
    supplied_ids = {str(UUID(value)) for value in re.findall(UUID_PATTERN, question)}
    def visit(kind, where, exists):
        for predicate in where:
            if not isinstance(predicate, (Equal, InValues)):
                continue
            values = (predicate.value,) if isinstance(predicate, Equal) else predicate.value
            spec = FIELDS[kind][predicate.field]
            if spec.reference:
                for value in values:
                    if predicate.field == "ci_id":
                        if value not in supplied_ids:
                            raise ValueError("Internal CI UUIDs must appear literally in the question")
                    elif not literal_in_question(value, question):
                        raise ValueError("Names, hostnames, and emails must come from the question; preserve aliases literally")
        for edge in exists:
            visit(edge.target_kind, edge.where, edge.exists)
    visit(plan.root, plan.where, plan.exists)


class QueryPlanner:
    def __init__(self, client, *, aliases=None):
        self.client = client
        self.aliases = aliases or Aliases()

    def _context(self):
        hints = []
        for (namespace, label), _ in sorted(self.aliases.mapping.items())[:100]:
            scope = IdentifierScope.from_namespace(namespace)
            hints.append(dict(authority=scope.authority_id, alias=label,
                              canonical=self.aliases.canonical_key((namespace, label))[1]))
        edges = [dict(relation=str(relation), source=str(source), target=str(target))
                 for relation, pairs in RELATION_ENDPOINTS.items() for source, target in sorted(pairs)]
        return dict(fields=catalog(), directed_edges=edges, application_alias_hints=hints)

    def plan(self, request: AskPlanRequest) -> PlanningResponse:
        messages = [dict(role="system", content=SYSTEM + "\nVocabulary:\n" + json.dumps(self._context())),
                    dict(role="user", content=json.dumps(dict(question=request.question,
                                                             pagination=dict(limit=request.limit, offset=request.offset))))]
        for attempt in range(2):
            raw = self.client.complete(messages=messages, schema=PlanningResponse.model_json_schema(),
                                       schema_name="cmdb_query_plan")
            try:
                value = decode_object(raw)
                response = PlanningResponse.model_validate_json(json.dumps(value))
                if isinstance(response.result, Planned):
                    validate_references(response.result.plan, request.question)
                    # Only these two trusted, request-validated values can override a plan.
                    plan = response.result.plan.model_copy(update={"limit": request.limit, "offset": request.offset})
                    response = PlanningResponse(result=Planned(status="planned", plan=plan))
                return response
            except (ValidationError, ValueError, RecursionError) as exc:
                if attempt:
                    raise AIError("Query planning failed schema or reference validation; no query was executed.") from None
                if isinstance(exc, ValidationError):
                    first = exc.errors(include_input=False, include_context=False)[0]
                    hint = "/".join(str(p) for p in first["loc"])[:160] + ": " + first["type"]
                elif isinstance(exc, RecursionError):
                    hint = "Plan nesting is too deep"
                else:
                    hint = str(exc)[:240]
                messages.extend([dict(role="assistant", content=raw[:16384]), dict(
                    role="user", content="Return a corrected complete planning response. Validation failure: " + hint +
                    ". Use only allowed fields, typed values and directions. Do not drop essential conditions; "
                    "return unsupported or ask clarification if needed.")])
        raise AssertionError("Unreachable planning retry state")
