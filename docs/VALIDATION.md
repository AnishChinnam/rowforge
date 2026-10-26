# Validation record

Local validation on macOS / Python 3.13 (October 26, 2026):

- Browser: created an account, queued the generated sample file, observed real worker completion, and downloaded the error report.
- Sample result: 5,100 data rows, 4,554 accepted, 454 rejected, 92 duplicates. Counts sum to the input count.
- Recovery experiment: 50,000 rows / 1,766,697 input bytes. Sent SIGKILL after 500 rows, started two replacement workers, and recovered in 3.091 seconds including the configured two-second lease wait. Final output contained 50,000 unique IDs, zero duplicate IDs, and two attempts.
- The recovery time is one observed local run, **not** a general benchmark or production guarantee. The normal lease is 15 seconds; this experiment explicitly sets a two-second lease.
- Automated SQLite/API/worker tests: **33 passed**, including concurrent quota admission, concurrent idempotency, and bounded private previews. Live PostgreSQL and Redis tests are included in `tests/test_services.py` and in the service CI job.

Docker, PostgreSQL, and Redis binaries are not installed on this Mac. Therefore the Compose stack and real-service tests were **not** executed locally. Those tests are skipped unless `POSTGRES_TEST_URL` and `REDIS_TEST_URL` are supplied. No remote CI result is claimed.

Reproduce the crash experiment with `python scripts/verify_recovery.py`. It uses a temporary database and writes its latest measured report to `output/recovery-report.json`.
