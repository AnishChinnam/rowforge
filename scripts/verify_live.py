"""Exercise the real HTTP API and separately running workers using synthetic data."""

import argparse
import csv
import io
import json
import secrets
import time
import uuid

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8082")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=15) as client:
        response = client.get("/health")
        response.raise_for_status()
        database = response.json()["database"]
        response = client.post(
            "/api/auth/register",
            json={"email": f"smoke-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(24)},
        )
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = response.json()["csrf"]
        payload = "id,email,amount\n1,ada@example.com,12\n1,ada@example.com,12\n2,invalid,3\n3,bob@example.com,NaN\n"
        files = {"file": ("smoke.csv", payload, "text/csv")}
        form = {"rules": json.dumps({"email": ["email"], "numeric": ["amount"]})}
        headers = {"Idempotency-Key": "smoke-upload"}
        response = client.post("/api/jobs", files=files, data=form, headers=headers)
        response.raise_for_status()
        job_id = response.json()["id"]
        repeated = client.post("/api/jobs", files=files, data=form, headers=headers)
        repeated.raise_for_status()
        assert repeated.json()["id"] == job_id
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job_id}")
            response.raise_for_status()
            job = response.json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("No completed result within 45 seconds. Check worker logs.")
        assert job["status"] == "completed", job
        assert [job[k] for k in ["processed", "accepted", "rejected", "duplicates"]] == [4, 1, 2, 1]
        result = client.get(f"/api/jobs/{job_id}/download/cleaned")
        result.raise_for_status()
        assert list(csv.reader(io.StringIO(result.text)))[1] == ["1", "ada@example.com", "12"]
        preview = client.get(f"/api/jobs/{job_id}/preview/errors")
        preview.raise_for_status()
        assert len(preview.json()["rows"]) == 2
        assert client.post("/api/logout").status_code == 200
        assert client.get(f"/api/jobs/{job_id}/download/cleaned").status_code == 401
        print(
            json.dumps(
                {
                    "status": "passed",
                    "database": database,
                    "checks": [
                        "authentication",
                        "real worker processing",
                        "idempotency",
                        "row accounting",
                        "cleaned download",
                        "error preview",
                        "session revocation",
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
