import hashlib
import shutil

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.db import Database
from app.models import Opportunity
from app.services import add_opportunity
from ops.backup import backup, restore


def test_encrypted_restore_drill_preserves_database_artifacts_and_excludes_keys(db, settings, tmp_path):
    lead = add_opportunity(db, {"url": "https://fixture.example/job", "title": "Private fixture", "employer": "Fixture"})
    artifact = settings.artifacts_dir / "fixture.pdf"
    artifact.write_bytes(b"fixture-private-document")
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    key = tmp_path / "backup.key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    archive = tmp_path / "backup.encrypted"
    backup(settings.data_dir, settings.db_url, archive, key)
    assert b"Private fixture" not in archive.read_bytes()
    assert b"fixture-private-document" not in archive.read_bytes()
    db.engine.dispose()
    shutil.move(settings.data_dir, tmp_path / "previous-data")
    restore(archive, key, settings.data_dir, settings.db_url)
    restored = Database(settings.db_url)
    restored.initialize()
    with restored.read() as session:
        assert session.get(Opportunity, lead["id"]).title == "Private fixture"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected
    assert not settings.owner_path.exists()
    assert not settings.key_path.exists()
    restored.engine.dispose()
    with pytest.raises(ValueError, match="empty"):
        restore(archive, key, settings.data_dir, settings.db_url)
    archive.write_bytes(archive.read_bytes()[:-1] + b"X")
    with pytest.raises(InvalidToken):
        restore(archive, key, tmp_path / "new-target", settings.db_url)
