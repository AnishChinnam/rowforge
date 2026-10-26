# Explain it before you put it on your résumé

## A 60-second demo

1. Create a local account; click **Try sample data**.
2. Show the live job state, accepted/rejected/duplicate counts, and activity trail.
3. Download the cleaned file and error report. Explain one failed email/number row.
4. Run `python scripts/verify_recovery.py`; show the measured report.
5. Explain why a second account cannot access the first account's job or files.

## Questions you should be able to answer

- Why isn't a FastAPI BackgroundTask sufficient for crash recovery?
- Where is the durable queue? What happens if Redis is down?
- What race exists between selecting a queued job and updating it? How does the conditional UPDATE solve it?
- Why is lease expiry alone insufficient? What does the attempt token fence?
- What happens if the same job executes twice? Why is that different from publishing a result twice?
- How do retries distinguish invalid data from infrastructure failures?
- Why restart a CSV from the beginning instead of resuming at an arbitrary byte offset?
- What happens to multiline CSV records, dedupe state, and invalid duplicate rows?
- What limitations do SQLite, shared filesystem storage, and in-memory dedupe impose?
- Why do the output files escape formula-like values, including negative strings?
- What must change before accepting arbitrary sensitive customer data on the public Internet?

## Honest résumé wording

After running and understanding it:

> Built a CSV processing platform with authenticated workspaces, SQL-backed job leases, idempotent uploads, validation reports, and recoverable background workers; verified interruption recovery and stale-worker isolation through automated tests.

Only mention PostgreSQL/Redis deployment as tested experience after running Compose and verifying it. Use measured recovery results from your own machine; do not claim an invented user count, production uptime, exactly-once execution, or benchmark throughput. This project was developed with AI assistance; understanding and independently modifying the code is part of making it yours.
