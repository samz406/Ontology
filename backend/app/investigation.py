"""Deterministic, bounded refund investigation with source and rule provenance."""

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    AssertionRevision, AuditEvent, Entity, Evidence, IdentityBinding,
    Investigation, PropertyDefinition, RuleDefinition, SemanticBundle,
    SourceRecord, SourceSystem, Tenant, utcnow,
)
from .rules import Fact, evaluate
from .schemas import InvestigationCreate, iso, naive_utc
from .security import Principal, assert_property_access, assert_visible_entity


@dataclass
class FactRead:
    fact: Fact
    assertions: list[AssertionRevision]
    sources: list[dict]


def _fact_read(db: Session, principal: Principal, entity: Entity, predicate: str,
               as_of, mapping_versions: dict) -> FactRead:
    prop = db.scalar(select(PropertyDefinition).where(
        PropertyDefinition.tenant_id == principal.tenant_id,
        PropertyDefinition.class_name == entity.class_name,
        PropertyDefinition.name == predicate,
    ))
    assert_property_access(principal, prop)
    sources = db.scalars(select(SourceSystem).where(
        SourceSystem.tenant_id == principal.tenant_id,
        SourceSystem.id.in_([prop.authority_source_system_id]) if prop.authority_source_system_id else SourceSystem.id.in_(list(mapping_versions)),
    )).all()
    if not sources:
        return FactRead(Fact("MISSING", reason="SOURCE_NOT_CONFIGURED"), [], [])
    now = utcnow()
    source_info = [{"source_system_id": source.id,
                    "last_successful_sync_at": iso(source.last_successful_sync_at),
                    "complete_until": iso(source.complete_until)} for source in sources]
    stale = any(source.last_successful_sync_at is None or
                now - source.last_successful_sync_at > timedelta(seconds=source.max_age_seconds)
                for source in sources)
    allowed_source_ids = [s.id for s in sources]
    revisions = db.scalars(select(AssertionRevision).join(
        IdentityBinding, IdentityBinding.source_record_id == AssertionRevision.source_record_id,
    ).where(
        AssertionRevision.tenant_id == principal.tenant_id,
        IdentityBinding.tenant_id == principal.tenant_id,
        IdentityBinding.entity_id == entity.id,
        IdentityBinding.status == "CONFIRMED",
        AssertionRevision.entity_id == entity.id,
        AssertionRevision.predicate == predicate,
        AssertionRevision.source_system_id.in_(allowed_source_ids),
        AssertionRevision.valid_from <= as_of,
        (AssertionRevision.valid_to.is_(None) | (AssertionRevision.valid_to > as_of)),
    ).order_by(AssertionRevision.source_version.desc())).all()
    latest_by_record: dict[str, AssertionRevision] = {}
    for revision in revisions:
        if mapping_versions.get(revision.source_system_id) == revision.mapping_version:
            latest_by_record.setdefault(revision.source_record_id, revision)
    active = [a for a in latest_by_record.values() if a.status == "ACTIVE"]
    if stale:
        return FactRead(Fact("STALE", reason="SOURCE_STALE"), active, source_info)
    if not active:
        return FactRead(Fact("MISSING", reason="NO_ASSERTION"), [], source_info)
    values = {json.dumps(a.value, sort_keys=True, ensure_ascii=False) for a in active}
    if len(values) > 1:
        return FactRead(Fact("CONFLICT", assertion_ids=[a.id for a in active], reason="SOURCE_CONFLICT"), active, source_info)
    value = active[0].value
    return FactRead(Fact("NULL" if value is None else "RESOLVED", value,
                         [a.id for a in active]), active, source_info)


def _serialize(db: Session, inv: Investigation) -> dict:
    evidence = db.scalars(select(Evidence).where(Evidence.investigation_id == inv.id,
                                                 Evidence.tenant_id == inv.tenant_id)).all()
    return {
        "investigation_id": inv.id,
        "decision": {"kind": inv.decision, "reason_codes": inv.reason_codes},
        "execution": {"state": inv.execution_state},
        "quality": inv.quality,
        "consistency": {"mode": "BOUNDED_FRESHNESS", "unified_snapshot": False},
        "semantic_bundle_id": inv.bundle_id,
        "identity_epoch": inv.identity_epoch,
        "sources": inv.source_reads,
        "rule_trace": inv.rule_trace,
        "evidence_ids": [item.id for item in evidence],
        "presentation_mode": "TEMPLATE",
        "display_message": {
            "ELIGIBLE": "按当前已发布规则和可用证据，符合示例退款资格；执行前仍需复核最新状态。",
            "INELIGIBLE": "按当前已发布规则和可用证据，不符合示例退款资格。",
            "CONFLICT": "关键事实存在来源冲突，需人工核实。",
            "INDETERMINATE": "证据不完整或过期，暂时无法判断退款资格。",
        }.get(inv.decision, "本次调查无法完成。"),
    }


def get_investigation(db: Session, principal: Principal, investigation_id: str) -> dict:
    inv = db.scalar(select(Investigation).where(Investigation.id == investigation_id,
                                                Investigation.tenant_id == principal.tenant_id))
    if inv is None or (principal.role != "admin" and principal.actor_id != inv.requester_id):
        raise HTTPException(404, "Investigation not found")
    assert_visible_entity(principal, db.get(Entity, inv.entity_id))
    # A revoked field permission makes the former answer unavailable to this caller.
    rule = db.get(RuleDefinition, db.get(SemanticBundle, inv.bundle_id).rule_id)
    for fact in rule.required_facts:
        prop = db.scalar(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id,
                            PropertyDefinition.class_name == "Order", PropertyDefinition.name == fact))
        assert_property_access(principal, prop)
    return _serialize(db, inv)


def investigate(db: Session, principal: Principal, request: InvestigationCreate,
                idempotency_key: str | None) -> dict:
    if principal.role not in {"admin", "analyst"}:
        raise HTTPException(403, "Investigation role required")
    request_hash = hashlib.sha256(json.dumps(request.model_dump(mode="json"),
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if idempotency_key:
        previous = db.scalar(select(Investigation).where(
            Investigation.tenant_id == principal.tenant_id,
            Investigation.requester_id == principal.actor_id,
            Investigation.idempotency_key == idempotency_key))
        if previous:
            if previous.request_hash != request_hash:
                raise HTTPException(409, "Idempotency key reused for a different request")
            return get_investigation(db, principal, previous.id)
    now = utcnow()
    as_of = naive_utc(request.business_as_of) if request.business_as_of else now
    if abs((now - as_of).total_seconds()) > 60:
        raise HTTPException(422, "Historical or future investigation is not supported by this connector")
    source = db.scalar(select(SourceSystem).where(SourceSystem.id == request.anchor.source_system_id,
                                                  SourceSystem.tenant_id == principal.tenant_id))
    if source is None:
        raise HTTPException(404, "Entity not found")
    record = db.scalar(select(SourceRecord).where(SourceRecord.tenant_id == principal.tenant_id,
        SourceRecord.source_system_id == source.id, SourceRecord.record_key == request.anchor.source_record_key))
    binding = db.scalar(select(IdentityBinding).where(IdentityBinding.tenant_id == principal.tenant_id,
        IdentityBinding.source_record_id == record.id, IdentityBinding.status == "CONFIRMED")) if record else None
    if binding is None:
        raise HTTPException(404, "Entity not found")
    entity = assert_visible_entity(principal, db.get(Entity, binding.entity_id))
    if entity.class_name != "Order":
        raise HTTPException(422, "Refund template requires an Order")
    tenant = db.get(Tenant, principal.tenant_id)
    bundle = db.get(SemanticBundle, tenant.active_bundle_id) if tenant and tenant.active_bundle_id else None
    if bundle is None or bundle.tenant_id != principal.tenant_id:
        raise HTTPException(503, "No published semantic bundle")
    rule = db.get(RuleDefinition, bundle.rule_id)
    if rule is None or rule.status != "PUBLISHED" or rule.applies_to != "Order" or not (
        rule.effective_from <= as_of and (rule.effective_to is None or as_of < rule.effective_to)
    ):
        raise HTTPException(422, "No rule applies at this time")

    facts: dict[str, Fact] = {}
    selected: dict[str, FactRead] = {}
    sources: dict[str, dict] = {}
    for predicate in rule.required_facts:
        read = _fact_read(db, principal, entity, predicate, as_of, bundle.mapping_versions)
        facts[predicate], selected[predicate] = read.fact, read
        for item in read.sources:
            sources[item["source_system_id"]] = item
    state, trace = evaluate(rule.expression, facts)
    decision = {"TRUE": "ELIGIBLE", "FALSE": "INELIGIBLE", "CONFLICT": "CONFLICT", "UNKNOWN": "INDETERMINATE"}[state]
    reasons = sorted({fact.reason for fact in facts.values() if fact.reason})
    stale = any(fact.state == "STALE" for fact in facts.values())
    quality = {"truncated": False, "freshness": "STALE" if stale else "CURRENT",
               "source_completeness": "UNKNOWN",  # No closed-world claim without source proof.
               "identity_status": "CONFIRMED",
               "missing_required_facts": [name for name, fact in facts.items() if fact.state in {"MISSING", "STALE"}]}
    db.refresh(tenant)
    if tenant.identity_epoch != bundle.identity_epoch:
        raise HTTPException(409, "Identity epoch changed; retry investigation")
    inv = Investigation(tenant_id=principal.tenant_id, requester_id=principal.actor_id,
        idempotency_key=idempotency_key, request_hash=request_hash, entity_id=entity.id, bundle_id=bundle.id,
        identity_epoch=bundle.identity_epoch, question=request.question,
        decision=decision, execution_state="PARTIAL" if stale else "COMPLETED",
        reason_codes=reasons, quality=quality, rule_trace=trace, source_reads=list(sources.values()))
    db.add(inv)
    db.flush()
    # Evidence is the exact fact revision used for this decision; avoid persisting unbounded text.
    for read in selected.values():
        for assertion in read.assertions:
            db.add(Evidence(tenant_id=principal.tenant_id, investigation_id=inv.id,
                assertion_id=assertion.id, entity_id=entity.id, predicate=assertion.predicate,
                observed_value=assertion.value, source_record_id=assertion.source_record_id,
                source_version=assertion.source_version, observed_at=assertion.observed_at))
    db.add(AuditEvent(tenant_id=principal.tenant_id, actor_id=principal.actor_id,
                      action="INVESTIGATE", target_id=inv.id,
                      details={"decision": decision, "execution": inv.execution_state,
                               "bundle_id": bundle.id}))
    db.commit()
    return _serialize(db, inv)
