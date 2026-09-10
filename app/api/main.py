import hashlib
import hmac
import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pypdf import PdfReader
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.schemas import (AnswerInput, AssociationInput, ReconciliationInput, ControlsInput, EmailAlertInput, FactInput, Login,
    ManualSubmissionInput, OpportunityInput, PacketInput, PolicyInput, PreferencesInput,
    ResolutionInput, ReviewInput, SourceInput, SourcePatch, StateInput)
from app.config import ROOT, Settings
from app.db import Database
from app.models import (Application, AuthSession, Candidate, Connection, Control,
    DocumentArtifact, HumanTask, Notification, Opportunity, Packet, Requirement, Source,
    SourceOccurrence, SourceRun, now_iso, uid)
from app.security.auth import ensure_key, get_session, new_session, verify_password
from app.security.pdf import reject_active_content
from app.security.urls import canonical_url
from app import services as svc


def create_app(settings: Settings | None = None, database: Database | None = None):
    settings = settings or Settings()
    settings.validate_runtime()
    db = database or Database(settings.db_url)
    key = ensure_key(settings)
    tickets = URLSafeTimedSerializer(key, salt="artifact-download-v1")

    @asynccontextmanager
    async def lifespan(app):
        db.initialize()
        yield

    app = FastAPI(title="Opportunity Autopilot", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.settings = db, settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.public_url).hostname]
        if settings.production else ["127.0.0.1", "localhost", "testserver"])

    @app.exception_handler(LookupError)
    async def not_found(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    from app.integrations.gmail import GmailError
    @app.exception_handler(GmailError)
    async def gmail_error(request, exc):
        return JSONResponse({"detail": exc.code}, status_code=409)

    @app.middleware("http")
    async def private_boundary(request: Request, call_next):
        public_paths = {"/api/v1/session", "/api/v1/login", "/api/v1/health", "/api/v1/connections/gmail/callback"}
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        if mutation:
            origin = request.headers.get("origin")
            allowed = {settings.public_url.rstrip("/")}
            if not settings.production:
                allowed.update({"http://127.0.0.1:5173", "http://localhost:5173", "http://testserver"})
            if origin and origin not in allowed:
                return JSONResponse({"detail": "Untrusted request origin"}, status_code=403)
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
            if size > 12_000_000:
                return JSONResponse({"detail": "Request exceeds 12 MB limit"}, status_code=413)
            chunks, received = [], 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > 12_000_000:
                    return JSONResponse({"detail": "Request exceeds 12 MB limit"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        if request.url.path.startswith("/api/") and request.url.path not in public_paths:
            session = get_session(db, request.cookies.get("oa_session"))
            if not session:
                return JSONResponse({"detail": "Owner authentication required"}, status_code=401)
            request.state.owner_session = session
            if mutation and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.data["csrf"]):
                return JSONResponse({"detail": "Invalid CSRF token; refresh the session"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-src 'self' blob:; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    api = APIRouter(prefix="/api/v1")

    @api.get("/health")
    def health():
        with db.read() as session:
            session.execute(select(Control.id).limit(1))
        return {"status": "ok", "version": "0.1.0"}

    @api.get("/session")
    def session_info(request: Request):
        session = get_session(db, request.cookies.get("oa_session"))
        return {"authenticated": bool(session), "csrf_token": session.data["csrf"] if session else None,
            "owner": "Abdessamad Jaouad" if session else None,
            "setup_required": not settings.owner_path.exists(), "live_enabled": settings.live_submissions_enabled,
            "paid_services_enabled": settings.paid_services_enabled}

    @api.post("/login")
    def login(values: Login, request: Request):
        if not settings.owner_path.exists():
            raise HTTPException(409, "Run python -m app setup in the local terminal to set the owner password")
        limit_key = "login:" + hashlib.sha256((request.client.host if request.client else "local").encode()).hexdigest()[:48]
        with db.write() as session:
            limit = session.get(AuthSession, limit_key)
            current = now_iso()
            if limit and limit.expires_at > current and limit.data.get("failures", 0) >= 10:
                raise HTTPException(429, "Too many failed sign-ins; wait 15 minutes")
            if not verify_password(settings, values.password):
                if not limit:
                    limit = AuthSession(id=limit_key, expires_at=current, data={})
                    session.add(limit)
                limit.data = {"failures": limit.data.get("failures", 0) + 1 if limit.expires_at > current else 1}
                limit.expires_at = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
                failed = True
            else:
                if limit:
                    session.delete(limit)
                failed = False
        if failed:
            raise HTTPException(401, "Incorrect password")
        token, csrf = new_session(db)
        response = JSONResponse({"authenticated": True, "csrf_token": csrf, "owner": "Abdessamad Jaouad",
                                 "setup_required": False, "live_enabled": settings.live_submissions_enabled})
        response.set_cookie("oa_session", token, httponly=True, secure=settings.production,
                            samesite="strict", max_age=28800, path="/")
        return response

    @api.post("/logout")
    def logout(request: Request):
        with db.write() as session:
            record = session.get(AuthSession, request.state.owner_session.id)
            if record:
                session.delete(record)
        response = JSONResponse({"ok": True})
        response.delete_cookie("oa_session", path="/")
        return response

    @api.get("/dashboard")
    def dashboard():
        with db.read() as session:
            automation = svc.automation_view(session, settings)
            opportunities = [x for x in session.scalars(select(Opportunity).order_by(Opportunity.created_at.desc()))
                             if not x.data.get("history_import")]
            applications = session.scalars(select(Application)).all()
            tasks = session.scalars(select(HumanTask).where(HumanTask.status == "open")).all()
            return {"counts": {"opportunities": len(opportunities), "applications": len(applications),
                "needs_human": len(tasks), "confirmed": sum(x.state == "submitted_confirmed" for x in applications),
                "uncertain": sum(x.state == "submission_uncertain" for x in applications)},
                "controls": automation["controls"], "coverage": automation["coverage"],
                "notifications": automation["notifications"],
                "recent_opportunities": [svc.opportunity_view(session, x) for x in opportunities[:6]]}

    @api.get("/profile")
    def profile():
        with db.read() as session:
            return svc.profile_view(session)

    @api.post("/profile/import")
    def profile_import():
        return svc.import_profile(db, settings.portfolio_root)

    @api.patch("/profile/facts/{identity}")
    def profile_fact(identity: str, values: FactInput):
        return svc.confirm_fact(db, identity, values.value, values.note)

    @api.post("/profile/answers")
    def answer(values: AnswerInput):
        with db.write() as session:
            return svc.save_answer(session, **values.model_dump())

    @api.patch("/profile/preferences")
    def preferences(values: PreferencesInput):
        with db.write() as session:
            candidate = svc.must_get(session, Candidate, "owner")
            candidate.data = {**candidate.data, "preferences": {
                **candidate.data.get("preferences", {}), **values.model_dump(exclude_none=True)}}
            svc.snapshot_profile(session, "preferences_changed")
            return svc.record_json(candidate)

    @api.get("/opportunities")
    def opportunities(search: str = "", country: str = "", track: str = "", eligibility: str = "", route: str = "", source: str = ""):
        with db.read() as session:
            source_ids = set(session.scalars(select(SourceOccurrence.opportunity_id).where(
                SourceOccurrence.source_id == source))) if source else None
            rows = [svc.opportunity_view(session, x) for x in session.scalars(select(Opportunity)
                    .order_by(Opportunity.created_at.desc()))]
            return [x for x in rows if not x.get("history_import") and (source_ids is None or x["id"] in source_ids)
                and (not search or search.casefold() in (x["title"] + " " + x["employer"]).casefold())
                and all(not value or x.get(key) == value for key, value in
                        (("country", country), ("track", track), ("eligibility", eligibility), ("route", route)))]

    @api.post("/opportunities", status_code=201)
    def opportunity_create(values: OpportunityInput):
        return svc.add_opportunity(db, values.model_dump())

    @api.get("/opportunities/{identity}")
    def opportunity_detail(identity: str):
        with db.read() as session:
            opportunity = svc.must_get(session, Opportunity, identity)
            output = svc.opportunity_view(session, opportunity)
            application = session.scalar(select(Application).where(Application.opportunity_id == identity))
            application_data = svc.application_view(session, application, True) if application else {}
            return {**output, "application": application_data,
                "occurrences": [svc.record_json(x) for x in session.scalars(select(SourceOccurrence).where(
                    SourceOccurrence.opportunity_id == identity))],
                "requirements": [svc.record_json(x) for x in session.scalars(select(Requirement).where(
                    Requirement.opportunity_id == identity))],
                **{key: application_data.get(key, []) for key in ("packets", "tasks", "timeline")}}

    @api.post("/opportunities/{identity}/verify")
    def verify(identity: str):
        from app.opportunities.discovery import verify_opportunity
        return verify_opportunity(db, settings, identity)

    @api.post("/opportunities/{identity}/assess")
    def assess(identity: str):
        from app.matching.service import assess_opportunity
        return assess_opportunity(db, identity)

    @api.post("/opportunities/{identity}/packet")
    def packet(identity: str, values: PacketInput):
        from app.documents.service import prepare_packet
        return prepare_packet(db, settings, identity, **values.model_dump())

    @api.post("/opportunities/{identity}/review")
    def review(identity: str, values: ReviewInput):
        canonical_url(values.evidence_url)
        with db.write() as session:
            opportunity = svc.must_get(session, Opportunity, identity)
            for field in ("country", "track", "route"):
                if getattr(values, field) is not None:
                    setattr(opportunity, field, getattr(values, field))
            opportunity.data = {**opportunity.data, "owner_review": values.model_dump(exclude_none=True),
                "owner_reviewed_at": now_iso(), "destination": values.destination or opportunity.data.get("destination", "")}
            if values.requirements is not None:
                opportunity.data = {**opportunity.data, "requirements": [r.model_dump(exclude_none=True) for r in values.requirements]}
            opportunity.content_hash = svc.digest({"source": opportunity.content_hash, "review": opportunity.data["owner_review"]})
            svc.invalidate(session, reason="opportunity_reviewed", opportunity_id=identity)
            application = session.scalar(select(Application).where(Application.opportunity_id == identity))
            svc.audit(session, application.id, "owner_evidence_added", **values.model_dump(exclude_none=True))
            return svc.opportunity_view(session, opportunity)

    @api.get("/applications")
    def applications(search: str = "", state: str = ""):
        with db.read() as session:
            rows = [svc.application_view(session, x) for x in session.scalars(select(Application)
                .order_by(Application.updated_at.desc()))]
            return [x for x in rows if (not state or x["state"] == state) and
                (not search or search.casefold() in (x["title"] + " " + x["employer"]).casefold())]

    @api.get("/applications/{identity}")
    def application_detail(identity: str):
        with db.read() as session:
            return svc.application_view(session, svc.must_get(session, Application, identity), True)

    @api.post("/applications/{identity}/manual-submission")
    def manual(identity: str, values: ManualSubmissionInput):
        return svc.manual_submission(db, identity, **values.model_dump())

    @api.post("/applications/{identity}/state")
    def application_state(identity: str, values: StateInput):
        with db.write() as session:
            application = svc.must_get(session, Application, identity)
            if application.state in {"submitting", "submission_uncertain"}:
                raise ValueError("Reconcile the pending submission before changing its status")
            if values.state == "skipped" and (application.state in svc.PRESERVE_STATES or
                                               application.data.get("has_submission_evidence")):
                raise ValueError("An existing submission cannot be reset to skipped")
            application.state, application.updated_at = values.state, now_iso()
            svc.audit(session, identity, "owner_reported_status", **values.model_dump())
            return svc.application_view(session, application)

    @api.post("/applications/{identity}/queue")
    def queue(identity: str):
        from app.applications.dispatch import queue_application
        return queue_application(db, settings, identity)

    @api.post("/applications/{identity}/reconcile")
    def reconcile(identity: str):
        from app.applications.reconciliation import reconcile_application
        return reconcile_application(db, settings, identity)

    @api.post("/applications/{identity}/reconciliation-decision")
    def reconciliation_decision(identity: str, values: ReconciliationInput):
        from app.applications.reconciliation import owner_reconciliation
        return owner_reconciliation(db, identity, **values.model_dump())

    @api.post("/messages/{identity}/associate")
    def message_association(identity: str, values: AssociationInput):
        from app.applications.reconciliation import associate_message
        return associate_message(db, identity, **values.model_dump())

    @api.get("/messages/{identity}")
    def message_detail(identity: str):
        from app.models import EmailMessage
        with db.read() as session:
            return svc.record_json(svc.must_get(session, EmailMessage, identity))

    @api.post("/applications/{identity}/browser-preflight")
    def browser_preflight(identity: str):
        from app.applications.dispatch import browser_packet
        from app.browser import BrowserHandoff, BrowserProvider
        with db.read() as session:
            application = svc.must_get(session, Application, identity)
            opportunity = svc.must_get(session, Opportunity, application.opportunity_id)
            packet = session.scalar(select(Packet).where(Packet.application_id == identity)
                                    .order_by(Packet.created_at.desc()).limit(1))
            if not packet or not packet.data.get("manifest_hash"):
                raise ValueError("Prepare a packet before inspecting the form")
            values = browser_packet(session, opportunity, packet)
        try:
            result = BrowserProvider(db, settings).inspect(values)
        except BrowserHandoff as exc:
            result = {"status": "needs_human", "reasons": exc.reasons, "evidence": exc.evidence, "submitted": False}
        with db.write() as session:
            application = svc.must_get(session, Application, identity)
            if result["status"] == "needs_human":
                item = svc.task(session, f"browser:{identity}", "Website application needs your input", result["reasons"][0],
                    application=application, context={"url": values["url"], "packet_id": packet.id,
                        "answers": values["answers"], "evidence": result["evidence"]})
                result["task_id"] = item.id
            else:
                item = session.scalar(select(HumanTask).where(HumanTask.group_key == f"browser:{identity}"))
                if item:
                    item.status = "resolved"
            svc.audit(session, identity, "browser_preflight", **result)
        return result

    @api.get("/human-tasks/{identity}/screenshot")
    def human_screenshot(identity: str):
        with db.read() as session:
            task = svc.must_get(session, HumanTask, identity)
            context = task.data.get("context", {}).get("evidence", {})
            value = context.get("screenshot_path") or context.get("handoff", {}).get("screenshot_path")
            if not value:
                raise HTTPException(404, "No screenshot available")
            path = Path(value).resolve()
            if not path.is_relative_to((settings.data_dir / "browser-traces").resolve()) or not path.is_file():
                raise HTTPException(404, "Screenshot expired")
            return FileResponse(path, media_type="image/png")

    @api.post("/opportunities/{identity}/extract")
    def model_extract(identity: str):
        from app.integrations.optional import extract_requirements
        with db.read() as session:
            opportunity = svc.must_get(session, Opportunity, identity)
            description = opportunity.data.get("description", "")
        return extract_requirements(db, settings, identity, description)

    @api.get("/human-tasks")
    def human_tasks():
        with db.read() as session:
            return [svc.record_json(x) for x in session.scalars(select(HumanTask).order_by(
                HumanTask.status, HumanTask.priority, HumanTask.created_at))]

    @api.post("/human-tasks/{identity}/resolve")
    def resolve(identity: str, values: ResolutionInput):
        return svc.resolve_task(db, identity, values.model_dump())

    @api.get("/documents")
    def documents():
        with db.read() as session:
            return [svc.packet_view(session, x) for x in session.scalars(select(Packet)
                .order_by(Packet.created_at.desc()))]

    @api.get("/profile/attachments")
    def profile_attachments():
        with db.read() as session:
            return [svc.record_json(x) for x in session.scalars(select(DocumentArtifact).where(
                DocumentArtifact.packet_id.is_(None)).order_by(DocumentArtifact.created_at.desc()))]

    @api.post("/profile/attachments")
    async def upload(kind: str = Form(...), file: UploadFile = File(...), application_id: str = Form("")):
        if kind not in {"transcript", "certificate", "language_test", "paper", "cv", "cover_letter", "proposal"}:
            raise HTTPException(422, "Unsupported document kind")
        raw = await file.read(10_000_001)
        if len(raw) > 10_000_000:
            raise HTTPException(413, "PDF exceeds 10 MB")
        if not raw.startswith(b"%PDF-"):
            raise HTTPException(422, "Upload a PDF file")
        try:
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 100:
                raise ValueError("Encrypted or oversized PDF")
            reject_active_content(reader)
        except Exception:
            raise HTTPException(422, "PDF could not be safely parsed") from None
        if application_id:
            with db.read() as session:
                svc.must_get(session, Application, application_id)
        identity = uid()
        filename = f"{kind}-{identity[:12]}.pdf"
        path = settings.artifacts_dir / filename
        path.write_bytes(raw)
        path.chmod(0o600)
        with db.write() as session:
            packet = None
            if application_id:
                application = svc.must_get(session, Application, application_id)
                packet = Packet(id=uid(), application_id=application_id, status="draft", data={
                    "family": "manual", "language": "unknown", "manifest": {"origin": "owner_upload",
                    "profile_revision": svc.must_get(session, Candidate, "owner").revision},
                    "validation": {"valid": False, "blockers": ["manual_packet_requires_validation"]}})
                session.add(packet)
                session.flush()
                if application.state not in svc.PRESERVE_STATES:
                    application.state = "drafting"
            artifact = DocumentArtifact(id=identity, packet_id=packet.id if packet else None,
                path=str(path), sha256=hashlib.sha256(raw).hexdigest(), mime_type="application/pdf", size_bytes=len(raw),
                data={"filename": filename, "kind": kind, "source_filename": Path(file.filename or "document.pdf").name,
                      "owner_supplied": True, "disclosure_approved": False})
            session.add(artifact)
            svc.audit(session, application_id or "owner", "document_uploaded", artifact_id=identity, kind_name=kind)
            return svc.record_json(artifact)

    @api.post("/artifacts/{identity}/ticket")
    def artifact_ticket(identity: str, request: Request):
        with db.read() as session:
            svc.must_get(session, DocumentArtifact, identity)
        token = tickets.dumps({"id": identity, "session": request.state.owner_session.id})
        return {"url": f"/api/v1/artifacts/{identity}/download?ticket={token}", "expires_in": 60}

    @api.get("/artifacts/{identity}/download")
    def artifact_download(identity: str, ticket: str, request: Request):
        try:
            data = tickets.loads(ticket, max_age=60)
            if data != {"id": identity, "session": request.state.owner_session.id}:
                raise BadSignature("Ticket scope mismatch")
        except (BadSignature, SignatureExpired):
            raise HTTPException(403, "Download ticket expired or invalid") from None
        with db.read() as session:
            artifact = svc.must_get(session, DocumentArtifact, identity)
            path = Path(artifact.path).resolve()
            if not path.is_relative_to(settings.artifacts_dir.resolve()) or not path.is_file():
                raise HTTPException(404, "Artifact not found")
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
                raise HTTPException(409, "Artifact integrity check failed")
            return FileResponse(path, media_type=artifact.mime_type, filename=artifact.data["filename"])

    @api.get("/sources")
    def sources():
        with db.read() as session:
            return [svc.record_json(x) for x in session.scalars(select(Source).order_by(Source.name))]

    @api.post("/sources")
    def source_create(values: SourceInput):
        from app.opportunities.discovery import register_source
        return register_source(db, values.model_dump())

    @api.patch("/sources/{identity}")
    def source_update(identity: str, values: SourcePatch):
        with db.write() as session:
            source = svc.must_get(session, Source, identity)
            source.data = {**source.data, **values.model_dump(exclude_none=True)}
            return svc.record_json(source)

    @api.post("/sources/{identity}/poll")
    def source_poll(identity: str):
        with db.write() as session:
            source = svc.must_get(session, Source, identity)
            if source.data.get("permission_status") != "permitted":
                raise ValueError("Review source access permission before polling")
            return svc.record_json(svc.enqueue_job(session, f"poll:{identity}", "poll", {"source_id": identity}))

    @api.get("/source-runs")
    def source_runs():
        with db.read() as session:
            return [svc.record_json(x) for x in session.scalars(select(SourceRun)
                .order_by(SourceRun.created_at.desc()).limit(100))]

    @api.post("/discovery/email-alert")
    def email_alert(values: EmailAlertInput):
        from app.opportunities.discovery import import_email_alert
        return import_email_alert(db, values.raw_email)

    @api.get("/automation")
    def automation():
        with db.read() as session:
            return svc.automation_view(session, settings)

    @api.patch("/controls")
    def controls(values: ControlsInput):
        with db.write() as session:
            control = svc.must_get(session, Control, "global")
            control.data = {**control.data, **values.model_dump(exclude_none=True)}
            svc.audit(session, "owner", "controls_changed", changes=values.model_dump(exclude_none=True))
            active = session.scalars(select(Application).where(Application.state == "submitting")).all()
            return {**svc.record_json(control), "in_flight": [x.id for x in active]}

    @api.post("/policy")
    def policy(values: PolicyInput):
        return svc.save_policy(db, settings, values.model_dump())

    @api.post("/connections/gmail/start")
    def gmail_start(request: Request):
        from app.integrations.gmail import begin_oauth
        return begin_oauth(db, settings, request.state.owner_session.id)

    @api.get("/connections/gmail/callback")
    def gmail_callback(state: str, code: str = "", error: str = ""):
        from app.integrations.gmail import complete_oauth
        complete_oauth(db, settings, state, code, error)
        return RedirectResponse("/#automation", status_code=303)

    @api.post("/connections/gmail/disconnect")
    def gmail_disconnect():
        with db.write() as session:
            connection = svc.must_get(session, Connection, "gmail")
            connection.status, connection.encrypted_secret, connection.data = "unconfigured", None, {}
            svc.audit(session, "owner", "gmail_disconnected")
        return {"status": "unconfigured"}

    @api.post("/notifications/{identity}/read")
    def notification_read(identity: str):
        with db.write() as session:
            notification = svc.must_get(session, Notification, identity)
            notification.status = "read"
            return svc.record_json(notification)

    @api.post("/automation/tick")
    def tick():
        from app.workers.scheduler import run_tick
        return run_tick(db, settings)

    @api.post("/jobs/{identity}/retry")
    def retry_job(identity: str):
        from app.models import Job
        with db.write() as session:
            job = svc.must_get(session, Job, identity)
            if job.state != "failed":
                raise ValueError("Only failed processing jobs can be retried")
            job.state, job.due_at = "pending", now_iso()
            job.data = {**job.data, "attempts": 0, "error": None}
            svc.audit(session, "owner", "processing_retry_requested", job_id=identity)
            return svc.record_json(job)

    @api.post("/automation/digest")
    def daily_digest():
        from app.workers.notifications import build_digest
        return build_digest(db, settings)

    app.include_router(api)

    @app.get("/{path:path}")
    def web(path: str):
        dist = ROOT / "web" / "dist"
        requested = (dist / path).resolve()
        if not requested.is_relative_to(dist.resolve()):
            raise HTTPException(404)
        if requested.is_file():
            return FileResponse(requested)
        if path.startswith("api/"):
            raise HTTPException(404)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse({"detail": "Build the frontend with npm run build in web/"}, status_code=503)

    return app
