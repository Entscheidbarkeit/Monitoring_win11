"""Settings for the loopback-only monitoring service."""
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass
class Settings:
    upstream: str = "http://127.0.0.1:11434"
    database: Path = Path("data/monitor.sqlite3")
    sample_interval: float = 1.0
    collect_system: bool = True
    gpu_enabled: bool = True
    request_timeout: float = 600.0
    max_body_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_env(cls):
        settings = cls(
            upstream=os.getenv("LLM_MONITOR_UPSTREAM", "http://127.0.0.1:11434").rstrip("/"),
            database=Path(os.getenv("LLM_MONITOR_DB", "data/monitor.sqlite3")),
            sample_interval=float(os.getenv("LLM_MONITOR_INTERVAL", "1")),
            gpu_enabled=os.getenv("LLM_MONITOR_GPU", "1") != "0",
        )
        if not 0.2 <= settings.sample_interval <= 60:
            raise ValueError("LLM_MONITOR_INTERVAL must be between 0.2 and 60 seconds")
        # A fixed upstream prevents request bodies/headers becoming proxy targets.
        if settings.upstream not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
            raise ValueError("Upstream must be the local Ollama server on port 11434")
        return settings
