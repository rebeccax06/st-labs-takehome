"""Resolve literal references through exact attributes and scoped application keys.

This is query lookup, never identity merging. No fuzzy matching is performed.
"""

from sqlalchemy import func, or_, select

from app.db.models import ConfigurationItem, EntityIdentifier
from app.identity.namespaces import IdentifierScope
from app.identity.resolver import MODELS
from app.schemas.ask import AskClarification, Candidate, QueryWarning, ResolvedReference
from app.schemas.query_plan import Equal, InValues
from app.querying.registry import FIELDS

MAX_REFERENCES = 100
MAX_CANDIDATES = 20


def references(plan):
    found = set()
    def visit(kind, where, exists):
        for predicate in where:
            if isinstance(predicate, (Equal, InValues)) and FIELDS[kind][predicate.field].reference:
                values = (predicate.value,) if isinstance(predicate, Equal) else predicate.value
                found.update((kind, predicate.field, value) for value in values)
        for edge in exists:
            visit(edge.target_kind, edge.where, edge.exists)
    visit(plan.root, plan.where, plan.exists)
    return sorted(found)


class ReferenceResolver:
    def __init__(self, session, organization_id, policy):
        self.session = session
        self.organization_id = organization_id
        self.policy = policy

    def matches(self, kind, field, value):
        model = MODELS[kind]
        column = model.id if field == "ci_id" else getattr(model, field)
        # SQLite lower() provides ASCII case-insensitive exact equality, not fuzzy
        # matching or wildcard matching. Other Unicode characters remain exact.
        terms = [column == value] if field == "ci_id" else [func.lower(column) == value.lower()]
        if kind == "application" and field == "name":
            keys = set()
            for source in self.policy.sources:
                if source.organization_id != self.organization_id:
                    continue
                scope = IdentifierScope(organization_id=self.organization_id, ci_type="application",
                    authority_id=source.identifier_authorities.get("app_name", source.source_id),
                    identifier_type="app_name")
                name = " ".join(value.split())
                if "app_name" in source.case_insensitive_identifiers:
                    name = name.casefold()
                keys.add(self.policy.aliases.canonical_key((scope.to_namespace(), name)))
            if keys:
                terms.append(model.id.in_(select(EntityIdentifier.ci_id).where(or_(*[
                    (EntityIdentifier.namespace == namespace) & (EntityIdentifier.value == name)
                    for namespace, name in sorted(keys)]))))
        return select(model.id).where(model.organization_id == self.organization_id, or_(*terms))

    def resolve(self, keys):
        resolved, warnings = {}, []
        for kind, field, value in keys:
            statement = self.matches(kind, field, value)
            ids = list(self.session.scalars(statement.order_by(MODELS[kind].id).limit(MAX_CANDIDATES + 1)))
            if len(ids) > 1:
                total = self.session.scalar(select(func.count()).select_from(statement.subquery()))
                candidates = self.session.scalars(select(ConfigurationItem).where(
                    ConfigurationItem.organization_id == self.organization_id,
                    ConfigurationItem.id.in_(ids[:MAX_CANDIDATES])).order_by(ConfigurationItem.id))
                return AskClarification(question=f"Which {kind} do you mean by {field} '{value}'? "
                    "Ask again using one candidate's internal CI UUID. Candidates are limited to 20.",
                    candidates=[Candidate(id=ci.id, kind=ci.kind, display_name=ci.display_name) for ci in candidates],
                    candidate_total=total), []
            ci_id = ids[0] if ids else None
            resolved[(kind, field, value)] = ci_id
            if ci_id is None:
                warnings.append(QueryWarning(code="reference_not_found", field=field,
                    message=f"No recorded {kind} matches {field} '{value}'. This is not proof of real-world absence."))
        return resolved, warnings


def reference_views(resolved):
    return [ResolvedReference(kind=kind, field=field, value=value, ci_id=ci_id)
            for (kind, field, value), ci_id in sorted(resolved.items())]
