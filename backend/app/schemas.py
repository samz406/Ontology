"""Typed external inputs. Authentication context is never an input field."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    max_age_seconds: int = Field(default=3600, ge=1, le=31_536_000)


class PropertyCreate(BaseModel):
    class_name: str = Field(default="Order", min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.]*$")
    data_type: Literal["string", "number", "boolean"] = "string"
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    authority_source_system_id: str | None = None


class EntityCreate(BaseModel):
    class_name: str = Field(default="Order", min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    owner_actor_id: str | None = Field(default=None, max_length=128)
    visibility: Literal["shared", "private"] = "shared"


class BindingCreate(BaseModel):
    source_system_id: str
    record_key: str = Field(min_length=1, max_length=256)
    entity_id: str
    method: Literal["manual", "verified_external_key"] = "manual"


class MappingCreate(BaseModel):
    source_system_id: str
    target_class: str = "Order"
    field_map: dict[str, str] = Field(min_length=1)
    version: int = Field(ge=1)

    @field_validator("field_map")
    @classmethod
    def bound_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 64 or any(not key or len(key) > 128 for key in value):
            raise ValueError("Mapping fields exceed configured limits")
        return value


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    applies_to: str = "Order"
    expression: dict[str, Any]
    required_facts: list[str] = Field(min_length=1)
    version: int = Field(ge=1)
    effective_from: datetime
    effective_to: datetime | None = None


class IngestEvent(BaseModel):
    source_system_id: str
    record_key: str = Field(min_length=1, max_length=256)
    source_version: int = Field(ge=1)
    valid_from: datetime
    valid_to: datetime | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    deleted: bool = False

    @field_validator("fields")
    @classmethod
    def limit_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 64 or any(len(key) > 128 or len(str(item)) > 2048 for key, item in value.items()):
            raise ValueError("Event fields exceed configured size limits")
        return value

    @field_validator("valid_from", "valid_to")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamp must contain a timezone")
        return value


class Anchor(BaseModel):
    source_system_id: str
    source_record_key: str = Field(min_length=1, max_length=256)


class InvestigationCreate(BaseModel):
    investigation_type: Literal["refund_eligibility"] = "refund_eligibility"
    anchor: Anchor
    question: str = Field(default="", max_length=2000)
    business_as_of: datetime | None = None

    @field_validator("business_as_of")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamp must contain a timezone")
        return value


def naive_utc(value: datetime) -> datetime:
    from datetime import timezone

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=None).isoformat(timespec="seconds") + "Z" if value is not None else None
