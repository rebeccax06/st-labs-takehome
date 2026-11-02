from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import aliased

from app.config import Settings
from app.db.models import (
    Application, ConfigurationItem, Device, EntityIdentifier, IngestionRun,
    Observation, Relationship, User, UserGroup,
)
from app.db.session import create_sqlite_engine, initialize_database, session_factory
from app.identity.namespaces import IdentifierScope
from conftest import ci


def run(**overrides):
    values = dict(
        organization_id="org-a", source_id="inventory", format="json",
        payload_sha256="a" * 64, policy_version="v1",
        received_at=datetime.now(UTC), completed_at=datetime.now(UTC), result={},
    )
    return IngestionRun(**(values | overrides))


def test_initialize_is_repeatable_and_preserves_data(engine, db):
    item = ci(hostname="shared-hostname")
    db.add(item)
    db.commit()
    initialize_database(engine)
    assert set(inspect(engine).get_table_names()) == {
        "configuration_items", "devices", "users", "applications", "user_groups",
        "entity_identifiers", "relationships", "ingestion_runs", "observations",
    }
    with session_factory(engine)() as fresh:
        loaded = fresh.get(ConfigurationItem, item.id)
        assert isinstance(loaded, Device)
        assert loaded.hostname == "shared-hostname"


def test_file_database_pragmas_and_persistence(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", sqlite_busy_timeout_ms=1234)
    engine = create_sqlite_engine(settings)
    initialize_database(engine)
    with engine.connect() as first, engine.connect() as second:
        for conn in (first, second):
            assert conn.scalar(text("PRAGMA foreign_keys")) == 1
            assert conn.scalar(text("PRAGMA busy_timeout")) == 1234
            assert conn.scalar(text("PRAGMA journal_mode")) == "wal"
    with session_factory(engine).begin() as db:
        db.add(ci())
    engine.dispose()
    engine = create_sqlite_engine(settings)
    try:
        with session_factory(engine)() as db:
            assert db.scalar(select(func.count()).select_from(Device)) == 1
    finally:
        engine.dispose()


def test_foreign_key_enforced_even_outside_orm(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("INSERT INTO user_groups (user_id,group_name) VALUES (:id,'Engineering')"),
                     {"id": str(uuid4())})


def test_hostnames_and_ips_are_not_unique(db):
    db.add_all([ci(hostname="duplicate", ip_address="10.0.0.1"),
                ci(hostname="duplicate", ip_address="10.0.0.1")])
    db.commit()
    assert len(db.scalars(select(Device)).all()) == 2


def test_unknown_mfa_is_distinct_from_false(db):
    unknown, disabled, enabled = ci("user"), ci("user", mfa_enabled=False), ci("user", mfa_enabled=True)
    db.add_all([unknown, disabled, enabled])
    db.commit()
    assert db.scalars(select(User.id).where(User.mfa_enabled.is_(False))).all() == [disabled.id]
    assert db.scalars(select(User.id).where(User.mfa_enabled.is_(None))).all() == [unknown.id]


def test_identifiers_unique_within_namespace(db):
    db.add_all([ci(value="same"), ci(value="same")])
    with pytest.raises(IntegrityError):
        db.flush()


def test_identical_identifier_values_can_have_different_scopes(db):
    first, second = ci(value="42"), ci(value="42")
    second.identifiers[0].namespace = IdentifierScope(
        organization_id="org-a", ci_type="device", authority_id="other", identifier_type="external_id"
    ).to_namespace()
    third = ci(organization="org-b", value="42")
    fourth = ci("user", value="42")
    db.add_all([first, second, third, fourth])
    db.commit()
    assert db.scalar(select(func.count()).select_from(EntityIdentifier)) == 4


def test_one_ci_supports_multiple_identifiers(db):
    item = ci()
    item.identifiers.append(EntityIdentifier(namespace=IdentifierScope(
        organization_id="org-a", ci_type="device", authority_id="serials", identifier_type="serial"
    ).to_namespace(), value="SN-100"))
    db.add(item)
    db.commit()
    assert len(item.identifiers) == 2
    assert len({key.ci_id for key in item.identifiers}) == 1


@pytest.mark.parametrize("part,value", [("organization_id", "org-b"), ("ci_type", "user")])
def test_identifier_scope_must_match_ci(db, part, value):
    item = ci()
    scope = IdentifierScope.from_namespace(item.identifiers[0].namespace)
    item.identifiers[0].namespace = scope.model_copy(update={part: value}).to_namespace()
    db.add(item)
    with pytest.raises(ValueError, match="namespace must match"):
        db.flush()


@pytest.mark.parametrize("model,kind", [(Device, "user"), (User, "device"), (ConfigurationItem, "device")])
def test_subtype_kind_agreement(db, model, kind):
    db.add(model(organization_id="org-a", display_name="Invalid", kind=kind))
    with pytest.raises(ValueError, match="matching subtype"):
        db.flush()


def test_ci_requires_identifier_and_last_identifier_cannot_be_removed(db):
    item = ci()
    db.add(item)
    db.commit()
    db.delete(item.identifiers[0])
    with pytest.raises(ValueError, match="at least one identifier"):
        db.flush()
    db.rollback()
    db.add(Device(organization_id="org-a", display_name="Keyless"))
    with pytest.raises(ValueError, match="at least one identifier"):
        db.flush()


@pytest.mark.parametrize("field,value", [("kind", "user"), ("organization_id", "org-b"), ("id", "changed")])
def test_ci_identity_cannot_be_changed(db, field, value):
    item = ci()
    db.add(item)
    db.commit()
    setattr(item, field, value)
    with pytest.raises(ValueError):
        db.flush()


@pytest.mark.parametrize("relation,source_kind,target_kind", [
    ("assigned_to", "device", "user"), ("uses", "user", "application"),
    ("integrates_with", "application", "application"), ("owned_by", "application", "user"),
    ("depends_on", "device", "device"), ("depends_on", "device", "application"),
    ("depends_on", "application", "device"), ("depends_on", "application", "application"),
])
def test_allowed_edge_types(db, relation, source_kind, target_kind):
    source, target = ci(source_kind), ci(target_kind)
    edge = Relationship(from_ci=source, to_ci=target, relation_type=relation)
    db.add(edge)
    db.commit()
    assert edge.from_ci_id == source.id
    assert edge.to_ci_id == target.id


@pytest.mark.parametrize("relation,source,target", [
    ("assigned_to", "user", "device"), ("uses", "device", "application"),
    ("owned_by", "application", "device"), ("depends_on", "user", "application"),
    ("made_up", "device", "user"),
])
def test_invalid_edge_types(db, relation, source, target):
    db.add(Relationship(from_ci=ci(source), to_ci=ci(target), relation_type=relation))
    with pytest.raises(ValueError):
        db.flush()


def test_cross_organization_relationship_rejected(db):
    db.add(Relationship(from_ci=ci(), to_ci=ci("user", organization="org-b"), relation_type="assigned_to"))
    with pytest.raises(ValueError, match="share an organization"):
        db.flush()


def test_changing_loaded_edge_fk_is_validated(db):
    source, target, invalid = ci(), ci("user"), ci("application")
    edge = Relationship(from_ci=source, to_ci=target, relation_type="assigned_to")
    db.add_all([edge, invalid])
    db.commit()
    assert edge.to_ci is target
    edge.to_ci_id = invalid.id
    with pytest.raises(ValueError, match="Invalid endpoints"):
        db.flush()


def test_duplicate_edge_is_rejected(db):
    source, target = ci("user"), ci("application")
    db.add_all([Relationship(from_ci=source, to_ci=target, relation_type="uses"),
                Relationship(from_ci=source, to_ci=target, relation_type="uses")])
    with pytest.raises(IntegrityError):
        db.flush()


def test_only_one_current_assignee_but_multiple_access_edges(db):
    device, first, second, app = ci(), ci("user"), ci("user"), ci("application")
    db.add_all([
        Relationship(from_ci=device, to_ci=first, relation_type="assigned_to"),
        Relationship(from_ci=first, to_ci=app, relation_type="uses"),
        Relationship(from_ci=second, to_ci=app, relation_type="uses"),
    ])
    db.commit()
    db.add(Relationship(from_ci=device, to_ci=second, relation_type="assigned_to"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_self_edge_is_rejected_by_database(db):
    item = ci("application")
    db.add(Relationship(from_ci=item, to_ci=item, relation_type="depends_on"))
    with pytest.raises(IntegrityError):
        db.flush()


@pytest.mark.parametrize("kind,values", [
    ("device", {"status": "deactivated"}), ("user", {"status": "ACTIVE"}),
    ("application", {"app_type": "on-premise"}),
    ("application", {"reported_user_count": -1}),
])
def test_invalid_domain_values_rejected_by_database(db, kind, values):
    db.add(ci(kind, **values))
    with pytest.raises(IntegrityError):
        db.flush()


def test_group_membership_is_unique(db):
    user = ci("user")
    db.add_all([UserGroup(user=user, group_name="Engineering"),
                UserGroup(user=user, group_name="Engineering")])
    with pytest.raises(IntegrityError):
        db.flush()


def test_replay_key_unique_and_policy_version_allows_reprocessing(db):
    db.add_all([run(), run(policy_version="v2")])
    db.commit()
    db.add(run())
    with pytest.raises(IntegrityError):
        db.flush()


def test_quarantined_evidence_does_not_require_ci(db):
    db.add(Observation(run=run(), record_key="/0", ci_id=None, raw_record={"name": "Unknown"},
                       normalized_record={"candidate_ids": []}, origin="adapter",
                       resolution_status="identity_conflict"))
    db.commit()
    assert db.scalar(select(func.count()).select_from(ConfigurationItem)) == 0
    assert db.scalar(select(Observation)).ci_id is None


def test_observation_links_and_json_round_trip(db):
    item = ci()
    observation = Observation(run=run(), ci=item, record_key="/0", raw_record={"original": False},
                              normalized_record={"mfa_enabled": None}, origin="ai",
                              resolution_status="resolved")
    db.add(observation)
    db.commit()
    db.expire_all()
    assert observation.ci.id == item.id
    assert observation.raw_record == {"original": False}
    assert observation.normalized_record == {"mfa_enabled": None}


def test_observation_paths_unique_within_run(db):
    ingest = run()
    for _ in range(2):
        db.add(Observation(run=ingest, record_key="/0", raw_record={}, normalized_record={},
                           origin="adapter", resolution_status="unresolved"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_observation_cannot_link_different_organization(db):
    db.add(Observation(run=run(), ci=ci(organization="org-b"), record_key="/0",
                       raw_record={}, normalized_record={}, origin="adapter", resolution_status="resolved"))
    with pytest.raises(ValueError, match="share an organization"):
        db.flush()


def test_failure_rolls_back_entire_unit_of_work(engine):
    with pytest.raises(IntegrityError):
        with session_factory(engine).begin() as db:
            db.add(ci(value="duplicate"))
            db.flush()
            db.add(ci(value="duplicate"))
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(ConfigurationItem)) == 0
        assert db.scalar(select(func.count()).select_from(EntityIdentifier)) == 0


def test_utc_timestamp_round_trip_and_naive_rejection(db):
    offset_time = datetime(2025, 1, 1, 10, tzinfo=timezone(timedelta(hours=2)))
    item = ci(last_checkin=offset_time)
    db.add(item)
    db.commit()
    db.expire(item)
    assert item.last_checkin == offset_time.astimezone(UTC)
    assert item.last_checkin.tzinfo is UTC
    assert item.created_at.tzinfo is UTC
    item.last_checkin = datetime(2025, 1, 1)
    with pytest.raises(StatementError, match="timezone-aware"):
        db.flush()


def test_relationships_can_be_queried_in_both_directions(db):
    device, user = ci(), ci("user", mfa_enabled=False)
    db.add(Relationship(from_ci=device, to_ci=user, relation_type="assigned_to"))
    db.commit()
    assignee = aliased(User)
    query = select(Device).join(Relationship, Relationship.from_ci_id == Device.id).join(
        assignee, Relationship.to_ci_id == assignee.id
    ).where(Relationship.relation_type == "assigned_to", assignee.mfa_enabled.is_(False))
    assert db.scalars(query).all() == [device]
    assert db.scalars(select(Relationship.from_ci_id).where(
        Relationship.to_ci_id == user.id
    )).all() == [device.id]
