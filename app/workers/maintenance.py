import shutil
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.models import (Application, AuthSession, Connection, Control, HumanTask, Job, Opportunity, Source,
                        SourceOccurrence, now_iso)
from app.services import audit, must_get
from app.workers.notifications import notify


def health_check(db, settings, *, disk_usage=shutil.disk_usage):
    current = datetime.now(UTC)
    free = disk_usage(settings.data_dir).free
    issues = []
    with db.write() as session:
        controls = must_get(session, Control, "global")
        from app.workers.budget import BudgetExceeded, check_budget
        try:
            check_budget(session, settings)
        except BudgetExceeded as exc:
            issues.append(str(exc))
            notify(session, "budget-limit", "Processing budget reached", str(exc), urgency="attention")
        for source in session.scalars(select(Source)):
            last = source.data.get("last_success_at")
            if source.status == "healthy" and last and current - datetime.fromisoformat(last) > timedelta(
                    seconds=source.data.get("poll_interval_seconds", 900) * 2):
                source.status = "stale"
        if free < settings.minimum_free_disk_bytes:
            issues.append("disk_space_low")
            controls.data = {**controls.data, "paused": True, "operational_hold": "disk_space_low"}
            notify(session, "disk-space", "Low disk space; submissions paused",
                   f"{free} bytes free. Free disk space and explicitly resume.", urgency="urgent")
        for connection in session.scalars(select(Connection)):
            expiry = connection.data.get("refresh_expires_at") or connection.data.get("session_expires_at")
            if expiry:
                try:
                    remaining = datetime.fromisoformat(expiry) - current
                except (ValueError, TypeError):
                    remaining = timedelta(0)
                if remaining <= timedelta(days=2):
                    if remaining <= timedelta(0):
                        connection.status = "expired"
                    issues.append(f"connection_expiry:{connection.id}")
                    notify(session, f"connection-expiry:{connection.id}", "Connection needs renewal",
                           f"{connection.id}: {expiry}", urgency="urgent")
            elif connection.id == "gmail" and connection.status == "connected" and settings.google_oauth_testing:
                connected = connection.data.get("connected_at")
                if connected and current - datetime.fromisoformat(connected) >= timedelta(days=5):
                    issues.append("gmail_testing_refresh_lifetime")
                    notify(session, "gmail-testing-lifetime", "Gmail testing credentials may expire",
                           "External OAuth apps in Testing commonly have seven-day refresh tokens. Reconnect securely or complete Google production requirements.", urgency="attention")
        for opportunity in session.scalars(select(Opportunity).where(Opportunity.availability == "open")):
            value = opportunity.data.get("deadline")
            if not value:
                continue
            try:
                deadline = datetime.fromisoformat(value)
                remaining = deadline - current
            except (ValueError, TypeError):
                continue
            if remaining <= timedelta(0):
                opportunity.availability = "expired"
            elif remaining < timedelta(days=2):
                application = session.scalar(select(Application).where(Application.opportunity_id == opportunity.id))
                if application and application.state in {"discovered", "assessing", "drafting", "ready", "needs_human"}:
                    notify(session, f"deadline:{opportunity.id}", "Application deadline approaching",
                        f"{opportunity.title}: {value}", urgency="urgent", context={"application_id": application.id})
                    for task in session.scalars(select(HumanTask).where(HumanTask.application_id == application.id,
                                                                      HumanTask.status == "open")):
                        task.priority = min(task.priority, 5)
        lagged = session.scalars(select(Job).where(Job.state == "pending",
            Job.due_at < (current - timedelta(minutes=30)).isoformat())).all()
        if lagged:
            issues.append("processing_lag")
            notify(session, "processing-lag", "Background queue is delayed", f"{len(lagged)} jobs overdue by 30 minutes.")
        controls.data = {**controls.data, "last_health_check": now_iso(), "free_disk_bytes": free, "health_issues": issues}
    return {"issues": issues, "free_disk_bytes": free}


def retention(db, settings):
    with db.write() as session:
        days = must_get(session, Control, "global").data.get("retention_days", 90)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        session.execute(delete(AuthSession).where(AuthSession.expires_at < now_iso()))
        removed = 0
        for occurrence in session.scalars(select(SourceOccurrence)):
            snapshots = occurrence.data.get("snapshots", [])
            keep = [x for i, x in enumerate(snapshots) if i == len(snapshots) - 1 or x.get("captured_at", "") >= cutoff.isoformat()]
            removed += len(snapshots) - len(keep)
            occurrence.data = {**occurrence.data, "snapshots": keep}
        audit(session, "owner", "retention_applied", removed_listing_snapshots=removed)
    traces = settings.data_dir / "browser-traces"
    files = 0
    if traces.is_dir():
        for path in traces.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(traces.resolve()) and \
                    path.stat().st_mtime < cutoff.timestamp():
                path.unlink()
                files += 1
    return {"removed_listing_snapshots": removed, "removed_temporary_files": files}


def mailbox_maintenance(db, settings):
    from app.applications.reconciliation import poll_mailbox, reconcile_application
    from app.integrations.gmail import get_gmail_provider
    with db.read() as session:
        connection = session.get(Connection, "gmail")
        if not connection or connection.status not in {"connected", "error"} or not connection.encrypted_secret:
            return {"status": "unconfigured"}
        candidates = [x.id for x in session.scalars(select(Application).where(
            Application.state == "submission_uncertain"))]
    provider = get_gmail_provider(db, settings)
    for identity in candidates:
        reconcile_application(db, settings, identity, provider=provider)
    return poll_mailbox(db, settings, provider=provider)


def queue_ready(db, settings):
    from app.applications.dispatch import queue_application
    if not settings.live_submissions_enabled:
        return {"queued": 0, "reason": "live_submissions_disabled"}
    with db.read() as session:
        candidates = [x.id for x in session.scalars(select(Application).where(
            Application.state.in_(["drafting", "ready", "needs_human"])))]
    return {"queued": sum(queue_application(db, settings, x)["queued"] for x in candidates)}


def scheduled_backup(db, settings):
    if not settings.backup_key_file or not settings.backup_directory:
        return {"status": "unconfigured"}
    from ops.backup import backup
    day = datetime.now(UTC).strftime("%Y%m%d")
    destination = settings.backup_directory / f"autopilot-{day}.encrypted"
    if destination.exists():
        return {"status": "already_backed_up"}
    result = backup(settings.data_dir, settings.db_url, destination, settings.backup_key_file)
    with db.write() as session:
        audit(session, "owner", "backup_completed", bytes=result["bytes"], date=day)
        controls = must_get(session, Control, "global")
        controls.data = {**controls.data, "last_backup_at": now_iso()}
    return {"status": "encrypted_backup_created"}
