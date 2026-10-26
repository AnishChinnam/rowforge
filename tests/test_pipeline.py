import csv
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from rowforge.db import jobs
from rowforge.processing import InvalidCSV, process
from rowforge.security import hash_password, verify_password
from rowforge.worker import claim, guarded_update, run_job


def upload(client, content="name,email,amount\n Ada ,ada@example.com,12\n", rules=None, key="job-1"):
    return client.post(
        "/api/jobs",
        files={"file": ("input.csv", content, "text/csv")},
        data={"rules": json.dumps(rules or {})},
        headers={"Idempotency-Key": key},
    )


def finish(app):
    job = claim(app.state.engine, app.state.settings, "test-worker")
    assert job
    run_job(app.state.engine, app.state.settings, job)
    return job


def test_password_hash():
    hashed = hash_password("example-password")
    assert verify_password("example-password", hashed)
    assert not verify_password("wrong-password", hashed)
    assert "example-password" not in hashed


def test_auth_required(client):
    assert client.get("/api/jobs").status_code == 401


def test_session_logout_and_csrf(account):
    assert account.get("/api/me").status_code == 200
    token = account.headers.pop("X-CSRF-Token")
    assert account.post("/api/logout").status_code == 403
    account.headers["X-CSRF-Token"] = token
    assert account.post("/api/logout").status_code == 200
    assert account.get("/api/me").status_code == 401


def test_wrong_password_and_duplicate_account(account):
    assert (
        account.post(
            "/api/auth/login", json={"email": "owner@example.com", "password": "not-the-password"}
        ).status_code
        == 401
    )
    assert (
        account.post(
            "/api/auth/register", json={"email": "owner@example.com", "password": "another-password"}
        ).status_code
        == 409
    )


def test_cross_origin_blocked(account):
    assert account.post("/api/logout", headers={"Origin": "https://other.example"}).status_code == 403


def test_private_jobs_and_downloads(app, account):
    job_id = upload(account).json()["id"]
    finish(app)
    with TestClient(app) as other:
        result = other.post(
            "/api/auth/register", json={"email": "other@example.com", "password": "other-password"}
        )
        other.headers["X-CSRF-Token"] = result.json()["csrf"]
        assert other.get("/api/jobs").json() == []
        for path in [f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/download/cleaned"]:
            assert other.get(path).status_code == 404
        assert other.post(f"/api/jobs/{job_id}/cancel").status_code == 404
        assert other.post(f"/api/jobs/{job_id}/retry").status_code == 404


def test_idempotent_upload(account):
    first, second = upload(account), upload(account)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(account.get("/api/jobs").json()) == 1
    assert upload(account, content="a\ndifferent\n").status_code == 409


def test_idempotency_is_per_account(app, account):
    one = upload(account).json()["id"]
    with TestClient(app) as other:
        r = other.post(
            "/api/auth/register", json={"email": "second@example.com", "password": "long-password"}
        )
        other.headers["X-CSRF-Token"] = r.json()["csrf"]
        assert upload(other).json()["id"] != one


def test_pipeline_counts_and_error_report(app, account):
    source = "name,email,amount\n Ada ,ada@example.com,12\nAda,ada@example.com,12\nBob,bad,13\n,blank@example.com,NaN\nExtra,x@example.com,1,extra\n"
    job_id = upload(
        account, source, {"required": ["name"], "email": ["email"], "numeric": ["amount"]}
    ).json()["id"]
    finish(app)
    detail = account.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"
    assert (detail["processed"], detail["accepted"], detail["rejected"], detail["duplicates"]) == (5, 1, 3, 1)
    assert detail["processed"] == detail["accepted"] + detail["rejected"] + detail["duplicates"]
    assert list(csv.reader(io.StringIO(account.get(f"/api/jobs/{job_id}/download/cleaned").text)))[1] == [
        "Ada",
        "ada@example.com",
        "12",
    ]
    errors = account.get(f"/api/jobs/{job_id}/download/errors").text
    assert "invalid email format" in errors and "finite number" in errors and "found 4" in errors
    assert [e["kind"] for e in detail["events"]] == ["completed", "claimed", "queued"]


def test_download_not_available_before_completion(account):
    job_id = upload(account).json()["id"]
    assert account.get(f"/api/jobs/{job_id}/download/cleaned").status_code == 409


@pytest.mark.parametrize(
    "content,message",
    [
        ("name,name\na,b\n", "unique"),
        ("name,\na,b\n", "nonempty"),
        ("\xff".encode("latin1"), "UTF-8"),
        ('name\n"unclosed\n', "valid UTF-8"),
    ],
)
def test_invalid_csv_fails_without_results(app, account, content, message):
    job_id = upload(account, content).json()["id"]
    finish(app)
    detail = account.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed" and message in detail["error"]
    assert account.get(f"/api/jobs/{job_id}/download/cleaned").status_code == 409


def test_missing_rule_column(app, account):
    job_id = upload(account, rules={"required": ["missing"]}).json()["id"]
    finish(app)
    assert account.get(f"/api/jobs/{job_id}").json()["error"] == "Missing rule columns: missing"


def test_formula_injection_neutralized(app, account):
    job_id = upload(account, "name\n=HYPERLINK(1)\n +SUM(1)\n@evil\n-2\n").json()["id"]
    finish(app)
    data = account.get(f"/api/jobs/{job_id}/download/cleaned").text
    assert "'=HYPERLINK" in data and "'+SUM" in data and "'@evil" in data and "'-2" in data


def test_queued_cancellation_and_retry(app, account):
    job_id = upload(account).json()["id"]
    assert account.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    assert claim(app.state.engine, app.state.settings, "worker") is None
    assert account.post(f"/api/jobs/{job_id}/retry").json()["status"] == "queued"
    finish(app)
    assert account.get(f"/api/jobs/{job_id}").json()["status"] == "completed"
    assert account.post(f"/api/jobs/{job_id}/retry").status_code == 409


def test_running_cancellation_fences_old_worker(app, account):
    job_id = upload(account).json()["id"]
    job = claim(app.state.engine, app.state.settings, "interrupted")
    account.post(f"/api/jobs/{job_id}/cancel")
    run_job(app.state.engine, app.state.settings, job)
    assert account.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"
    assert not guarded_update(app.state.engine, job, status="completed")


def test_expired_lease_reclaimed_and_stale_owner_fenced(app, account):
    job_id = upload(account).json()["id"]
    first = claim(app.state.engine, app.state.settings, "first")
    with app.state.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(lease_until=time.time() - 1))
    second = claim(app.state.engine, app.state.settings, "second")
    assert second["attempts"] == 2 and first["lease_token"] != second["lease_token"]
    assert not guarded_update(app.state.engine, first, processed=999)
    run_job(app.state.engine, app.state.settings, first)
    run_job(app.state.engine, app.state.settings, second)
    result = account.get(f"/api/jobs/{job_id}").json()
    assert result["status"] == "completed" and result["attempts"] == 2
    assert account.get(f"/api/jobs/{job_id}/download/cleaned").status_code == 200


def test_last_expired_attempt_becomes_terminal(app, account):
    job_id = upload(account).json()["id"]
    with app.state.engine.begin() as conn:
        conn.execute(
            update(jobs).where(jobs.c.id == job_id).values(status="running", attempts=3, lease_until=0)
        )
    assert claim(app.state.engine, app.state.settings, "worker") is None
    assert account.get(f"/api/jobs/{job_id}").json()["status"] == "failed"


def test_two_workers_only_one_claims(app, account):
    upload(account)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda name: claim(app.state.engine, app.state.settings, name), ["one", "two"])
        )
    assert sum(r is not None for r in results) == 1


def test_transient_failure_retries(app, account):
    job_id = upload(account).json()["id"]
    first = claim(app.state.engine, app.state.settings, "worker")
    with patch("rowforge.worker.process", side_effect=OSError("disk unavailable")):
        run_job(app.state.engine, app.state.settings, first)
    detail = account.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "queued"
    assert claim(app.state.engine, app.state.settings, "worker") is None  # backoff respected
    with app.state.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(available_at=0))
    finish(app)
    assert account.get(f"/api/jobs/{job_id}").json()["status"] == "completed"


def test_size_limit(app, account):
    app.state.settings = replace(app.state.settings, max_upload=10)
    # create_app captures settings: use a separate bounded instance.
    from rowforge.api import create_app

    bounded = create_app(app.state.settings)
    with TestClient(bounded) as client:
        r = client.post(
            "/api/auth/login", json={"email": "owner@example.com", "password": "correct-horse-123"}
        )
        client.headers["X-CSRF-Token"] = r.json()["csrf"]
        assert upload(client).status_code == 413


def test_csv_row_limit(tmp_path):
    source = tmp_path / "in.csv"
    source.write_text("x\n1\n2\n3\n")
    rules = {"required": [], "email": [], "numeric": [], "trim": True, "dedupe": True}
    with pytest.raises(InvalidCSV, match="2-row"):
        process(source, tmp_path / "out", rules, 2, 1, lambda _: None)


def test_no_trim_and_keep_duplicates(app, account):
    job_id = upload(account, "name\n Ada \n Ada \n", {"trim": False, "dedupe": False}).json()["id"]
    finish(app)
    detail = account.get(f"/api/jobs/{job_id}").json()
    assert detail["accepted"] == 2 and detail["duplicates"] == 0
    assert " Ada " in account.get(f"/api/jobs/{job_id}/download/cleaned").text


def test_empty_and_invalid_uploads(account):
    assert upload(account, "").status_code == 422
    assert upload(account, rules={"numeric": [""]}).status_code == 422
    assert (
        account.post(
            "/api/jobs", files={"file": ("bad.txt", "text")}, headers={"Idempotency-Key": "a"}
        ).status_code
        == 422
    )
    assert account.post("/api/jobs", files={"file": ("a.csv", "a\nb")}).status_code == 422


def test_heartbeat_extends_lease(app, account):
    upload(account, "name\na\nb\nc\nd\n")
    job = claim(app.state.engine, app.state.settings, "slow-worker")
    run_job(app.state.engine, app.state.settings, job, batch_delay=0.5)
    with app.state.engine.connect() as conn:
        result = conn.execute(select(jobs).where(jobs.c.id == job["id"])).mappings().one()
    assert result["status"] == "completed"


def test_sample_download(client):
    result = client.get("/api/sample")
    assert result.status_code == 200
    assert len(list(csv.reader(io.StringIO(result.text)))) == 5101


def test_preview_is_bounded_and_private(app, account):
    content = "name,email\n" + "".join(f"Customer {i},bad-email\n" for i in range(15))
    job_id = upload(account, content, {"email": ["email"]}).json()["id"]
    assert account.get(f"/api/jobs/{job_id}/preview/errors").status_code == 409
    finish(app)
    result = account.get(f"/api/jobs/{job_id}/preview/errors").json()
    assert len(result["rows"]) == 10 and result["more_rows"]
    assert result["columns"] == ["source_line", "validation_errors", "name", "email"]
    assert result["rows"][0][1] == "email: invalid email format"
    assert account.get(f"/api/jobs/{job_id}/preview/cleaned").json()["rows"] == []
    assert account.get(f"/api/jobs/{job_id}/preview/anything").status_code == 404
    with TestClient(app) as other:
        response = other.post(
            "/api/auth/register", json={"email": "preview@example.com", "password": "preview-test-pass"}
        )
        assert response.status_code == 200
        assert other.get(f"/api/jobs/{job_id}/preview/errors").status_code == 404


def test_preview_truncates_wide_long_rows(app, account):
    content = ",".join(f"col{i}" for i in range(15)) + "\n" + ",".join(["x" * 300] * 15) + "\n"
    job_id = upload(account, content).json()["id"]
    finish(app)
    preview = account.get(f"/api/jobs/{job_id}/preview/cleaned").json()
    assert preview["more_columns"] and len(preview["columns"]) == 12
    assert len(preview["rows"][0][0]) == 200
    assert (
        len(list(csv.reader(io.StringIO(account.get(f"/api/jobs/{job_id}/download/cleaned").text)))[1][0])
        == 300
    )


def test_concurrent_admission_cap_and_retry(app, account):
    ids = [upload(account, key=f"job-{i}").json()["id"] for i in range(19)]

    # Independent clients share the same valid session, as simultaneous browser requests would.
    def submit(index):
        with TestClient(app) as client:
            client.cookies.update(account.cookies)
            client.headers["X-CSRF-Token"] = account.headers["X-CSRF-Token"]
            return upload(client, key=f"racing-{index}").status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit, [1, 2]))
    assert sorted(statuses) == [201, 429]
    assert len(account.get("/api/jobs").json()) == 20
    # Idempotent retries are still valid at capacity.
    assert upload(account, key="job-0").json()["id"] == ids[0]
    account.post(f"/api/jobs/{ids[0]}/cancel")
    assert upload(account, key="replacement").status_code == 201
    assert account.post(f"/api/jobs/{ids[0]}/retry").status_code == 429
    assert account.get(f"/api/jobs/{ids[0]}").json()["status"] == "cancelled"


def test_concurrent_same_key_returns_same_job(app, account):
    def submit(_):
        with TestClient(app) as client:
            client.cookies.update(account.cookies)
            client.headers["X-CSRF-Token"] = account.headers["X-CSRF-Token"]
            response = upload(client)
            assert response.status_code == 201, response.text
            return response.json()["id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(submit, [1, 2]))
    assert ids[0] == ids[1]
    assert len(list((app.state.settings.data_dir / "inputs").iterdir())) == 1


def test_mapping_validates_destination_and_preserves_accounting(app, account):
    source = "Customer Number,Email Address,notes\n1,ada@example.com,hello\n1,ada@example.com,hello\n2,bad,no\n"
    rules = {"mapping": {"Customer Number": "customer_id", "Email Address": "email"},
             "required": ["customer_id"], "email": ["email"]}
    job_id = upload(account, source, rules).json()["id"]
    finish(app)
    detail = account.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"
    assert (detail["processed"], detail["accepted"], detail["rejected"], detail["duplicates"]) == (3, 1, 1, 1)
    cleaned = account.get(f"/api/jobs/{job_id}/download/cleaned").text
    assert list(csv.reader(io.StringIO(cleaned))) == [
        ["customer_id", "email", "notes"], ["1", "ada@example.com", "hello"]]
    assert "email: invalid email format" in account.get(f"/api/jobs/{job_id}/download/errors").text
    assert detail["rules"]["mapping"] == rules["mapping"]


@pytest.mark.parametrize("mapping,message", [
    ({"missing": "id"}, "Missing mapping columns"),
    ({"name": "email"}, "duplicate destination columns"),
])
def test_mapping_invalid_file_fails_without_publishing(app, account, mapping, message):
    job_id = upload(account, rules={"mapping": mapping}).json()["id"]
    finish(app)
    detail = account.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert message in detail["error"]
    assert account.get(f"/api/jobs/{job_id}/download/cleaned").status_code == 409


@pytest.mark.parametrize("mapping", [
    {"name": ""}, {" name": "id", "name ": "other"},
    {"name": "id", "email": "id"}, {"name": "x" * 201},
])
def test_invalid_mapping_configuration_rejected(account, mapping):
    assert upload(account, rules={"mapping": mapping}).status_code == 422


def test_mapping_changes_idempotency_fingerprint(account):
    assert upload(account, rules={"mapping": {"name": "full_name"}}).status_code == 201
    assert upload(account, rules={"mapping": {"name": "customer_name"}}).status_code == 409
