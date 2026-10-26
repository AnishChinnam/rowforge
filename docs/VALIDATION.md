# Validation record

Local validation on macOS / Python 3.13 (October 26, 2026):

- Browser: created an account, queued the generated sample file, observed real worker completion, and downloaded the error report.
- Sample result: 5,100 data rows, 4,554 accepted, 454 rejected, 92 duplicates. Counts sum to the input count.
- Recovery experiment: 50,000 rows / 1,766,697 input bytes. Sent SIGKILL after 500 rows, started two replacement workers, and recovered in 3.091 seconds including the configured two-second lease wait. Final output contained 50,000 unique IDs, zero duplicate IDs, and two attempts.
- The recovery time is one observed local run, **not** a general benchmark or production guarantee. The normal lease is 15 seconds; this experiment explicitly sets a two-second lease.
- Automated SQLite/API/worker tests: **33 passed**, including concurrent quota admission, concurrent idempotency, and bounded private previews. Live PostgreSQL and Redis tests are included in `tests/test_services.py` and in the service CI job.

Docker, PostgreSQL, and Redis binaries are not installed on this Mac, so those checks were run on GitHub rather than locally. Local service tests are skipped unless `POSTGRES_TEST_URL` and `REDIS_TEST_URL` are supplied.

## Verified GitHub Actions run

[Run 40411211056](https://github.com/AnishChinnam/rowforge/actions/runs/40411211056) passed all five jobs on October 26, 2026:

- Python 3.11, 3.12, and 3.13: lint and the local/API/worker test suite.
- PostgreSQL 17 and Redis 7: actual service integration checks plus the worker SIGKILL recovery experiment.
- Docker Compose: built the image, launched PostgreSQL/Redis/API/two worker containers, and passed the live HTTP smoke test (authentication, idempotency, processing, row accounting, downloads, private previews, logout).

Reproduce the crash experiment with `python scripts/verify_recovery.py`. It uses a temporary database and writes its latest measured report to `output/recovery-report.json`.
