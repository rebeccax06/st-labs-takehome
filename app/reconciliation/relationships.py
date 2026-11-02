"""Project current typed edges; assignment is scalar, other assertions additive."""

from sqlalchemy import select

from app.db.models import Relationship
from app.reconciliation.evidence import Candidate, choose


def project_relationships(session, ci, observations, policy):
    current = list(session.scalars(select(Relationship).where(Relationship.from_ci_id == ci.id)))
    assertions = [o for o in observations
                  if o.normalized_record.get("record_kind") == "relationship"
                  and o.resolution_status == "resolved"
                  and o.normalized_record.get("target_ci_id") is not None]
    assignment = [Candidate(o.normalized_record["target_ci_id"], o.id, o.run.source_id, o.observed_at)
                  for o in assertions if o.normalized_record["relation_type"] == "assigned_to"]
    selected = choose(assignment, policy.authority(ci.organization_id, ci.kind, "assigned_to"))
    if selected is not None:
        state = dict(ci.field_state or {})
        state["assigned_to"] = {"status": selected.status, "observation_ids": list(selected.observation_ids)}
        ci.field_state = state
        removed = [edge for edge in current if edge.relation_type == "assigned_to" and edge.to_ci_id != selected.value]
        for edge in removed:
            session.delete(edge)
        if removed:
            # Release the partial unique assignment key before inserting its replacement.
            session.flush()
        current = [edge for edge in current if edge not in removed]
    wanted = {(o.normalized_record["relation_type"], o.normalized_record["target_ci_id"])
              for o in assertions if o.normalized_record["relation_type"] != "assigned_to"}
    if selected is not None and selected.value is not None:
        wanted.add(("assigned_to", selected.value))
    existing = {(edge.relation_type, edge.to_ci_id) for edge in current}
    for relation, target in sorted(wanted - existing):
        session.add(Relationship(from_ci_id=ci.id, to_ci_id=target, relation_type=relation))
