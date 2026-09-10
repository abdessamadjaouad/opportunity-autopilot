import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright

from app.browser import AdapterRegistry, BrowserHandoff, BrowserProvider, BrowserSessionStore
from app.browser.adapters import form_schema
from app.models import SubmissionAttempt
from app.services import add_opportunity, digest


@pytest.fixture
def form_server():
    state = {"extra": "", "letter": False, "posts": [], "receipt": True}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = ('<!doctype html><html><body><h1>Fixture employer application</h1>'
                '<div data-legal>Fixture privacy terms v1</div>' + state["extra"] +
                '<form method="POST" action="/submit" enctype="multipart/form-data">'
                '<label>Name<input name="name" required></label>'
                '<label>Motivation<textarea name="motivation" maxlength="100" required></textarea></label>'
                '<label>CV<input type="file" name="cv" accept=".pdf" required></label>' +
                ('<input type="file" name="letter" accept=".pdf" required>' if state["letter"] else '') +
                '<button type="submit">Apply</button></form></body></html>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["posts"].append(self.rfile.read(int(self.headers["content-length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<h1 id="receipt" data-application-id="FIXTURE-123">Received</h1>' if state["receipt"] else b"Uncertain")

        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["origin"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def configured(db, settings, form_server):
    url = form_server["origin"] + "/apply"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url)
        schema = form_schema(page, "form")
        legal = page.locator("[data-legal]").all_text_contents()
        browser.close()
    adapter = {"family": "native-form-v1", "simulator": True, "approved": True, "version": "fixture-1",
        "origin": form_server["origin"], "path": "/apply", "action_path": "/submit", "form_selector": "form",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(), "review_note": "Local fixture only",
        "schema_hash": digest(schema), "legal_hash": digest(legal), "read_paths": [],
        "fields": {"name": "name", "motivation": "motivation_text"}, "files": {"cv": "cv", "letter": "cover_letter"},
        "word_limits": {"motivation": 20}, "submit_selector": 'button[type="submit"]', "receipt_selector": "#receipt"}
    registry = AdapterRegistry([adapter], allow_simulators=True)
    opportunity = add_opportunity(db, {"url": "https://fixture.example/application", "title": "Fixture", "employer": "Fixture"})
    path = settings.artifacts_dir / "fixture.pdf"
    path.write_bytes(b"%PDF-1.4 fixture upload bytes")
    packet = {"url": url, "application_id": opportunity["application_id"], "manifest_hash": "fixture-manifest",
        "answers": {"name": "Fixture Owner", "motivation_text": "Owner confirmed fixture answer."},
        "documents": [{"kind": "cv", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}
    settings.live_submissions_enabled = True
    with db.write() as session:
        session.add(SubmissionAttempt(id="fixture-attempt", application_id=packet["application_id"],
            message_id="<browser@fixture.example>", data={"manifest_hash": packet["manifest_hash"]}))
    return BrowserProvider(db, settings, registry), packet, adapter


@pytest.mark.browser
def test_cv_only_preflight_has_no_effect_then_exact_receipt(db, settings, form_server):
    provider, packet, _ = configured(db, settings, form_server)
    assert provider.inspect(packet)["submitted"] is False
    assert form_server["posts"] == []
    receipt = provider.submit(packet, attempt_id="fixture-attempt", before_external_write=lambda: True)
    assert receipt.provider_id == "FIXTURE-123"
    assert receipt.evidence["simulation"] is True
    assert len(form_server["posts"]) == 1
    assert b'name="cv"' in form_server["posts"][0]
    assert b'name="letter"' not in form_server["posts"][0]


@pytest.mark.browser
def test_cv_letter_requested_only(db, settings, form_server):
    form_server["letter"] = True
    provider, packet, _ = configured(db, settings, form_server)
    with pytest.raises(BrowserHandoff, match="missing_upload:letter"):
        provider.inspect(packet)
    packet["documents"].append({**packet["documents"][0], "kind": "cover_letter"})
    provider.submit(packet, attempt_id="fixture-attempt", before_external_write=lambda: True)
    assert b'name="letter"' in form_server["posts"][0]


@pytest.mark.browser
@pytest.mark.parametrize("change,reason", [
    ("captcha", "captcha"), ("mfa", "mfa"), ("consent", "legal_consent_changed"),
    ("schema", "form_schema_changed"), ("authorship", "authorship_review"),
    ("words", "text_limit:motivation"), ("file", "upload_integrity_or_size:cv")])
def test_exception_preserves_handoff_without_post(db, settings, form_server, change, reason):
    provider, packet, _ = configured(db, settings, form_server)
    if change == "captcha":
        form_server["extra"] = "Complete CAPTCHA"
    elif change == "mfa":
        form_server["extra"] = "Enter verification code"
    elif change == "consent":
        form_server["extra"] = "<div data-legal>New background check consent</div>"
    elif change == "schema":
        form_server["letter"] = True
    elif change == "authorship":
        form_server["extra"] = "AI assistance prohibited"
    elif change == "words":
        packet["answers"]["motivation_text"] = "x " * 21
    elif change == "file":
        packet["documents"][0]["sha256"] = "forged"
    with pytest.raises(BrowserHandoff) as caught:
        provider.submit(packet, attempt_id="fixture-attempt", before_external_write=lambda: True)
    assert reason in caught.value.reasons
    assert caught.value.evidence["screenshot_path"]
    assert not form_server["posts"]


@pytest.mark.browser
def test_pause_at_post_and_uncertain_receipt(db, settings, form_server):
    provider, packet, _ = configured(db, settings, form_server)
    with pytest.raises(BrowserHandoff):
        provider.submit(packet, attempt_id="fixture-attempt", before_external_write=lambda: False)
    assert not form_server["posts"]
    form_server["receipt"] = False
    with pytest.raises(RuntimeError, match="uncertain"):
        provider.submit(packet, attempt_id="fixture-attempt", before_external_write=lambda: True)
    assert len(form_server["posts"]) == 1


def test_empty_registry_and_encrypted_session(db, settings, tmp_path):
    assert not AdapterRegistry().configured_for("https://employer.example/apply")
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"version": 1, "adapters": [{"simulator": True}]}))
    with pytest.raises(ValueError, match="Simulator"):
        AdapterRegistry.load(path)
    store = BrowserSessionStore(db, settings)
    state = {"cookies": [{"name": "fixture", "value": "private-test-session", "domain": "employer.example", "path": "/"}], "origins": []}
    store.save("https://employer.example", state, (datetime.now(UTC) + timedelta(hours=1)).isoformat())
    assert store.load("https://employer.example") == state
    assert b"private-test-session" not in (settings.data_dir / "autopilot.sqlite").read_bytes()
