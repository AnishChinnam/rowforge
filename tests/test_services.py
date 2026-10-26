"""Real service checks run in CI / Compose environments, never against a production schema."""

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update
from sqlalchemy.engine import make_url

from rowforge.api import create_app
from rowforge.config import Settings
from rowforge.db import jobs
from rowforge.queue import Wakeup
from rowforge.worker import claim, guarded_update, run_job


@pytest.mark.skipif(not os.getenv("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL not configured")
def test_postgres_claim_recovery_and_private_download(tmp_path):
    url = os.environ["POSTGRES_TEST_URL"]
    schema = "test_rowforge_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
    settings = Settings(database_url=isolated.render_as_string(hide_password=False), data_dir=tmp_path)
    app = None
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            account = client.post(
                "/api/auth/register",
                json={"email": "postgres@example.com", "password": "postgres-test-password"},
            )
            assert account.status_code == 200
            client.headers["X-CSRF-Token"] = account.json()["csrf"]

            def upload():
                return client.post(
                    "/api/jobs",
                    files={"file": ("test.csv", "name,email\nAda,ada@example.com\n")},
                    data={"rules": json.dumps({"email": ["email"]})},
                    headers={"Idempotency-Key": "pg-test"},
                )

            first = upload()
            assert first.status_code == 201, first.text
            assert upload().json()["id"] == first.json()["id"]
            job_id = first.json()["id"]
            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(
                    executor.map(lambda name: claim(app.state.engine, settings, name), ["one", "two"])
                )
            assert sum(job is not None for job in claims) == 1
            stale = next(job for job in claims if job)
            with app.state.engine.begin() as conn:
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(lease_until=time.time() - 1))
            recovered = claim(app.state.engine, settings, "replacement")
            assert recovered and recovered["attempts"] == 2
            assert not guarded_update(app.state.engine, stale, status="completed")
            run_job(app.state.engine, settings, recovered)
            assert client.get(f"/api/jobs/{job_id}").json()["status"] == "completed"
            assert "ada@example.com" in client.get(f"/api/jobs/{job_id}/download/cleaned").text
            assert client.post("/api/logout").status_code == 200
            assert client.get(f"/api/jobs/{job_id}/download/cleaned").status_code == 401
    finally:
        if app:
            app.state.engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv("REDIS_TEST_URL"), reason="REDIS_TEST_URL not configured")
def test_real_redis_hint():
    wakeup = Wakeup(os.environ["REDIS_TEST_URL"])
    wakeup.notify()
    assert wakeup.client.get("rowforge:wakeup") == b"1"
    assert wakeup.client.ttl("rowforge:wakeup") > 0
    wakeup.wait()
    assert wakeup.client.get("rowforge:wakeup") is None


def test_redis_outage_does_not_raise():
    from unittest.mock import Mock

    wakeup = Wakeup("")
    wakeup.client = Mock()
    wakeup.client.set.side_effect = redis.ConnectionError("offline")
    wakeup.notify()  # The job's database commit doesn't depend on this hint succeeding.
