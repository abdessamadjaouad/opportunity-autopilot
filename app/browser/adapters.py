import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright

from app.applications.contracts import DefinitiveRejection, ProviderAcceptance
from app.connectors.http import FetchResponse, _PinnedHTTPSConnection, network_url, public_addresses
from app.models import Connection, SubmissionAttempt, uid
from app.security.auth import ensure_key
from app.services import digest
from app.workers.budget import reserve


class BrowserHandoff(DefinitiveRejection):
    def __init__(self, reasons, evidence=None):
        super().__init__(reasons[0])
        self.reasons, self.evidence = reasons, evidence or {}


class AdapterRegistry:
    def __init__(self, entries=(), *, allow_simulators=False):
        self.entries, self.allow_simulators = list(entries), allow_simulators
        for entry in self.entries:
            if entry.get("simulator") and not allow_simulators:
                raise ValueError("Simulator adapters cannot be loaded as production configuration")
            if entry.get("family") != "native-form-v1":
                raise ValueError("Unsupported browser adapter family")

    @classmethod
    def load(cls, path):
        if not path:
            return cls()
        path = Path(path)
        if path.stat().st_mode & 0o022 or path.stat().st_size > 1_000_000:
            raise ValueError("Registry must be owner-controlled and under 1 MB")
        value = json.loads(path.read_text())
        if value.get("version") != 1:
            raise ValueError("Unsupported registry version")
        return cls(value["adapters"])

    def resolve(self, url):
        parsed = urlsplit(url)
        for entry in self.entries:
            if entry.get("origin") != f"{parsed.scheme}://{parsed.netloc}" or parsed.path != entry.get("path"):
                continue
            if entry.get("simulator"):
                if not self.allow_simulators or parsed.hostname not in {"127.0.0.1", "localhost"}:
                    continue
            else:
                network_url(url)
            try:
                valid = datetime.fromisoformat(entry["expires_at"]) > datetime.now(UTC)
            except (KeyError, ValueError, TypeError):
                valid = False
            if (entry.get("approved") is True and entry.get("version") and entry.get("review_note")
                    and valid and entry.get("schema_hash") and entry.get("legal_hash") is not None):
                return entry
        raise BrowserHandoff(["browser_adapter_unconfigured_or_expired"])

    def configured_for(self, url):
        try:
            self.resolve(url)
            return True
        except (BrowserHandoff, ValueError):
            return False


class BrowserSessionStore:
    def __init__(self, db, settings):
        self.db, self.cipher = db, Fernet(ensure_key(settings))

    def load(self, origin):
        with self.db.read() as session:
            record = session.get(Connection, "browser:" + digest(origin)[:32])
            if not record or not record.encrypted_secret:
                return None
            if record.data.get("session_expires_at", "") <= datetime.now(UTC).isoformat():
                raise BrowserHandoff(["browser_session_expired"])
            return json.loads(self.cipher.decrypt(record.encrypted_secret.encode()))

    def save(self, origin, value, expires_at):
        payload = json.dumps(value).encode()
        if len(payload) > 1_000_000:
            raise ValueError("Browser session too large")
        for cookie in value.get("cookies", []):
            if cookie.get("domain", "").lstrip(".") != urlsplit(origin).hostname:
                raise ValueError("Session cookie outside approved origin")
        with self.db.write() as session:
            identity = "browser:" + digest(origin)[:32]
            record = session.get(Connection, identity)
            if not record:
                record = Connection(id=identity)
                session.add(record)
            record.status = "connected"
            record.encrypted_secret = self.cipher.encrypt(payload).decode()
            record.data = {"origin": origin, "session_expires_at": expires_at}


def form_schema(page, selector):
    return page.locator(selector).evaluate("""form => Array.from(form.elements).map(e => ({
      name:e.name, type:e.type, required:e.required||false, maxLength:e.maxLength ?? -1,
      accept:e.accept||'', options:e.options?Array.from(e.options).map(o=>o.value):[],
      pattern:e.pattern||'', multiple:e.multiple||false, disabled:e.disabled||false
    }))""")


def _transport(method, url, headers, body, *, simulator):
    if simulator:
        if urlsplit(url).hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Fixture destination must remain loopback")
        with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
            result = client.request(method, url, headers=headers, content=body)
            if len(result.content) > 8_000_000:
                raise ValueError("Oversized browser response")
            return FetchResponse(result.status_code, result.content, dict(result.headers), url)
    parsed = urlsplit(network_url(url))
    connection = _PinnedHTTPSConnection(parsed.hostname, public_addresses(parsed.hostname)[0], 15)
    try:
        connection.request(method, urlunsplit(("", "", parsed.path, parsed.query, "")), body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise ValueError("Oversized browser response")
        return FetchResponse(response.status, data, dict(response.getheaders()), url)
    finally:
        connection.close()


class BrowserProvider:
    def __init__(self, db, settings, registry=None):
        self.db, self.settings = db, settings
        self.registry = registry or AdapterRegistry.load(settings.browser_adapter_registry)
        self.is_simulator = bool(self.registry.allow_simulators)

    def inspect(self, packet):
        return self._run(packet)

    def submit(self, packet, *, attempt_id, before_external_write):
        if not self.settings.live_submissions_enabled:
            raise BrowserHandoff(["live_submissions_disabled"])
        with self.db.read() as session:
            attempt = session.get(SubmissionAttempt, attempt_id)
            if not attempt or attempt.application_id != packet["application_id"] or \
                    attempt.data.get("manifest_hash") != packet["manifest_hash"]:
                raise BrowserHandoff(["browser_attempt_missing_or_mismatched"])
        return self._run(packet, attempt_id=attempt_id, before_external_write=before_external_write)

    def _run(self, packet, attempt_id=None, before_external_write=None):
        adapter = self.registry.resolve(packet["url"])
        evidence = {"adapter_version": adapter["version"], "url": packet["url"], "simulation": self.is_simulator}
        effect_started, post_count, network_errors = False, 0, []
        origin = adapter["origin"]
        action_url = urljoin(origin, adapter["action_path"])
        allowed_reads = {adapter["path"], *adapter.get("read_paths", [])}
        store = BrowserSessionStore(self.db, self.settings)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(java_script_enabled=False, service_workers="block",
                    accept_downloads=False, storage_state=store.load(origin), viewport={"width": 1100, "height": 800})
                def route(request_route):
                    nonlocal effect_started, post_count
                    request = request_route.request
                    parsed = urlsplit(request.url)
                    try:
                        if f"{parsed.scheme}://{parsed.netloc}" != origin or request.resource_type != "document":
                            request_route.abort()
                            return
                        if request.method == "GET" and parsed.path in allowed_reads:
                            with self.db.write() as session:
                                reserve(session, self.settings, uid(), "browser_read")
                        elif request.method == "POST" and request.url == action_url and attempt_id and post_count == 0:
                            if before_external_write is None or before_external_write() is not True:
                                raise BrowserHandoff(["browser_final_gate_refused"])
                            post_count += 1
                            effect_started = True
                        else:
                            raise BrowserHandoff(["browser_request_not_permitted"])
                        headers = {k: v for k, v in request.all_headers().items()
                                   if k.lower() in {"content-type", "cookie", "accept", "origin", "referer"}}
                        body = request.post_data_buffer
                        if body and len(body) > 25_000_000:
                            raise ValueError("Oversized browser request")
                        result = _transport(request.method, request.url, headers, body, simulator=self.is_simulator)
                        request_route.fulfill(status=result.status, body=result.body,
                            headers={k: v for k, v in result.headers.items() if k.lower() in {
                                "content-type", "location", "set-cookie"}})
                    except Exception as exc:
                        network_errors.append(getattr(exc, "code", type(exc).__name__))
                        request_route.abort()
                context.route("**/*", route)
                page = context.new_page()
                page.goto(packet["url"], wait_until="domcontentloaded", timeout=30000)
                text = page.locator("body").inner_text()
                checks = [(r"captcha|recaptcha|hcaptcha", "captcha"),
                          (r"multi.factor|verification code|one.time (?:code|password)", "mfa"),
                          (r"(?:ai|artificial intelligence).{0,40}(?:prohibited|not permitted)|own work declaration", "authorship_review"),
                          (r"camera permission|record (?:yourself|a video)|take (?:an exam|a test)|pay.*(?:fee|application)", "consequential_requirement")]
                reasons = [reason for pattern, reason in checks if re.search(pattern, text, re.I)]
                if page.locator('input[type="password"]').count():
                    reasons.append("login_required")
                form = page.locator(adapter["form_selector"])
                if form.count() != 1:
                    reasons.append("form_changed")
                else:
                    schema = form_schema(page, adapter["form_selector"])
                    evidence["schema_hash"] = digest(schema)
                    if digest(schema) != adapter["schema_hash"]:
                        reasons.append("form_schema_changed")
                    legal = page.locator(adapter.get("legal_selector", "[data-legal]")).all_text_contents()
                    if digest(legal) != adapter["legal_hash"]:
                        reasons.append("legal_consent_changed")
                    if (form.get_attribute("method") or "get").lower() != "post" or \
                            urljoin(page.url, form.get_attribute("action") or page.url) != action_url:
                        reasons.append("form_destination_changed")
                    mapping, files = adapter.get("fields", {}), adapter.get("files", {})
                    for field in schema:
                        name, kind = field["name"], field["type"]
                        if kind in {"submit", "button", "reset"}:
                            continue
                        if kind == "hidden":
                            if name not in adapter.get("hidden_fields", []):
                                reasons.append("unknown_hidden_field:" + name)
                        elif kind == "file":
                            if name not in files or not any(x["kind"] == files[name] for x in packet["documents"]):
                                if field["required"]:
                                    reasons.append("missing_upload:" + name)
                        elif kind in {"checkbox", "radio"}:
                            if field["required"] or name in mapping:
                                reasons.append("consent_or_choice_requires_human:" + name)
                        elif name not in mapping or mapping[name] not in packet["answers"]:
                            if field["required"]:
                                reasons.append("unknown_required_field:" + name)
                    if reasons:
                        raise BrowserHandoff(sorted(set(reasons)), evidence)
                    for name, key in mapping.items():
                        if key not in packet["answers"]:
                            continue
                        value = str(packet["answers"][key])
                        field = next((x for x in schema if x["name"] == name), None)
                        if not field:
                            raise BrowserHandoff(["mapped_field_missing:" + name], evidence)
                        if field["maxLength"] >= 0 and len(value) > field["maxLength"] or \
                                len(value.split()) > adapter.get("word_limits", {}).get(name, 100000):
                            raise BrowserHandoff(["text_limit:" + name], evidence)
                        target = form.locator('[name="' + name.replace('"', '\\"') + '"]')
                        if field["type"] == "select-one":
                            target.select_option(value)
                        else:
                            target.fill(value)
                    for name, kind in files.items():
                        document = next((x for x in packet["documents"] if x["kind"] == kind), None)
                        if not document:
                            continue
                        path = Path(document["path"]).resolve()
                        if not path.is_relative_to(self.settings.artifacts_dir.resolve()) or path.is_symlink() or \
                                path.stat().st_size > adapter.get("file_limits", {}).get(name, 10_000_000) or \
                                hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"]:
                            raise BrowserHandoff(["upload_integrity_or_size:" + name], evidence)
                        form.locator('[name="' + name.replace('"', '\\"') + '"]').set_input_files(str(path))
                    if not form.evaluate("form => form.checkValidity()"):
                        raise BrowserHandoff(["form_validation_failed"], evidence)
                if reasons:
                    raise BrowserHandoff(sorted(set(reasons)), evidence)
                if not attempt_id:
                    return {"status": "preflight_passed", **evidence, "submitted": False}
                form.locator(adapter["submit_selector"]).click(timeout=30000)
                if network_errors or not effect_started:
                    raise RuntimeError("Browser request incomplete")
                receipt = page.locator(adapter["receipt_selector"])
                reference = receipt.get_attribute(adapter.get("receipt_attribute", "data-application-id")) if receipt.count() == 1 else None
                if not reference or not re.fullmatch(adapter.get("receipt_pattern", r"[A-Za-z0-9_-]{3,150}"), reference):
                    raise RuntimeError("No verifiable browser receipt")
                return ProviderAcceptance(reference, evidence={**evidence, "kind": "browser_receipt", "receipt_id": reference})
            except Exception as exc:
                if effect_started:
                    raise RuntimeError("browser_submission_uncertain") from None
                error = exc if isinstance(exc, BrowserHandoff) else BrowserHandoff(
                    network_errors or ["browser_unavailable_or_page_changed"], evidence)
                # Capture before fields are filled when possible; never save full network traces/cookies.
                if "page" in locals() and not page.is_closed():
                    try:
                        page.locator("input,textarea,select").evaluate_all("nodes => nodes.forEach(n => n.style.visibility='hidden')")
                        directory = self.settings.data_dir / "browser-traces"
                        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                        shot = directory / f"{uid()}.png"
                        page.screenshot(path=str(shot), timeout=5000)
                        shot.chmod(0o600)
                        error.evidence["screenshot_path"] = str(shot)
                    except Exception:
                        error.evidence["screenshot_unavailable"] = True
                raise error
            finally:
                browser.close()
