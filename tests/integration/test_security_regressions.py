import io

import pytest
from pypdf import PdfWriter
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models import AuditEvent
from app.security.auth import ensure_key, set_owner_password
from app.security.urls import canonical_url


def test_pdf_javascript_indirect_objects_rejected(owner):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_js("app.alert('untrusted fixture')")
    content = io.BytesIO()
    writer.write(content)
    response = owner.post("/api/v1/profile/attachments", data={"kind": "transcript"},
        files={"file": ("active.pdf", content.getvalue(), "application/pdf")})
    assert response.status_code == 422


def test_chunked_request_limit(client):
    def body():
        for _ in range(13):
            yield b"x" * 1_000_000
    response = client.post("/api/v1/login", content=body(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_spaces_in_owner_password_are_preserved(client, settings):
    set_owner_password(settings, " fixture password with spaces ")
    assert client.post("/api/v1/login", json={"password": " fixture password with spaces "}).status_code == 200
    assert client.post("/api/v1/login", json={"password": "fixture password with spaces"}).status_code == 401


@pytest.mark.parametrize("host", ["localhost", "jobs.localhost", "localhost.localdomain", "2130706433",
    "0x7f000001", "0177.0.0.1", "internal", "[::1]", "10.1.1.1", "169.254.169.254"])
def test_private_host_aliases_rejected(host):
    with pytest.raises(ValueError):
        canonical_url(f"https://{host}/jobs")


def test_query_order_not_silently_collapsed():
    assert canonical_url("https://employer.example/jobs?id=1&id=2") != canonical_url(
        "https://employer.example/jobs?id=2&id=1")


def test_malformed_encryption_key_stops_startup(settings):
    settings.key_path.parent.mkdir(exist_ok=True, parents=True)
    settings.key_path.write_text("")
    with pytest.raises(ValueError):
        ensure_key(settings)


def test_database_prevents_bulk_audit_mutation(db):
    with db.write() as session:
        session.add(AuditEvent(id="immutable", entity_id="owner", kind="fixture", data={}))
    with pytest.raises(DBAPIError, match="append-only"):
        with db.write() as session:
            session.execute(text("UPDATE audit_events SET kind='changed' WHERE id='immutable'"))


def test_schema_version_exists_and_migration_is_repeatable(db):
    db.initialize()
    with db.read() as session:
        assert session.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
