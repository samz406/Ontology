"""Explicit development fixture; never execute automatically in production."""

import os

from sqlalchemy import select

from .db import database_url, initialize_dev_schema, make_engine, make_session_factory
from .models import (
    ApiKey, ClassDefinition, Entity, IdentityBinding, MappingDefinition,
    PropertyDefinition, RuleDefinition, SemanticBundle, SourceRecord,
    SourceSystem, SyncTask, Tenant, utcnow,
)
from .security import hash_token
from .worker import run_once


def seed_demo(factory, admin_token: str, analyst_token: str, source_token: str) -> dict:
    if len({admin_token, analyst_token, source_token}) != 3 or any(len(t) < 16 for t in (admin_token, analyst_token, source_token)):
        raise ValueError("Demo tokens must be distinct and at least 16 characters")
    with factory.begin() as db:
        existing = db.get(Tenant, "demo")
        if existing is not None:
            return {"tenant_id": existing.id, "already_exists": True}
        tenant = Tenant(id="demo", name="Demo business")
        db.add(tenant)
        db.add_all([
            ApiKey(tenant_id="demo", actor_id="admin", role="admin", token_hash=hash_token(admin_token)),
            ApiKey(tenant_id="demo", actor_id="analyst", role="analyst", token_hash=hash_token(analyst_token)),
            ClassDefinition(tenant_id="demo", name="Order", description="Customer order"),
        ])
        db.flush()
        sources = {}
        for name in ("erp", "payment", "shipping"):
            source = SourceSystem(tenant_id="demo", name=name, max_age_seconds=86400)
            db.add(source)
            db.flush()
            sources[name] = source
        db.add(ApiKey(tenant_id="demo", actor_id="source:demo", role="source",
                      source_system_id=sources["payment"].id, token_hash=hash_token(source_token)))
        for name, source_name in (("order.status", "erp"), ("payment.status", "payment"),
                                  ("shipment.status", "shipping")):
            db.add(PropertyDefinition(tenant_id="demo", class_name="Order", name=name,
                data_type="string", sensitivity="internal", authority_source_system_id=sources[source_name].id))
        db.flush()
        entity = Entity(tenant_id="demo", class_name="Order", display_name="ORDER-10001",
                        visibility="shared")
        db.add(entity)
        db.flush()
        for name, key in (("erp", "ORDER-10001"), ("payment", "PAY-10001"), ("shipping", "SHIP-10001")):
            record = SourceRecord(tenant_id="demo", source_system_id=sources[name].id, record_key=key)
            db.add(record)
            db.flush()
            db.add(IdentityBinding(tenant_id="demo", source_record_id=record.id, entity_id=entity.id,
                                   status="CONFIRMED", method="verified_external_key", reviewer_id="admin"))
            db.add(MappingDefinition(tenant_id="demo", source_system_id=sources[name].id,
                target_class="Order", field_map={"status": f"{'shipment' if name == 'shipping' else name if name != 'erp' else 'order'}.status"},
                version=1, status="PUBLISHED"))
        db.flush()
        rule = RuleDefinition(tenant_id="demo", name="demo_refund_eligibility", version=1,
            applies_to="Order", status="PUBLISHED", approved_by="admin",
            required_facts=["payment.status", "shipment.status"],
            expression={"all": [
                {"fact": "payment.status", "op": "eq", "value": "SETTLED"},
                {"fact": "shipment.status", "op": "eq", "value": "NOT_SHIPPED"},
            ]}, effective_from=utcnow().replace(year=2020))
        db.add(rule)
        db.flush()
        bundle = SemanticBundle(tenant_id="demo", rule_id=rule.id, identity_epoch=tenant.identity_epoch,
            mapping_versions={source.id: 1 for source in sources.values()})
        db.add(bundle)
        db.flush()
        tenant.active_bundle_id = bundle.id
        now = utcnow().replace(microsecond=0).isoformat() + "Z"
        for name, key, value in (("erp", "ORDER-10001", "PAID"),
                                 ("payment", "PAY-10001", "SETTLED"),
                                 ("shipping", "SHIP-10001", "NOT_SHIPPED")):
            db.add(SyncTask(tenant_id="demo", source_system_id=sources[name].id,
                payload={"source_system_id": sources[name].id, "record_key": key, "source_version": 1,
                         "valid_from": now, "valid_to": None, "fields": {"status": value}, "deleted": False}))
        result = {"tenant_id": "demo", "order_key": "ORDER-10001", "order_source_id": sources["erp"].id,
                  "payment_source_id": sources["payment"].id, "shipping_source_id": sources["shipping"].id}
    for _ in range(3):
        outcome = run_once(factory)
        if outcome != "APPLIED":
            raise RuntimeError(f"Demo ingestion failed: {outcome}")
    return result


def main():
    if os.getenv("APP_ENV", "development") == "production":
        raise RuntimeError("Demo seed is disabled in production")
    admin = os.getenv("DEMO_ADMIN_TOKEN", "local-demo-admin-token-12345")
    analyst = os.getenv("DEMO_ANALYST_TOKEN", "local-demo-analyst-token-12345")
    source = os.getenv("DEMO_SOURCE_TOKEN", "local-demo-source-token-12345")
    engine = make_engine(database_url())
    initialize_dev_schema(engine)
    result = seed_demo(make_session_factory(engine), admin, analyst, source)
    print("Demo tenant ready:", result)
    print("Development admin token:", admin)
    print("Development analyst token:", analyst)


if __name__ == "__main__":
    main()
