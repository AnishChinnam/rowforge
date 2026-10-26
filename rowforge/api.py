import csv
import hashlib
import io
import json
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .db import auth_limits, connect, events, jobs, record, sessions, users
from .queue import Wakeup
from .security import digest, hash_password, verify_password

STATIC = Path(__file__).parent / "static"


class IdempotentReplay(Exception):
    pass


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=128)


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: list[str] = Field(default_factory=list, max_length=100)
    email: list[str] = Field(default_factory=list, max_length=100)
    numeric: list[str] = Field(default_factory=list, max_length=100)
    trim: bool = True
    dedupe: bool = True


def public_job(row):
    keys = [
        "id",
        "filename",
        "input_bytes",
        "rules",
        "status",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "attempts",
        "processed",
        "accepted",
        "rejected",
        "duplicates",
        "error",
        "worker_id",
    ]
    value = {k: row[k] for k in keys}
    value["progress"] = (
        100
        if row["status"] == "completed"
        else min(95, int(row["progress_bytes"] / max(1, row["input_bytes"]) * 100))
    )
    return value


def create_app(settings=None):
    settings = settings or Settings.from_env()
    engine = connect(settings)
    wakeup = Wakeup(settings.redis_url)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()

    app = FastAPI(title="RowForge API", version="0.1.0", lifespan=lifespan)
    app.state.engine, app.state.settings = engine, settings

    @app.middleware("http")
    async def boundary(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.url.path == "/api/jobs":
                length = request.headers.get("content-length")
                if not length or not length.isdigit():
                    return PlainTextResponse("Upload requires Content-Length", status_code=411)
                if int(length) > settings.max_upload + 64 * 1024:
                    return PlainTextResponse("Upload exceeds the request size limit", status_code=413)
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin and origin != expected:
                return PlainTextResponse("Cross-origin mutations are not allowed", status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("Cross-site mutations are not allowed", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path != "/docs":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def identity(request: Request):
        token = request.cookies.get("rowforge_session", "")
        with engine.connect() as conn:
            row = (
                conn.execute(
                    select(sessions.c.user_id, sessions.c.csrf, users.c.email)
                    .join(users, sessions.c.user_id == users.c.id)
                    .where(sessions.c.token_hash == digest(token), sessions.c.expires_at > time.time())
                )
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(401, "Sign in to continue.")
        if request.method not in {"GET", "HEAD"}:
            supplied = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(supplied, row["csrf"]):
                raise HTTPException(403, "Refresh the page and try again (CSRF token missing).")
        return dict(row)

    def throttle(request, email):
        # Shared DB counters limit repeated authentication attempts across API processes.
        key = digest(f"{request.client.host if request.client else 'unknown'}:{email}")
        now = time.time()
        with engine.begin() as conn:
            conn.execute(delete(auth_limits).where(auth_limits.c.window < now - 600))
            # A nested savepoint handles concurrent first inserts without aborting PostgreSQL transactions.
            try:
                with conn.begin_nested():
                    conn.execute(auth_limits.insert().values(key=key, window=now, count=0))
            except IntegrityError:
                pass
            result = conn.execute(
                update(auth_limits)
                .where(auth_limits.c.key == key, auth_limits.c.count < 20)
                .values(count=auth_limits.c.count + 1)
            )
            limited = result.rowcount == 0
        if limited:
            raise HTTPException(429, "Too many attempts. Try again in ten minutes.")

    def issue_session(conn, user_id, request, response):
        token, csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        conn.execute(delete(sessions).where(sessions.c.expires_at < time.time()))
        if request.cookies.get("rowforge_session"):
            conn.execute(
                delete(sessions).where(sessions.c.token_hash == digest(request.cookies["rowforge_session"]))
            )
        conn.execute(
            sessions.insert().values(
                token_hash=digest(token), user_id=user_id, csrf=csrf, expires_at=time.time() + 86400
            )
        )
        response.set_cookie(
            "rowforge_session",
            token,
            httponly=True,
            samesite="lax",
            secure=settings.secure_cookies,
            max_age=86400,
        )
        return csrf

    @app.post("/api/auth/{action}")
    def authenticate(action: str, credentials: Credentials, request: Request, response: Response):
        if action not in {"register", "login"}:
            raise HTTPException(404)
        email = credentials.email.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise HTTPException(422, "Enter a valid email address.")
        throttle(request, email)
        with engine.begin() as conn:
            user = conn.execute(select(users).where(users.c.email == email)).mappings().first()
            if action == "register":
                if user:
                    raise HTTPException(409, "An account with this email already exists.")
                user_id = str(uuid.uuid4())
                try:
                    conn.execute(
                        users.insert().values(
                            id=user_id,
                            email=email,
                            password_hash=hash_password(credentials.password),
                            created_at=time.time(),
                        )
                    )
                except IntegrityError:
                    raise HTTPException(409, "An account with this email already exists.") from None
            else:
                if not user or not verify_password(credentials.password, user["password_hash"]):
                    # Equalize the expensive part for unknown accounts.
                    if not user:
                        hash_password(credentials.password)
                    raise HTTPException(401, "Email or password is incorrect.")
                user_id = user["id"]
            csrf = issue_session(conn, user_id, request, response)
        return {"email": email, "csrf": csrf}

    @app.get("/api/me")
    def me(user=Depends(identity)):
        return {"email": user["email"], "csrf": user["csrf"]}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user=Depends(identity)):
        with engine.begin() as conn:
            conn.execute(
                delete(sessions).where(sessions.c.token_hash == digest(request.cookies["rowforge_session"]))
            )
        response.delete_cookie("rowforge_session")
        return {"ok": True}

    @app.get("/api/jobs")
    def list_jobs(user=Depends(identity)):
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    select(jobs)
                    .where(jobs.c.user_id == user["user_id"])
                    .order_by(jobs.c.created_at.desc())
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return [public_job(row) for row in rows]

    def owned(job_id, user):
        with engine.connect() as conn:
            row = (
                conn.execute(select(jobs).where(jobs.c.id == job_id, jobs.c.user_id == user["user_id"]))
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(404, "Job not found.")
        return dict(row)

    def reserve_capacity(conn, user_id, idempotency_key=None):
        # A write lock on the owner's row serializes admission for this account.
        # Works as a row lock on PostgreSQL and a writer lock on SQLite.
        conn.execute(update(users).where(users.c.id == user_id).values(email=users.c.email))
        if (
            idempotency_key
            and conn.execute(
                select(jobs.c.id).where(jobs.c.user_id == user_id, jobs.c.idempotency_key == idempotency_key)
            ).first()
        ):
            raise IdempotentReplay
        count = conn.execute(
            select(func.count())
            .select_from(jobs)
            .where(jobs.c.user_id == user_id, jobs.c.status.in_(["queued", "running"]))
        ).scalar_one()
        if count >= 20:
            raise HTTPException(429, "You already have 20 active jobs. Wait or cancel one.")

    @app.get("/api/jobs/{job_id}")
    def detail(job_id: str, user=Depends(identity)):
        row = owned(job_id, user)
        with engine.connect() as conn:
            history = (
                conn.execute(
                    select(events.c.created_at, events.c.kind, events.c.message)
                    .where(events.c.job_id == job_id)
                    .order_by(events.c.id.desc())
                    .limit(40)
                )
                .mappings()
                .all()
            )
        return {**public_job(row), "events": [dict(e) for e in history]}

    @app.post("/api/jobs", status_code=201)
    def upload(
        request: Request, file: UploadFile = File(...), rules: str = Form("{}"), user=Depends(identity)
    ):
        try:
            parsed = Rules.model_validate_json(rules).model_dump()
        except ValueError:
            raise HTTPException(422, "Rules must contain valid column lists and boolean options.") from None
        for field in ("required", "email", "numeric"):
            parsed[field] = list(dict.fromkeys(v.strip() for v in parsed[field]))
            if any(not v or len(v) > 200 for v in parsed[field]):
                raise HTTPException(422, "Rule column names must contain 1–200 characters.")
        filename = (file.filename or "upload.csv").replace("\\", "/").split("/")[-1][:255]
        if not filename.lower().endswith(".csv"):
            raise HTTPException(422, "Choose a .csv file.")
        key = request.headers.get("idempotency-key", "")
        if not 1 <= len(key) <= 100:
            raise HTTPException(422, "Provide an Idempotency-Key header (1–100 characters).")
        job_id = str(uuid.uuid4())
        directory = settings.data_dir.resolve() / "inputs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{job_id}.csv"
        total = 0
        hasher = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode())
        # Starlette spools the multipart file; bounded reads avoid copying it all into memory.
        try:
            with open(path, "wb") as stream:
                while chunk := file.file.read(64 * 1024):
                    total += len(chunk)
                    if total > settings.max_upload:
                        raise HTTPException(413, "File exceeds the 10 MiB upload limit.")
                    hasher.update(chunk)
                    stream.write(chunk)
            if not total:
                raise HTTPException(422, "The file is empty.")
            fingerprint = hasher.hexdigest()
            now = time.time()

            with engine.connect() as conn:
                existing = (
                    conn.execute(
                        select(jobs).where(jobs.c.user_id == user["user_id"], jobs.c.idempotency_key == key)
                    )
                    .mappings()
                    .first()
                )
            if existing:
                path.unlink(missing_ok=True)
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(
                        409, "That idempotency key was used for a different file or rule set."
                    )
                return public_job(existing)

            def save():
                with engine.begin() as conn:
                    reserve_capacity(conn, user["user_id"], key)
                    conn.execute(
                        jobs.insert().values(
                            id=job_id,
                            user_id=user["user_id"],
                            idempotency_key=key,
                            fingerprint=fingerprint,
                            filename=filename,
                            input_path=str(path),
                            input_bytes=total,
                            rules=parsed,
                            status="queued",
                            created_at=now,
                            updated_at=now,
                            available_at=now,
                            attempts=0,
                            processed=0,
                            accepted=0,
                            rejected=0,
                            duplicates=0,
                            progress_bytes=0,
                        )
                    )
                    record(conn, job_id, "queued", "Upload saved. Waiting for an available worker.")

            try:
                save()
            except (IntegrityError, IdempotentReplay):
                with engine.connect() as conn:
                    existing = (
                        conn.execute(
                            select(jobs).where(
                                jobs.c.user_id == user["user_id"], jobs.c.idempotency_key == key
                            )
                        )
                        .mappings()
                        .first()
                    )
                if not existing:
                    raise
                path.unlink(missing_ok=True)
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(
                        409, "That idempotency key was used for a different file or rule set."
                    )
                return public_job(existing)
            wakeup.notify()
            return detail(job_id, user)
        except Exception:
            # A committed job owns its input, even if a later response or Redis hint fails.
            with engine.connect() as conn:
                committed = conn.execute(select(jobs.c.id).where(jobs.c.id == job_id)).first()
            if not committed:
                path.unlink(missing_ok=True)
            raise
        finally:
            file.file.close()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, user=Depends(identity)):
        owned(job_id, user)
        now = time.time()
        with engine.begin() as conn:
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status.in_(["queued", "running"]))
                .values(
                    status="cancelled", updated_at=now, finished_at=now, lease_until=None, lease_token=None
                )
            )
            if not result.rowcount:
                raise HTTPException(409, "Only queued or running jobs can be cancelled.")
            record(
                conn, job_id, "cancelled", "Cancelled by owner; any stale worker result will be discarded."
            )
        return detail(job_id, user)

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, user=Depends(identity)):
        owned(job_id, user)
        with engine.begin() as conn:
            reserve_capacity(conn, user["user_id"])
            result = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status.in_(["failed", "cancelled"]))
                .values(
                    status="queued",
                    attempts=0,
                    available_at=time.time(),
                    updated_at=time.time(),
                    finished_at=None,
                    error=None,
                    processed=0,
                    accepted=0,
                    rejected=0,
                    duplicates=0,
                    progress_bytes=0,
                )
            )
            if not result.rowcount:
                raise HTTPException(409, "Only failed or cancelled jobs can be retried.")
            record(conn, job_id, "retried", "Owner requested a fresh processing attempt.")
        wakeup.notify()
        return detail(job_id, user)

    @app.get("/api/jobs/{job_id}/preview/{kind}")
    def preview(job_id: str, kind: str, user=Depends(identity)):
        row = owned(job_id, user)
        if kind not in {"cleaned", "errors"}:
            raise HTTPException(404)
        if row["status"] != "completed":
            raise HTTPException(409, "Preview is available only after completion.")
        path = row["result_path" if kind == "cleaned" else "errors_path"]
        if not path or not Path(path).is_file():
            raise HTTPException(410, "Result file is unavailable. Contact the server operator.")
        with open(path, encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            columns = next(reader, [])
            rows = []
            for _, values in zip(range(11), reader):
                rows.append(values)
        # The file itself is complete. Limit display to 10 rows / 12 columns / 200 characters per cell.
        return {
            "columns": [v[:200] for v in columns[:12]],
            "rows": [[v[:200] for v in values[:12]] for values in rows[:10]],
            "more_rows": len(rows) > 10,
            "more_columns": len(columns) > 12,
            "display_limit": 10,
        }

    @app.get("/api/jobs/{job_id}/download/{kind}")
    def download(job_id: str, kind: str, user=Depends(identity)):
        row = owned(job_id, user)
        if kind not in {"cleaned", "errors"}:
            raise HTTPException(404)
        if row["status"] != "completed":
            raise HTTPException(409, "Results are available only after completion.")
        path = row["result_path" if kind == "cleaned" else "errors_path"]
        if not path or not Path(path).is_file():
            raise HTTPException(410, "Result file is unavailable. Contact the server operator.")
        return FileResponse(path, media_type="text/csv", filename=f"{kind}-{row['filename']}")

    @app.get("/api/sample")
    def sample():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["customer_id", "name", "email", "amount", "region"])
        for i in range(1, 5001):
            row = [
                i,
                f"  Customer {i}  ",
                "invalid-email" if i % 17 == 0 else f"customer{i}@example.com",
                "not-a-number" if i % 31 == 0 else f"{i * 1.27:.2f}",
                ["West", "East", "North"][i % 3],
            ]
            writer.writerow(row)
            if i % 50 == 0:
                writer.writerow(row)
        return PlainTextResponse(
            output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="sample-customers.csv"'},
        )

    @app.get("/health")
    def health():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": engine.dialect.name, "redis_configured": bool(settings.redis_url)}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
