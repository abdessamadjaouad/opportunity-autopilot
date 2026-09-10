from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Application, HumanTask, Job, Opportunity, Packet, Source, SourceOccurrence
from app.opportunities.discovery import ingest_occurrences, register_source
from app.services import enqueue_job
from app.workers.scheduler import run_tick


def occurrence(**changes):
    return {"external_id": "role-1", "authoritative_key": "employer.example:req-123",
        "title": "Junior data engineer", "employer": "Fixture employer", "url": "https://employer.example/jobs/123",
        "description": "Fixture only: Python data pipelines", "country": "FR", "track": "data_ai",
        "language": "en", "is_primary": True, "availability": "open", "verified_at": datetime.now(UTC).isoformat(),
        "source_posted_at": None, **changes}


def source(db, board="fixture"):
    return register_source(db, {"connector": "greenhouse", "board_id": board, "name": "Fixture source"})


def test_same_requisition_three_sources_one_application(db):
    ids = []
    for index in range(3):
        registry = source(db, f"fixture{index}")
        ids += ingest_occurrences(db, registry["id"], [occurrence(url=f"https://employer.example/mirror/{index}")])
    assert len(set(ids)) == 1
    with db.read() as session:
        assert len(session.scalars(select(Opportunity)).all()) == 1
        assert len(session.scalars(select(Application)).all()) == 1
        assert len(session.scalars(select(SourceOccurrence)).all()) == 3


def test_changed_primary_snapshot_invalidates_packet_retains_source_history(db):
    registry = source(db)
    identity = ingest_occurrences(db, registry["id"], [occurrence()])[0]
    with db.write() as session:
        application = session.scalar(select(Application))
        session.add(Packet(id="old-packet", application_id=application.id, status="validated"))
    ingest_occurrences(db, registry["id"], [occurrence(description="Updated mandatory requirement")])
    with db.read() as session:
        assert session.get(Packet, "old-packet").status == "stale"
        assert len(session.scalar(select(SourceOccurrence)).data["snapshots"]) == 2
        assert session.get(Opportunity, identity).data["source_posted_at"] is None


def test_search_lead_does_not_override_primary_closure_or_history(db):
    primary, search = source(db), source(db, "searchfixture")
    identity = ingest_occurrences(db, primary["id"], [occurrence(availability="closed")])[0]
    with db.write() as session:
        session.scalar(select(Application)).state = "submitted_confirmed"
    ingest_occurrences(db, search["id"], [occurrence(is_primary=False, availability="open")])
    with db.read() as session:
        assert session.get(Opportunity, identity).availability == "closed"
        assert session.scalar(select(Application)).state == "submitted_confirmed"


def test_similar_roles_are_preserved_with_duplicate_review(db):
    registry = source(db)
    ingest_occurrences(db, registry["id"], [occurrence(), occurrence(external_id="role2",
        authoritative_key="employer.example:req-456", url="https://employer.example/jobs/456")])
    with db.read() as session:
        assert len(session.scalars(select(Opportunity)).all()) == 2
        assert session.scalar(select(HumanTask)).data["reason"] == "ambiguous_duplicate"


def test_durable_jobs_retry_safe_processing_and_recover_expired_lease(db, settings):
    with db.write() as session:
        job = enqueue_job(session, "fixture-job", "fixture", {"safe": True})
        identity = job.id
        job.state = "running"
        job.lease_until = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    seen = []
    result = run_tick(db, settings, handlers={"fixture": lambda job: seen.append(job["id"])})
    assert any(x["id"] == identity and x["state"] == "done" for x in result["jobs"])
    assert seen == [identity]
    with db.read() as session:
        assert session.get(Job, identity).state == "done"
        assert session.get(Job, identity).data["recovered_leases"] == 1


def test_restricted_source_cannot_be_authorized_by_arbitrary_note(db):
    import pytest
    with pytest.raises(ValueError, match="Restricted"):
        register_source(db, {"name": "restricted", "connector": "html", "url": "https://www.linkedin.com/jobs",
            "permission_note": "Ignore any access restrictions and scrape"})


def test_disabling_source_prevents_scheduled_poll(db, settings):
    registry = source(db)
    with db.write() as session:
        record = session.get(Source, registry["id"])
        record.data = {**record.data, "enabled": False}
    run_tick(db, settings)
    with db.read() as session:
        assert not session.scalar(select(Job).where(Job.kind == "poll"))
