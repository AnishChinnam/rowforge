import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///data/rowforge.db"
    data_dir: Path = Path("data")
    redis_url: str = ""
    secure_cookies: bool = False
    max_upload: int = 10 * 1024 * 1024
    max_rows: int = 200_000
    lease_seconds: float = 15.0
    max_attempts: int = 3
    batch_size: int = 250

    @classmethod
    def from_env(cls):
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///data/rowforge.db"),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            redis_url=os.getenv("REDIS_URL", ""),
            secure_cookies=os.getenv("COOKIE_SECURE", "0") == "1",
            lease_seconds=max(1.0, float(os.getenv("LEASE_SECONDS", "15"))),
        )
