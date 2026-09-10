from sqlalchemy import select

from app.matching.engine import assess
from app.models import Application, Assessment, Candidate, DocumentArtifact, HumanTask, Opportunity, Requirement, now_iso
from app.services import PRESERVE_STATES, audit, enqueue_job, must_get, profile_view, record_json, task


def assess_opportunity(db, identity):
    with db.write() as session:
        opportunity = must_get(session, Opportunity, identity)
        profile = profile_view(session)
        profile["attachments"] = [record_json(x) for x in session.scalars(select(DocumentArtifact).where(
            DocumentArtifact.packet_id.is_(None)))]
        result = assess(profile, record_json(opportunity))
        candidate = must_get(session, Candidate, "owner")
        assessment = Assessment(opportunity_id=identity, profile_revision=candidate.revision,
            posting_hash=opportunity.content_hash, data=result)
        session.add(assessment)
        session.flush()
        application = session.scalar(select(Application).where(Application.opportunity_id == identity))
        if not application:
            application = Application(opportunity_id=identity)
            session.add(application)
            session.flush()
        for requirement in result["requirements"]:
            session.add(Requirement(opportunity_id=identity, data={**requirement, "assessment_id": assessment.id}))
        open_reasons = set(result["blockers"])
        for item in session.scalars(select(HumanTask).where(HumanTask.application_id == application.id)):
            if item.group_key.startswith("assessment:") and item.data.get("reason") not in open_reasons:
                item.status = "resolved"
                item.data = {**item.data, "resolution": "Reassessment resolved this blocker", "resolved_at": now_iso()}
        for reason in open_reasons:
            requirement = next((r for r in result["requirements"] if "requirement:" + r["kind"] == reason), None)
            key = f"assessment:{identity}:{reason}"
            task(session, key, reason.replace("_", " ").replace(":", ": "), reason,
                application=application, question=requirement["reason"] if requirement else "Add verified relocation/deadline evidence",
                context={"url": opportunity.url, "requirement": requirement, "assessment_id": assessment.id})
        if application.state not in PRESERVE_STATES and not application.data.get("has_submission_evidence"):
            application.state = "skipped" if result["recommendation"] == "skip" else "needs_human" if open_reasons else "drafting"
            application.updated_at = now_iso()
            if result["recommendation"] != "skip" and result["fit"] in {"strong", "plausible"}:
                enqueue_job(session, f"prepare:{identity}", "prepare", {"opportunity_id": identity})
        audit(session, application.id, "assessment_created", assessment_id=assessment.id,
              eligibility=result["eligibility"], relocation=result["relocation"], fit=result["fit"])
        return record_json(assessment)
