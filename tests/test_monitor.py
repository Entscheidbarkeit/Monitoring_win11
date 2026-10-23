import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
import httpx

from config import Settings
from database import Database
from main import create_app
from telemetry import RequestStats


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error


def native_final():
    return {"model": "test:q3", "message": {"role": "assistant", "content": ""}, "done": True,
            "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 4,
            "eval_duration": 200_000_000, "load_duration": 10_000_000, "prompt_eval_duration": 5_000_000}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.response = None
        self.upstream_body = None
        self.settings = Settings(database=Path(self.tmp.name)/"db.sqlite3", collect_system=False)

        async def handler(request):
            path = request.url.path
            if path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "test:q3", "digest": "abc"}]})
            if path == "/api/ps":
                return httpx.Response(200, json={"models": []})
            if path == "/api/version":
                return httpx.Response(200, json={"version": "test"})
            if path == "/api/show":
                return httpx.Response(200, json={"parameters": "num_ctx 4096\ntemperature 0.7", "system": "Private system text", "template": "template", "details": {"quantization_level": "Q3_K_M"}})
            self.upstream_body = json.loads(request.content)
            return self.response()

        self.app = create_app(self.settings, transport=httpx.MockTransport(handler))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def record(self, response):
        return self.client.get("/monitor/api/requests/" + response.headers["x-request-id"]).json()

    def test_native_stream_bytes_tokens_and_private_text(self):
        data = (json.dumps({"message": {"content": "你好 SECRET_RESPONSE"}}, ensure_ascii=False) + "\n" + json.dumps(native_final()) + "\n").encode()
        self.response = lambda: httpx.Response(200, stream=Chunks([data[:24], data[24:25], data[25:]]), headers={"content-type": "application/x-ndjson"})
        r = self.client.post("/api/chat", json={"model": "test:q3", "stream": True, "messages": [{"role": "user", "content": "SECRET_PROMPT"}], "options": {"temperature": 0.4}})
        self.assertEqual(r.content, data)
        record = self.record(r)
        self.assertEqual(record["state"], "success")
        self.assertEqual(record["input_tokens"], 12)
        self.assertEqual(record["output_tokens"], 4)
        self.assertEqual(record["engine_tps"], 20)
        self.assertEqual(record["usage_complete"], 1)
        self.assertEqual(record["config"]["known_options"]["temperature"], 0.4)
        self.assertNotIn("SECRET", json.dumps(record))
        self.assertNotIn("Private system text", json.dumps(record))

    def test_sse_usage_and_no_fake_engine_timing(self):
        data = b'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\ndata: [DONE]\n\n'
        self.response = lambda: httpx.Response(200, stream=Chunks([data[:9], data[9:]]), headers={"content-type": "text/event-stream"})
        r = self.client.post("/v1/chat/completions", json={"model": "test:q3", "stream": True, "messages": []})
        self.assertEqual(r.content, data)
        self.assertTrue(self.upstream_body["stream_options"]["include_usage"])
        record = self.record(r)
        self.assertEqual(record["output_tokens"], 2)
        self.assertIsNone(record["engine_tps"])
        self.assertGreater(record["wall_tps"], 0)
        self.assertEqual(record["usage_complete"], 1)

    def test_nonstream_utf8_and_unobservable_ttft(self):
        payload = {"model": "test:q3", "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        self.response = lambda: httpx.Response(200, json=payload)
        r = self.client.post("/v1/chat/completions", json={"model": "test:q3", "messages": []})
        self.assertEqual(r.json(), payload)
        self.assertIsNone(self.record(r)["ttft_ms"])
        self.assertEqual(self.record(r)["state"], "success")

    def test_incomplete_stream_has_unknown_tokens(self):
        self.response = lambda: httpx.Response(200, stream=Chunks([b'{"message":{"content":"partial"}}\n']))
        r = self.client.post("/api/chat", json={"model": "test:q3"})
        record = self.record(r)
        self.assertEqual(record["state"], "interrupted")
        self.assertIsNone(record["output_tokens"])
        self.assertEqual(record["usage_complete"], 0)

    def test_upstream_failure_preserves_status_without_saving_body(self):
        self.response = lambda: httpx.Response(503, json={"error": "SECRET upstream text"})
        r = self.client.post("/api/chat", json={"model": "test:q3"})
        self.assertEqual(r.status_code, 503)
        record = self.record(r)
        self.assertEqual(record["state"], "error")
        self.assertNotIn("SECRET", json.dumps(record))

    def test_broken_upstream_stream_is_finalized(self):
        self.response = lambda: httpx.Response(200, stream=Chunks([b'{"message":{"content":"part"}}\n'], error=httpx.ReadError("broken")))
        r = self.client.post("/api/chat", json={"model": "test:q3"})
        self.assertEqual(self.record(r)["state"], "error")
        self.assertIsNone(self.record(r)["output_tokens"])

    def test_health_dashboard_validation_and_filters(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertIn("LLM 主机监控", self.client.get("/").text)
        self.assertEqual(self.client.post("/api/chat", json={"model": "x", "stream": "yes"}).status_code, 400)
        self.assertEqual(self.client.get("/monitor/api/history?seconds=-1").status_code, 422)
        self.assertEqual(self.client.get("/monitor/api/requests?model=x%27%20OR%201=1").json()["total"], 0)


class PersistenceTests(unittest.TestCase):
    def test_rollup_retention_and_restart_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"db.sqlite3"
            db = Database(path)
            now = time.time()
            with db.transaction() as c:
                for ts, cpu in ((now-180, 10), (now-175, 30), (now-8*86400, 80)):
                    c.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)", (ts,"utc",cpu,50,None,None,10,None,"{}"))
            db.start_request({"id":"old","started":now,"endpoint":"/api/chat","model":"test","client":"local","streaming":True}, {})
            db.maintain(now)
            self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
            minute = db.connection.execute("SELECT * FROM minutes").fetchone()
            self.assertIsNotNone(minute)
            db.close()
            db = Database(path)
            self.assertEqual(db.request("old")["state"], "interrupted")
            db.close()

    def test_native_no_newline_and_split_utf8(self):
        stats = RequestStats()
        data = (json.dumps({"message":{"content":"汉字"}}, ensure_ascii=False)+"\n"+json.dumps(native_final())).encode()
        for byte in data:
            stats.feed(bytes([byte]))
        stats.end()
        result = stats.result("success", 200)
        self.assertEqual(result["engine_tps"], 20)
        self.assertEqual(result["usage_complete"], 1)


if __name__ == "__main__":
    unittest.main()
