from collections import namedtuple
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import BudgetLedger, Connection, Control, Job, Notification
from app.services import enqueue_job
from app.workers.budget import BudgetExceeded, reserve
from app.workers.maintenance import health_check
from app.workers.notifications import build_digest
from app.workers.scheduler import run_tick


def test_budget_is_reserved_atomically_and_disabled_paid_work_stays_disabled(db, settings):
    with db.write() as session:
        with pytest.raises(BudgetExceeded, match="paid_services_disabled"):
            reserve(session, settings, "paid", "model", cents=1)
        control = session.get(Control, "global")
        control.data = {**control.data, "daily_request_cap": 1}
        reserve(session, settings, "first", "discovery")
        session.flush()
        reserve(session, settings, "first", "discovery")
        with pytest.raises(BudgetExceeded, match="daily_request_limit"):
            reserve(session, settings, "second", "discovery")
    with db.read() as session:
        assert len(session.scalars(select(BudgetLedger)).all()) == 1


def test_disk_pressure_pauses_and_expired_credentials_alert(db, settings):
    with db.write() as session:
        connection = session.get(Connection, "gmail")
        connection.status = "connected"
        connection.data = {"refresh_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    usage = namedtuple("Usage", "total used free")(100, 99, 1)
    result = health_check(db, settings, disk_usage=lambda _: usage)
    assert set(result["issues"]) == {"disk_space_low", "connection_expiry:gmail"}
    with db.read() as session:
        assert session.get(Control, "global").data["paused"] is True
        assert session.get(Connection, "gmail").status == "expired"
        assert len(session.scalars(select(Notification)).all()) == 2


def test_one_daily_digest_and_durable_periodic_cadence(db, settings):
    first = run_tick(db, settings)
    assert first["processed"] == 6
    assert run_tick(db, settings)["processed"] == 0
    first_digest = build_digest(db, settings)
    with db.write() as session:
        session.get(Notification, first_digest["id"]).status = "read"
    assert build_digest(db, settings)["status"] == "read"


def test_running_job_receives_changed_input_without_losing_resume(db, settings):
    with db.write() as session:
        enqueue_job(session, "work", "fixture", {"revision": 1})
    seen = []
    def handler(job):
        seen.append(job["revision"])
        if job["revision"] == 1:
            with db.write() as session:
                enqueue_job(session, "work", "fixture", {"revision": 2})
        return {"status": "done"}
    run_tick(db, settings, handlers={"fixture": handler})
    assert seen == [1, 2]
    with db.read() as session:
        job = session.scalar(select(Job).where(Job.key == "work"))
        assert job.state == "done"
        assert job.data["revision"] == 2
