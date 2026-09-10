"""Packet orchestration. Rendering cannot confer eligibility or transmission evidence."""
import hashlib
import re
from pathlib import Path

from sqlalchemy import select

from app.models import (Answer, Application, Assessment, Candidate, DocumentArtifact, HumanTask, Opportunity,
                        Packet, now_iso, uid)
from app.services import (PRESERVE_STATES, audit, digest, latest_policy, must_get, packet_view,
                          profile_view, record_json, task)

FAMILIES = {"data_ai": "data_engineering", "software": "software_devops", "data_bi": "data_bi", "academic": "academic"}
SUPPLIED_KINDS = {"transcript", "certificate", "language_test", "paper"}


def requested_documents(opportunity):
    explicit = opportunity.get("requested_documents")
    if explicit is not None:
        return [{"kind": x, "required": True} if isinstance(x, str) else x for x in explicit], []
    reviewed = opportunity.get("owner_review", {}).get("requirements")
    if reviewed is not None:
        docs = [{"kind": r["value"], "required": r.get("mandatory") is True,
                 **{k: r[k] for k in ("max_words", "max_pages", "max_bytes") if k in r}}
                for r in reviewed if r.get("kind") == "document"]
        if docs:
            return docs, []
    text = opportunity.get("description", "").lower()
    documents = []
    for pattern, kind in ((r"\bcv\b|resume|curriculum vitae", "cv"),
                          (r"cover letter|lettre de motivation", "cover_letter"),
                          (r"research statement", "research_statement"),
                          (r"research proposal|projet de recherche", "proposal"),
                          (r"transcript|releve de notes", "transcript"),
                          (r"language certificate|ielts|toefl", "language_test")):
        if re.search(pattern, text):
            documents.append({"kind": kind, "required": True})
    # Until forms/owner review establish precise requirements, a preview is never upload-ready.
    return documents or [{"kind": "cv", "required": True}], ["document_requirements_unverified"]


def prepare_packet(db, settings, identity, family=None, language=None):
    from app.documents import build_documents
    from app.matching.service import assess_opportunity
    with db.read() as session:
        opportunity = record_json(must_get(session, Opportunity, identity))
        candidate = must_get(session, Candidate, "owner")
        existing_assessment = session.scalar(select(Assessment).where(Assessment.opportunity_id == identity,
            Assessment.profile_revision == candidate.revision, Assessment.posting_hash == opportunity["content_hash"])
            .order_by(Assessment.created_at.desc()).limit(1))
    if not existing_assessment:
        assess_opportunity(db, identity)
    with db.read() as session:
        opportunity_record = must_get(session, Opportunity, identity)
        opportunity = record_json(opportunity_record)
        profile = profile_view(session)
        current_policy = record_json(latest_policy(session))
        application = session.scalar(select(Application).where(Application.opportunity_id == identity))
        application_id = application.id
        opportunity["application_id"] = application_id
        assessment = session.scalar(select(Assessment).where(Assessment.opportunity_id == identity,
            Assessment.profile_revision == profile["candidate"]["revision"],
            Assessment.posting_hash == opportunity["content_hash"]).order_by(Assessment.created_at.desc()).limit(1))
        assessment_id = assessment.id
        supplies = [record_json(x) | {"path": x.path} for x in session.scalars(select(DocumentArtifact).where(
            DocumentArtifact.packet_id.is_(None)))]
        answers = [record_json(x) for x in session.scalars(select(Answer))]
    language = language or opportunity.get("language", "en")
    family = family or ("ai_mlops" if re.search(r"mlops|machine learning|\bai\b", opportunity["title"], re.I)
                        else FAMILIES.get(opportunity.get("track"), "data_engineering"))
    requested, blockers = requested_documents(opportunity)
    packet_id = uid()
    output_dir = settings.artifacts_dir / f"application-{application_id}" / f"packet-{packet_id}"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    page_limit = 2 if current_policy.get("academic_two_pages_approved") is True else 1
    if "ai_assistance_prohibited" in assessment.data.get("blockers", []):
        result = {"artifacts": [], "validation": {"valid": False, "errors": [],
            "blockers": ["ai_assistance_prohibited"]}, "selected_fact_ids": [], "text_answers": {},
            "template_version": "not-rendered-authorship-restriction"}
    else:
        try:
            result = build_documents(profile, opportunity, output_dir, family=family, language=language,
                requested_documents=[x for x in requested if x.get("kind") not in SUPPLIED_KINDS],
                academic_page_limit=page_limit, tex_sandbox=settings.tex_sandbox)
        except (OSError, RuntimeError, ValueError) as exc:
            result = {"artifacts": [], "validation": {"valid": False, "errors": [type(exc).__name__],
                "blockers": ["document_render_failed"]}, "selected_fact_ids": [], "text_answers": {},
                "template_version": "render-failed"}
    validation = result["validation"]
    blockers += validation.get("blockers", [])
    supplied_artifacts = []
    for requirement in requested:
        if requirement.get("kind") not in SUPPLIED_KINDS:
            continue
        artifact = next((x for x in supplies if x.get("kind") == requirement["kind"]), None)
        if not artifact:
            if requirement.get("required") is True:
                blockers.append("missing_document:" + requirement["kind"])
            continue
        approved = any(x.get("key") == f"document_disclosure:{artifact['id']}" and x.get("value") is True
            and x.get("confirmed") is True and x.get("consent") is True for x in answers)
        if not approved:
            blockers.append("document_disclosure_unapproved:" + artifact["id"])
        if requirement.get("max_bytes") and artifact["size_bytes"] > requirement["max_bytes"]:
            blockers.append("document_size_limit:" + requirement["kind"])
        if requirement.get("max_pages") or requirement.get("max_words"):
            from pypdf import PdfReader
            reader = PdfReader(artifact["path"])
            if requirement.get("max_pages") and len(reader.pages) > requirement["max_pages"]:
                blockers.append("document_page_limit:" + requirement["kind"])
            if requirement.get("max_words") and sum(len((p.extract_text() or "").split()) for p in reader.pages) > requirement["max_words"]:
                blockers.append("document_word_limit:" + requirement["kind"])
        supplied_artifacts.append(artifact)
    artifacts = result.get("artifacts", []) + supplied_artifacts
    selected_ids = result.get("selected_fact_ids", [])
    selected_facts = [x for x in profile["facts"] if x["id"] in selected_ids or x["key"] in selected_ids]
    if not selected_facts:
        blockers.append("no_profile_facts_selected")
    if any(x["status"] != "confirmed" for x in selected_facts):
        blockers.append("selected_profile_facts_unconfirmed")
    validation = {**validation, "blockers": sorted(set(blockers))}
    validation["valid"] = validation.get("valid") is True and not validation["blockers"] and not validation.get("errors")
    used_answer_ids = {x["id"] for x in answers if x.get("confirmed") is True and (
        (x.get("context") == identity and x.get("key") in {"document." + r["kind"] for r in requested}) or
        x.get("key") in {f"document_disclosure:{a['id']}" for a in supplied_artifacts})}
    if opportunity.get("route") == "browser":
        used_answer_ids.update(x["id"] for x in answers if x["id"] in current_policy.get("approved_answers", [])
                              and x.get("confirmed") is True and x.get("context", "global") in {"global", identity})
    manifest = {"application_id": application_id, "assessment_id": assessment_id,
        "posting_hash": opportunity["content_hash"], "profile_revision": profile["candidate"]["revision"],
        "policy_revision": current_policy["id"], "selected_facts": [{"id": x["id"], "key": x["key"],
            "revision": x["revision"], "value_hash": digest(x["value"]), "status": x["status"]} for x in selected_facts],
        "requested_documents": requested, "answers": result.get("text_answers", {}),
        "answer_revisions": {x["id"]: x.get("revision") for x in answers if x["id"] in used_answer_ids},
        "document_hashes": [{"filename": x["filename"], "sha256": x["sha256"], "kind": x["kind"]} for x in artifacts],
        "template_version": result.get("template_version"), "model_version": "none-deterministic",
        "destination": opportunity.get("destination", ""), "job_reference": opportunity.get("external_id", ""),
        "subject": opportunity.get("required_subject") or f"Application: {opportunity['title']} [{application_id[:10]}]",
        "language": language, "family": family, "validation": validation, "created_at": now_iso()}
    with db.write() as session:
        latest_opportunity = must_get(session, Opportunity, identity)
        latest_candidate = must_get(session, Candidate, "owner")
        stale = (latest_opportunity.content_hash != manifest["posting_hash"] or
                 latest_candidate.revision != manifest["profile_revision"] or latest_policy(session).id != manifest["policy_revision"])
        packet = Packet(id=packet_id, application_id=application_id,
            status="stale" if stale else "validated" if validation["valid"] else "draft",
            data={"language": language, "family": family, "manifest": manifest,
                  "manifest_hash": digest(manifest), "validation": validation})
        session.add(packet)
        session.flush()
        for artifact in result.get("artifacts", []):
            path = Path(artifact["path"]).resolve()
            if not path.is_relative_to(output_dir.resolve()) or not path.is_file():
                raise ValueError("Renderer returned an invalid artifact path")
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                raise ValueError("Rendered artifact integrity mismatch")
            session.add(DocumentArtifact(packet_id=packet_id, path=str(path), sha256=artifact["sha256"],
                size_bytes=artifact["size_bytes"], mime_type=artifact["mime_type"],
                data={"filename": artifact["filename"], "kind": artifact["kind"]}))
        # Keep original supplied files unchanged and refer to their immutable artifact IDs.
        packet.data = {**packet.data, "supplied_artifact_ids": [x["id"] for x in supplied_artifacts]}
        application = must_get(session, Application, application_id)
        for item in session.scalars(select(HumanTask).where(HumanTask.application_id == application_id)):
            if item.group_key.startswith(f"packet:{identity}:") and item.data.get("reason") not in validation["blockers"]:
                item.status = "resolved"
                item.data = {**item.data, "resolution": "Replacement packet resolved the blocker", "resolved_at": now_iso()}
        if application.state not in PRESERVE_STATES and not application.data.get("has_submission_evidence"):
            application.state = "drafting" if validation["valid"] else "needs_human"
            application.data = {**application.data, "packet_id": packet_id}
            application.updated_at = now_iso()
        for blocker in validation["blockers"]:
            task(session, f"packet:{identity}:{blocker}", "Packet needs review", blocker, application=application,
                 context={"url": opportunity["url"], "packet_id": packet_id,
                          "documents": manifest["document_hashes"], "answers": manifest["answers"]})
        audit(session, application_id, "packet_prepared", packet_id=packet_id, status=packet.status,
              manifest_hash=packet.data["manifest_hash"])
        return packet_view(session, packet)
