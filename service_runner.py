"""Windows Task Scheduler entry point; no admin token or interactive window needed."""
import os
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    app_dir = Path(__file__).resolve().parent
    root = app_dir.parent
    (root / "logs").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    os.chdir(app_dir)
    os.environ["LLM_MONITOR_DB"] = str(root / "data" / "monitor.sqlite3")
    log_config = {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {"file": {"class": "logging.handlers.RotatingFileHandler", "formatter": "plain",
            "filename": str(root / "logs" / "monitor.log"), "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3, "encoding": "utf-8"}},
        "root": {"handlers": ["file"], "level": "INFO"},
        "loggers": {"uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
                    "httpx": {"level": "WARNING"}},
    }
    uvicorn.run("main:app", host="127.0.0.1", port=18080, workers=1, access_log=False, log_config=log_config)
