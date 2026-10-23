"""SQLite persistence. Short, locked transactions shared by sampler and API."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


class Database:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.lock:
            self.connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS samples (
                    ts REAL PRIMARY KEY, timestamp TEXT NOT NULL,
                    cpu REAL, ram REAL, gpu REAL, vram REAL,
                    ram_used REAL, vram_used REAL, snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS minutes (
                    ts REAL PRIMARY KEY, cpu REAL, ram REAL, gpu REAL, vram REAL,
                    ram_used REAL, vram_used REAL, samples INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS configs (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL,
                    endpoint TEXT NOT NULL, model TEXT NOT NULL, response_model TEXT,
                    config_id TEXT REFERENCES configs(id), client TEXT, streaming INTEGER NOT NULL,
                    state TEXT NOT NULL, http_status INTEGER, finish_reason TEXT, error TEXT,
                    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, cached_input_tokens INTEGER,
                    usage_complete INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL, ttft_ms REAL, load_ms REAL,
                    prompt_eval_ms REAL, eval_ms REAL, engine_tps REAL, wall_tps REAL,
                    response_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS requests_started ON requests(started DESC);
                CREATE INDEX IF NOT EXISTS requests_model ON requests(model, started DESC);
            """)
            self.connection.execute("UPDATE requests SET state='interrupted', error='Monitor restarted before request completed' WHERE state='running'")
            self.connection.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            with self.connection:
                yield self.connection

    def close(self):
        with self.lock:
            self.connection.close()

    def add_sample(self, sample):
        ts = time.time()
        devices = sample["gpu"]["devices"]
        percentages = [d["utilization_percent"] for d in devices if d["utilization_percent"] is not None]
        used = [d["memory_used_bytes"] for d in devices if d["memory_used_bytes"] is not None]
        total = [d["memory_total_bytes"] for d in devices if d["memory_total_bytes"] is not None]
        vram_used = sum(used) if used else None
        vram = sum(used) / sum(total) * 100 if total and sum(total) and len(used) == len(total) else None
        with self.transaction() as conn:
            conn.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)", (
                ts, sample["timestamp"], sample["cpu"]["percent"], sample["memory"]["percent"],
                sum(percentages) / len(percentages) if percentages else None, vram,
                sample["memory"]["used_bytes"], vram_used, encode(sample),
            ))

    def maintain(self, now=None):
        now = time.time() if now is None else now
        minute = int(now // 60) * 60
        with self.transaction() as conn:
            latest = conn.execute("SELECT MAX(ts) FROM minutes").fetchone()[0]
            since = max(now - 7 * 86400, latest if latest is not None else now - 7 * 86400)
            conn.execute("""INSERT OR REPLACE INTO minutes
                SELECT CAST(ts/60 AS INTEGER)*60, AVG(cpu),AVG(ram),AVG(gpu),AVG(vram),
                       AVG(ram_used),AVG(vram_used),COUNT(*) FROM samples
                WHERE ts>=? AND ts<? GROUP BY CAST(ts/60 AS INTEGER)""", (since, minute))
            conn.execute("DELETE FROM samples WHERE ts<?", (now - 7 * 86400,))
            conn.execute("DELETE FROM minutes WHERE ts<?", (now - 30 * 86400,))
            conn.execute("DELETE FROM requests WHERE started<? AND state!='running'", (now - 30 * 86400,))
            conn.execute("DELETE FROM configs WHERE id NOT IN (SELECT config_id FROM requests WHERE config_id IS NOT NULL)")

    def start_request(self, record, config):
        config_text = encode(config)
        config_id = hashlib.sha256(config_text.encode()).hexdigest()
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO configs VALUES (?,?)", (config_id, config_text))
            conn.execute("""INSERT INTO requests
                (id,started,endpoint,model,config_id,client,streaming,state)
                VALUES (?,?,?,?,?,?,?,'running')""", (
                record["id"], record["started"], record["endpoint"], record["model"],
                config_id, record["client"], int(record["streaming"]),
            ))

    def finish_request(self, request_id, values):
        allowed = {
            "finished", "response_model", "state", "http_status", "finish_reason", "error",
            "input_tokens", "output_tokens", "total_tokens", "usage_complete", "duration_ms",
            "cached_input_tokens",
            "ttft_ms", "load_ms", "prompt_eval_ms", "eval_ms", "engine_tps", "wall_tps", "response_bytes",
        }
        fields = [key for key in values if key in allowed]
        if fields:
            with self.transaction() as conn:
                conn.execute("UPDATE requests SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?",
                             [values[key] for key in fields] + [request_id])

    def request(self, request_id):
        with self.lock:
            row = self.connection.execute("""SELECT requests.*, configs.snapshot AS config
                FROM requests LEFT JOIN configs ON configs.id=requests.config_id WHERE requests.id=?""", (request_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result["config"]) if result["config"] else None
        return result

    def requests(self, *, model=None, state=None, since=0, limit=100, offset=0):
        where, values = ["started>=?"], [since]
        for field, value in (("model", model), ("state", state)):
            if value:
                where.append(field + "=?")
                values.append(value)
        clause = " AND ".join(where)
        with self.lock:
            total = self.connection.execute("SELECT COUNT(*) FROM requests WHERE " + clause, values).fetchone()[0]
            rows = self.connection.execute("SELECT * FROM requests WHERE " + clause + " ORDER BY started DESC LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
        return {"total": total, "items": [dict(row) for row in rows]}

    def summary(self, since=0):
        with self.lock:
            rows = self.connection.execute("""SELECT model,COUNT(*) AS requests,
                SUM(state='running') AS active,SUM(state='success') AS succeeded,
                SUM(state NOT IN ('running','success')) AS failed,
                SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                SUM(eval_ms) AS eval_ms,
                SUM(CASE WHEN eval_ms>0 THEN output_tokens END) AS timed_output_tokens,
                AVG(ttft_ms) AS avg_ttft_ms,AVG(duration_ms) AS avg_duration_ms
                FROM requests WHERE started>=? GROUP BY model ORDER BY requests DESC""", (since,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["engine_tps"] = item["timed_output_tokens"] / (item["eval_ms"] / 1000) if item["eval_ms"] else None
            result.append(item)
        return result

    def history(self, seconds, limit=600):
        since = time.time() - seconds
        table = "samples" if seconds <= 6 * 3600 else "minutes"
        with self.lock:
            rows = self.connection.execute("SELECT ts,cpu,ram,gpu,vram,ram_used,vram_used FROM " + table + " WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        # Bound browser payload while preserving the latest point.
        stride = max(1, (len(rows) + limit - 1) // limit)
        selected = list(rows[::stride])
        if rows and (not selected or selected[-1]["ts"] != rows[-1]["ts"]):
            selected.append(rows[-1])
        return {"resolution": "raw" if table == "samples" else "minute", "items": [dict(row) for row in selected]}
