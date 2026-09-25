"""REST API for the bounded first production pilot."""

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import database_url, db_session, initialize_dev_schema, make_engine, make_session_factory
from .investigation import get_investigation, investigate
from .models import (
    ApiKey, AuditEvent, ClassDefinition, Entity, Evidence, IdentityBinding,
    MappingDefinition, PropertyDefinition, RuleDefinition, SemanticBundle,
    SourceRecord, SourceSystem, SyncTask, Tenant, utcnow,
)
from .rules import InvalidRule, validate_rule
from .schemas import (
    BindingCreate, EntityCreate, IngestEvent, InvestigationCreate,
    MappingCreate, PropertyCreate, RuleCreate, SourceCreate, iso, naive_utc,
)
from .security import (
    Principal, assert_property_access, assert_visible_entity, get_principal,
    hash_token, require_admin,
)


def create_app(db_url: str | None = None, auto_create: bool | None = None) -> FastAPI:
    db_url = db_url or database_url()
    if auto_create is None:
        auto_create = os.getenv("AUTO_CREATE_SCHEMA", "0") == "1"
    engine = make_engine(db_url)
    if auto_create:
        if os.getenv("APP_ENV", "development") == "production":
            raise RuntimeError("Production schema must be initialized by Alembic")
        initialize_dev_schema(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="Ontology Agent", version="0.1.0", lifespan=lifespan,
                  root_path=os.getenv("ROOT_PATH", ""))
    app.state.session_factory = make_session_factory(engine)
    app.add_middleware(CORSMiddleware,
        allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")],
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.get("/healthz")
    def healthz(db: Session = Depends(db_session)):
        db.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/v1/me")
    def me(principal: Principal = Depends(get_principal)):
        return {"tenant_id": principal.tenant_id, "actor_id": principal.actor_id, "role": principal.role}

    @app.get("/v1/sources")
    def list_sources(db: Session = Depends(db_session), principal: Principal = Depends(get_principal)):
        if principal.role not in {"admin", "analyst"}:
            raise HTTPException(403, "Investigation role required")
        sources = db.scalars(select(SourceSystem).where(SourceSystem.tenant_id == principal.tenant_id,
                                                       SourceSystem.enabled.is_(True))).all()
        return [{"id": source.id, "name": source.name} for source in sources]

    @app.post("/v1/admin/sources", status_code=201)
    def create_source(body: SourceCreate, db: Session = Depends(db_session),
                      principal: Principal = Depends(require_admin)):
        if db.scalar(select(SourceSystem).where(SourceSystem.tenant_id == principal.tenant_id,
                                                SourceSystem.name == body.name)):
            raise HTTPException(409, "Source already exists")
        source = SourceSystem(tenant_id=principal.tenant_id, name=body.name,
                              max_age_seconds=body.max_age_seconds)
        db.add(source)
        db.flush()
        raw_token = secrets.token_urlsafe(32)
        db.add(ApiKey(tenant_id=principal.tenant_id, actor_id=f"source:{source.id}",
                      token_hash=hash_token(raw_token), role="source", source_system_id=source.id))
        db.add(AuditEvent(tenant_id=principal.tenant_id, actor_id=principal.actor_id,
                          action="SOURCE_CREATE", target_id=source.id, details={"name": source.name}))
        db.commit()
        return {"id": source.id, "name": source.name, "ingest_token": raw_token}

    @app.post("/v1/admin/properties", status_code=201)
    def create_property(body: PropertyCreate, db: Session = Depends(db_session),
                        principal: Principal = Depends(require_admin)):
        if not db.scalar(select(ClassDefinition).where(ClassDefinition.tenant_id == principal.tenant_id,
                                                        ClassDefinition.name == body.class_name)):
            raise HTTPException(422, "Class not registered")
        if body.authority_source_system_id and not db.scalar(select(SourceSystem).where(
            SourceSystem.id == body.authority_source_system_id,
            SourceSystem.tenant_id == principal.tenant_id)):
            raise HTTPException(422, "Source not registered")
        if db.scalar(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id,
            PropertyDefinition.class_name == body.class_name, PropertyDefinition.name == body.name)):
            raise HTTPException(409, "Property already exists")
        prop = PropertyDefinition(tenant_id=principal.tenant_id, **body.model_dump())
        db.add(prop)
        db.commit()
        return {"id": prop.id, "name": prop.name}

    @app.post("/v1/admin/entities", status_code=201)
    def create_entity(body: EntityCreate, db: Session = Depends(db_session),
                      principal: Principal = Depends(require_admin)):
        if not db.scalar(select(ClassDefinition).where(ClassDefinition.tenant_id == principal.tenant_id,
                                                        ClassDefinition.name == body.class_name)):
            raise HTTPException(422, "Class not registered")
        entity = Entity(tenant_id=principal.tenant_id, **body.model_dump())
        db.add(entity)
        db.commit()
        return {"id": entity.id, "class_name": entity.class_name, "display_name": entity.display_name}

    @app.post("/v1/admin/bindings", status_code=201)
    def create_binding(body: BindingCreate, db: Session = Depends(db_session),
                       principal: Principal = Depends(require_admin)):
        source = db.scalar(select(SourceSystem).where(SourceSystem.id == body.source_system_id,
                                                     SourceSystem.tenant_id == principal.tenant_id))
        entity = db.scalar(select(Entity).where(Entity.id == body.entity_id,
                                                 Entity.tenant_id == principal.tenant_id))
        if source is None or entity is None:
            raise HTTPException(422, "Source or entity not registered")
        record = db.scalar(select(SourceRecord).where(SourceRecord.tenant_id == principal.tenant_id,
            SourceRecord.source_system_id == source.id, SourceRecord.record_key == body.record_key))
        if record is not None and db.scalar(select(IdentityBinding).where(
            IdentityBinding.tenant_id == principal.tenant_id, IdentityBinding.source_record_id == record.id)):
            raise HTTPException(409, "Source record already bound")
        if record is None:
            record = SourceRecord(tenant_id=principal.tenant_id, source_system_id=source.id,
                                   record_key=body.record_key)
            db.add(record)
            db.flush()
        binding = IdentityBinding(tenant_id=principal.tenant_id, source_record_id=record.id,
            entity_id=entity.id, method=body.method, status="CONFIRMED", reviewer_id=principal.actor_id)
        db.add(binding)
        db.flush()
        tenant = db.get(Tenant, principal.tenant_id)
        tenant.identity_epoch += 1
        _advance_bundle_epoch(db, tenant)
        db.add(AuditEvent(tenant_id=principal.tenant_id, actor_id=principal.actor_id,
                          action="BINDING_CONFIRM", target_id=entity.id, details={"binding_id": binding.id}))
        db.commit()
        return {"id": binding.id, "entity_id": entity.id, "status": binding.status}

    @app.patch("/v1/admin/bindings/{binding_id}")
    def revoke_binding(binding_id: str, db: Session = Depends(db_session),
                       principal: Principal = Depends(require_admin)):
        binding = db.scalar(select(IdentityBinding).where(IdentityBinding.id == binding_id,
                                                        IdentityBinding.tenant_id == principal.tenant_id))
        if binding is None:
            raise HTTPException(404, "Binding not found")
        if binding.status != "CONFIRMED":
            raise HTTPException(409, "Binding is not confirmed")
        binding.status = "REVOKED"
        binding.revision += 1
        tenant = db.get(Tenant, principal.tenant_id)
        tenant.identity_epoch += 1
        _advance_bundle_epoch(db, tenant)
        db.add(AuditEvent(tenant_id=principal.tenant_id, actor_id=principal.actor_id,
                          action="BINDING_REVOKE", target_id=binding.id, details={}))
        db.commit()
        return {"id": binding.id, "status": binding.status}

    @app.post("/v1/admin/mappings", status_code=201)
    def create_mapping(body: MappingCreate, db: Session = Depends(db_session),
                       principal: Principal = Depends(require_admin)):
        source = db.scalar(select(SourceSystem).where(SourceSystem.id == body.source_system_id,
                                                     SourceSystem.tenant_id == principal.tenant_id))
        if source is None:
            raise HTTPException(422, "Source not registered")
        if db.scalar(select(MappingDefinition).where(MappingDefinition.tenant_id == principal.tenant_id,
                                                     MappingDefinition.source_system_id == source.id)):
            raise HTTPException(409, "Mapping upgrades require shadow validation and are disabled in this pilot")
        if len(set(body.field_map.values())) != len(body.field_map):
            raise HTTPException(422, "Multiple source fields map to one property")
        for predicate in body.field_map.values():
            prop = db.scalar(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id,
                 PropertyDefinition.class_name == body.target_class, PropertyDefinition.name == predicate))
            if prop is None or prop.authority_source_system_id != source.id:
                raise HTTPException(422, f"Property {predicate} is not authoritative for this source")
        mapping = MappingDefinition(tenant_id=principal.tenant_id, status="PUBLISHED", **body.model_dump())
        db.add(mapping)
        db.commit()
        return {"id": mapping.id, "version": mapping.version, "status": mapping.status}

    @app.post("/v1/admin/rules", status_code=201)
    def create_rule(body: RuleCreate, db: Session = Depends(db_session),
                    principal: Principal = Depends(require_admin)):
        if body.applies_to != "Order":
            raise HTTPException(422, "Only the Order investigation template is supported")
        try:
            validate_rule(body.expression, body.required_facts)
        except InvalidRule as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.effective_from.tzinfo is None or (body.effective_to and body.effective_to.tzinfo is None):
            raise HTTPException(422, "Rule timestamps require timezone")
        effective_from, effective_to = naive_utc(body.effective_from), (naive_utc(body.effective_to) if body.effective_to else None)
        if effective_to is not None and effective_to <= effective_from:
            raise HTTPException(422, "Invalid effective interval")
        for predicate in body.required_facts:
            if not db.scalar(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id,
                 PropertyDefinition.class_name == body.applies_to, PropertyDefinition.name == predicate)):
                raise HTTPException(422, f"Property {predicate} not registered")
        if db.scalar(select(RuleDefinition).where(RuleDefinition.tenant_id == principal.tenant_id,
                                                 RuleDefinition.name == body.name, RuleDefinition.version == body.version)):
            raise HTTPException(409, "Rule version already exists")
        rule = RuleDefinition(tenant_id=principal.tenant_id, name=body.name, applies_to=body.applies_to,
            expression=body.expression, required_facts=body.required_facts, version=body.version,
            effective_from=effective_from, effective_to=effective_to, status="DRAFT")
        db.add(rule)
        db.commit()
        return {"id": rule.id, "status": rule.status, "version": rule.version}

    @app.post("/v1/admin/rules/{rule_id}/publish")
    def publish_rule(rule_id: str, db: Session = Depends(db_session),
                     principal: Principal = Depends(require_admin)):
        rule = db.scalar(select(RuleDefinition).where(RuleDefinition.id == rule_id,
                                                      RuleDefinition.tenant_id == principal.tenant_id))
        if rule is None:
            raise HTTPException(404, "Rule not found")
        if rule.status != "DRAFT":
            raise HTTPException(409, "Only a draft can be published")
        tenant = db.get(Tenant, principal.tenant_id)
        mappings = db.scalars(select(MappingDefinition).where(MappingDefinition.tenant_id == principal.tenant_id,
                                                              MappingDefinition.status == "PUBLISHED")).all()
        mapping_versions = {m.source_system_id: m.version for m in mappings}
        for fact in rule.required_facts:
            prop = db.scalar(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id,
                PropertyDefinition.class_name == rule.applies_to, PropertyDefinition.name == fact))
            if prop.authority_source_system_id not in mapping_versions or not any(
                fact in mapping.field_map.values() and mapping.source_system_id == prop.authority_source_system_id
                and mapping.target_class == rule.applies_to for mapping in mappings
            ):
                raise HTTPException(422, f"Fact {fact} has no mapped authority source")
        rule.status = "PUBLISHED"
        rule.approved_by = principal.actor_id
        bundle = SemanticBundle(tenant_id=principal.tenant_id, rule_id=rule.id,
            mapping_versions=mapping_versions, identity_epoch=tenant.identity_epoch)
        db.add(bundle)
        db.flush()
        tenant.active_bundle_id = bundle.id
        db.add(AuditEvent(tenant_id=principal.tenant_id, actor_id=principal.actor_id,
                          action="BUNDLE_PUBLISH", target_id=bundle.id, details={"rule_id": rule.id}))
        db.commit()
        return {"semantic_bundle_id": bundle.id, "rule_id": rule.id, "mapping_versions": mapping_versions}

    @app.get("/v1/admin/state")
    def admin_state(db: Session = Depends(db_session), principal: Principal = Depends(require_admin)):
        tenant = db.get(Tenant, principal.tenant_id)
        sources = db.scalars(select(SourceSystem).where(SourceSystem.tenant_id == principal.tenant_id)).all()
        props = db.scalars(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id)).all()
        entities = db.scalars(select(Entity).where(Entity.tenant_id == principal.tenant_id)).all()
        rules = db.scalars(select(RuleDefinition).where(RuleDefinition.tenant_id == principal.tenant_id)).all()
        tasks = db.scalars(select(SyncTask).where(SyncTask.tenant_id == principal.tenant_id).order_by(SyncTask.created_at.desc()).limit(20)).all()
        return {"active_bundle_id": tenant.active_bundle_id,
                "sources": [{"id": s.id, "name": s.name, "max_age_seconds": s.max_age_seconds,
                             "last_successful_sync_at": iso(s.last_successful_sync_at)} for s in sources],
                "properties": [{"name": p.name, "sensitivity": p.sensitivity, "authority_source_system_id": p.authority_source_system_id} for p in props],
                "entities": [{"id": e.id, "class_name": e.class_name, "display_name": e.display_name, "visibility": e.visibility} for e in entities],
                "rules": [{"id": r.id, "name": r.name, "version": r.version, "status": r.status} for r in rules],
                "tasks": [{"id": t.id, "state": t.state, "error_code": t.error_code} for t in tasks]}

    @app.post("/v1/ingest/events", status_code=202)
    def enqueue_event(body: IngestEvent, db: Session = Depends(db_session),
                      principal: Principal = Depends(get_principal)):
        if principal.role not in {"admin", "source"} or (
            principal.role == "source" and body.source_system_id != principal.source_system_id):
            raise HTTPException(403, "Source access denied")
        source = db.scalar(select(SourceSystem).where(SourceSystem.id == body.source_system_id,
                                                      SourceSystem.tenant_id == principal.tenant_id,
                                                      SourceSystem.enabled.is_(True)))
        if source is None:
            raise HTTPException(404, "Source not found")
        task = SyncTask(tenant_id=principal.tenant_id, source_system_id=source.id,
                        payload=body.model_dump(mode="json"))
        db.add(task)
        db.commit()
        return {"task_id": task.id, "state": "PENDING"}

    @app.get("/v1/admin/sync/tasks/{task_id}")
    def get_task(task_id: str, db: Session = Depends(db_session),
                 principal: Principal = Depends(require_admin)):
        task = db.scalar(select(SyncTask).where(SyncTask.id == task_id,
                                                SyncTask.tenant_id == principal.tenant_id))
        if task is None:
            raise HTTPException(404, "Task not found")
        return {"id": task.id, "state": task.state, "error_code": task.error_code,
                "attempts": task.attempts, "completed_at": iso(task.completed_at)}

    @app.post("/v1/investigations")
    def post_investigation(body: InvestigationCreate, idempotency_key: str | None = Header(None),
                           db: Session = Depends(db_session), principal: Principal = Depends(get_principal)):
        if idempotency_key is not None and len(idempotency_key) > 128:
            raise HTTPException(422, "Idempotency key too long")
        return investigate(db, principal, body, idempotency_key)

    @app.get("/v1/investigations/{investigation_id}")
    def read_investigation(investigation_id: str, db: Session = Depends(db_session),
                           principal: Principal = Depends(get_principal)):
        return get_investigation(db, principal, investigation_id)

    @app.get("/v1/investigations")
    def list_investigations(db: Session = Depends(db_session), principal: Principal = Depends(get_principal)):
        if principal.role not in {"admin", "analyst"}:
            raise HTTPException(403, "Investigation role required")
        from .models import Investigation
        query = select(Investigation).where(Investigation.tenant_id == principal.tenant_id)
        if principal.role != "admin":
            query = query.where(Investigation.requester_id == principal.actor_id)
        items = db.scalars(query.order_by(Investigation.created_at.desc()).limit(30)).all()
        visible = []
        for item in items:
            try:
                get_investigation(db, principal, item.id)
            except HTTPException:
                continue
            entity = db.get(Entity, item.entity_id)
            visible.append({"id": item.id, "decision": item.decision,
                            "execution_state": item.execution_state,
                            "created_at": iso(item.created_at), "entity_name": entity.display_name})
        return visible

    @app.get("/v1/evidence/{evidence_id}")
    def read_evidence(evidence_id: str, db: Session = Depends(db_session),
                      principal: Principal = Depends(get_principal)):
        evidence = db.scalar(select(Evidence).where(Evidence.id == evidence_id,
                                                    Evidence.tenant_id == principal.tenant_id))
        if evidence is None:
            raise HTTPException(404, "Evidence not found")
        from .models import Investigation
        inv = db.get(Investigation, evidence.investigation_id)
        if principal.role != "admin" and principal.actor_id != inv.requester_id:
            raise HTTPException(404, "Evidence not found")
        assert_visible_entity(principal, db.get(Entity, evidence.entity_id))
        prop = db.scalar(select(PropertyDefinition).where(PropertyDefinition.tenant_id == principal.tenant_id,
            PropertyDefinition.class_name == "Order", PropertyDefinition.name == evidence.predicate))
        assert_property_access(principal, prop)
        record = db.get(SourceRecord, evidence.source_record_id)
        db.add(AuditEvent(tenant_id=principal.tenant_id, actor_id=principal.actor_id,
                          action="EVIDENCE_READ", target_id=evidence.id, details={}))
        db.commit()
        return {"id": evidence.id, "predicate": evidence.predicate,
                "observed_value": evidence.observed_value,
                "source_system_id": record.source_system_id, "source_record_key": record.record_key,
                "source_version": evidence.source_version, "observed_at": iso(evidence.observed_at)}

    @app.get("/v1/admin/audit-events")
    def audit_events(db: Session = Depends(db_session), principal: Principal = Depends(require_admin)):
        events = db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == principal.tenant_id)
                            .order_by(AuditEvent.created_at.desc()).limit(100)).all()
        return [{"id": e.id, "action": e.action, "actor_id": e.actor_id,
                 "target_id": e.target_id, "details": e.details, "created_at": iso(e.created_at)} for e in events]

    return app


def _advance_bundle_epoch(db: Session, tenant: Tenant):
    if tenant.active_bundle_id:
        old = db.get(SemanticBundle, tenant.active_bundle_id)
        new = SemanticBundle(tenant_id=tenant.id, rule_id=old.rule_id,
            mapping_versions=old.mapping_versions, identity_epoch=tenant.identity_epoch)
        db.add(new)
        db.flush()
        tenant.active_bundle_id = new.id


app = create_app()
