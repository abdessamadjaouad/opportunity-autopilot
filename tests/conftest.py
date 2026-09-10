import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Settings
from app.db import Database
from app.security.auth import set_owner_password


@pytest.fixture
def settings(tmp_path):
    config = Settings(data_dir=tmp_path / "data", portfolio_root=tmp_path / "portfolio",
                      public_url="http://testserver", _env_file=None)
    config.validate_runtime()
    config.portfolio_root.mkdir()
    set_owner_password(config, "fixture-owner-password")
    return config


@pytest.fixture
def db(settings):
    database = Database(settings.db_url)
    database.initialize()
    yield database
    database.engine.dispose()


@pytest.fixture
def client(settings, db):
    with TestClient(create_app(settings, db)) as test:
        yield test


@pytest.fixture
def owner(client):
    response = client.post("/api/v1/login", json={"password": "fixture-owner-password"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client
