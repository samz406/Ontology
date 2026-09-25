"""Tenant and actor identity are derived solely from server-stored bearer keys."""

import hashlib
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import db_session
from .models import ApiKey, Entity, PropertyDefinition


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor_id: str
    role: str
    source_system_id: str | None = None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_principal(request: Request, db: Session = Depends(db_session)) -> Principal:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or len(header) <= 7:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    key = db.scalar(select(ApiKey).where(ApiKey.token_hash == hash_token(header[7:]), ApiKey.active.is_(True)))
    if key is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return Principal(key.tenant_id, key.actor_id, key.role, key.source_system_id)


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return principal


def can_view_entity(principal: Principal, entity: Entity) -> bool:
    return (
        entity.tenant_id == principal.tenant_id
        and entity.status == "ACTIVE"
        and principal.role != "source"
        and (principal.role == "admin" or entity.visibility == "shared" or entity.owner_actor_id == principal.actor_id)
    )


def assert_visible_entity(principal: Principal, entity: Entity | None) -> Entity:
    if entity is None or not can_view_entity(principal, entity):
        # Same response hides existence of inaccessible objects.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entity not found")
    return entity


def assert_property_access(principal: Principal, prop: PropertyDefinition | None) -> None:
    if prop is None or prop.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unregistered property")
    if principal.role == "source" or (prop.sensitivity == "restricted" and principal.role != "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Property access denied")

