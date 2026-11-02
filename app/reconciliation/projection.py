"""Rebuild an affected CI's scalar projection from retained observations."""

import json

from sqlalchemy import select

from app.db.models import Observation, User, UserGroup
from app.reconciliation.evidence import Candidate, choose
from app.reconciliation.policy import ATTRIBUTES, Policy


def project_attributes(session, ci, observations: list[Observation], policy: Policy):
    state = dict(ci.field_state or {})
    entity_observations = [o for o in observations if o.normalized_record.get("record_kind") == "entity"]
    for field in ATTRIBUTES[ci.kind].model_fields:
        if field == "groups":
            continue
        candidates = [Candidate(o.normalized_record.get("attributes", {}).get(field), o.id,
                                o.run.source_id, o.observed_at,
                                o.normalized_record.get("reference_only", False)) for o in entity_observations]
        selected = choose(candidates, policy.authority(ci.organization_id, ci.kind, field))
        if selected is not None:
            # Evidence is JSON; restore typed datetime values at the persistence boundary.
            validated = ATTRIBUTES[ci.kind].model_validate_json(json.dumps({field: selected.value}))
            setattr(ci, field, getattr(validated, field))
            state[field] = {"status": selected.status, "observation_ids": list(selected.observation_ids)}
    if isinstance(ci, User):
        groups = {group for o in entity_observations
                  for group in o.normalized_record.get("attributes", {}).get("groups", [])}
        current = set(session.scalars(select(UserGroup.group_name).where(UserGroup.user_id == ci.id)))
        for group in sorted(groups - current):
            session.add(UserGroup(user_id=ci.id, group_name=group))
    ci.is_reference = not any(not o.normalized_record.get("reference_only", False) for o in entity_observations)
    primary = {"device": "hostname", "user": "name", "application": "name"}[ci.kind]
    ci.display_name = getattr(ci, primary) or f"{ci.kind}:{ci.id}"
    ci.field_state = state
