"""Composition root: builds the long-lived collaborators shared by the API, worker and CLI."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from afterstate.config import Settings, get_settings
from afterstate.db.session import get_sessionmaker
from afterstate.engine.cleanup import CleanupService
from afterstate.engine.pgtools import locate_pg_tools
from afterstate.engine.pipeline import Services
from afterstate.engine.store import RunStore
from afterstate.storage.local import LocalArtifactStorage


def build_services(settings: Settings | None = None, sf: sessionmaker[Session] | None = None) -> Services:
    settings = settings or get_settings()
    sf = sf or get_sessionmaker()
    store = RunStore(sf)
    return Services(
        settings=settings,
        sf=sf,
        store=store,
        storage=LocalArtifactStorage(settings.artifact_dir),
        tools=locate_pg_tools(settings.pg_bin_dir),
        cleanup=CleanupService(store, sf, settings.scratch_dir),
    )
