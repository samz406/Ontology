"""Durable ingestion: claim jobs, validate monotonic source versions, append revisions."""

import hashlib
import json
import logging
import os
import time
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import database_url, make_engine, make_session_factory
from .models import (
    AssertionRevision, AuditEvent, Entity, IdentityBinding, MappingDefinition,
    PropertyDefinition, SemanticBundle, SourceRecord, SourceSystem, SyncCheckpoint, SyncTask, Tenant,
    utcnow,
)
from .schemas import IngestEvent, naive_utc

LOG = logging.getLogger(__name__)


class IngestError(Exception):
    pass


def process_event(db: Session, task: SyncTask) -> str:
    event = IngestEvent.model_validate(task.payload)
    source = db.scalar(select(SourceSystem).where(SourceSystem.id == event.source_system_id, SourceSystem.tenant_id == task.tenant_id))
    if source is None or not source.enabled:
        raise IngestError("SOURCE_UNAVAILABLE")
    record = db.scalar(select(SourceRecord).where(
        SourceRecord.tenant_id == task.tenant_id,
        SourceRecord.source_system_id == source.id,
        SourceRecord.record_key == event.record_key,
    ))
    if record is None:
        raise IngestError("UNBOUND_SOURCE_RECORD")
    binding = db.scalar(select(IdentityBinding).where(
        IdentityBinding.tenant_id == task.tenant_id,
        IdentityBinding.source_record_id == record.id,
        IdentityBinding.status == "CONFIRMED",
    ))
    if binding is None:
        raise IngestError("IDENTITY_NOT_CONFIRMED")
    tenant = db.get(Tenant, task.tenant_id)
    bundle = db.get(SemanticBundle, tenant.active_bundle_id) if tenant.active_bundle_id else None
    pinned_version = bundle.mapping_versions.get(source.id) if bundle else None
    mapping_query = select(MappingDefinition).where(
        MappingDefinition.tenant_id == task.tenant_id,
        MappingDefinition.source_system_id == source.id,
        MappingDefinition.status == "PUBLISHED",
    )
    if pinned_version is not None:
        mapping_query = mapping_query.where(MappingDefinition.version == pinned_version)
    mapping = db.scalar(mapping_query.order_by(MappingDefinition.version.desc()))
    if mapping is None:
        raise IngestError("MAPPING_NOT_PUBLISHED")
    entity = db.get(Entity, binding.entity_id)
    if entity is None or entity.tenant_id != task.tenant_id or entity.class_name != mapping.target_class:
        raise IngestError("MAPPING_CLASS_MISMATCH")

    checkpoint = db.scalar(select(SyncCheckpoint).where(
        SyncCheckpoint.tenant_id == task.tenant_id,
        SyncCheckpoint.source_record_id == record.id,
    ).with_for_update())
    normalized = event.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if checkpoint is not None and event.source_version <= checkpoint.last_version:
        if event.source_version == checkpoint.last_version and digest != checkpoint.last_payload_hash:
            raise IngestError("EVENT_VERSION_CONFLICT")
        return "DUPLICATE_OR_OLD"

    valid_from = naive_utc(event.valid_from)
    valid_to = naive_utc(event.valid_to) if event.valid_to else None
    if valid_to is not None and valid_to <= valid_from:
        raise IngestError("INVALID_VALID_INTERVAL")
    if event.deleted:
        latest = db.scalars(select(AssertionRevision).where(
            AssertionRevision.tenant_id == task.tenant_id,
            AssertionRevision.source_record_id == record.id,
        ).order_by(AssertionRevision.source_version.desc())).all()
        predicates = {a.predicate for a in latest}
        mapped_values = {predicate: None for predicate in predicates}
    else:
        if not event.fields:
            raise IngestError("EMPTY_EVENT")
        if set(event.fields) - set(mapping.field_map):
            raise IngestError("UNMAPPED_FIELD")
        mapped_values = {mapping.field_map[field]: value for field, value in event.fields.items()}
        if len(mapped_values) != len(event.fields):
            raise IngestError("AMBIGUOUS_MAPPING")

    for predicate, value in mapped_values.items():
        prop = db.scalar(select(PropertyDefinition).where(
            PropertyDefinition.tenant_id == task.tenant_id,
            PropertyDefinition.class_name == mapping.target_class,
            PropertyDefinition.name == predicate,
        ))
        if prop is None:
            raise IngestError("PROPERTY_NOT_REGISTERED")
        if not event.deleted and value is not None:
            valid_type = {
                "string": isinstance(value, str),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
            }.get(prop.data_type, False)
            if not valid_type:
                raise IngestError("PROPERTY_TYPE_MISMATCH")
        db.add(AssertionRevision(
            tenant_id=task.tenant_id, entity_id=binding.entity_id,
            source_record_id=record.id, source_system_id=source.id,
            predicate=predicate, value=value, source_version=event.source_version,
            mapping_version=mapping.version, valid_from=valid_from, valid_to=valid_to,
            source_updated_at=valid_from, observed_at=utcnow(), ingested_at=utcnow(),
            status="RETRACTED" if event.deleted else "ACTIVE",
        ))
    if checkpoint is None:
        db.add(SyncCheckpoint(tenant_id=task.tenant_id, source_record_id=record.id,
                              last_version=event.source_version, last_payload_hash=digest))
    else:
        checkpoint.last_version = event.source_version
        checkpoint.last_payload_hash = digest
    source.last_successful_sync_at = utcnow()
    db.add(AuditEvent(tenant_id=task.tenant_id, actor_id="worker", action="INGEST_EVENT",
                      target_id=record.id, details={"version": event.source_version, "deleted": event.deleted}))
    return "APPLIED"


def run_once(factory) -> str | None:
    now = utcnow()
    with factory.begin() as db:
        task = db.scalar(select(SyncTask).where(
            or_(SyncTask.state == "PENDING", (SyncTask.state == "PROCESSING") & (SyncTask.locked_until < now)),
            SyncTask.next_attempt_at <= now,
        ).order_by(SyncTask.created_at, SyncTask.id).with_for_update(skip_locked=True).limit(1))
        if task is None:
            return None
        task.state = "PROCESSING"
        task.attempts += 1
        task.locked_until = now + timedelta(minutes=5)
        task_id = task.id

    try:
        with factory.begin() as db:
            task = db.get(SyncTask, task_id)
            outcome = process_event(db, task)
            task.state = "DONE"
            task.completed_at = utcnow()
            task.locked_until = None
            task.error_code = None
        return outcome
    except Exception as exc:
        # Do not put source data or exception arguments in logs or audit events.
        code = str(exc) if isinstance(exc, IngestError) else "UNEXPECTED_INGEST_ERROR"
        with factory.begin() as db:
            task = db.get(SyncTask, task_id)
            task.error_code = code
            task.locked_until = None
            task.state = "FAILED" if task.attempts >= 3 or isinstance(exc, IngestError) else "PENDING"
            task.next_attempt_at = utcnow() + timedelta(seconds=min(60, 2 ** task.attempts))
            db.add(AuditEvent(tenant_id=task.tenant_id, actor_id="worker", action="INGEST_FAILED",
                              target_id=task.id, details={"reason": code}))
        LOG.warning("Ingestion failed task=%s reason=%s", task_id, code)
        return code


def main():
    logging.basicConfig(level=logging.INFO)
    engine = make_engine(database_url())
    factory = make_session_factory(engine)
    poll_interval = float(os.getenv("WORKER_POLL_SECONDS", "1"))
    LOG.info("Ingestion worker started")
    while True:
        if run_once(factory) is None:
            time.sleep(poll_interval)


if __name__ == "__main__":
    main()
