"""Atomic CMDB ingestion and exact-replay handling over prepared records."""

from collections import defaultdict
import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from app.db.models import IngestionRun, Observation, Relationship, new_id, utc_now
from app.identity.resolver import IdentityInput, IdentityResolver, fingerprint
from app.ingestion.errors import InputError
from app.ingestion.relationships import resolve_targets
from app.ingestion.service import prepare_ingestion, validate_request
from app.reconciliation.policy import Policy
from app.reconciliation.projection import project_attributes
from app.reconciliation.relationships import project_relationships


class DatabaseBusy(RuntimeError):
    pass


class WarningLog:
    def __init__(self, prepared):
        self.count = prepared.warning_count
        self.items = [w.model_dump(mode="json") for w in prepared.warnings]

    def add(self, code, path, message, **details):
        self.count += 1
        if len(self.items) < 50:
            self.items.append(dict(code=code, path=path, message=message, **details))


def _observations(session, ids):
    grouped = defaultdict(list)
    ids = sorted(ids)
    for start in range(0, len(ids), 400):
        rows = session.scalars(select(Observation).options(selectinload(Observation.run)).where(
            Observation.ci_id.in_(ids[start:start + 400])
        ))
        for row in rows:
            grouped[row.ci_id].append(row)
    return grouped


def _edges(session, ids):
    keys = set()
    ids = sorted(ids)
    for start in range(0, len(ids), 400):
        keys.update(session.execute(select(Relationship.from_ci_id, Relationship.relation_type,
                                           Relationship.to_ci_id).where(
            Relationship.from_ci_id.in_(ids[start:start + 400])
        )).all())
    return keys


class IngestService:
    def __init__(self, sessions, policy: Policy, *, adapters=None, extractor=None):
        self.sessions = sessions
        self.policy = policy
        self.adapters = adapters
        self.extractor = extractor

    def _replay_key(self, request):
        preparation_version = self.extractor.version if self.extractor is not None else "deterministic"
        version = hashlib.sha256((self.policy.version + ":" + preparation_version + ":" + request.format).encode()).hexdigest()
        return dict(organization_id=request.organization_id, source_id=request.source_id,
                    payload_sha256=hashlib.sha256(request.content.encode("utf-8")).hexdigest(), policy_version=version)

    def ingest(self, request):
        validate_request(request, self.policy.sources, self.adapters)
        # End the read session before any network call; replay incurs no model cost.
        with self.sessions() as session:
            previous = session.scalar(select(IngestionRun).filter_by(**self._replay_key(request)))
            if previous is not None:
                return previous.result | {"replayed": True}
        prepared = prepare_ingestion(request, self.policy.sources, adapters=self.adapters, extractor=self.extractor)
        return self.persist(request, prepared)

    def persist(self, request, prepared):
        """Persist validated preparation. Future AI extraction can use this boundary too."""
        if (request.organization_id, request.source_id) != (prepared.organization_id, prepared.source_id) or any(
            row.organization_id != request.organization_id for row in prepared.batch.records
        ):
            raise InputError("Prepared batch does not match the requested source/organization")
        replay_key = self._replay_key(request)
        try:
            with self.sessions.begin() as session:
                # Serialize SQLite writers before identity reads, avoiding stale read-to-write upgrades.
                session.connection(execution_options={"sqlite_write": True})
                previous = session.scalar(select(IngestionRun).filter_by(**replay_key))
                if previous is not None:
                    return previous.result | {"replayed": True}
                return self._write(session, request, prepared, replay_key)
        except (IntegrityError, OperationalError) as exc:
            # A racing exact replay may have committed while this transaction lost a race.
            try:
                with self.sessions() as session:
                    previous = session.scalar(select(IngestionRun).filter_by(**replay_key))
                    if previous is not None:
                        return previous.result | {"replayed": True}
            except OperationalError:
                pass
            if isinstance(exc, OperationalError) and any(word in str(exc).lower() for word in ("locked", "busy")):
                raise DatabaseBusy("Database is busy; retry the request") from exc
            raise

    def _write(self, session, request, prepared, replay_key):
        log = WarningLog(prepared)
        now = utc_now()
        run = IngestionRun(id=new_id(), **replay_key, format=request.format,
                           received_at=now, completed_at=now, result={})
        session.add(run)
        resolver = IdentityResolver(session, self.policy.aliases)
        records = prepared.batch.records
        roots = resolver.resolve_many([
            IdentityInput(f"r:{i}", row.ci_type, row.organization_id, row.identifiers)
            for i, row in enumerate(records)
        ])
        quarantined = 0
        for i, record in enumerate(records):
            resolved = roots[f"r:{i}"]
            if resolved.ci is None:
                quarantined += 1
                log.add("identity_conflict" if resolved.status == "identity_conflict" else "missing_identifiers",
                        record.record_key,
                        ("Conflicting identifiers: record retained without changing any CI" if resolved.status == "identity_conflict"
                         else "No trusted stable identifier: provide a source entity ID or configure a trusted identifier authority. Name-only records remain evidence."),
                        candidate_ids=list(resolved.candidate_ids))
            data = record.model_dump(mode="json", exclude={"raw_record"}, exclude_unset=True)
            data.update(record_kind="entity", reference_only=False, candidate_ids=list(resolved.candidate_ids),
                        attributes=record.attributes.model_dump(mode="json", exclude_unset=True, exclude_none=True),
                        source_metadata=prepared.source_metadata)
            session.add(Observation(id=new_id(), run=run, ci=resolved.ci, record_key=record.record_key,
                                    observed_at=record.observed_at, raw_record=record.raw_record,
                                    normalized_record=data, origin=prepared.origin, resolution_status=resolved.status))
        session.flush()
        evidence = _observations(session, resolver.touched)
        for ci in list(resolver.touched.values()):
            project_attributes(session, ci, evidence[ci.id], self.policy)

        # All root CIs exist before targets are resolved, so forward references are safe.
        assertions = resolve_targets(resolver, records, roots)
        session.flush()
        before_edges = _edges(session, resolver.touched)
        unresolved = 0
        for edge in assertions:
            target = edge.resolution.ci
            if target is None:
                unresolved += 1
                log.add("unresolved_reference", edge.record.record_key,
                        "Relationship target has no unambiguous stable identity",
                        candidate_ids=list(edge.resolution.candidate_ids),
                        relation_type=edge.assertion.relation_type)
            token = f"{edge.record.record_key}/_cmdb/relationships/{edge.assertion_index}"
            data = dict(record_kind="relationship", relation_type=edge.assertion.relation_type,
                        target=edge.assertion.target.model_dump(mode="json"),
                        target_ci_id=target.id if target else None,
                        candidate_ids=list(edge.resolution.candidate_ids),
                        evidence=edge.assertion.evidence.model_dump(mode="json") if edge.assertion.evidence else None)
            session.add(Observation(id=new_id(), run=run, ci_id=edge.source_ci_id,
                                    record_key=token, observed_at=edge.record.observed_at,
                                    raw_record={"reference": edge.assertion.target.display_name},
                                    normalized_record=data, origin=prepared.origin, resolution_status=edge.resolution.status))
            if target is not None:
                canonical = [self.policy.aliases.canonical(i) for i in edge.assertion.target.identifiers]
                name = next((i.value for i in canonical if i.scope.identifier_type == "app_name"), None)
                attributes = {"name": name} if target.kind == "application" and name else {}
                session.add(Observation(id=new_id(), run=run, ci=target,
                                        record_key=f"{edge.record.record_key}/_cmdb/references/{edge.assertion_index}",
                                        observed_at=edge.record.observed_at,
                                        raw_record={"reference": edge.assertion.target.display_name},
                                        normalized_record=dict(record_kind="entity", reference_only=True, attributes=attributes,
                                                               identifiers=[i.model_dump(mode="json") for i in canonical]),
                                        origin=prepared.origin, resolution_status="resolved"))
        session.flush()
        evidence = _observations(session, resolver.touched)
        for ci in resolver.touched.values():
            project_attributes(session, ci, evidence[ci.id], self.policy)
            project_relationships(session, ci, evidence[ci.id], self.policy)
            for field, state in ci.field_state.items():
                if state["status"] == "conflict":
                    log.add("attribute_conflict", f"/ci/{ci.id}/{field}",
                            "Competing evidence has no defensible winner; field is unknown",
                            observation_ids=state["observation_ids"])
        session.flush()
        after_edges = _edges(session, resolver.touched)
        updated = sum(fingerprint(ci) != resolver.before[ci_id]
                      for ci_id, ci in resolver.touched.items() if ci_id not in resolver.created)
        result = dict(
            run_id=run.id, replayed=False,
            records=dict(received=len(records), resolved=len(records) - quarantined, quarantined=quarantined),
            entities=dict(created=len(resolver.created), updated=updated,
                          unchanged=len(resolver.touched) - len(resolver.created) - updated),
            relationships=dict(created=len(after_edges - before_edges), removed=len(before_edges - after_edges),
                               unchanged=len(before_edges & after_edges)),
            warning_count=log.count, warnings=log.items, unresolved_reference_count=unresolved,
        )
        run.completed_at = utc_now()
        run.result = result
        session.flush()
        return result
