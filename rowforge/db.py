import time
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)

metadata = MetaData()
users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False),
    Column("csrf", String(64), nullable=False),
    Column("expires_at", Float, nullable=False),
)
jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", ForeignKey("users.id"), nullable=False, index=True),
    Column("idempotency_key", String(100), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("input_path", Text, nullable=False),
    Column("input_bytes", Integer, nullable=False),
    Column("rules", JSON, nullable=False),
    Column("status", String(20), nullable=False, index=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("available_at", Float, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_token", String(36)),
    Column("lease_until", Float),
    Column("worker_id", String(100)),
    Column("processed", Integer, nullable=False, default=0),
    Column("accepted", Integer, nullable=False, default=0),
    Column("rejected", Integer, nullable=False, default=0),
    Column("duplicates", Integer, nullable=False, default=0),
    Column("progress_bytes", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("result_path", Text),
    Column("errors_path", Text),
    UniqueConstraint("user_id", "idempotency_key", name="uq_owner_idempotency"),
)
events = Table(
    "job_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", ForeignKey("jobs.id"), nullable=False, index=True),
    Column("created_at", Float, nullable=False),
    Column("kind", String(40), nullable=False),
    Column("message", Text, nullable=False),
)
auth_limits = Table(
    "auth_limits",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("window", Float, nullable=False),
    Column("count", Integer, nullable=False),
)


def connect(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite:///"):
        path = settings.database_url.removeprefix("sqlite:///")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30}
        if settings.database_url.startswith("sqlite")
        else {},
    )
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def pragma(dbapi, _):
            dbapi.execute("PRAGMA journal_mode=WAL")
            dbapi.execute("PRAGMA foreign_keys=ON")

    metadata.create_all(engine)
    return engine


def record(conn, job_id, kind, message):
    conn.execute(events.insert().values(job_id=job_id, kind=kind, message=message, created_at=time.time()))
