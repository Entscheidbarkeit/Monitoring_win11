"""Opt-in real inference through the deployed gateway; writes a verification receipt."""
import argparse
import json
from pathlib import Path
import time

import httpx


def run(base):
    report = {"base_url": base, "checks": []}
    with httpx.Client(base_url=base, trust_env=False, timeout=180) as client:
        health = client.get("/healthz"); health.raise_for_status()
        system = client.get("/monitor/api/system").json()
        assert system["sample"] is not None and system["sample"]["cpu"]["percent"] is not None
        report["hostname"] = system["sample"]["hostname"]
        report["gpu"] = system["sample"]["gpu"]
        for model in ("qwen3.8-27b-orca:q3_k_m", "qwen3.8-27b-uncensored:iq3_m"):
            with client.stream("POST", "/api/chat", json={
                "model": model, "messages": [{"role":"user","content":"请用三句话解释电脑内存与硬盘的区别。"}],
                "stream":True, "think":False, "options":{"num_predict":160,"seed":51},
            }) as response:
                response.raise_for_status()
                request_id = response.headers["x-request-id"]
                events = [json.loads(line) for line in response.iter_lines() if line.strip()]
            final = events[-1]
            record = client.get("/monitor/api/requests/"+request_id).json()
            assert record["state"] == "success", record
            assert record["output_tokens"] == final["eval_count"]
            assert record["input_tokens"] == final["prompt_eval_count"]
            assert record["engine_tps"] > 0 and record["ttft_ms"] > 0
            assert record["config"]["observed_digest"]
            report["checks"].append({"case":"native-stream","record":record})

        with client.stream("POST", "/v1/chat/completions", json={
            "model":"qwen3.8-27b-orca:q3_k_m", "messages":[{"role":"user","content":"用一句话说明什么是路由器。"}],
            "stream":True,"reasoning_effort":"none","max_tokens":128,
        }) as response:
            response.raise_for_status()
            request_id = response.headers["x-request-id"]
            lines = list(response.iter_lines())
            assert any("[DONE]" in line for line in lines)
        record = client.get("/monitor/api/requests/"+request_id).json()
        assert record["state"] == "success" and record["usage_complete"] == 1, record
        assert record["engine_tps"] is None and record["wall_tps"] > 0
        report["checks"].append({"case":"openai-stream","record":record})

        with client.stream("POST", "/api/chat", json={
            "model":"qwen3.8-27b-orca:q3_k_m", "messages":[{"role":"user","content":"请详细介绍计算机的发展历史，写一篇很长的文章。"}],
            "think":False,"stream":True,"options":{"num_predict":2048},
        }) as response:
            response.raise_for_status()
            request_id = response.headers["x-request-id"]
            for line in response.iter_lines():
                if line and json.loads(line).get("message",{}).get("content"):
                    break
        for _ in range(30):
            record = client.get("/monitor/api/requests/"+request_id).json()
            if record["state"] != "running":
                break
            time.sleep(0.2)
        assert record["state"] == "cancelled", record
        assert record["output_tokens"] is None and record["usage_complete"] == 0
        report["checks"].append({"case":"client-cancellation","record":record})

        response = client.post("/api/chat", json={"model":"monitor-does-not-exist:missing","messages":[{"role":"user","content":"hello"}],"stream":False})
        assert response.status_code >= 400
        record = client.get("/monitor/api/requests/"+response.headers["x-request-id"]).json()
        assert record["state"] == "error"
        report["checks"].append({"case":"upstream-error","record":record})
        report["summary"] = client.get("/monitor/api/summary").json()
        report["history_points"] = len(client.get("/monitor/api/history?seconds=3600").json()["items"])
        assert report["history_points"] >= 2
    report["passed"] = True
    report["verified_at"] = time.time()
    return report


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--base-url",default="http://127.0.0.1:18080")
    parser.add_argument("--output",default="verification/live-report.json")
    args=parser.parse_args()
    report=run(args.base_url)
    path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":report["passed"],"host":report["hostname"],"checks":[check["case"] for check in report["checks"]],"history_points":report["history_points"]}))
