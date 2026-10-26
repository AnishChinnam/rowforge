import logging
import shutil
import threading
import time
import uuid

from sqlalchemy import and_, or_, select, update

from .db import jobs, record
from .processing import InvalidCSV, LostLease, process
from .queue import Wakeup

log = logging.getLogger(__name__)


def claim(engine, settings, worker_id):
    """CAS claims work on SQLite and PostgreSQL; no select-then-unconditional-write race."""
    now = time.time()
    eligible = or_(
        and_(jobs.c.status == "queued", jobs.c.available_at <= now),
        and_(jobs.c.status == "running", jobs.c.lease_until < now),
    )
    with engine.begin() as conn:
        exhausted = (
            conn.execute(select(jobs.c.id).where(eligible, jobs.c.attempts >= settings.max_attempts))
            .scalars()
            .all()
        )
        for job_id in exhausted:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, eligible, jobs.c.attempts >= settings.max_attempts)
                .values(
                    status="failed",
                    finished_at=now,
                    updated_at=now,
                    error="Retry limit reached after worker interruption.",
                    lease_token=None,
                    lease_until=None,
                )
            )
            if result.rowcount:
                record(conn, job_id, "failed", "Retry limit reached; manual retry is available.")
        candidates = (
            conn.execute(
                select(jobs.c.id)
                .where(eligible, jobs.c.attempts < settings.max_attempts)
                .order_by(jobs.c.created_at)
                .limit(16)
            )
            .scalars()
            .all()
        )
        for job_id in candidates:
            token = str(uuid.uuid4())
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, eligible, jobs.c.attempts < settings.max_attempts)
                .values(
                    status="running",
                    lease_token=token,
                    lease_until=now + settings.lease_seconds,
                    worker_id=worker_id,
                    attempts=jobs.c.attempts + 1,
                    updated_at=now,
                    started_at=now,
                    processed=0,
                    accepted=0,
                    rejected=0,
                    duplicates=0,
                    progress_bytes=0,
                    error=None,
                )
            )
            if result.rowcount:
                job = dict(conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one())
                record(conn, job_id, "claimed", f"Worker {worker_id} claimed attempt {job['attempts']}.")
                return job
    return None


def guarded_update(engine, job, **values):
    now = time.time()
    with engine.begin() as conn:
        result = conn.execute(
            update(jobs)
            .where(
                jobs.c.id == job["id"],
                jobs.c.status == "running",
                jobs.c.lease_token == job["lease_token"],
                jobs.c.lease_until > now,
            )
            .values(updated_at=now, **values)
        )
        return result.rowcount == 1


def run_job(engine, settings, job, batch_delay=0):
    directory = settings.data_dir.resolve() / "results" / job["id"] / job["lease_token"]
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(max(0.05, settings.lease_seconds / 3)):
            try:
                if not guarded_update(engine, job, lease_until=time.time() + settings.lease_seconds):
                    lost.set()
                    return
            except Exception:
                log.exception("Heartbeat failed")
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    published = False
    try:

        def checkpoint(counts):
            if lost.is_set() or not guarded_update(engine, job, **counts):
                raise LostLease
            if batch_delay:
                time.sleep(batch_delay)

        counts = process(
            job["input_path"], directory, job["rules"], settings.max_rows, settings.batch_size, checkpoint
        )
        # Files are closed before publication. Only the current unexpired owner can publish paths.
        now = time.time()
        with engine.begin() as conn:
            result = conn.execute(
                update(jobs)
                .where(
                    jobs.c.id == job["id"],
                    jobs.c.status == "running",
                    jobs.c.lease_token == job["lease_token"],
                    jobs.c.lease_until > now,
                )
                .values(
                    **counts,
                    status="completed",
                    finished_at=now,
                    updated_at=now,
                    lease_until=None,
                    lease_token=None,
                    result_path=str(directory / "cleaned.csv"),
                    errors_path=str(directory / "errors.csv"),
                )
            )
            if result.rowcount:
                record(
                    conn,
                    job["id"],
                    "completed",
                    f"Processed {counts['processed']:,} rows; results published.",
                )
                published = True
    except LostLease:
        log.info("Lease lost or job cancelled: %s", job["id"])
    except Exception as exc:
        deterministic = isinstance(exc, InvalidCSV)
        terminal = deterministic or job["attempts"] >= settings.max_attempts
        message = str(exc) if deterministic else "Processing interrupted by a storage or worker error."
        now = time.time()
        with engine.begin() as conn:
            result = conn.execute(
                update(jobs)
                .where(
                    jobs.c.id == job["id"],
                    jobs.c.status == "running",
                    jobs.c.lease_token == job["lease_token"],
                    jobs.c.lease_until > now,
                )
                .values(
                    status="failed" if terminal else "queued",
                    error=message,
                    available_at=now + 2 ** job["attempts"],
                    lease_until=None,
                    lease_token=None,
                    updated_at=now,
                    finished_at=now if terminal else None,
                )
            )
            if result.rowcount:
                record(conn, job["id"], "failed" if terminal else "retry_scheduled", message)
        if not deterministic:
            log.exception("Job processing error")
    finally:
        stop.set()
        thread.join(timeout=2)
        if not published:
            shutil.rmtree(directory, ignore_errors=True)


def work(engine, settings, stop=None, batch_delay=0, worker_id=None):
    stop = stop or threading.Event()
    worker_id = worker_id or "worker-" + uuid.uuid4().hex[:8]
    wakeup = Wakeup(settings.redis_url)
    log.info("Worker %s ready", worker_id)
    while not stop.is_set():
        try:
            job = claim(engine, settings, worker_id)
            if job:
                run_job(engine, settings, job, batch_delay)
            else:
                wakeup.wait()
        except Exception:
            log.exception("Worker loop error; retrying in one second")
            stop.wait(1)
