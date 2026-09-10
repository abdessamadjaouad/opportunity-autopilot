import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.applications.contracts import DefinitiveRejection, EmailAttachment, EmailEnvelope
from app.config import Settings
from app.db import Database
from app.integrations.email_provider import GmailProvider, get_gmail_provider, normalize_message, serialize_envelope
from app.integrations.gmail import (GMAIL_URL, SCOPES, TOKEN_URL, GmailError, _decrypt, _encrypt,
                                    access_credentials, begin_oauth, complete_oauth)
from app.models import AuthSession, Connection, Notification


@pytest.fixture
def environment(tmp_path):
    secret = tmp_path / "client-secret"
    secret.write_text("fixture-client-secret")
    secret.chmod(0o600)
    settings = Settings(data_dir=tmp_path / "data", portfolio_root=tmp_path,
        google_client_id="fixture-client.apps.googleusercontent.com", google_client_secret_file=secret)
    settings.validate_runtime()
    db = Database(settings.db_url)
    db.initialize()
    with db.write() as session:
        session.add(AuthSession(id="fixture-owner-session", expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                                data={"csrf": "fixture-csrf"}))
    yield db, settings
    db.engine.dispose()


def tokens(**changes):
    return {"access_token": "fixture-access-secret", "refresh_token": "fixture-refresh-secret",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "scope": " ".join(sorted(SCOPES)), **changes}


def connect_fixture(db, settings, **changes):
    with db.write() as session:
        connection = session.get(Connection, "gmail")
        connection.status = "connected"
        connection.encrypted_secret = _encrypt(settings, tokens(**changes))
        connection.data = {"email_address": "owner@fixture.example", "email": "owner@fixture.example"}


def envelope(**changes):
    return EmailEnvelope(**{"message_id": "<fixture-application@autopilot.example>",
        "recipient": "applications@employer.example", "subject": "Fixture application", "text": "Fixture draft only.",
        "attachments": (EmailAttachment("fixture-cv.pdf", "application/pdf", b"%PDF-fixture-content"),), **changes})


def start(environment):
    db, settings = environment
    result = begin_oauth(db, settings, "fixture-owner-session")
    params = parse_qs(urlsplit(result["authorization_url"]).query)
    return result, params


def test_unconfigured_oauth_and_provider_do_not_access_network(environment):
    db, settings = environment
    with pytest.raises(GmailError, match="unconfigured"):
        begin_oauth(db, settings.model_copy(update={"google_client_id": ""}), "fixture-owner-session")
    with pytest.raises(GmailError, match="unconfigured"):
        get_gmail_provider(db, settings)


def test_oauth_has_exact_scopes_pkce_and_private_one_time_state(environment):
    db, settings = environment
    result, params = start(environment)
    assert urlsplit(result["authorization_url"]).netloc == "accounts.google.com"
    assert set(params["scope"][0].split()) == SCOPES
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["include_granted_scopes"] == ["false"]
    assert "not an OAuth permission boundary" in result["read_access_notice"]
    with db.read() as session:
        record = session.scalar(select(AuthSession).where(AuthSession.id.like("oauth:%")))
        secret = _decrypt(settings, record.data["encrypted_verifier"])["verifier"]
        assert secret not in json.dumps(record.data)
        assert params["state"][0] not in json.dumps(record.data)
        expected = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode().rstrip("=")
        assert params["code_challenge"] == [expected]
        assert record.data["owner_session_id"] == "fixture-owner-session"


def test_successful_oauth_persists_only_encrypted_tokens(environment):
    db, settings = environment
    _, params = start(environment)
    calls = []
    def handler(request):
        calls.append(request)
        if str(request.url) == TOKEN_URL:
            fields = parse_qs(request.content.decode())
            assert fields["code"] == ["fixture-authorization-code"]
            assert fields["code_verifier"]
            return httpx.Response(200, json={"access_token": "fixture-access-secret", "refresh_token": "fixture-refresh-secret",
                "expires_in": 3600, "token_type": "Bearer", "scope": " ".join(SCOPES)})
        assert str(request.url) == GMAIL_URL + "/profile"
        assert request.headers["authorization"] == "Bearer fixture-access-secret"
        return httpx.Response(200, json={"emailAddress": "owner@fixture.example"})
    result = complete_oauth(db, settings, params["state"][0], "fixture-authorization-code", transport=httpx.MockTransport(handler))
    assert result["status"] == "connected"
    assert "fixture-access-secret" not in json.dumps(result)
    with db.read() as session:
        connection = session.get(Connection, "gmail")
        assert connection.status == "connected"
        assert "fixture-refresh-secret" not in json.dumps(connection.data)
        assert "fixture-refresh-secret" not in connection.encrypted_secret
        assert _decrypt(settings, connection.encrypted_secret)["refresh_token"] == "fixture-refresh-secret"
        assert not session.scalar(select(AuthSession).where(AuthSession.id.like("oauth:%")))
    with pytest.raises(GmailError, match="state invalid or expired"):
        complete_oauth(db, settings, params["state"][0], "fixture-authorization-code", transport=httpx.MockTransport(handler))
    assert len(calls) == 2


@pytest.mark.parametrize("action", ["expire_state", "delete_owner", "expire_owner"])
def test_state_is_expiring_and_bound_to_existing_owner_session(environment, action):
    db, settings = environment
    _, params = start(environment)
    with db.write() as session:
        if action == "expire_state":
            record = session.scalar(select(AuthSession).where(AuthSession.id.like("oauth:%")))
            record.expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        else:
            owner = session.get(AuthSession, "fixture-owner-session")
            if action == "delete_owner":
                session.delete(owner)
            else:
                owner.expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(GmailError):
        complete_oauth(db, settings, params["state"][0], "fixture-code",
                       transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected token exchange")))


def test_declined_oauth_consumes_state_without_network(environment):
    db, settings = environment
    _, params = start(environment)
    result = complete_oauth(db, settings, params["state"][0], error="access_denied")
    assert result["reason"] == "gmail_authorization_declined"
    with pytest.raises(GmailError):
        complete_oauth(db, settings, params["state"][0], error="access_denied")


@pytest.mark.parametrize("scope", ["https://mail.google.com/", "https://www.googleapis.com/auth/gmail.send", ""])
def test_wrong_or_partial_scope_never_marks_connection_healthy(environment, scope):
    db, settings = environment
    _, params = start(environment)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "fixture-a",
        "refresh_token": "fixture-r", "expires_in": 3600, "scope": scope}))
    with pytest.raises(GmailError, match="scopes mismatch"):
        complete_oauth(db, settings, params["state"][0], "fixture-code", transport=transport)
    with db.read() as session:
        assert session.get(Connection, "gmail").status == "unconfigured"


def test_oauth_token_exchange_redirect_is_not_followed(environment):
    db, settings = environment
    _, params = start(environment)
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://attacker.example/token"})
    with pytest.raises(GmailError):
        complete_oauth(db, settings, params["state"][0], "fixture-code", transport=httpx.MockTransport(handler))
    assert calls == [TOKEN_URL]


def test_local_remote_http_redirect_is_rejected(environment):
    db, settings = environment
    with pytest.raises(GmailError, match="https or loopback"):
        begin_oauth(db, settings.model_copy(update={"public_url": "http://remote.example"}), "fixture-owner-session")


def test_send_returns_acceptance_only_and_preserves_message_id(environment):
    db, settings = environment
    settings = settings.model_copy(update={"live_submissions_enabled": True})
    connect_fixture(db, settings)
    sent = []
    def handler(request):
        assert request.method == "POST" and str(request.url) == GMAIL_URL + "/messages/send"
        sent.append(message_from_bytes(base64.urlsafe_b64decode(json.loads(request.content)["raw"]), policy=policy.default))
        return httpx.Response(200, json={"id": "fixture-provider-id", "threadId": "fixture-thread"})
    result = get_gmail_provider(db, settings, transport=httpx.MockTransport(handler)).send(envelope())
    assert result.provider_id == "fixture-provider-id" and result.evidence["delivery_confirmed"] is False
    assert sent[0]["Message-ID"] == envelope().message_id and sent[0]["To"] == envelope().recipient
    assert sent[0]["Bcc"] is None and sent[0]["Cc"] is None
    assert list(sent[0].iter_attachments())[0].get_filename() == "fixture-cv.pdf"


def test_live_switch_blocks_send_even_with_connected_mailbox(environment):
    db, settings = environment
    connect_fixture(db, settings)
    provider = get_gmail_provider(db, settings, transport=httpx.MockTransport(lambda request: pytest.fail("No live send")))
    with pytest.raises(DefinitiveRejection, match="live_submissions_disabled"):
        provider.send(envelope())


@pytest.mark.parametrize("status", [400, 401, 403, 413, 429])
def test_explicit_provider_rejection_is_definitive_and_not_retried(environment, status):
    db, settings = environment
    settings = settings.model_copy(update={"live_submissions_enabled": True})
    connect_fixture(db, settings)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "fixture provider refusal"}})
    with pytest.raises(DefinitiveRejection):
        GmailProvider(db, settings, transport=httpx.MockTransport(handler)).send(envelope())
    assert len(calls) == 1


@pytest.mark.parametrize("result", ["timeout", "server_error", "malformed_success"])
def test_possible_acceptance_is_uncertain_without_resending(environment, result):
    db, settings = environment
    settings = settings.model_copy(update={"live_submissions_enabled": True})
    connect_fixture(db, settings)
    calls = []
    def handler(request):
        calls.append(request)
        if result == "timeout":
            raise httpx.ReadTimeout("fixture network timeout after possible acceptance")
        return httpx.Response(500 if result == "server_error" else 200, json={})
    with pytest.raises(GmailError):
        GmailProvider(db, settings, transport=httpx.MockTransport(handler)).send(envelope())
    assert len(calls) == 1


@pytest.mark.parametrize("changes", [{"subject": "Hello\r\nBcc: attacker@example.com"},
    {"recipient": "good@example.com,attacker@example.com"}, {"message_id": "bad id"},
    {"attachments": (EmailAttachment("../passport.pdf", "application/pdf", b"fixture"),)}])
def test_mime_injection_is_rejected_before_send(changes):
    with pytest.raises(DefinitiveRejection):
        serialize_envelope(envelope(**changes), "owner@fixture.example")


def test_refresh_retains_offline_token_when_provider_does_not_rotate(environment):
    db, settings = environment
    connect_fixture(db, settings, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    def handler(request):
        assert str(request.url) == TOKEN_URL
        assert parse_qs(request.content.decode())["grant_type"] == ["refresh_token"]
        return httpx.Response(200, json={"access_token": "fixture-new-access", "expires_in": 3600})
    access, _ = access_credentials(db, settings, transport=httpx.MockTransport(handler))
    assert access == "fixture-new-access"
    with db.read() as session:
        encrypted = session.get(Connection, "gmail").encrypted_secret
        assert _decrypt(settings, encrypted)["refresh_token"] == "fixture-refresh-secret"


def test_revoked_refresh_expires_connection_and_creates_visible_notification(environment):
    db, settings = environment
    connect_fixture(db, settings, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    with pytest.raises(GmailError, match="refresh revoked"):
        access_credentials(db, settings, transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid_grant", "description": "fixture-refresh-secret"})))
    with db.read() as session:
        connection = session.get(Connection, "gmail")
        assert connection.status == "expired"
        assert "fixture-refresh-secret" not in json.dumps(connection.data)
        assert session.scalar(select(Notification)).data["urgency"] == "urgent"


def test_disconnect_during_refresh_cannot_resurrect_connection(environment):
    db, settings = environment
    connect_fixture(db, settings, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    def handler(request):
        with db.write() as session:
            connection = session.get(Connection, "gmail")
            connection.status, connection.encrypted_secret = "unconfigured", None
        return httpx.Response(200, json={"access_token": "fixture-new", "expires_in": 3600})
    with pytest.raises(GmailError, match="changed during refresh"):
        access_credentials(db, settings, transport=httpx.MockTransport(handler))


def test_disconnect_during_oauth_exchange_cannot_reconnect_mailbox(environment):
    db, settings = environment
    _, params = start(environment)
    def handler(request):
        if str(request.url) == TOKEN_URL:
            with db.write() as session:
                connection = session.get(Connection, "gmail")
                connection.status, connection.encrypted_secret, connection.data = "unconfigured", None, {}
            return httpx.Response(200, json={"access_token": "fixture-a", "refresh_token": "fixture-r",
                "expires_in": 3600, "scope": " ".join(SCOPES)})
        return httpx.Response(200, json={"emailAddress": "owner@fixture.example"})
    with pytest.raises(GmailError, match="cancelled or superseded"):
        complete_oauth(db, settings, params["state"][0], "fixture-code", transport=httpx.MockTransport(handler))
    with db.read() as session:
        assert session.get(Connection, "gmail").status == "unconfigured"


def test_declining_reconnect_preserves_existing_connection(environment):
    db, settings = environment
    connect_fixture(db, settings)
    _, params = start(environment)
    assert complete_oauth(db, settings, params["state"][0], error="access_denied")["status"] == "connected"


def mail_raw(identity="fixture-message", *, labels=None, message_id=None, html=False):
    text = "<p>Your fixture application has been received.</p><script>stealSecrets()</script>" if html else "Fixture interview invitation."
    return {"id": identity, "threadId": "fixture-thread", "internalDate": "1788868800000", "labelIds": labels or ["INBOX"],
        "payload": {"mimeType": "text/html" if html else "text/plain", "headers": [
            {"name": "From", "value": "Fixture Recruiter <recruiter@employer.example>"},
            {"name": "To", "value": "owner@fixture.example"},
            {"name": "Message-ID", "value": message_id or "<reply@employer.example>"},
            {"name": "In-Reply-To", "value": envelope().message_id},
            {"name": "References", "value": "<older@employer.example> " + envelope().message_id},
            {"name": "Subject", "value": "Fixture interview invitation"}],
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


def test_sent_lookup_requires_exact_header_and_sent_label(environment):
    db, settings = environment
    connect_fixture(db, settings)
    def handler(request):
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == "in:sent rfc822msgid:" + envelope().message_id
            return httpx.Response(200, json={"messages": [{"id": "fixture-a"}, {"id": "fixture-b"}]})
        matching = request.url.path.endswith("fixture-a")
        return httpx.Response(200, json=mail_raw("fixture-a" if matching else "fixture-b", labels=["SENT"],
            message_id=envelope().message_id if matching else "<different@autopilot.example>"))
    result = GmailProvider(db, settings, transport=httpx.MockTransport(handler)).find_sent(envelope().message_id)
    assert len(result) == 1 and result[0]["provider_id"] == "fixture-a"


def test_polling_has_bounded_resume_query_and_correlated_reply_headers(environment):
    db, settings = environment
    connect_fixture(db, settings)
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/messages"):
            assert request.url.params["maxResults"] == "100"
            assert settings.gmail_query in request.url.params["q"]
            if request.url.params.get("pageToken"):
                return httpx.Response(200, json={"messages": []})
            return httpx.Response(200, json={"messages": [{"id": "fixture-message"}], "nextPageToken": "fixture-next"})
        return httpx.Response(200, json=mail_raw(html=True))
    provider = GmailProvider(db, settings, transport=httpx.MockTransport(handler))
    first = provider.poll_messages()
    assert not first["complete"] and first["messages"][0]["in_reply_to"] == envelope().message_id
    assert envelope().message_id in first["messages"][0]["references"]
    assert "stealSecrets" not in first["messages"][0]["body"]
    second = provider.poll_messages(first["cursor"])
    assert second["complete"]
    assert calls[0].url.params["q"] == calls[2].url.params["q"]
    assert json.loads(second["cursor"])["until"] is None


def test_message_normalization_never_downloads_attachments():
    raw = mail_raw()
    raw["payload"] = {"mimeType": "multipart/mixed", "headers": raw["payload"]["headers"], "parts": [
        raw["payload"], {"mimeType": "application/pdf", "filename": "untrusted.pdf", "body": {"attachmentId": "not-fetched"}}]}
    result = normalize_message(raw)
    assert "Fixture interview" in result["body"]
    assert "attachmentId" not in result


def test_invalid_poll_cursor_and_provider_id_do_not_turn_into_requests(environment):
    db, settings = environment
    connect_fixture(db, settings)
    provider = GmailProvider(db, settings, transport=httpx.MockTransport(lambda request: pytest.fail("No request")))
    with pytest.raises(GmailError):
        provider.poll_messages('{"v":1,"after":true}')
    with pytest.raises(DefinitiveRejection):
        provider.find_sent("attacker query after:0")


def test_expired_page_cursor_restarts_same_query_window(environment):
    db, settings = environment
    connect_fixture(db, settings)
    queries = []
    def handler(request):
        queries.append(request.url.params["q"])
        if request.url.params.get("pageToken"):
            return httpx.Response(400, json={"error": {"code": 400}})
        return httpx.Response(200, json={"messages": []})
    now = int(datetime.now(UTC).timestamp())
    cursor = json.dumps({"v": 1, "after": now - 3600, "until": now, "page": "expired-fixture"})
    result = GmailProvider(db, settings, transport=httpx.MockTransport(handler)).poll_messages(cursor)
    assert result["complete"] and len(queries) == 2 and queries[0] == queries[1]


def test_missing_provider_date_stays_unknown():
    raw = mail_raw()
    raw.pop("internalDate")
    assert normalize_message(raw)["date"] is None


@pytest.mark.parametrize("authentication,expected", [
    ([], False),
    (["mx.google.com; dmarc=pass header.from=employer.example"], True),
    (["mx.google.com; dmarc=fail header.from=employer.example"], False),
    (["mx.google.com; dmarc=pass header.from=attacker.example"], False),
    (["attacker.example; dmarc=pass header.from=employer.example"], False),
    (["mx.google.com; dmarc=fail header.from=employer.example",
      "mx.google.com; dmarc=pass header.from=employer.example"], False),
])
def test_incoming_sender_authentication_uses_first_google_result(authentication, expected):
    raw = mail_raw()
    raw["payload"]["headers"].extend({"name": "Authentication-Results", "value": value}
                                       for value in authentication)
    assert normalize_message(raw)["authenticated_sender"] is expected
