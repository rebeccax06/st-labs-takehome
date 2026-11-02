"""SQLite engine setup and the validated ORM unit of work.

Use CMDBSession for application writes. Bulk SQL bypasses the cross-table
checks below and is reserved for schema maintenance and database tests.
"""

from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.models import (
    Application, Base, ConfigurationItem, Device, EntityIdentifier,
    Observation, Relationship, User, new_id, utc_now,
)
from app.domain import validate_endpoints
from app.identity.namespaces import IdentifierScope


class CMDBSession(Session):
    """Session with cross-table model invariants checked before flush."""


def create_sqlite_engine(settings: Settings | None = None) -> Engine:
    settings = settings or Settings.from_env()
    url = make_url(settings.database_url)
    if url.drivername not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Only SQLite is supported by the prototype initializer")
    options = {"connect_args": {"check_same_thread": False}}
    if url.database in (None, "", ":memory:"):
        options["poolclass"] = StaticPool
    engine = create_engine(url, **options)

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record):
        # Explicit transaction control avoids sqlite3's legacy DDL/savepoint behavior.
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={settings.sqlite_busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.get_execution_options().get("sqlite_write") else "BEGIN")

    return engine


def initialize_database(engine: Engine) -> None:
    """Create missing tables; never drop tables or pretend to migrate a schema."""
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[CMDBSession]:
    return sessionmaker(bind=engine, class_=CMDBSession, expire_on_commit=False)


def _related(session: Session, obj, relationship_name: str, fk_name: str):
    """Respect a newly assigned FK instead of a previously loaded relationship."""
    state = inspect(obj)
    fk = getattr(obj, fk_name)
    if state.attrs[fk_name].history.has_changes() and fk is not None:
        for candidate in session.new:
            if isinstance(candidate, ConfigurationItem) and candidate.id == fk:
                return candidate
        return session.get(ConfigurationItem, fk)
    related = getattr(obj, relationship_name)
    if related is not None:
        return related
    return session.get(ConfigurationItem, fk) if fk is not None else None


@event.listens_for(CMDBSession, "before_flush")
def validate_unit_of_work(session: CMDBSession, _context, _instances) -> None:
    changed = list(session.new | session.dirty)
    affected_cis: set[ConfigurationItem] = set()
    expected_types = {"device": Device, "user": User, "application": Application}
    for obj in changed:
        if isinstance(obj, ConfigurationItem):
            if type(obj) is not expected_types.get(obj.kind):
                raise ValueError("A CI must have exactly one matching subtype")
            if obj in session.new and obj.id is None:
                obj.id = new_id()
            if obj not in session.new:
                for field in ("id", "kind", "organization_id"):
                    if inspect(obj).attrs[field].history.has_changes():
                        raise ValueError(f"CI {field} cannot change")
            if obj in session.dirty and session.is_modified(obj, include_collections=False):
                obj.updated_at = utc_now()
            affected_cis.add(obj)

    for obj in list(session.new | session.dirty | session.deleted):
        if isinstance(obj, EntityIdentifier):
            ci = _related(session, obj, "ci", "ci_id")
            if ci is None:
                raise ValueError("Identifier must reference an existing CI")
            affected_cis.add(ci)
            # An identifier move also affects the previous CI's minimum-key invariant.
            if obj not in session.new:
                old_ci_id = session.scalar(
                    select(EntityIdentifier.ci_id).where(EntityIdentifier.id == obj.id)
                )
                old_ci = session.get(ConfigurationItem, old_ci_id) if old_ci_id else None
                if old_ci is not None:
                    affected_cis.add(old_ci)
            if obj not in session.deleted:
                scope = IdentifierScope.from_namespace(obj.namespace)
                if (scope.organization_id, scope.ci_type) != (ci.organization_id, ci.kind):
                    raise ValueError("Identifier namespace must match CI organization and type")
                if not obj.value or not obj.value.strip():
                    raise ValueError("Identifier value must not be blank")
        elif isinstance(obj, Relationship) and obj not in session.deleted:
            source = _related(session, obj, "from_ci", "from_ci_id")
            target = _related(session, obj, "to_ci", "to_ci_id")
            if source is None or target is None:
                raise ValueError("Relationship endpoints must exist")
            if source.organization_id != target.organization_id:
                raise ValueError("Relationship endpoints must share an organization")
            validate_endpoints(obj.relation_type, source.kind, target.kind)
        elif isinstance(obj, Observation) and obj not in session.deleted:
            ci = _related(session, obj, "ci", "ci_id")
            run = obj.run
            if inspect(obj).attrs.run_id.history.has_changes() and obj.run_id is not None:
                from app.db.models import IngestionRun
                run = session.get(IngestionRun, obj.run_id)
            if run is None:
                raise ValueError("Observation must reference an ingestion run")
            if ci is not None and ci.organization_id != run.organization_id:
                raise ValueError("Observation run and CI must share an organization")

    for ci in affected_cis - set(session.deleted):
        # Query persisted keys, then overlay pending changes without triggering a flush.
        stored_ids = set(session.scalars(
            select(EntityIdentifier.id).where(EntityIdentifier.ci_id == ci.id)
        )) if ci not in session.new else set()
        for identifier in session.deleted | session.dirty:
            if isinstance(identifier, EntityIdentifier):
                stored_ids.discard(identifier.id)
        pending = any(
            isinstance(identifier, EntityIdentifier)
            and identifier not in session.deleted
            and _related(session, identifier, "ci", "ci_id") is ci
            for identifier in session.new | session.dirty
        )
        if not stored_ids and not pending:
            raise ValueError("Every CI must have at least one identifier")
