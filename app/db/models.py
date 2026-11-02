"""Relational model. Cross-table invariants are checked by CMDBSession."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite drops timezone information; restore UTC on reads explicitly."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timestamps must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class ConfigurationItem(Base):
    __tablename__ = "configuration_items"
    __table_args__ = (
        CheckConstraint("kind IN ('device','user','application')", name="ck_ci_kind"),
        Index("ix_ci_organization_kind", "organization_id", "kind", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(Text)
    is_reference: Mapped[bool] = mapped_column(Boolean(create_constraint=True), default=False)
    field_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)

    identifiers: Mapped[list["EntityIdentifier"]] = relationship(back_populates="ci")
    __mapper_args__ = {"polymorphic_on": kind, "polymorphic_identity": "ci"}


class Device(ConfigurationItem):
    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive','unknown')", name="ck_device_status"),
        Index("ix_device_hostname", "hostname"),
        Index("ix_device_os", "os_family", "ci_id"),
        Index("ix_device_status", "status", "ci_id"),
    )

    ci_id: Mapped[str] = mapped_column(ForeignKey("configuration_items.id"), primary_key=True)
    hostname: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(Text)
    mac_address: Mapped[str | None] = mapped_column(Text)
    serial_number: Mapped[str | None] = mapped_column(Text)
    os_family: Mapped[str | None] = mapped_column(Text)
    os_version: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(20))
    encrypted: Mapped[bool | None] = mapped_column(Boolean(create_constraint=True))
    device_type: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    last_checkin: Mapped[datetime | None] = mapped_column(UTCDateTime())
    __mapper_args__ = {"polymorphic_identity": "device"}


class User(ConfigurationItem):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active','inactive','suspended','unknown')", name="ck_user_status"),
        Index("ix_user_mfa", "mfa_enabled", "ci_id"),
        Index("ix_user_email", "email"),
    )

    ci_id: Mapped[str] = mapped_column(ForeignKey("configuration_items.id"), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    team: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(20))
    mfa_enabled: Mapped[bool | None] = mapped_column(Boolean(create_constraint=True))
    last_login: Mapped[datetime | None] = mapped_column(UTCDateTime())
    groups: Mapped[list["UserGroup"]] = relationship(back_populates="user")
    __mapper_args__ = {"polymorphic_identity": "user"}


class Application(ConfigurationItem):
    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint("app_type IN ('saas','on_prem','unknown')", name="ck_app_type"),
        CheckConstraint("reported_user_count >= 0", name="ck_app_user_count"),
        Index("ix_application_name", "name"),
    )

    ci_id: Mapped[str] = mapped_column(ForeignKey("configuration_items.id"), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    app_type: Mapped[str | None] = mapped_column(String(20))
    vendor: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    deployment: Mapped[str | None] = mapped_column(Text)
    owner_team: Mapped[str | None] = mapped_column(Text)
    reported_user_count: Mapped[int | None] = mapped_column(Integer)
    sso_enabled: Mapped[bool | None] = mapped_column(Boolean(create_constraint=True))
    __mapper_args__ = {"polymorphic_identity": "application"}


class UserGroup(Base):
    __tablename__ = "user_groups"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.ci_id"), primary_key=True)
    group_name: Mapped[str] = mapped_column(Text, primary_key=True)
    user: Mapped[User] = relationship(back_populates="groups")


class EntityIdentifier(Base):
    __tablename__ = "entity_identifiers"
    __table_args__ = (
        UniqueConstraint("namespace", "value", name="uq_identifier_namespace_value"),
        CheckConstraint("length(value) > 0", name="ck_identifier_value"),
        Index("ix_identifier_ci", "ci_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ci_id: Mapped[str] = mapped_column(ForeignKey("configuration_items.id"))
    namespace: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    ci: Mapped[ConfigurationItem] = relationship(back_populates="identifiers")


class Relationship(Base):
    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint("from_ci_id", "to_ci_id", "relation_type", name="uq_relationship_edge"),
        CheckConstraint("from_ci_id <> to_ci_id", name="ck_relationship_not_self"),
        CheckConstraint(
            "relation_type IN ('assigned_to','uses','depends_on','integrates_with','owned_by')",
            name="ck_relationship_type",
        ),
        Index("ix_relationship_out", "from_ci_id", "relation_type", "to_ci_id"),
        Index("ix_relationship_in", "to_ci_id", "relation_type", "from_ci_id"),
        Index("uq_device_assignment", "from_ci_id", unique=True,
              sqlite_where=text("relation_type = 'assigned_to'"),
              postgresql_where=text("relation_type = 'assigned_to'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_ci_id: Mapped[str] = mapped_column(ForeignKey("configuration_items.id"))
    to_ci_id: Mapped[str] = mapped_column(ForeignKey("configuration_items.id"))
    relation_type: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    from_ci: Mapped[ConfigurationItem] = relationship(foreign_keys=[from_ci_id])
    to_ci: Mapped[ConfigurationItem] = relationship(foreign_keys=[to_ci_id])


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        UniqueConstraint("organization_id", "source_id", "payload_sha256", "policy_version",
                         name="uq_ingestion_replay"),
        CheckConstraint("format IN ('json','csv','yaml','text')", name="ck_run_format"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_payload_hash_length"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(10))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime())
    result: Mapped[dict[str, Any]] = mapped_column(JSON)


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("run_id", "record_key", name="uq_observation_record"),
        CheckConstraint("origin IN ('adapter','ai')", name="ck_observation_origin"),
        CheckConstraint("resolution_status IN ('resolved','unresolved','identity_conflict')",
                        name="ck_observation_status"),
        CheckConstraint("resolution_status <> 'resolved' OR ci_id IS NOT NULL",
                        name="ck_resolved_observation_ci"),
        Index("ix_observation_ci_time", "ci_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("ingestion_runs.id"))
    record_key: Mapped[str] = mapped_column(Text)
    ci_id: Mapped[str | None] = mapped_column(ForeignKey("configuration_items.id"))
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    raw_record: Mapped[Any] = mapped_column(JSON, nullable=False)
    normalized_record: Mapped[dict[str, Any]] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(10))
    resolution_status: Mapped[str] = mapped_column(String(30))
    run: Mapped[IngestionRun] = relationship()
    ci: Mapped[ConfigurationItem | None] = relationship()
