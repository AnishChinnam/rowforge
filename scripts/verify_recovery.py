"""Kill a real worker mid-file, recover with two contenders, and record measured results.

Uses an isolated temporary SQLite DB and files; never modifies the user's workspace.
Run from the repository: python scripts/verify_recovery.py
"""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from rowforge.api import create_app
from rowforge.config import Settings


def wait_until(fn, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError("Verification did not finish within the deadline")


def main():
    with tempfile.TemporaryDirectory(prefix="rowforge-recovery-") as temp:
        root = Path(temp)
        settings = Settings(
            database_url=f"sqlite:///{root}/verify.db", data_dir=root / "files", lease_seconds=2
        )
        app = create_app(settings)
        env = {
            **os.environ,
            "DATABASE_URL": settings.database_url,
            "DATA_DIR": str(settings.data_dir),
            "LEASE_SECONDS": "2",
        }
        workers = []
        with open(root / "worker.log", "w") as log, TestClient(app) as client:

            def spawn(delay):
                child = subprocess.Popen(
                    [sys.executable, "-m", "rowforge", "worker", "--batch-delay", str(delay)],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                workers.append(child)
                return child

            try:
                response = client.post(
                    "/api/auth/register",
                    json={"email": "recovery@example.com", "password": "recovery-test-only"},
                )
                client.headers["X-CSRF-Token"] = response.json()["csrf"]
                rows = 50_000
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["id", "email", "amount"])
                writer.writerows((i, f"user{i}@example.com", i / 10) for i in range(rows))
                body = output.getvalue()
                response = client.post(
                    "/api/jobs",
                    files={"file": ("recovery.csv", body)},
                    data={
                        "rules": json.dumps({"required": ["id"], "email": ["email"], "numeric": ["amount"]})
                    },
                    headers={"Idempotency-Key": "recovery-check"},
                )
                assert response.status_code == 201, response.text
                job_id = response.json()["id"]

                def detail():
                    return client.get(f"/api/jobs/{job_id}").json()

                first = spawn(0.03)
                before = wait_until(
                    lambda: (j if j["processed"] >= 500 else None) if (j := detail()) else None
                )
                first.kill()
                first.wait()
                interrupted_at = time.monotonic()
                spawn(0)
                spawn(0)
                completed = wait_until(
                    lambda: j if (j := detail())["status"] in {"completed", "failed"} else None
                )
                elapsed = time.monotonic() - interrupted_at
                assert completed["status"] == "completed", completed
                assert completed["attempts"] == 2, completed
                assert completed["accepted"] == rows and completed["processed"] == rows
                data = client.get(f"/api/jobs/{job_id}/download/cleaned").text
                parsed = list(csv.reader(io.StringIO(data)))
                assert len(parsed) == rows + 1
                assert len({row[0] for row in parsed[1:]}) == rows
                report = {
                    "scenario": "SIGKILL mid-file; two replacement workers",
                    "database": "sqlite",
                    "input_rows": rows,
                    "input_bytes": len(body.encode()),
                    "rows_before_kill": before["processed"],
                    "attempts": completed["attempts"],
                    "output_rows": len(parsed) - 1,
                    "duplicate_output_ids": 0,
                    "recovery_seconds": round(elapsed, 3),
                    "includes_lease_wait": True,
                    "verified_at_unix": time.time(),
                }
                Path("output").mkdir(exist_ok=True)
                Path("output/recovery-report.json").write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps(report, indent=2))
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.terminate()
                for worker in workers:
                    try:
                        worker.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait()


if __name__ == "__main__":
    main()
