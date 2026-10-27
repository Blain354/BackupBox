"""HTTP API, static web UI and captive portal.

Captive portal: on the BackupBox access point every DNS name resolves to the Pi
(dnsmasq ``address=/#/10.42.0.1``). When the phone joins the Wi-Fi it checks for
internet (connectivitycheck.gstatic.com, captive.apple.com…); answering those
checks with a redirect makes Android and iOS show « Se connecter au réseau »,
which opens BackupBox. That is the phone notification that works offline.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .manager import ActionError, Manager

STATIC_DIR = Path(__file__).parent / "static"


LOCAL_HOSTNAMES = {"10.42.0.1", "backupbox.lan", "backupbox.local"}


def is_foreign_host(host: str, local_names: set[str]) -> bool:
    """True for a public DNS name that the access point's DNS sent to the Pi."""
    host = host.strip().lower().rstrip(".")
    if host.startswith("["):  # IPv6 literal
        return False
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not host or "." not in host or host in local_names:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


class SourceBody(BaseModel):
    source_name: str | None = None
    preset: str | None = None


class PresetBody(BaseModel):
    preset: str


class NameBody(BaseModel):
    name: str


class ProjectBody(BaseModel):
    project: str


class SettingsBody(BaseModel):
    auto_backup: bool | None = None
    countdown_s: int | None = None
    auto_eject: bool | None = None
    verify: bool | None = None
    strict_recheck: bool | None = None
    led: bool | None = None
    auto_drain: bool | None = None
    default_preset: str | None = None
    buzzer: bool | None = None
    beep_interval_s: int | None = None
    captive_portal: bool | None = None
    ntfy_url: str | None = None


def create_app(manager: Manager, portal_url: str = "http://10.42.0.1/", extra_hostnames: str = "") -> FastAPI:
    app = FastAPI(title="BackupBox", version=__version__, docs_url="/api/docs", redoc_url=None)
    local_names = LOCAL_HOSTNAMES | {h.strip().lower() for h in extra_hostnames.split(",") if h.strip()}

    @app.middleware("http")
    async def captive_portal(request: Request, call_next):
        if manager.store.settings.captive_portal and is_foreign_host(request.headers.get("host", ""), local_names):
            return RedirectResponse(portal_url, status_code=302, headers={"Cache-Control": "no-store"})
        return await call_next(request)

    @app.exception_handler(ActionError)
    async def _action_error(_: Request, exc: ActionError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/api/state")
    def state():
        return manager.snapshot()

    @app.get("/api/history")
    def history():
        return manager.store.history()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return manager.job_detail(job_id)

    @app.post("/api/jobs/{job_id}/source")
    def job_source(job_id: str, body: NameBody):
        return manager.set_job_source(job_id, body.name).to_dict()

    @app.post("/api/jobs/{job_id}/preset")
    def job_preset(job_id: str, body: PresetBody):
        return manager.set_job_preset(job_id, body.preset).to_dict()

    @app.post("/api/jobs/{job_id}/hold")
    def job_hold(job_id: str):
        return manager.hold_job(job_id).to_dict()

    @app.post("/api/jobs/{job_id}/start")
    def job_start(job_id: str):
        return manager.start_job_now(job_id).to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str):
        return manager.cancel_job(job_id).to_dict()

    @app.post("/api/devices/{key}/backup")
    def device_backup(key: str, body: SourceBody | None = None):
        return manager.start_backup(
            key, body.source_name if body else None, body.preset if body else None
        ).to_dict()

    @app.post("/api/devices/{key}/eject")
    def device_eject(key: str):
        return manager.eject(key).to_dict(manager.platform)

    @app.post("/api/devices/{key}/mount")
    def device_mount(key: str):
        return manager.remount(key).to_dict(manager.platform)

    @app.post("/api/devices/{key}/init-destination")
    def device_init(key: str, body: ProjectBody):
        return manager.init_destination(key, body.project).to_dict(manager.platform)

    @app.post("/api/devices/{key}/select")
    def device_select(key: str):
        return manager.select_destination(key).to_dict(manager.platform)

    @app.post("/api/buffer/drain")
    def buffer_drain():
        return [job.to_dict() for job in manager.drain_now()]

    @app.get("/api/destination/projects")
    def projects():
        return manager.projects()

    @app.post("/api/destination/project")
    def set_project(body: ProjectBody):
        return manager.set_project(body.project).to_dict(manager.platform)

    @app.get("/api/settings")
    def get_settings():
        return manager.snapshot()["settings"]

    @app.put("/api/settings")
    def put_settings(body: SettingsBody):
        changes = body.model_dump(exclude_none=True)
        if not changes:
            raise HTTPException(400, "Aucun réglage fourni.")
        settings = manager.update_settings(changes)
        return settings.__dict__

    @app.post("/api/system/{action}")
    def system(action: str):
        if action not in {"poweroff", "reboot"}:
            raise HTTPException(404, "Action inconnue.")
        manager.power(action)
        return {"ok": True}

    @app.get("/api/health")
    def health():
        return {"version": __version__, **manager.health()}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
