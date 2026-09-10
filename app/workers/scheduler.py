import logging
import signal
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Control, Job, Source, now_iso, uid
from app.services import enqueue_job, must_get, record_json

logger = logging.getLogger(__name__)
PERIODIC = {"health": 300, "mailbox": 300, "digest": 3600, "retention": 86400, "queue_ready": 60, "backup": 86400}


def schedule_due(db):
    current = now_iso()
    with db.write() as session:
        for kind, interval in PERIODIC.items():
            job = session.scalar(select(Job).where(Job.key == f"periodic:{kind}"))
            if not job or job.state == "done" and job.due_at <= current:
                enqueue_job(session, f"periodic:{kind}", kind, {"interval": interval})
        for source in session.scalars(select(Source)):
            if (source.data.get("enabled") and source.data.get("permission_status") == "permitted" and
                    (source.data.get("next_check_at") or current) <= current):
                enqueue_job(session, f"poll:{source.id}", "poll", {"source_id": source.id})
        for job in session.scalars(select(Job).where(Job.state == "running", Job.lease_until < current)):
            job.state = "pending"
            job.data = {**job.data, "recovered_leases": job.data.get("recovered_leases", 0) + 1}
            job.due_at = current


def claim_job(db):
    with db.write() as session:
        job = session.scalar(select(Job).where(Job.state == "pending", Job.due_at <= now_iso())
                             .order_by(Job.due_at).limit(1))
        if not job:
            return None
        job.state = "running"
        job.lease_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        job.data = {**job.data, "attempts": job.data.get("attempts", 0) + 1, "lease_token": uid()}
        session.flush()
        return record_json(job)


def run_tick(db, settings, *, max_jobs=20, handlers=None):
    from app.applications.dispatch import recover_inflight
    recover_inflight(db)
    schedule_due(db)
    completed = []
    for _ in range(max_jobs):
        job = claim_job(db)
        if not job:
            break
        try:
            if handlers and job["kind"] in handlers:
                result = handlers[job["kind"]](job)
            elif job["kind"] == "poll":
                from app.opportunities.discovery import poll_source
                result = poll_source(db, settings, job["source_id"])
            elif job["kind"] == "assess":
                from app.matching.service import assess_opportunity
                result = assess_opportunity(db, job["opportunity_id"])
            elif job["kind"] == "verify":
                from app.opportunities.discovery import verify_opportunity
                result = verify_opportunity(db, settings, job["opportunity_id"])
            elif job["kind"] == "prepare":
                from app.documents.service import prepare_packet
                result = prepare_packet(db, settings, job["opportunity_id"])
            elif job["kind"] in {"health", "retention", "mailbox", "queue_ready"}:
                from app.workers.maintenance import health_check, mailbox_maintenance, queue_ready, retention
                result = {"health": health_check, "retention": retention,
                          "mailbox": mailbox_maintenance, "queue_ready": queue_ready}[job["kind"]](db, settings)
            elif job["kind"] == "digest":
                from app.workers.notifications import build_digest
                result = build_digest(db, settings)
            elif job["kind"] == "backup":
                from app.workers.maintenance import scheduled_backup
                result = scheduled_backup(db, settings)
            else:
                raise ValueError("Unsupported durable job kind")
            with db.write() as session:
                stored = must_get(session, Job, job["id"])
                if stored.data.get("lease_token") != job["lease_token"]:
                    continue
                stored.state, stored.lease_until = "done", None
                stored.due_at = (datetime.now(UTC) + timedelta(seconds=job.get("interval", 0))).isoformat()
                rerun = stored.data.get("rerun")
                stored.data = {**stored.data, "last_finished_at": now_iso(), "attempts": 0,
                    "last_result": {k: v for k, v in (result or {}).items() if k in {"id", "status", "processed", "queued", "issues"}},
                    "error": None}
                if rerun is not None:
                    stored.state, stored.data, stored.due_at = "pending", rerun, now_iso()
            completed.append({"id": job["id"], "state": "done"})
        except Exception as exc:
            # Only retry processing/discovery here. Submission effects have a separate outbox service.
            with db.write() as session:
                stored = must_get(session, Job, job["id"])
                if stored.data.get("lease_token") != job["lease_token"]:
                    continue
                attempts = stored.data.get("attempts", 1)
                stored.state = "pending" if attempts < 5 else "failed"
                stored.lease_until = None
                stored.due_at = (datetime.now(UTC) + timedelta(seconds=min(3600, 30 * 2 ** attempts))).isoformat()
                stored.data = {**stored.data, "error": type(exc).__name__}
                from app.workers.notifications import notify
                notify(session, f"job-error:{stored.id}", "Background processing needs attention",
                       f"{stored.kind}: {type(exc).__name__}", urgency="attention", context={"job_id": stored.id})
            completed.append({"id": job["id"], "state": "retry_pending", "error": type(exc).__name__})
    with db.write() as session:
        control = must_get(session, Control, "global")
        control.data = {**control.data, "worker_heartbeat": now_iso()}
    return {"processed": len(completed), "jobs": completed}


def run_loop(db, settings):
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        from app.applications.dispatch import dispatch_next
        from app.applications.notifications import dispatch_notification
        from app.applications.followups import dispatch_followup
        for action in (run_tick, dispatch_next, dispatch_notification, dispatch_followup):
            try:
                action(db, settings)
            except Exception as exc:
                from app.workers.notifications import notify
                with db.write() as session:
                    notify(session, f"worker:{action.__name__}", "Background operation needs attention",
                           type(exc).__name__, urgency="attention")
        for _ in range(30):
            if stopped:
                break
            time.sleep(1)
