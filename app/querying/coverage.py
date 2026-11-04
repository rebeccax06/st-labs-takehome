"""Bounded evidence checks, not a general completeness scoring engine."""

from sqlalchemy import func, or_, select

from app.db.models import ConfigurationItem, IngestionRun, Observation, Relationship
from app.schemas.ask import QueryWarning
from app.schemas.query_plan import IsNull


def count(session, statement):
    return session.scalar(select(func.count()).select_from(statement.subquery()))


def coverage_warnings(session, compiler, plan):
    warnings = [QueryWarning(code="recorded_evidence_only", message=
        "Results describe recorded inventory, not a complete live directory. Unknown or conflicted values "
        "do not satisfy equality filters; separate identities are never joined by similar names.")]
    checked_edges = set()

    def visit(kind, where, exists, path=()):
        candidates, ci, attrs = compiler.reachable(plan, path)
        label = "root" + "".join(f".exists[{index}]" for index in path)
        for field in sorted({p.field for p in where if not isinstance(p, IsNull)}):
            column = compiler.column(ci, attrs, field)
            missing = count(session, candidates.where(column.is_(None)))
            if missing:
                warnings.append(QueryWarning(code="unknown_field", path=label, field=field, count=missing,
                    message=f"{missing} reachable {kind} CIs have no usable {field} value before this node's "
                    "filters. They cannot satisfy equality/membership on that field; values may be missing or conflicted."))
            conflicts = count(session, candidates.where(ci.c.field_state[field]["status"].as_string() == "conflict"))
            if conflicts:
                warnings.append(QueryWarning(code="conflicting_field", path=label, field=field, count=conflicts,
                    message=f"{conflicts} reachable {kind} CIs have conflicting {field} evidence; "
                    "their projected value is unknown. Inspect CI details for source attribution."))
        references = count(session, candidates.where(ci.c.is_reference.is_(True)))
        if references:
            warnings.append(QueryWarning(code="reference_items", path=label, count=references,
                message=f"{references} reachable {kind} CIs are references with potentially incomplete attributes."))
        for index, edge in enumerate(exists):
            # Edge coverage is organization/type-wide; do not imply complete graph
            # coverage from the presence of even one edge.
            source_kind, target_kind = (kind, edge.target_kind) if edge.direction == "out" else (edge.target_kind, kind)
            key = (edge.relation, source_kind, target_kind)
            if key not in checked_edges:
                checked_edges.add(key)
                source, target = ConfigurationItem.__table__.alias(), ConfigurationItem.__table__.alias()
                recorded = session.scalar(select(func.count()).select_from(Relationship.__table__
                    .join(source, Relationship.from_ci_id == source.c.id)
                    .join(target, Relationship.to_ci_id == target.c.id)).where(
                        source.c.organization_id == compiler.organization_id, target.c.organization_id == compiler.organization_id,
                        source.c.kind == source_kind, target.c.kind == target_kind,
                        Relationship.relation_type == edge.relation))
                if not recorded:
                    warnings.append(QueryWarning(code="no_recorded_relationships", path=label, count=0,
                        message=f"No recorded {source_kind} -> {target_kind} {edge.relation} edges in this organization. "
                        "Missing evidence does not prove that no real-world relationship exists."))
            unresolved = select(Observation.id).join(IngestionRun).outerjoin(
                ConfigurationItem, ConfigurationItem.id == Observation.ci_id).where(
                    IngestionRun.organization_id == compiler.organization_id,
                    Observation.normalized_record["record_kind"].as_string() == "relationship",
                    Observation.normalized_record["relation_type"].as_string() == edge.relation,
                    Observation.resolution_status != "resolved")
            if edge.direction == "out":
                unresolved = unresolved.where(or_(Observation.ci_id.in_(candidates), Observation.ci_id.is_(None)))
                scope = "reachable sources or unlinked records"
            else:
                unresolved = unresolved.where(or_(ConfigurationItem.kind == source_kind, Observation.ci_id.is_(None)))
                scope = "organization-wide source records (unresolved targets cannot be scoped to this query)"
            missing = count(session, unresolved)
            if missing:
                warnings.append(QueryWarning(code="unresolved_relationships", path=label, count=missing,
                    message=f"{missing} retained {edge.relation} observations have unresolved identity in {scope}. "
                    "Counts refer to observations, not distinct real-world edges."))
            if edge.relation == "assigned_to" and edge.direction == "out":
                conflicts = count(session, candidates.where(ci.c.field_state["assigned_to"]["status"].as_string() == "conflict"))
                if conflicts:
                    warnings.append(QueryWarning(code="conflicting_assignments", path=label, count=conflicts,
                        message=f"{conflicts} reachable devices have conflicting assignment evidence; no assignee edge is selected."))
            visit(edge.target_kind, edge.where, edge.exists, path + (index,))
    visit(plan.root, plan.where, plan.exists)
    return warnings
