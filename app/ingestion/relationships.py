"""Resolve relationship targets using the same scoped-key batch resolver."""

from dataclasses import dataclass

from app.identity.resolver import IdentityInput, Resolution
from app.schemas.normalized import NormalizedRecord, RelationshipAssertion


@dataclass
class ResolvedAssertion:
    record_index: int
    assertion_index: int
    record: NormalizedRecord
    assertion: RelationshipAssertion
    source_ci_id: str
    resolution: Resolution


def resolve_targets(resolver, records, roots) -> list[ResolvedAssertion]:
    items = []
    pending = []
    for i, record in enumerate(records):
        root = roots[f"r:{i}"]
        if root.ci is None:
            continue
        for j, assertion in enumerate(record.relationships):
            token = f"e:{i}:{j}"
            target = assertion.target
            items.append(IdentityInput(token, target.ci_type, record.organization_id,
                                       target.identifiers, reference_only=True))
            pending.append((token, i, j, record, assertion, root.ci.id))
    outcomes = resolver.resolve_many(items)
    result = []
    for token, i, j, record, assertion, source_id in pending:
        outcome = outcomes[token]
        # Self edges remain evidence but never enter the constrained current graph.
        if outcome.ci is not None and outcome.ci.id == source_id:
            outcome = Resolution("unresolved", candidate_ids=(source_id,))
        result.append(ResolvedAssertion(i, j, record, assertion, source_id, outcome))
    return result
