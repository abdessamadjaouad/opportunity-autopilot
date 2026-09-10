"""Run with TEST_POSTGRES_URL pointing to an isolated disposable database."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db import Database
from app.models import Application, AuditEvent, Control
from app.services import add_opportunity


@pytest.mark.postgres
def test_postgres_migrations_unique_applications_and_serialized_controls():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    db = Database(url)
    db.initialize()
    db.initialize()
    with db.read() as session:
        assert session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
    def create(_):
        return add_opportunity(db, {"url": "https://fixture.example/jobs/concurrency", "title": "Fixture only",
                                    "employer": "PostgreSQL test"})["application_id"]
    with ThreadPoolExecutor(max_workers=6) as executor:
        identities = list(executor.map(create, range(12)))
    assert len(set(identities)) == 1
    def increment(_):
        with db.write() as session:
            controls = session.get(Control, "global")
            controls.data = {**controls.data, "concurrency_counter": controls.data.get("concurrency_counter", 0) + 1}
    with db.write() as session:
        controls = session.get(Control, "global")
        controls.data = {**controls.data, "concurrency_counter": 0}
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(increment, range(12)))
    with db.read() as session:
        assert session.get(Control, "global").data["concurrency_counter"] == 12
        assert session.scalar(select(Application).where(Application.id == identities[0]))
        event = session.scalar(select(AuditEvent).limit(1))
    with pytest.raises(DBAPIError, match="append-only"):
        with db.write() as session:
            session.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event.id})
    db.engine.dispose()
