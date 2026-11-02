"""Read-only CI projections and bounded evidence/edge pagination."""

from sqlalchemy import func, literal, select, union_all

from app.db.models import ConfigurationItem, IngestionRun, Observation, Relationship
from app.identity.resolver import MODELS
from app.reconciliation.policy import ATTRIBUTES


class MissingCI(LookupError):
    pass


class InventoryService:
    def __init__(self, session, organization_id):
        self.session = session
        self.organization_id = organization_id

    def view(self, ci):
        values = {field: getattr(ci, field) for field in ATTRIBUTES[ci.kind].model_fields if field != "groups"}
        if ci.kind == "user":
            values["groups"] = sorted(group.group_name for group in ci.groups)
        output = dict(id=ci.id, kind=ci.kind, display_name=ci.display_name,
                      is_reference=ci.is_reference, created_at=ci.created_at, updated_at=ci.updated_at,
                      attributes=ATTRIBUTES[ci.kind](**values).model_dump(mode="json"),
                      has_conflicts=any(s["status"] == "conflict" for s in ci.field_state.values()))
        if ci.kind == "device":
            output["assigned_user_id"] = self.session.scalar(select(Relationship.to_ci_id).where(
                Relationship.from_ci_id == ci.id, Relationship.relation_type == "assigned_to"))
        elif ci.kind == "application":
            output["observed_assignment_count"] = self.session.scalar(select(func.count()).select_from(Relationship).where(
                Relationship.to_ci_id == ci.id, Relationship.relation_type == "uses"))
            output["owner_user_ids"] = list(self.session.scalars(select(Relationship.to_ci_id).where(
                Relationship.from_ci_id == ci.id, Relationship.relation_type == "owned_by").order_by(Relationship.to_ci_id)))
        return output

    def list(self, kind, filters, limit, offset):
        model = MODELS[kind]
        allowed = {"device": {"status", "os_family"}, "user": {"status", "mfa_enabled"}, "application": {"app_type"}}
        if not set(filters).issubset(allowed[kind]):
            raise ValueError("Unsupported inventory filter")
        predicates = [model.organization_id == self.organization_id]
        predicates.extend(getattr(model, field) == value for field, value in filters.items() if value is not None)
        total = self.session.scalar(select(func.count()).select_from(model).where(*predicates))
        items = self.session.scalars(select(model).where(*predicates).order_by(model.id).limit(limit).offset(offset)).all()
        return dict(data=[self.view(ci) for ci in items], total=total, limit=limit, offset=offset)

    def detail(self, ci_id, edge_limit, edge_offset):
        ci = self.session.get(ConfigurationItem, ci_id)
        if ci is None or ci.organization_id != self.organization_id:
            raise MissingCI("Configuration item not found")
        output = self.view(ci)
        graph = union_all(
            select(Relationship.id, literal("out").label("direction"), Relationship.relation_type,
                   Relationship.to_ci_id.label("other_ci_id")).where(Relationship.from_ci_id == ci_id),
            select(Relationship.id, literal("in").label("direction"), Relationship.relation_type,
                   Relationship.from_ci_id.label("other_ci_id")).where(Relationship.to_ci_id == ci_id),
        ).subquery()
        edges = self.session.execute(select(graph).order_by(graph.c.direction, graph.c.relation_type, graph.c.id)
                                     .limit(edge_limit).offset(edge_offset)).mappings().all()
        # Include bounded supporting evidence even when the winner is older than
        # the recent-observation summary. Stored field state retains every ID.
        states = {}
        evidence_ids = set()
        for field, state in ci.field_state.items():
            ids = state["observation_ids"][:20]
            states[field] = dict(status=state["status"], observation_ids=ids,
                                 observation_count=len(state["observation_ids"]))
            evidence_ids.update(ids)
        evidence = {}
        if evidence_ids:
            for obs, source_id in self.session.execute(select(Observation, IngestionRun.source_id)
                    .join(IngestionRun).where(Observation.id.in_(sorted(evidence_ids)))):
                evidence[obs.id] = (obs, source_id)
        for field, state in states.items():
            state["evidence"] = []
            for obs_id in state["observation_ids"]:
                obs, source_id = evidence[obs_id]
                value = (obs.normalized_record.get("target_ci_id") if field == "assigned_to"
                         else obs.normalized_record.get("attributes", {}).get(field))
                state["evidence"].append(dict(observation_id=obs_id, source_id=source_id,
                                             run_id=obs.run_id, value=value,
                                             extraction_evidence=obs.normalized_record.get("attribute_evidence", {}).get(field),
                                             observed_at=obs.observed_at.isoformat() if obs.observed_at else None))
        output.update(identifiers=[dict(id=i.id, namespace=i.namespace, value=i.value)
                                   for i in sorted(ci.identifiers, key=lambda i: (i.namespace, i.value))],
                      field_state=states,
                      relationships=dict(data=[dict(row) for row in edges],
                                         total=self.session.scalar(select(func.count()).select_from(graph)),
                                         limit=edge_limit, offset=edge_offset))
        rows = self.session.execute(select(Observation, IngestionRun.source_id)
                                    .join(IngestionRun).where(Observation.ci_id == ci_id)
                                    .order_by(IngestionRun.completed_at.desc(), Observation.record_key, Observation.id)
                                    .limit(20)).all()
        output["provenance"] = dict(total=self.session.scalar(select(func.count()).select_from(Observation).where(
            Observation.ci_id == ci_id)), data=[dict(
                observation_id=obs.id, source_id=source, run_id=obs.run_id, record_key=obs.record_key,
                observed_at=obs.observed_at, resolution_status=obs.resolution_status,
                origin=obs.origin, attributes=obs.normalized_record.get("attributes", {}),
                relation_type=obs.normalized_record.get("relation_type"),
                target_ci_id=obs.normalized_record.get("target_ci_id"),
            ) for obs, source in rows])
        unresolved = self.session.scalar(select(func.count()).select_from(Observation).where(
            Observation.ci_id == ci_id, Observation.resolution_status != "resolved",
            Observation.normalized_record["record_kind"].as_string() == "relationship"))
        output["warnings"] = ([dict(code="unresolved_references", count=unresolved)] if unresolved else [])
        return output
