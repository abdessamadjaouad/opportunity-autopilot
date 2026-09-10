"""Transactional workflow services shared by API and durable workers."""
import hashlib
import json
from collections import Counter

from sqlalchemy import select

from app.db import DEFAULT_POLICY
from app.models import (Answer, Application, Assessment, AuditEvent, Candidate,
    Connection, Control, DocumentArtifact, EmailMessage, HumanTask, Job, Notification, Opportunity,
    OutboxEvent, Packet, PolicyRevision, ProfileFact, ProfileRevision, Source,
    SourceOccurrence, SourceRun, SubmissionAttempt, now_iso, uid)
from app.security.urls import canonical_url

PRESERVE_STATES = {"submitting", "submission_uncertain", "sent_unconfirmed", "submitted_confirmed",
                   "interview", "offer", "rejected", "withdrawn"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def record_json(record):
    if record is None:
        return None
    output = dict(record.data or {})
    for column in record.__table__.columns:
        if column.name not in {"data", "encrypted_secret", "path"}:
            output[column.name] = getattr(record, column.name)
    return output


def must_get(session, cls, identity):
    record = session.get(cls, identity)
    if record is None:
        raise LookupError(f"{cls.__name__} not found")
    return record


def audit(session, entity_id, kind, **data):
    session.add(AuditEvent(entity_id=entity_id, kind=kind, data=data))


def latest_policy(session):
    return session.scalar(select(PolicyRevision).order_by(PolicyRevision.created_at.desc()).limit(1))


def task(session, key, title, reason, *, application=None, question="", context=None, priority=50):
    item = session.scalar(select(HumanTask).where(HumanTask.group_key == key))
    data = {"title": title, "reason": reason, "question": question or title, "context": context or {}}
    if application:
        data["opportunity_id"] = application.opportunity_id
    if item:
        item.status = "open"
        item.data = {**item.data, **data}
    else:
        item = HumanTask(group_key=key, application_id=application.id if application else None,
                         priority=priority, data=data)
        session.add(item)
    session.flush()
    return item


def invalidate(session, *, reason, opportunity_id=None):
    applications = session.scalars(select(Application).where(Application.opportunity_id == opportunity_id)
        if opportunity_id else select(Application)).all()
    for application in applications:
        for packet in session.scalars(select(Packet).where(Packet.application_id == application.id)):
            if packet.status != "stale":
                packet.status = "stale"
                audit(session, application.id, "packet_invalidated", packet_id=packet.id, reason=reason)
        for outbox in session.scalars(select(OutboxEvent).where(
                OutboxEvent.application_id == application.id, OutboxEvent.state == "pending")):
            outbox.state = "cancelled"
            outbox.data = {**outbox.data, "reason": reason}
        if application.state not in PRESERVE_STATES and not application.data.get("has_submission_evidence"):
            application.state = "assessing"
            application.updated_at = now_iso()
            opportunity = session.get(Opportunity, application.opportunity_id)
            if opportunity and not opportunity.data.get("history_import"):
                enqueue_job(session, f"assess:{opportunity.id}", "assess", {"opportunity_id": opportunity.id})


def snapshot_profile(session, reason):
    candidate = must_get(session, Candidate, "owner")
    revision = ProfileRevision(id=uid(), data={"reason": reason, "facts": [record_json(x) for x in
        session.scalars(select(ProfileFact)).all()], "answers": [record_json(x) for x in
        session.scalars(select(Answer)).all()], "preferences": candidate.data.get("preferences", {})})
    session.add(revision)
    candidate.revision = revision.id
    audit(session, "owner", "profile_revised", revision=revision.id, reason=reason)
    invalidate(session, reason=reason)
    return revision


def import_profile(db, portfolio_root):
    from app.profile import build_profile_evidence
    evidence = build_profile_evidence(portfolio_root)
    with db.write() as session:
        candidate = must_get(session, Candidate, "owner")
        candidate.name = evidence["name"]
        changes = 0
        for fact in evidence["facts"]:
            previous = session.scalar(select(ProfileFact).where(ProfileFact.key == fact["key"]))
            payload = {k: v for k, v in fact.items() if k not in {"key", "status", "revision"}}
            if previous is None:
                previous = ProfileFact(key=fact["key"], status=fact["status"], data=payload)
                session.add(previous)
                changes += 1
            elif digest([{k: v for k, v in s.items() if k != "extracted_at"} for s in
                         previous.data.get("sources", [])]) != digest([{k: v for k, v in s.items()
                         if k != "extracted_at"} for s in payload.get("sources", [])]):
                # Reimport never erases an owner correction or changes confirmed truth silently.
                previous.data = {**previous.data, "new_source_evidence": payload}
                previous.status = "conflicting"
                previous.revision += 1
                changes += 1
            session.flush()
            if previous.status != "confirmed":
                task(session, f"profile:{fact['key']}", fact.get("label", fact["key"]),
                     "profile_conflicting" if previous.status == "conflicting" else "profile_unconfirmed",
                     question="Confirm or correct this fact in Documents and profile.",
                     context={"fact_id": previous.id, "fact_key": fact["key"]})
        for history in evidence.get("history", []):
            key = "history:" + digest(history.get("source_path", history))
            if session.scalar(select(Opportunity).where(Opportunity.canonical_key == key)):
                continue
            opportunity = Opportunity(id=uid(), canonical_key=key, title=history["title"],
                employer=history.get("employer", "Historical draft"), url=history.get("url") or "",
                content_hash=digest(history), data={**history, "history_import": True,
                    "first_seen_at": now_iso(), "language": "fr", "is_fixture": False})
            session.add(opportunity)
            session.flush()
            application = Application(id=uid(), opportunity_id=opportunity.id, state="drafting",
                data={"import_evidence": history.get("source_path"), "submission_evidence": None})
            session.add(application)
            audit(session, application.id, "historical_draft_imported", source=history.get("source_path"),
                  note="No transmission evidence; imported as draft")
        if changes:
            snapshot_profile(session, "source_import")
        return {"imported": changes, "warnings": evidence.get("warnings", [])}


def profile_view(session):
    candidate = record_json(must_get(session, Candidate, "owner"))
    facts = [record_json(x) for x in session.scalars(select(ProfileFact).order_by(ProfileFact.key))]
    return {"candidate": candidate, "facts": facts,
        "answers": [record_json(x) for x in session.scalars(select(Answer))],
        "revisions": [record_json(x) for x in session.scalars(select(ProfileRevision)
            .order_by(ProfileRevision.created_at.desc()).limit(30))],
        "unresolved_count": sum(x["status"] != "confirmed" for x in facts)}


def confirm_fact(db, identity, value, note):
    with db.write() as session:
        fact = must_get(session, ProfileFact, identity)
        fact.data = {**fact.data, "value": value, "owner_correction": value, "owner_note": note,
                     "confirmed_at": now_iso()}
        fact.status = "confirmed"
        fact.revision += 1
        item = session.scalar(select(HumanTask).where(HumanTask.group_key == f"profile:{fact.key}"))
        if item:
            item.status = "resolved"
            item.data = {**item.data, "resolution": note or "Confirmed in profile", "resolved_at": now_iso()}
        snapshot_profile(session, f"fact_corrected:{fact.key}")
        return record_json(fact)


def save_answer(session, key, value, context="global", sensitive=False, consent=False):
    answer = session.scalar(select(Answer).where(Answer.key == key, Answer.context == context))
    payload = {"value": value, "confirmed": True, "sensitive": sensitive, "consent": consent,
               "revision": uid(), "confirmed_at": now_iso()}
    if answer:
        answer.data = payload
    else:
        answer = Answer(key=key, context=context, data=payload)
        session.add(answer)
    session.flush()
    audit(session, "owner", "answer_confirmed", answer_id=answer.id, key=key, context=context)
    snapshot_profile(session, f"answer_changed:{key}")
    return record_json(answer)


def add_opportunity(db, values):
    url = canonical_url(values["url"])
    with db.write() as session:
        previous = session.scalar(select(Opportunity).where(Opportunity.canonical_key == "url:" + url))
        if previous:
            return opportunity_view(session, previous)
        current = now_iso()
        data = {"description": values.get("description", ""), "language": values.get("language", "en"),
            "first_seen_at": current, "last_seen_at": current, "last_verified_at": None,
            "source_posted_at": None, "deadline": None, "deadline_text": "", "deadline_timezone": None,
            "is_fixture": False, "origin": "manual", "fit": "weak", "eligibility": "unknown",
            "relocation": "unknown"}
        opportunity = Opportunity(id=uid(), canonical_key="url:" + url, title=values["title"],
            employer=values["employer"], country=values.get("country", ""), track=values.get("track", "data_ai"),
            route=values.get("route", "manual"), url=url, content_hash=digest(data), data=data)
        session.add(opportunity)
        session.flush()
        session.add(SourceOccurrence(opportunity_id=opportunity.id, occurrence_key="manual:" + url,
            data={"url": url, "snapshot": values, "first_seen_at": current, "last_seen_at": current,
                  "content_hash": opportunity.content_hash, "source_name": "Manual import"}))
        application = Application(id=uid(), opportunity_id=opportunity.id)
        session.add(application)
        session.flush()
        audit(session, application.id, "opportunity_imported", url=url, availability="unverified")
        task(session, f"verify:{opportunity.id}", "Verify the official listing", "listing_unverified",
             application=application, context={"url": url})
        return opportunity_view(session, opportunity)


def opportunity_view(session, opportunity):
    data = record_json(opportunity)
    application = session.scalar(select(Application).where(Application.opportunity_id == opportunity.id))
    data.update(application_id=application.id if application else None,
                application_state=application.state if application else None)
    assessment = session.scalar(select(Assessment).where(Assessment.opportunity_id == opportunity.id,
        Assessment.posting_hash == opportunity.content_hash,
        Assessment.profile_revision == must_get(session, Candidate, "owner").revision)
        .order_by(Assessment.created_at.desc()).limit(1))
    data["assessment"] = record_json(assessment)
    if assessment:
        data.update({key: assessment.data.get(key, "unknown") for key in ("fit", "eligibility", "relocation")})
    else:
        data.update(fit="weak", eligibility="unknown", relocation="unknown")
    return data


def packet_view(session, packet):
    application = must_get(session, Application, packet.application_id)
    opportunity = must_get(session, Opportunity, application.opportunity_id)
    artifacts = session.scalars(select(DocumentArtifact).where(DocumentArtifact.packet_id == packet.id)).all()
    artifacts += [must_get(session, DocumentArtifact, x) for x in packet.data.get("supplied_artifact_ids", [])]
    return {**record_json(packet), "title": opportunity.title, "artifacts": [record_json(x) for x in artifacts]}


def application_view(session, application, detail=False):
    opportunity = must_get(session, Opportunity, application.opportunity_id)
    value = {**record_json(application), "title": opportunity.title, "employer": opportunity.employer,
             "country": opportunity.country, "route": opportunity.route, "url": opportunity.url,
             "history_import": opportunity.data.get("history_import", False)}
    if detail:
        associated_ids = {event.entity_id for event in session.scalars(select(AuditEvent).where(
            AuditEvent.kind == "email_associated")) if event.data.get("application_id") == application.id}
        value.update(timeline=[record_json(x) for x in session.scalars(select(AuditEvent).where(
            AuditEvent.entity_id == application.id).order_by(AuditEvent.created_at))],
            packets=[packet_view(session, x) for x in session.scalars(select(Packet).where(
                Packet.application_id == application.id).order_by(Packet.created_at.desc()))],
            tasks=[record_json(x) for x in session.scalars(select(HumanTask).where(
                HumanTask.application_id == application.id))],
            attempts=[record_json(x) for x in session.scalars(select(SubmissionAttempt).where(
                SubmissionAttempt.application_id == application.id))],
            messages=[record_json(x) for x in session.scalars(select(EmailMessage)) if x.application_id == application.id
                      or x.id in associated_ids])
    return value


def manual_submission(db, identity, evidence, reference=""):
    if len(evidence.strip()) < 10:
        raise ValueError("Describe the receipt or confirmation evidence (at least 10 characters)")
    with db.write() as session:
        application = must_get(session, Application, identity)
        if application.state == "submitting":
            raise ValueError("A dispatch has already started. Reconcile it before recording a manual submission")
        if application.state in {"sent_unconfirmed", "submission_uncertain"}:
            raise ValueError("Existing provider acceptance or uncertainty must be reconciled first")
        for event in session.scalars(select(OutboxEvent).where(OutboxEvent.application_id == identity,
                                                              OutboxEvent.state == "pending")):
            event.state = "cancelled"
            event.data = {**event.data, "reason": "owner_manual_submission"}
        application.state = "submitted_confirmed"
        application.updated_at = now_iso()
        application.data = {**application.data, "confirmation_kind": "owner_reported", "reference": reference,
                            "has_submission_evidence": True}
        audit(session, identity, "manual_submission_reported", evidence=evidence, reference=reference,
              confirmation_kind="owner_reported")
        for item in session.scalars(select(HumanTask).where(HumanTask.application_id == identity)):
            item.status = "resolved"
            item.data = {**item.data, "resolution": "Owner reported manual submission", "resolved_at": now_iso()}
        return application_view(session, application)


def resolve_task(db, identity, values):
    with db.write() as session:
        item = must_get(session, HumanTask, identity)
        if item.group_key.startswith("profile:"):
            raise ValueError("Confirm or correct the profile fact on the profile page before resuming")
        if item.data.get("reason") in {"submission_uncertain", "reconcile_submission"}:
            raise ValueError("Reconcile submission evidence; a note cannot establish acceptance or safe retry")
        if values.get("answer_key"):
            save_answer(session, values["answer_key"], values.get("answer_value"), values.get("context") or "global")
        item.status = "resolved"
        item.data = {**item.data, "resolution": values["resolution"], "resolved_at": now_iso()}
        audit(session, item.application_id or "owner", "human_task_resolved", task_id=item.id,
              resolution=values["resolution"])
        if item.application_id:
            application = must_get(session, Application, item.application_id)
            if application.state not in PRESERVE_STATES:
                application.state = "assessing"
                application.updated_at = now_iso()
                enqueue_job(session, f"assess:{application.opportunity_id}", "assess",
                            {"opportunity_id": application.opportunity_id})
        return record_json(item)


def enqueue_job(session, key, kind, data):
    job = session.scalar(select(Job).where(Job.key == key))
    if not job:
        job = Job(key=key, kind=kind, data=data)
        session.add(job)
    elif job.state != "running":
        job.state, job.due_at, job.data = "pending", now_iso(), data
    else:
        job.data = {**job.data, "rerun": data}
    session.flush()
    return job


def save_policy(db, settings, values):
    with db.write() as session:
        approved = values.pop("approve", False)
        config = {**DEFAULT_POLICY, **values, "approved": approved}
        if approved:
            if not settings.live_submissions_enabled:
                raise ValueError("Live submissions are disabled in server configuration; save a draft policy")
            if config["mode"] != "autopilot":
                raise ValueError("Only explicit autopilot mode can approve a submission policy")
            if session.scalar(select(ProfileFact).where(ProfileFact.status == "conflicting").limit(1)):
                raise ValueError("Resolve conflicting profile facts before approving a policy")
            if not session.scalar(select(ProfileFact).limit(1)):
                raise ValueError("Import and confirm the profile first")
            if not config.get("reviewed_packet_ids"):
                raise ValueError("Review representative packets before approving a policy")
            for packet_id in config["reviewed_packet_ids"]:
                packet = must_get(session, Packet, packet_id)
                from app.applications.dispatch import validate_packet_files
                if not validate_packet_files(session, settings, packet):
                    raise ValueError("Only current validated packets qualify for policy calibration")
            for key in ("allowed_countries", "allowed_tracks", "allowed_routes", "allowed_domains",
                        "allowed_document_families"):
                if not config.get(key):
                    raise ValueError(f"Set {key} explicitly")
            config["approved_at"] = now_iso()
            config["answer_revisions"] = {x.id: x.data.get("revision") for x in session.scalars(select(Answer))
                                          if x.id in config.get("approved_answers", []) and x.data.get("confirmed") is True}
            if set(config["answer_revisions"]) != set(config.get("approved_answers", [])):
                raise ValueError("Only existing confirmed answers can be approved")
            if config.get("followup_enabled") and (config.get("followup_max_count") != 1 or
                    config.get("followup_answer_id") not in config["answer_revisions"]):
                raise ValueError("Follow-up approval requires a bound of one and an approved answer-bank message")
            if "email" in config["allowed_routes"]:
                mailbox = must_get(session, Connection, "gmail")
                if mailbox.status != "connected" or not mailbox.data.get("email_address"):
                    raise ValueError("Connect the sending mailbox before approving the email route")
                config["sender_email"] = mailbox.data["email_address"].casefold()
        policy = PolicyRevision(id=uid(), data=config)
        session.add(policy)
        session.flush()
        invalidate(session, reason="policy_changed")
        audit(session, "owner", "policy_revised", policy_id=policy.id, approved=approved)
        return record_json(policy)


def automation_view(session, settings):
    controls = record_json(must_get(session, Control, "global"))
    from app.workers.budget import usage
    budget = usage(session)
    sources = session.scalars(select(Source)).all()
    applications = session.scalars(select(Application)).all()
    attempts = session.scalars(select(SubmissionAttempt)).all()
    source_data = [record_json(x) for x in sources]
    verified_times = [x.get("last_success_at") for x in source_data if x.get("last_success_at")]
    coverage = {"registered": len(sources), "healthy": sum(x.status == "healthy" for x in sources),
        "failed": sum(x.status in {"error", "rate_limited"} for x in sources),
        "last_check": max(verified_times) if verified_times else None}
    return {"controls": {**controls, "live_enabled": settings.live_submissions_enabled,
                          "paid_services_enabled": settings.paid_services_enabled},
        "policy": record_json(latest_policy(session)),
        "policy_history": [record_json(x) for x in session.scalars(select(PolicyRevision)
            .order_by(PolicyRevision.created_at.desc()).limit(20))],
        "connections": [record_json(x) for x in session.scalars(select(Connection))],
        "budget": budget, "coverage": coverage,
        "jobs": [record_json(x) for x in session.scalars(select(Job).order_by(Job.due_at).limit(100))],
        "source_runs": [record_json(x) for x in session.scalars(select(SourceRun)
            .order_by(SourceRun.created_at.desc()).limit(30))],
        "notifications": [record_json(x) for x in session.scalars(select(Notification)
            .order_by(Notification.created_at.desc()).limit(50))],
        "metrics": {"confirmed": sum(x.state == "submitted_confirmed" for x in applications),
            "uncertain": sum(x.state == "submission_uncertain" for x in applications),
            "attempts": len(attempts), "coverage_definition": "Registered sources successfully checked",
            "duplicate_send_incidents": len({x.entity_id for x in session.scalars(select(AuditEvent).where(
                AuditEvent.kind == "duplicate_send_detected"))}),
            "interviews": sum(x.state == "interview" for x in applications),
            "unique_opportunities": sum(not x.data.get("history_import") for x in session.scalars(select(Opportunity))),
            "validated_packets": sum(x.status == "validated" for x in session.scalars(select(Packet))),
            "packet_validation_failures": sum(x.status == "draft" and bool(x.data.get("validation", {}).get("blockers"))
                                               for x in session.scalars(select(Packet))),
            "human_reasons": dict(Counter(x.data.get("reason", "unknown") for x in
                session.scalars(select(HumanTask).where(HumanTask.status == "open"))))}}
