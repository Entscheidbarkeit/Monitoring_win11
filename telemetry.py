"""Parse token statistics without treating network chunks as tokens."""
import json
import math
import time


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


class RequestStats:
    def __init__(self, openai=False, streaming=True):
        self.openai = openai
        self.streaming = streaming
        self.started = time.monotonic()
        self.first = None
        self.buffer = b""
        self.sse_data = []
        self.finished = False
        self.values = {"input_tokens": None, "output_tokens": None, "total_tokens": None,
                       "usage_complete": 0, "response_bytes": 0, "error": None}

    def event(self, event):
        if not isinstance(event, dict):
            return
        if event.get("model"):
            self.values["response_model"] = str(event["model"])
        if event.get("error"):
            # Error messages can echo user text: retain an error class, not body.
            self.values["error"] = "Upstream returned an error event"
        has_output = False
        if self.openai:
            for choice in event.get("choices", []):
                content = choice.get("delta", {}) if self.streaming else choice.get("message", {})
                has_output |= bool(content.get("content") or content.get("reasoning") or content.get("reasoning_content") or content.get("tool_calls"))
                if choice.get("finish_reason"):
                    self.values["finish_reason"] = choice["finish_reason"]
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.values.update(input_tokens=number(usage.get("prompt_tokens")),
                                   output_tokens=number(usage.get("completion_tokens")),
                                   total_tokens=number(usage.get("total_tokens")))
            if not self.streaming:
                self.finished = True
        else:
            message = event.get("message", {})
            has_output = bool(message.get("content") or message.get("thinking") or message.get("tool_calls") or event.get("response") or event.get("thinking"))
            if event.get("done") is True:
                self.finished = True
                self.values.update(input_tokens=number(event.get("prompt_eval_count")),
                                   output_tokens=number(event.get("eval_count")),
                                   finish_reason=event.get("done_reason"))
                for source, target in (("load_duration", "load_ms"), ("prompt_eval_duration", "prompt_eval_ms"), ("eval_duration", "eval_ms")):
                    duration = number(event.get(source))
                    if duration is not None:
                        self.values[target] = duration / 1e6
                if number(event.get("prompt_eval_cached_count")) is not None:
                    self.values["cached_input_tokens"] = event["prompt_eval_cached_count"]
        if has_output and self.streaming and self.first is None:
            self.first = time.monotonic()

    def _parse(self, data):
        if not data:
            return
        if self.openai and data.strip() == b"[DONE]":
            self.finished = True
            return
        try:
            self.event(json.loads(data))
        except (ValueError, UnicodeError, TypeError):
            self.values["error"] = "Malformed upstream stream event"

    def _line(self, line):
        line = line.rstrip(b"\r")
        if self.openai:
            if line.startswith(b"data:"):
                self.sse_data.append(line[5:].lstrip(b" "))
            elif not line and self.sse_data:
                self._parse(b"\n".join(self.sse_data))
                self.sse_data.clear()
        else:
            self._parse(line)

    def feed(self, data):
        self.values["response_bytes"] += len(data)
        self.buffer += data
        if self.streaming:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self._line(line)
            if len(self.buffer) > 8 * 1024 * 1024:
                raise ValueError("Upstream event exceeds 8 MiB")

    def end(self):
        if self.streaming:
            if self.buffer:
                self._line(self.buffer)
            if self.openai and self.sse_data:
                self._parse(b"\n".join(self.sse_data))
                self.sse_data.clear()
        else:
            self._parse(self.buffer)
        self.buffer = b""

    def result(self, state, status):
        elapsed = max(0, time.monotonic() - self.started)
        values = dict(self.values)
        inp, out = values["input_tokens"], values["output_tokens"]
        values["usage_complete"] = int(self.finished and inp is not None and out is not None)
        if values["total_tokens"] is None and inp is not None and out is not None:
            values["total_tokens"] = inp + out
        values.update(finished=time.time(), state=state, http_status=status,
                      duration_ms=elapsed * 1000,
                      ttft_ms=(self.first - self.started) * 1000 if self.first is not None else None,
                      wall_tps=out / elapsed if out is not None and elapsed > 0 else None)
        duration = values.get("eval_ms")
        values["engine_tps"] = out / (duration / 1000) if out is not None and duration and duration > 0 else None
        return values
