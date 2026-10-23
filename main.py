"""Monitoring dashboard and Ollama gateway. Bind to loopback; publish via Tailscale."""
import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import threading
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import Settings
from database import Database
from gateway import ModelRegistry, proxy_generation, proxy_metadata
from systemCollector import SystemCollector

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def create_app(settings=None, *, transport=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings
        app.state.db = Database(settings.database)
        app.state.db.maintain()
        app.state.client = httpx.AsyncClient(
            base_url=settings.upstream, trust_env=False, follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx.Timeout(settings.request_timeout, connect=5), transport=transport,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
        app.state.registry = ModelRegistry(app.state.client)
        app.state.latest = None
        app.state.collector_error = None
        app.state.stop = threading.Event()

        def sample_loop():
            try:
                with SystemCollector(enable_gpu=settings.gpu_enabled) as collector:
                    collector.collect()
                    maintenance_at = time.monotonic() + 60
                    while not app.state.stop.wait(settings.sample_interval):
                        try:
                            sample = collector.collect()
                            app.state.db.add_sample(sample)
                            app.state.latest = sample
                            app.state.collector_error = None
                            if time.monotonic() >= maintenance_at:
                                app.state.db.maintain()
                                maintenance_at = time.monotonic() + 60
                        except Exception as exc:
                            app.state.collector_error = type(exc).__name__
                            log.exception("System sampling failed")
            except Exception as exc:
                app.state.collector_error = type(exc).__name__
                log.exception("Unable to start collector")

        async def model_loop():
            while True:
                await app.state.registry.refresh()
                await asyncio.sleep(5)

        thread = None
        if settings.collect_system:
            thread = threading.Thread(target=sample_loop, name="system-collector", daemon=True)
            thread.start()
        refresh = asyncio.create_task(model_loop())
        try:
            yield
        finally:
            app.state.stop.set()
            refresh.cancel()
            await asyncio.gather(refresh, return_exceptions=True)
            if thread:
                await asyncio.to_thread(thread.join)
            await app.state.client.aclose()
            app.state.db.close()

    app = FastAPI(title="LLM Monitor", lifespan=lifespan, docs_url="/monitor/docs", openapi_url="/monitor/openapi.json", redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "llm-monitor", "ollama_error": app.state.registry.error,
                "collector_error": app.state.collector_error}

    @app.get("/monitor/api/system")
    async def system():
        return {"sample": app.state.latest, "error": app.state.collector_error}

    @app.get("/monitor/api/history")
    async def history(seconds: int = Query(3600, ge=60, le=30 * 86400)):
        return app.state.db.history(seconds)

    @app.get("/monitor/api/models")
    async def models():
        registry = app.state.registry
        return {"models": registry.tags, "loaded": registry.loaded, "version": registry.version,
                "updated": registry.updated, "error": registry.error}

    @app.get("/monitor/api/requests")
    async def requests(model: str | None = None, state: str | None = None,
                       hours: int = Query(24, ge=1, le=720), limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0)):
        return app.state.db.requests(model=model, state=state, since=time.time()-hours*3600, limit=limit, offset=offset)

    @app.get("/monitor/api/requests/{request_id}")
    async def request_detail(request_id: str):
        record = app.state.db.request(request_id)
        if record is None:
            raise HTTPException(404, "Request not found")
        return record

    @app.get("/monitor/api/summary")
    async def summary(hours: int = Query(24, ge=1, le=720)):
        return {"models": app.state.db.summary(time.time()-hours*3600)}

    for path in ("/api/chat", "/api/generate", "/v1/chat/completions"):
        app.add_api_route(path, proxy_generation, methods=["POST"], include_in_schema=False)
    for path in ("/api/tags", "/api/ps", "/api/version", "/v1/models"):
        app.add_api_route(path, proxy_metadata, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/show", proxy_metadata, methods=["POST"], include_in_schema=False)
    return app


app = create_app()
