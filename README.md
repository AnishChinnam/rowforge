# RowForge

[![Tests](https://github.com/AnishChinnam/rowforge/actions/workflows/tests.yml/badge.svg)](https://github.com/AnishChinnam/rowforge/actions/workflows/tests.yml)

**A private CSV processing workspace with durable jobs, recoverable workers, and downloadable validation reports.**

Upload a UTF-8 CSV, choose required/email/numeric columns, and get a cleaned dataset plus a row-level error report. Processing happens in a separate worker process. Restarting the API doesn't erase work; an interrupted worker's lease expires and another worker restarts the file safely.

## Get the project

```sh
git clone https://github.com/AnishChinnam/rowforge.git
cd rowforge
```

If you already have the project on this Mac, use `cd ~/Desktop/rowforge` instead.

## Run on your Mac

Python 3.11+ is required. This mode uses SQLite and starts the API and a separate worker together; no Docker or Redis is required.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m rowforge demo
```

Open **http://127.0.0.1:8082**. Create a local account (any valid-format email and a password of at least 10 characters). There is no email delivery. Click **Try sample data** to validate a generated 5,100-row customer CSV. All rows are synthetic; results are actually computed, not prefilled. The sample validates `email` and `amount`, requires customer IDs/names/emails, trims whitespace, and removes identical valid rows.

Leave the command running. Ctrl+C stops both processes. Your account, files, and job history remain in `data/`. On later runs, use `.venv/bin/python -m rowforge demo` directly. No sample password or backdoor account is built in.

On macOS, you can also double-click **Start RowForge.command** inside the project folder. A browser-tested sample account may already be signed in on this computer; you can sign out and create your own account.

## PostgreSQL + Redis + multiple workers

Install Docker with Compose support, then:

```sh
docker compose up --build -d --scale worker=2
docker compose logs -f api worker
```

Open the same address, **http://127.0.0.1:8082**. Stop the local demo first to free the port. This stack uses PostgreSQL 17, Redis 7, an API container, and two worker containers. A one-shot init service creates the schema before API/workers start. Database and file volumes persist across `docker compose down`. Do not use `down -v` unless you intend to remove those data volumes.

Compose binds only to loopback. Its default database password is explicitly for local development. For a remote deployment, provide a strong `POSTGRES_PASSWORD`, TLS termination, `COOKIE_SECURE=1`, reverse-proxy upload/rate limits, database backups, and a shared durable file volume. Workers and API must see identical file paths. The current implementation is intended for a single host with a shared filesystem; it is not an S3-backed multi-region service.

## What it demonstrates

- **Durable job state:** PostgreSQL (or SQLite demo) stores uploads, status, attempts, progress, and events.
- **Atomic claiming:** compare-and-swap UPDATEs allow one worker to acquire a job; every claim gets a new random token.
- **Lease recovery:** heartbeats extend a 15-second lease. A crashed worker's job becomes eligible again after expiry.
- **Fenced publication:** an unexpired matching token is required to publish. Every attempt writes into a separate directory; stale workers cannot overwrite the winner's files.
- **Retries:** transient processing/storage errors use bounded exponential delays and a three-attempt budget. Invalid CSVs fail immediately. Failed and cancelled jobs can be retried manually.
- **Idempotent uploads:** a per-account unique key and file/rule fingerprint prevent duplicate jobs when a request is repeated. Reusing a key for different content returns 409.
- **Tenant isolation:** every job read, mutation, and download checks the authenticated owner. Passwords use salted PBKDF2; session cookies are HttpOnly with CSRF-protected mutations.
- **Auditable results:** processed = accepted + rejected + duplicates, with line-numbered validation errors and a job event trail. Preview cleaned or rejected rows directly in the dashboard before downloading.
- **Atomic admission control:** per-account locking keeps concurrent uploads and manual retries within the 20-active-job limit; idempotent replays still succeed at capacity.

### Redis has a deliberately limited role

Redis holds a short-lived wake-up hint. Workers poll PostgreSQL for durable work; Redis is **not** the authoritative queue. Lost hints, Redis outages, or an API crash after DB commit do not lose jobs. This avoids a database/queue dual-write correctness problem. It trades at most roughly one second of idle discovery latency for simpler recovery. There is no claim of exactly-once execution: attempts can repeat, while final result publication is fenced.

## Validation behavior

- Comma-delimited UTF-8, with optional BOM. Up to 10 MiB, 200,000 data rows, and 100 columns.
- Column names are trimmed, must be nonempty/unique, and are case-sensitive.
- Required rules demand a nonblank value; email rules check a basic format, not deliverability; numeric rules require a finite decimal.
- Blank email/numeric values are allowed unless also required.
- Exact-row deduplication happens **after** trimming and validation. Invalid duplicate rows each remain in the error report.
- Source line numbers refer to CSV parser line positions (multiline quoted rows may span physical lines).
- Formula-like strings in downloaded CSVs are prefixed with an apostrophe so opening them in a spreadsheet does not execute them. This also prefixes negative numeric strings; the UI does not silently claim byte-for-byte numeric preservation.
- Progress is approximate input-byte progress, capped at 95% until publication, not a precomputed percentage of rows.

## Verification

```sh
python -m pytest -q
ruff check rowforge tests scripts
python scripts/verify_recovery.py
```

The recovery script uses an isolated temporary database. It uploads 50,000 rows, kills a real worker after processing starts, launches two replacement workers, waits for lease expiry, and checks that exactly 50,000 unique output IDs are published. It writes measured results to `output/recovery-report.json`. It does not touch your workspace or generate fictional throughput numbers.

Tests cover authentication, CSRF, account isolation, idempotency, validation, formula safety, cancellation, concurrent claiming, lease recovery, stale-worker fencing, retry delays, heartbeat extension, and row limits. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/INTERVIEW.md](docs/INTERVIEW.md).

## API

Interactive reference: **http://127.0.0.1:8082/docs**.

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/register`, `/api/auth/login` | Create account or session |
| `GET /api/me` | Current account and CSRF token |
| `POST /api/logout` | Revoke session |
| `POST /api/jobs` | Multipart CSV + JSON rules; `Idempotency-Key` required |
| `GET /api/jobs`, `/api/jobs/{id}` | Owner's jobs, progress, and events |
| `POST /api/jobs/{id}/cancel`, `/retry` | Cancel or retry an eligible job |
| `GET /api/jobs/{id}/download/cleaned`, `/errors` | Download published results |
| `GET /api/jobs/{id}/preview/cleaned`, `/errors` | Private, bounded result previews |
| `GET /api/sample`, `/health` | Synthetic sample / DB health |

Mutations after authentication require the `X-CSRF-Token` returned at login or `/api/me`. All result downloads require the same session cookie. The health endpoint verifies database connectivity; it does not assert that a worker is currently alive or that Redis is healthy.

## Scope

Portfolio software, developed with AI assistance. No email verification, password reset, SSO, per-user storage quotas, automatic file retention, schema migration framework, cloud object storage, or multi-region coordination. Auth throttling is modest DB-backed per-IP/email accounting, not a replacement for edge-level abuse prevention. The 20-active-job limit is serialized per account across uploads and retries. Inactive/orphaned attempt files after a hard kill may require operator cleanup. Do not present it as a production service handling customer data.

GitHub Actions runs Python 3.11/3.12/3.13 checks, real PostgreSQL/Redis integration tests, and a complete Docker Compose smoke test with two workers. [Verified passing run](https://github.com/AnishChinnam/rowforge/actions/runs/40411211056). This is a source repository, not a publicly hosted application.

See [docs/VALIDATION.md](docs/VALIDATION.md) for the actual local checks and service-testing limitations.
