"""Relational state. Every tenant-scoped lookup must include tenant_id."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    # UTC without tzinfo keeps SQLite and PostgreSQL test comparisons equivalent.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active_bundle_id: Mapped[str | None] = mapped_column(String(64))
    identity_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin/analyst/source
    source_system_id: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ClassDefinition(Base):
    __tablename__ = "classes"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class SourceSystem(Base):
    __tablename__ = "source_systems"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    max_age_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    complete_until: Mapped[datetime | None] = mapped_column(DateTime)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PropertyDefinition(Base):
    __tablename__ = "properties"
    __table_args__ = (UniqueConstraint("tenant_id", "class_name", "name"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    class_name: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    data_type: Mapped[str] = mapped_column(String(20), default="string")
    sensitivity: Mapped[str] = mapped_column(String(20), default="internal")
    authority_source_system_id: Mapped[str | None] = mapped_column(String(64))


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    class_name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_actor_id: Mapped[str | None] = mapped_column(String(128))
    visibility: Mapped[str] = mapped_column(String(16), default="shared")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    redirect_id: Mapped[str | None] = mapped_column(String(64))


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("tenant_id", "source_system_id", "record_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_systems.id"), index=True)
    record_key: Mapped[str] = mapped_column(String(256), nullable=False)
    locator: Mapped[str | None] = mapped_column(String(512))


class IdentityBinding(Base):
    __tablename__ = "identity_bindings"
    __table_args__ = (UniqueConstraint("tenant_id", "source_record_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PROPOSED")
    method: Mapped[str] = mapped_column(String(32), default="manual")
    reviewer_id: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class MappingDefinition(Base):
    __tablename__ = "mappings"
    __table_args__ = (UniqueConstraint("tenant_id", "source_system_id", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_systems.id"))
    target_class: Mapped[str] = mapped_column(String(128), nullable=False)
    field_map: Mapped[dict] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="PUBLISHED")


class AssertionRevision(Base):
    __tablename__ = "assertion_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_record_id", "source_version", "mapping_version", "predicate"),
        Index("ix_assertion_entity_predicate", "tenant_id", "entity_id", "predicate"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    source_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"))
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_systems.id"))
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[object] = mapped_column(JSON, nullable=True)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"
    __table_args__ = (UniqueConstraint("tenant_id", "source_record_id"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"))
    last_version: Mapped[int] = mapped_column(Integer, default=0)
    last_payload_hash: Mapped[str | None] = mapped_column(String(64))


class RuleDefinition(Base):
    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    applies_to: Mapped[str] = mapped_column(String(128), nullable=False)
    expression: Mapped[dict] = mapped_column(JSON, nullable=False)
    required_facts: Mapped[list] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    approved_by: Mapped[str | None] = mapped_column(String(128))


class SemanticBundle(Base):
    __tablename__ = "semantic_bundles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("rules.id"))
    mapping_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    identity_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (UniqueConstraint("tenant_id", "requester_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    requester_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    bundle_id: Mapped[str] = mapped_column(ForeignKey("semantic_bundles.id"))
    identity_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_trace: Mapped[dict] = mapped_column(JSON, default=dict)
    source_reads: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    assertion_id: Mapped[str] = mapped_column(ForeignKey("assertion_revisions.id"))
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"))
    predicate: Mapped[str] = mapped_column(String(128))
    observed_value: Mapped[object] = mapped_column(JSON, nullable=True)
    source_record_id: Mapped[str] = mapped_column(ForeignKey("source_records.id"))
    source_version: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncTask(Base):
    __tablename__ = "sync_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_system_id: Mapped[str] = mapped_column(ForeignKey("source_systems.id"))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
