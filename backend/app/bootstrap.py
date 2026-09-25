"""Explicit tenant bootstrap. The initial admin token is never logged or stored in clear text."""

import os

from sqlalchemy import select

from .db import database_url, make_engine, make_session_factory
from .models import ApiKey, ClassDefinition, Tenant
from .security import hash_token


def bootstrap(factory, tenant_id: str, tenant_name: str, admin_actor_id: str, token: str) -> None:
    if not tenant_id or len(tenant_id) > 64 or not admin_actor_id or len(admin_actor_id) > 128:
        raise ValueError("Invalid tenant or admin identifier")
    if len(token) < 32:
        raise ValueError("Bootstrap token must have at least 32 characters")
    with factory.begin() as db:
        if db.get(Tenant, tenant_id) is not None:
            raise ValueError("Tenant already exists; bootstrap cannot rotate credentials")
        if db.scalar(select(ApiKey).where(ApiKey.token_hash == hash_token(token))):
            raise ValueError("Token already exists")
        db.add(Tenant(id=tenant_id, name=tenant_name))
        db.flush()
        db.add(ClassDefinition(tenant_id=tenant_id, name="Order", description="Customer order"))
        db.add(ApiKey(tenant_id=tenant_id, actor_id=admin_actor_id, role="admin",
                      token_hash=hash_token(token)))


def main() -> None:
    token = os.environ["BOOTSTRAP_ADMIN_TOKEN"]
    engine = make_engine(database_url())
    bootstrap(make_session_factory(engine), os.getenv("BOOTSTRAP_TENANT_ID", "default"),
              os.getenv("BOOTSTRAP_TENANT_NAME", "Default tenant"),
              os.getenv("BOOTSTRAP_ADMIN_ACTOR", "admin"), token)
    print("Tenant and initial admin created. Token was not printed.")


if __name__ == "__main__":
    main()
