"""Engine lifecycle and one-session-per-request/worker-job boundaries."""

import os
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./ontology.db")


def make_engine(url: str):
    options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, **options)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_dev_schema(engine) -> None:
    Base.metadata.create_all(engine)


def db_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session

