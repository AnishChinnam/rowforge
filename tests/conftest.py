import pytest
from fastapi.testclient import TestClient

from rowforge.api import create_app
from rowforge.config import Settings


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        data_dir=tmp_path / "files",
        batch_size=2,
        lease_seconds=1,
    )
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def account(client):
    response = client.post(
        "/api/auth/register", json={"email": "owner@example.com", "password": "correct-horse-123"}
    )
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return client
