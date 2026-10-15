"""Web dashboard.

Replaces the old CLI output.  Shows how many accounts are configured, how many
applications sit under each one, which consulates each application targets, the
target date window, and the latest availability seen per consulate.

Security: the dashboard exposes masked emails, schedule ids and appointment
dates.  Set ``dashboard.username`` / ``dashboard.password`` to put HTTP Basic
auth in front of it.  Without them the service is completely unauthenticated and
anyone who can reach the port can read it -- a warning is logged at startup.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .config import Config
from .metrics import render_metrics
from .network import ProviderError
from .store import Store
from .worker import Worker

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_basic = HTTPBasic(auto_error=False)


def create_app(config: Config, store: Store, worker: Optional[Worker] = None) -> FastAPI:
    app = FastAPI(
        title="US Visa Appointment Watcher",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)) -> None:
        if not config.dashboard.auth_enabled:
            return
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        # compare_digest on both fields so neither length nor content leaks via timing
        user_ok = secrets.compare_digest(
            credentials.username.encode(), config.dashboard.username.encode()
        )
        pass_ok = secrets.compare_digest(
            credentials.password.encode(), config.dashboard.password.encode()
        )
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, _: None = Depends(require_auth)):
        worker_context = worker.dashboard_context() if worker else {
            "groups": [], "sessions": [], "pool": [], "network": {"enabled": False},
        }
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "data": store.snapshot(),
                "auth_enabled": config.dashboard.auth_enabled,
                "poll_interval": config.poll_interval,
                "capsolver_enabled": config.capsolver_enabled,
                "telegram_enabled": config.telegram.enabled,
                "worker_running": bool(worker and worker.running),
                "groups": worker_context["groups"],
                "sessions": worker_context["sessions"],
                "pool": worker_context.get("pool", []),
                "matching": worker_context.get("matching", []),
                "reverify_sweeps": config.calendar_reverify_sweeps,
                "network": worker_context.get("network", {"enabled": False}),
            },
        )

    # -- network / OpenVPN egress ------------------------------------------

    def network_guard():
        guard = worker.network if worker else None
        if guard is None:
            raise HTTPException(status_code=404, detail="network guard not running")
        return guard

    def require_xhr(request: Request) -> None:
        # Basic auth is replayed by the browser on any request, including a
        # form another site submits. A custom header cannot be set
        # cross-origin without a CORS preflight this app never grants, so
        # requiring it keeps state-changing calls same-origin.
        if request.headers.get("x-requested-with") != "usvisa-dashboard":
            raise HTTPException(status_code=403, detail="missing X-Requested-With")

    @app.get("/api/network")
    def api_network(_: None = Depends(require_auth)):
        return JSONResponse(network_guard().status())

    @app.get("/api/network/egress")
    def api_network_egress(_: None = Depends(require_auth)):
        return JSONResponse(network_guard().egress_ip())

    @app.post("/api/network/probe")
    def api_network_probe(
        request: Request, _: None = Depends(require_auth),
    ):
        require_xhr(request)
        result = network_guard().probe()
        return JSONResponse({"verdict": result.verdict, "detail": result.detail, "at": result.at})

    @app.post("/api/network/switch")
    async def api_network_switch(
        request: Request, _: None = Depends(require_auth),
    ):
        require_xhr(request)
        guard = network_guard()
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        profile = str(body.get("profile") or "").strip()
        random = bool(body.get("random"))
        if not profile and not random:
            raise HTTPException(status_code=400, detail="profile or random is required")
        if profile and profile not in guard.profiles():
            raise HTTPException(status_code=400, detail=f"unknown profile: {profile}")
        try:
            return JSONResponse(guard.switch(profile=profile, random=random), status_code=202)
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.get("/api/state")
    def api_state(_: None = Depends(require_auth)):
        return JSONResponse(store.snapshot())

    @app.get("/metrics")
    def metrics(_: None = Depends(require_auth)):
        """Prometheus-format snapshot of the same gauges pushed to New Relic.

        Kept purely as a local debugging convenience -- ``curl`` it to see the
        current values without waiting for ingestion. Nothing scrapes it.
        """
        return PlainTextResponse(
            render_metrics(store.snapshot()),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/healthz")
    def healthz():
        """Unauthenticated liveness probe; deliberately leaks nothing."""
        return {
            "ok": True,
            "worker_running": bool(worker and worker.running),
            "worker_status": store.worker_status,
            "sweeps": store.sweep_count,
        }

    return app
