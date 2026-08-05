"""
FastAPI app assembly: create_app() wires config/db/auth/poller/jobs into app.state and
registers every route. run.py is the only thing that actually calls create_app() and
starts uvicorn -- kept separate so tests (or a future CLI) can import create_app()
without also binding a port.
"""

import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import correlate, rules, sweep
from . import state as state_mod
from .actions import ACTIONS
from .auth import COOKIE_NAME, AuthState, require_admin, require_session
from .clients import local_fs
from .clients.jellyfin import JellyfinAuthError
from .config import Config
from .db import Database
from .jobs import JobRunner
from .poller import Poller

log = logging.getLogger("dashboard.main")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset_version():
    # Cache-busting query param for /static/style.css -- browsers cache static files
    # aggressively, and this app gets edited/restarted far more often than a typical
    # deployed service, so every CSS tweak needs a way to actually reach the browser
    # without a manual hard-refresh. File mtime changes on every edit, which is
    # exactly the signal needed; cheap enough to stat on every render.
    try:
        return int((BASE_DIR / "static" / "style.css").stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["asset_version"] = _asset_version


def _get_or_create_secret(cfg):
    secret_path = cfg.dashboard_config_dir / "session_secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
    return secret_path.read_text(encoding="utf-8").strip()


def create_app():
    cfg = Config()
    db = Database(str(cfg.db_path))
    auth = AuthState(db, cfg, _get_or_create_secret(cfg))
    poller = Poller(db, cfg)
    jobs = JobRunner(db, cfg)

    @asynccontextmanager
    async def lifespan(app):
        jobs.reconcile_orphaned()
        await poller.poll_once()   # one synchronous poll before serving, so the
                                    # first page load isn't empty
        poller.start()
        yield
        await poller.stop()

    app = FastAPI(lifespan=lifespan, title="acquisitions dashboard")
    app.state.cfg = cfg
    app.state.db = db
    app.state.auth = auth
    app.state.poller = poller
    app.state.jobs = jobs
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def tpl(name, request, **ctx):
        return templates.TemplateResponse(name, {"request": request, "cfg": cfg, **ctx})

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return tpl("login.html", request, error=None)

    @app.post("/login")
    async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        try:
            cookie_value = auth.login(username, password)
        except JellyfinAuthError:
            return tpl("login.html", request, error="Invalid username or password.")
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(COOKIE_NAME, cookie_value, httponly=True, samesite="lax", max_age=14 * 86400)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        cookie_value = request.cookies.get(COOKIE_NAME)
        auth.logout(cookie_value)
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    # -----------------------------------------------------------------
    # Index / request list
    # -----------------------------------------------------------------

    def _index_data():
        snap = poller.snapshot
        rows = correlate.build_index(snap) if snap else []
        global_diags = rules.evaluate_global(snap, db, cfg) if snap else []
        health = state_mod.all_source_health(db)
        service_health = correlate.build_service_health(snap, db) if snap else []
        return rows, global_diags, health, service_health

    async def _filtered_rows(request: Request):
        snap = poller.snapshot
        rows = correlate.build_index(snap) if snap else []
        params = request.query_params
        stalled_ids = None
        if params.get("stalled") == "1" and snap:
            stalled_ids = await sweep.run_sweep(snap, cfg, db)
        return correlate.filter_and_sort_index(
            rows, q=params.get("q"), media_type=params.get("type"),
            status=params.get("status"), sort=params.get("sort"), stalled_ids=stalled_ids,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session=Depends(require_session)):
        rows = await _filtered_rows(request)
        _, global_diags, health, service_health = _index_data()
        return tpl("index.html", request, rows=rows, global_diags=global_diags, health=health,
                   service_health=service_health, session=session, params=request.query_params)

    @app.get("/partials/requests", response_class=HTMLResponse)
    async def partial_requests(request: Request, session=Depends(require_session)):
        rows = await _filtered_rows(request)
        return tpl("partials/_request_rows.html", request, rows=rows)

    @app.get("/partials/health", response_class=HTMLResponse)
    async def partial_health(request: Request, session=Depends(require_session)):
        _, global_diags, health, service_health = _index_data()
        return tpl("partials/_health.html", request, global_diags=global_diags, health=health, service_health=service_health)

    @app.post("/refresh", response_class=HTMLResponse)
    async def force_refresh(request: Request, session=Depends(require_session)):
        await poller.poll_once()
        _, global_diags, health, service_health = _index_data()
        return tpl("partials/_health.html", request, global_diags=global_diags, health=health, service_health=service_health)

    # -----------------------------------------------------------------
    # Notifications -- the in-app mirror of sweep.py's ntfy pushes (see
    # dashboard/notify.py, dashboard/sweep.py's _flush_pending). Viewable by any
    # signed-in account, same as traces themselves; no fix actions live here so
    # there's no reason to gate it behind require_admin.
    # -----------------------------------------------------------------

    @app.get("/partials/notif-badge", response_class=HTMLResponse)
    async def partial_notif_badge(request: Request, session=Depends(require_session)):
        unread = state_mod.count_unread_notifications(db)
        return tpl("partials/_notif_badge.html", request, unread=unread)

    @app.get("/notifications", response_class=HTMLResponse)
    async def notifications_page(request: Request, session=Depends(require_session)):
        rows = state_mod.list_notifications(db)
        # Visiting the page is what clears the badge -- read_at is set for
        # everything unread at the moment of this view, not per-row on click, so
        # the count next reflects only what's actually new since the last visit.
        state_mod.mark_all_notifications_read(db)
        return tpl("notifications.html", request, rows=rows, session=session)

    # -----------------------------------------------------------------
    # Single request trace
    # -----------------------------------------------------------------

    def _build_trace(request_id):
        snap = poller.snapshot
        if snap is None:
            return None
        trace = correlate.build_trace_detail(request_id, snap, cfg)
        if trace is None:
            return None
        is_seedbox = cfg.download_mode != "local"
        wrapper_summary, _ = local_fs.tail_wrapper_log(cfg.rclone_wrapper_log) if is_seedbox else (None, [])
        return rules.evaluate_trace(trace, snap, db, cfg, is_seedbox, wrapper_summary)

    @app.get("/request/{request_id}", response_class=HTMLResponse)
    async def request_detail(request: Request, request_id: int, session=Depends(require_session)):
        trace = _build_trace(request_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="request not found in current snapshot")
        return tpl("trace.html", request, trace=trace, session=session, actions=ACTIONS)

    @app.get("/partials/request/{request_id}", response_class=HTMLResponse)
    async def partial_request_detail(request: Request, request_id: int, session=Depends(require_session)):
        trace = _build_trace(request_id)
        if trace is None:
            raise HTTPException(status_code=404)
        return tpl("partials/_trace_body.html", request, trace=trace, session=session, actions=ACTIONS)

    @app.get("/api/trace/{request_id}")
    async def api_trace(request_id: int, session=Depends(require_session)):
        trace = _build_trace(request_id)
        if trace is None:
            raise HTTPException(status_code=404)
        # dataclasses aren't directly JSON-serializable (Enum members especially) --
        # this is a debugging aid, not a stable API, so a best-effort default= is fine.
        return JSONResponse(json.loads(json.dumps(trace, default=lambda o: getattr(o, "value", None) or vars(o))))

    # -----------------------------------------------------------------
    # Fix actions
    # -----------------------------------------------------------------

    @app.post("/actions/{action_id}/preview", response_class=HTMLResponse)
    async def action_preview(request: Request, action_id: str, session=Depends(require_admin)):
        if action_id not in ACTIONS:
            raise HTTPException(status_code=404)
        action = ACTIONS[action_id]
        form = await request.form()
        params = {k: v for k, v in form.items()}
        if action.requires_mode and action.requires_mode != cfg.download_mode:
            return tpl("partials/_action_confirm.html", request, action=action, params=params,
                       preview=None, blocked=f"This action needs DOWNLOAD_MODE={action.requires_mode}.")
        preview = action.preview(cfg, poller.snapshot, params)
        return tpl("partials/_action_confirm.html", request, action=action, params=params, preview=preview, blocked=None)

    @app.post("/actions/{action_id}/run", response_class=HTMLResponse)
    async def action_run(request: Request, action_id: str, session=Depends(require_admin)):
        if action_id not in ACTIONS:
            raise HTTPException(status_code=404)
        form = await request.form()
        params = {k: v for k, v in form.items() if not k.startswith("_")}
        job_id = jobs.start_job(action_id, params, session["username"])
        job = jobs.get_job(job_id)
        return tpl("partials/_job.html", request, job=job)

    @app.get("/partials/job/{job_id}", response_class=HTMLResponse)
    async def partial_job(request: Request, job_id: str, session=Depends(require_session)):
        job = jobs.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return tpl("partials/_job.html", request, job=job)

    # -----------------------------------------------------------------
    # Liveness (no auth -- no sensitive data, just confirms the process is up)
    # -----------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"ok": True, "last_poll": poller.snapshot.fetched_at if poller.snapshot else None}

    return app
