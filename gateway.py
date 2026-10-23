"""Bounded, streaming proxy to the fixed local Ollama server."""
import asyncio
import hashlib
import json
import logging
import time
import uuid

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from telemetry import RequestStats

log = logging.getLogger(__name__)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parameters(text):
    result = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts
        try:
            value = json.loads(value)
        except ValueError:
            pass
        if key in result:
            result[key] = result[key] + [value] if isinstance(result[key], list) else [result[key], value]
        else:
            result[key] = value
    return result


class ModelRegistry:
    def __init__(self, client):
        self.client = client
        self.tags = []
        self.loaded = []
        self.version = None
        self.updated = None
        self.error = None
        self.cache = {}

    async def refresh(self):
        try:
            responses = await asyncio.gather(*(self.client.get(path, timeout=5) for path in ("/api/tags", "/api/ps", "/api/version")))
            for response in responses:
                response.raise_for_status()
            self.tags = responses[0].json().get("models", [])
            self.loaded = responses[1].json().get("models", [])
            self.version = responses[2].json().get("version")
            self.updated = time.time()
            self.error = None
        except (httpx.HTTPError, ValueError) as exc:
            self.error = type(exc).__name__

    async def snapshot(self, model, body):
        tag = next((tag for tag in self.tags if tag["name"] in (model, model + ":latest")), {})
        cache_key = (model, tag.get("digest"))
        metadata = self.cache.get(cache_key)
        if metadata is None:
            try:
                response = await self.client.post("/api/show", json={"model": model}, timeout=5)
                response.raise_for_status()
                info = response.json()
                metadata = {"details": info.get("details"), "defaults": parameters(info.get("parameters")),
                            "default_system_sha256": digest(info.get("system") or ""),
                            "template_sha256": digest(info.get("template") or "")}
                self.cache[cache_key] = metadata
                # Bound cache size when models or tags are changed often.
                if len(self.cache) > 100:
                    self.cache.pop(next(iter(self.cache)))
            except (httpx.HTTPError, ValueError) as exc:
                metadata = {"defaults": {}, "metadata_error": type(exc).__name__}
        native = not body.get("_monitor_openai", False)
        options = body.get("options", {}) if native else {
            key: body[key] for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed", "max_tokens") if key in body
        }
        options = options if isinstance(options, dict) else {}
        known = dict(metadata["defaults"])
        known.update(options)
        messages = body.get("messages")
        systems = [m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "system"] if isinstance(messages, list) else []
        loaded = next((item for item in self.loaded if item.get("name") in (model, model + ":latest")), {})
        return {
            "model": model, "observed_digest": tag.get("digest"), "ollama_version": self.version,
            **metadata, "request_options": options, "known_options": known,
            "think": body.get("think"), "reasoning_effort": body.get("reasoning_effort"),
            "client_system_sha256": digest(json.dumps(systems, ensure_ascii=False)) if systems else None,
            "has_tools": bool(body.get("tools")),
            "observed_loaded": {key: loaded.get(key) for key in ("digest", "context_length", "size_vram", "runner")},
            "note": "Known options combine queried model defaults and request overrides; unspecified engine defaults are unknown. No messages or response text stored.",
        }


async def read_body(request, maximum):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > maximum:
            raise HTTPException(413, "Request exceeds configured size limit")
    try:
        body = json.loads(content)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Expected UTF-8 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


async def proxy_generation(request: Request):
    app = request.app
    body = await read_body(request, app.state.settings.max_body_bytes)
    model = body.get("model")
    if not isinstance(model, str) or not model or len(model) > 500:
        raise HTTPException(400, "model must be a nonempty string")
    openai = request.url.path.startswith("/v1/")
    streaming = body.get("stream", not openai)
    if not isinstance(streaming, bool):
        raise HTTPException(400, "stream must be boolean")
    if openai and streaming:
        options = body.get("stream_options")
        if options is None:
            body["stream_options"] = {"include_usage": True}
        elif isinstance(options, dict):
            options.setdefault("include_usage", True)
    stats = RequestStats(openai=openai, streaming=streaming)
    request_id = uuid.uuid4().hex
    peer = request.client.host if request.client else "unknown"
    client = request.headers.get("x-monitor-client", peer)[:128]
    snapshot_body = {**body, "_monitor_openai": openai}
    config = await app.state.registry.snapshot(model, snapshot_body)
    app.state.db.start_request({"id": request_id, "started": time.time(), "endpoint": request.url.path,
                                "model": model, "client": client, "streaming": streaming}, config)
    upstream = None
    finalized = False

    def finish(state, status):
        nonlocal finalized
        if not finalized:
            finalized = True
            app.state.db.finish_request(request_id, stats.result(state, status))

    try:
        upstream = await app.state.client.send(app.state.client.build_request(
            "POST", request.url.path, json=body), stream=True)
        headers = {"X-Request-ID": request_id, "Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        media_type = upstream.headers.get("content-type", "application/x-ndjson" if streaming else "application/json")
        headers["Content-Type"] = media_type
        if upstream.status_code >= 400 or not streaming:
            data = await upstream.aread()
            stats.feed(data)
            stats.end()
            state = "error" if upstream.status_code >= 400 or stats.values["error"] else "success" if stats.finished else "interrupted"
            if upstream.status_code >= 400:
                stats.values["error"] = "Upstream HTTP " + str(upstream.status_code)
            finish(state, upstream.status_code)
            return Response(data, status_code=upstream.status_code, headers=headers)

        async def stream():
            state = "interrupted"
            try:
                async for chunk in upstream.aiter_bytes():
                    stats.feed(chunk)
                    yield chunk
                stats.end()
                state = "error" if stats.values["error"] else "success" if stats.finished else "interrupted"
            except asyncio.CancelledError:
                state = "cancelled"
                raise
            except (httpx.HTTPError, ValueError) as exc:
                stats.values["error"] = type(exc).__name__
                state = "error"
                log.warning("Upstream stream failed for %s: %s", request_id, type(exc).__name__)
            finally:
                # Synchronous, short commit survives ASGI cancellation scopes.
                finish(state, upstream.status_code)
                await upstream.aclose()
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)
    except asyncio.CancelledError:
        finish("cancelled", None)
        raise
    except httpx.TimeoutException:
        stats.values["error"] = "Upstream timeout"
        finish("error", 504)
        return JSONResponse({"error": "Ollama request timed out", "request_id": request_id}, status_code=504)
    except (httpx.HTTPError, ValueError) as exc:
        stats.values["error"] = type(exc).__name__
        finish("error", 502)
        return JSONResponse({"error": "Unable to complete Ollama request", "request_id": request_id}, status_code=502)
    finally:
        if upstream is not None and (not streaming or finalized):
            await upstream.aclose()


async def proxy_metadata(request: Request):
    client = request.app.state.client
    try:
        kwargs = {}
        if request.method == "POST":
            kwargs["json"] = await read_body(request, request.app.state.settings.max_body_bytes)
        response = await client.request(request.method, request.url.path, timeout=10, **kwargs)
        return Response(response.content, status_code=response.status_code,
                        headers={"Content-Type": response.headers.get("content-type", "application/json")})
    except httpx.HTTPError:
        return JSONResponse({"error": "Ollama is unavailable"}, status_code=502)
