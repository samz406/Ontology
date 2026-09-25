from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.bootstrap import bootstrap
from app.models import ApiKey, PropertyDefinition, SourceSystem, SyncTask, Tenant, utcnow
from app.rules import Fact, evaluate, validate_rule, InvalidRule
from app.seed import seed_demo
from app.security import hash_token
from app.worker import run_once

ADMIN = "local-demo-admin-token-12345"
ANALYST = "local-demo-analyst-token-12345"
SOURCE = "local-demo-source-token-12345"


@pytest.fixture
def demo(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}", auto_create=True)
    info = seed_demo(app.state.session_factory, ADMIN, ANALYST, SOURCE)
    with TestClient(app) as client:
        yield client, app.state.session_factory, info


def headers(token=ANALYST):
    return {"Authorization": f"Bearer {token}"}


def investigate(client, info, token=ANALYST, **extra):
    payload = {"anchor": {"source_system_id": info["order_source_id"],
                          "source_record_key": info["order_key"]}, **extra}
    return client.post("/v1/investigations", headers=headers(token), json=payload)


def enqueue(client, info, version, status, token=ADMIN, deleted=False):
    return client.post("/v1/ingest/events", headers=headers(token), json={
        "source_system_id": info["shipping_source_id"], "record_key": "SHIP-10001",
        "source_version": version, "valid_from": utcnow().isoformat() + "Z",
        "fields": {} if deleted else {"status": status}, "deleted": deleted,
    })


def test_eligible_is_audited_and_evidence_requires_authorization(demo):
    client, factory, info = demo
    assert client.get("/v1/me").status_code == 401
    result = investigate(client, info)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["decision"]["kind"] == "ELIGIBLE"
    assert data["execution"]["state"] == "COMPLETED"
    assert data["consistency"]["unified_snapshot"] is False
    assert len(data["evidence_ids"]) == 2
    evidence_id = data["evidence_ids"][0]
    assert client.get(f"/v1/evidence/{evidence_id}", headers=headers()).status_code == 200
    assert client.get(f"/v1/evidence/{evidence_id}").status_code == 401
    assert client.get("/v1/admin/audit-events", headers=headers()).status_code == 403
    audit = client.get("/v1/admin/audit-events", headers=headers(ADMIN)).json()
    assert {entry["action"] for entry in audit} >= {"INVESTIGATE", "EVIDENCE_READ"}
    assert not any("SETTLED" in str(entry["details"]) for entry in audit)


def test_new_version_and_delete_never_resurrect_old_fact(demo):
    client, factory, info = demo
    assert enqueue(client, info, 2, "SHIPPED").status_code == 202
    assert run_once(factory) == "APPLIED"
    assert investigate(client, info).json()["decision"]["kind"] == "INELIGIBLE"
    assert enqueue(client, info, 1, "NOT_SHIPPED").status_code == 202
    assert run_once(factory) == "DUPLICATE_OR_OLD"
    assert investigate(client, info).json()["decision"]["kind"] == "INELIGIBLE"
    assert enqueue(client, info, 3, "", deleted=True).status_code == 202
    assert run_once(factory) == "APPLIED"
    after_delete = investigate(client, info).json()
    assert after_delete["decision"]["kind"] == "INDETERMINATE"
    assert "shipment.status" in after_delete["quality"]["missing_required_facts"]


def test_conflicting_same_version_is_quarantined(demo):
    client, factory, info = demo
    assert enqueue(client, info, 2, "SHIPPED").status_code == 202
    assert run_once(factory) == "APPLIED"
    assert enqueue(client, info, 2, "NOT_SHIPPED").status_code == 202
    assert run_once(factory) == "EVENT_VERSION_CONFLICT"
    with factory() as db:
        task = db.scalar(select(SyncTask).where(SyncTask.error_code == "EVENT_VERSION_CONFLICT"))
        assert task and task.state == "FAILED"
    assert investigate(client, info).json()["decision"]["kind"] == "INELIGIBLE"


def test_stale_source_and_revoked_property_are_not_eligible(demo):
    client, factory, info = demo
    old = investigate(client, info).json()
    with factory.begin() as db:
        source = db.get(SourceSystem, info["shipping_source_id"])
        source.last_successful_sync_at = utcnow() - timedelta(days=2)
    stale = investigate(client, info).json()
    assert stale["decision"]["kind"] == "INDETERMINATE"
    assert stale["execution"]["state"] == "PARTIAL"
    assert stale["quality"]["freshness"] == "STALE"
    with factory.begin() as db:
        prop = db.scalar(select(PropertyDefinition).where(PropertyDefinition.name == "shipment.status"))
        prop.sensitivity = "restricted"
    assert investigate(client, info).status_code == 403
    assert client.get(f"/v1/investigations/{old['investigation_id']}", headers=headers()).status_code == 403
    assert client.get(f"/v1/evidence/{old['evidence_ids'][1]}", headers=headers()).status_code == 403
    assert client.get("/v1/investigations", headers=headers()).json() == []


def test_tenant_scope_source_role_and_idempotency(demo):
    client, factory, info = demo
    with factory.begin() as db:
        db.add(Tenant(id="other", name="Other tenant"))
        db.add(ApiKey(tenant_id="other", actor_id="other", role="admin",
                      token_hash=hash_token("another-tenant-token-123456")))
    assert investigate(client, info, token="another-tenant-token-123456").status_code == 404
    assert investigate(client, info, token=SOURCE).status_code == 403
    assert enqueue(client, info, 2, "SHIPPED", token=SOURCE).status_code == 403
    assert client.get("/v1/sources", headers=headers(SOURCE)).status_code == 403
    payload = {"anchor": {"source_system_id": info["order_source_id"], "source_record_key": info["order_key"]}}
    first = client.post("/v1/investigations", headers={**headers(), "Idempotency-Key": "same"}, json=payload).json()
    second = client.post("/v1/investigations", headers={**headers(), "Idempotency-Key": "same"}, json=payload).json()
    assert first["investigation_id"] == second["investigation_id"]
    changed = client.post("/v1/investigations", headers={**headers(), "Idempotency-Key": "same"},
                          json={**payload, "question": "different"})
    assert changed.status_code == 409
    assert investigate(client, info, business_as_of="2020-01-01T00:00:00Z").status_code == 422


def test_revoking_identity_removes_its_evidence(demo):
    client, factory, info = demo
    with factory() as db:
        from app.models import IdentityBinding, SourceRecord
        binding = db.scalar(select(IdentityBinding).join(SourceRecord).where(
            SourceRecord.source_system_id == info["shipping_source_id"]))
        binding_id = binding.id
    response = client.patch(f"/v1/admin/bindings/{binding_id}", headers=headers(ADMIN))
    assert response.status_code == 200, response.text
    decision = investigate(client, info).json()
    assert decision["decision"]["kind"] == "INDETERMINATE"
    assert len(decision["evidence_ids"]) == 1


def test_empty_tenant_configure_ingest_publish_investigate(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'fresh.db'}", auto_create=True)
    bootstrap(app.state.session_factory, "fresh", "Fresh", "operator", "initial-admin-token-very-long-123456789")
    auth = headers("initial-admin-token-very-long-123456789")
    with TestClient(app) as client:
        assert client.get("/v1/me", headers=auth).json()["tenant_id"] == "fresh"
        source_tokens = {}
        source_ids = {}
        for name, predicate in (("orders", "order.status"), ("payments", "payment.status"),
                                ("shipping", "shipment.status")):
            source = client.post("/v1/admin/sources", headers=auth, json={"name": name}).json()
            source_ids[name] = source["id"]
            source_tokens[name] = source["ingest_token"]
            prop = client.post("/v1/admin/properties", headers=auth, json={
                "name": predicate, "authority_source_system_id": source["id"]})
            assert prop.status_code == 201, prop.text
        entity = client.post("/v1/admin/entities", headers=auth,
                             json={"display_name": "ORDER-42"}).json()
        for name, predicate in (("orders", "order.status"), ("payments", "payment.status"),
                                ("shipping", "shipment.status")):
            record_key = f"{name}-42"
            binding = client.post("/v1/admin/bindings", headers=auth, json={
                "source_system_id": source_ids[name], "entity_id": entity["id"], "record_key": record_key})
            assert binding.status_code == 201, binding.text
            mapping = client.post("/v1/admin/mappings", headers=auth, json={
                "source_system_id": source_ids[name], "field_map": {"status": predicate}, "version": 1})
            assert mapping.status_code == 201, mapping.text
        rule = client.post("/v1/admin/rules", headers=auth, json={
            "name": "refund", "version": 1, "effective_from": "2020-01-01T00:00:00Z",
            "required_facts": ["payment.status", "shipment.status"],
            "expression": {"all": [{"fact": "payment.status", "op": "eq", "value": "SETTLED"},
                                   {"fact": "shipment.status", "op": "eq", "value": "NOT_SHIPPED"}]}})
        assert rule.status_code == 201, rule.text
        assert client.post(f"/v1/admin/rules/{rule.json()['id']}/publish", headers=auth).status_code == 200
        for name, status in (("orders", "PAID"), ("payments", "SETTLED"),
                             ("shipping", "NOT_SHIPPED")):
            enqueued = client.post("/v1/ingest/events", headers=headers(source_tokens[name]), json={
                "source_system_id": source_ids[name], "record_key": f"{name}-42",
                "source_version": 1, "valid_from": utcnow().isoformat() + "Z",
                "fields": {"status": status}})
            assert enqueued.status_code == 202, enqueued.text
            assert run_once(app.state.session_factory) == "APPLIED"
        result = client.post("/v1/investigations", headers=auth, json={
            "anchor": {"source_system_id": source_ids["orders"], "source_record_key": "orders-42"}})
        assert result.status_code == 200, result.text
        assert result.json()["decision"]["kind"] == "ELIGIBLE"
        assert len(result.json()["evidence_ids"]) == 2


def test_rule_ast_is_bounded_and_keeps_unknown_distinct():
    expression = {"all": [{"fact": "a", "op": "eq", "value": "ok"},
                          {"fact": "b", "op": "eq", "value": "ok"}]}
    validate_rule(expression, ["a", "b"])
    assert evaluate(expression, {"a": Fact("RESOLVED", "ok"), "b": Fact("MISSING")})[0] == "UNKNOWN"
    assert evaluate(expression, {"a": Fact("RESOLVED", "bad"), "b": Fact("MISSING")})[0] == "FALSE"
    assert evaluate(expression, {"a": Fact("RESOLVED", "ok"), "b": Fact("CONFLICT")})[0] == "CONFLICT"
    with pytest.raises(InvalidRule):
        validate_rule({"python": "__import__('os')"}, ["a"])
