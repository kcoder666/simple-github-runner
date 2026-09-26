"""HTTP API + dashboard for the runner controller."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import DEFAULT_SETTINGS, Env
from .controller import Controller
from .github import parse_scope
from .store import Store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("controller.api")

STATIC = os.path.join(os.path.dirname(__file__), "static")
SESSION_COOKIE = "ghr_session"
SESSION_TTL = 7 * 86400

env = Env()
store = Store(env.data_dir)
controller = Controller(env, store)

if not env.admin_password or env.admin_password.startswith("<"):
    env.admin_password = store.kv_get("generated_admin_password") or secrets.token_urlsafe(18)
    store.kv_set("generated_admin_password", env.admin_password)
    log.warning("ADMIN_PASSWORD not set — using generated password: %s", env.admin_password)
SECRET = (env.secret_key or store.kv_get("secret_key") or "").encode()
if not SECRET:
    SECRET = secrets.token_hex(32).encode()
    store.kv_set("secret_key", SECRET.decode())


@asynccontextmanager
async def lifespan(_: FastAPI):
    controller.start()
    yield


app = FastAPI(title="GitHub Runner Controller", docs_url=None, redoc_url=None, lifespan=lifespan)


# --- auth -----------------------------------------------------------------------------

def _sign(payload: str) -> str:
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def _make_session() -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + SESSION_TTL}).encode()).decode()
    return f"{payload}.{_sign(payload)}"


def _valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return json.loads(base64.urlsafe_b64decode(payload))["exp"] > time.time()
    except (ValueError, KeyError):
        return False


def require_auth(request: Request) -> None:
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return
    # Bearer ADMIN_PASSWORD for scripts/curl.
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and hmac.compare_digest(header[7:], env.admin_password):
        return
    raise HTTPException(401, "not authenticated")


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response) -> dict:
    if not hmac.compare_digest(body.password.encode(), env.admin_password.encode()):
        time.sleep(1)
        raise HTTPException(401, "wrong password")
    response.set_cookie(SESSION_COOKIE, _make_session(), max_age=SESSION_TTL, httponly=True, samesite="strict",
                        secure=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true"))
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# --- state ----------------------------------------------------------------------------

@app.get("/api/state", dependencies=[Depends(require_auth)])
def state() -> dict:
    snap = controller.snapshot()
    snap["settings"] = store.settings()
    snap["now"] = time.time()
    return snap


@app.get("/api/events", dependencies=[Depends(require_auth)])
def events(limit: int = 200, target_id: int | None = None, level: str | None = None) -> list[dict]:
    return store.events(min(limit, 2000), target_id, level)


# --- targets --------------------------------------------------------------------------

class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    url: str
    labels: list[str] = []
    runner_group: str = "Default"
    min_idle: int = Field(1, ge=0, le=100)
    max_runners: int = Field(4, ge=0, le=200)
    docker_access: bool = True
    cpus: float | None = Field(None, gt=0, le=256)
    memory: str | None = Field(None, pattern=r"^\d+[bkmgBKMG]?$")
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        parse_scope(v)
        return v.strip().rstrip("/")

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: list[str]) -> list[str]:
        out = []
        for label in v:
            label = label.strip()
            if not label:
                continue
            if "," in label or len(label) > 100:
                raise ValueError(f"invalid label {label!r}")
            out.append(label)
        return out

    def row(self) -> dict[str, Any]:
        data = self.model_dump()
        data["labels"] = ",".join(self.labels)
        data["memory"] = self.memory or None
        if data["min_idle"] > data["max_runners"]:
            raise HTTPException(422, "min_idle cannot exceed max_runners")
        return data


@app.get("/api/targets", dependencies=[Depends(require_auth)])
def list_targets() -> list[dict]:
    return store.list_targets()


@app.post("/api/targets", dependencies=[Depends(require_auth)], status_code=201)
def create_target(body: TargetIn) -> dict:
    if any(t["name"] == body.name for t in store.list_targets()):
        raise HTTPException(409, f"a target named {body.name} already exists")
    t = store.create_target(body.row())
    controller.event("info", "target", f"target {t['name']} created ({t['url']})", t["id"])
    controller.wake.set()
    return t


@app.put("/api/targets/{target_id}", dependencies=[Depends(require_auth)])
def update_target(target_id: int, body: TargetIn) -> dict:
    current = store.get_target(target_id)
    if current is None:
        raise HTTPException(404, "no such target")
    if body.url != current["url"] and any(r["target_id"] == target_id for r in controller.runners):
        raise HTTPException(409, "disable the target and let it drain before changing its URL")
    if any(t["name"] == body.name and t["id"] != target_id for t in store.list_targets()):
        raise HTTPException(409, f"a target named {body.name} already exists")
    t = store.update_target(target_id, body.row())
    controller.event("info", "target", f"target {body.name} updated", target_id)
    controller.wake.set()
    return t  # type: ignore[return-value]


@app.delete("/api/targets/{target_id}", dependencies=[Depends(require_auth)])
def delete_target(target_id: int) -> dict:
    t = store.get_target(target_id)
    if t is None:
        raise HTTPException(404, "no such target")
    controller.purge_target(t)
    store.delete_target(target_id)
    controller.event("warn", "target", f"target {t['name']} deleted (its runners were removed)")
    return {"ok": True}


@app.post("/api/targets/{target_id}/recycle", dependencies=[Depends(require_auth)])
def recycle_target(target_id: int) -> dict:
    results = {}
    for r in [r for r in controller.runners if r["target_id"] == target_id]:
        if r["state"] != "busy":
            results[r["name"]] = controller.recycle_runner(r["name"])
    return {"results": results}


@app.post("/api/targets/{target_id}/reset-backoff", dependencies=[Depends(require_auth)])
def reset_backoff(target_id: int) -> dict:
    controller.reset_backoff(target_id)
    controller.wake.set()
    return {"ok": True}


# --- runners --------------------------------------------------------------------------

@app.post("/api/runners/{name}/recycle", dependencies=[Depends(require_auth)])
def recycle_runner(name: str, force: bool = False) -> dict:
    return {"result": controller.recycle_runner(name, force)}


@app.get("/api/runners/{name}/logs", dependencies=[Depends(require_auth)])
def runner_logs(name: str, tail: int = 400) -> PlainTextResponse:
    if not name.startswith(f"{env.name_prefix}-"):
        raise HTTPException(404, "not a managed runner")
    return PlainTextResponse(controller.docker.logs(name, min(tail, 5000)))


# --- system ---------------------------------------------------------------------------

class SettingsIn(BaseModel):
    values: dict[str, Any]


@app.get("/api/settings", dependencies=[Depends(require_auth)])
def get_settings() -> dict:
    return {"values": store.settings(), "defaults": DEFAULT_SETTINGS}


@app.put("/api/settings", dependencies=[Depends(require_auth)])
def put_settings(body: SettingsIn) -> dict:
    try:
        values = store.update_settings(body.values)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"invalid setting: {exc}") from exc
    controller.event("info", "settings", f"settings updated: {', '.join(sorted(body.values))}")
    controller.wake.set()
    return {"values": values}


def _background(fn, *args, **kwargs) -> None:
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


@app.post("/api/system/reconcile", dependencies=[Depends(require_auth)])
def trigger_reconcile() -> dict:
    controller.wake.set()
    return {"ok": True}


@app.post("/api/system/prune", dependencies=[Depends(require_auth)])
def trigger_prune(aggressive: bool = False) -> dict:
    _background(controller.prune, aggressive=aggressive, reason="manual")
    return {"ok": True, "message": "prune started"}


@app.post("/api/system/rebuild-image", dependencies=[Depends(require_auth)])
def trigger_rebuild() -> dict:
    if controller.docker.build_state.get("building"):
        raise HTTPException(409, "a build is already running")
    _background(controller.check_runner_version, force_build=True)
    return {"ok": True, "message": "build started"}


@app.post("/api/system/check-credentials", dependencies=[Depends(require_auth)])
def trigger_cred_check() -> dict:
    return controller.check_credentials()


# --- GitHub webhook ---------------------------------------------------------------------

@app.post("/webhook/github")
async def github_webhook(request: Request) -> dict:
    if not env.webhook_secret:
        raise HTTPException(404, "webhooks are not enabled (set GITHUB_WEBHOOK_SECRET)")
    body = await request.body()
    expected = "sha256=" + hmac.new(env.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(401, "bad signature")
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return {"ok": True, "result": "pong"}
    if event != "workflow_job":
        return {"ok": True, "result": f"ignored {event}"}
    return {"ok": True, "result": controller.on_workflow_job(json.loads(body))}


# --- health & metrics -------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> JSONResponse:
    ok, detail = controller.healthy()
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 503)


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """Prometheus exposition. Contains target names and counts only — no secrets."""
    lines = [
        "# HELP ghr_runners Runners per target and state.",
        "# TYPE ghr_runners gauge",
    ]
    names = {t["id"]: t["name"] for t in store.list_targets()}
    for tid, st in controller.target_status.items():
        for state, n in (st.get("counts") or {}).items():
            if state != "live":
                lines.append(f'ghr_runners{{target="{names.get(tid, tid)}",state="{state}"}} {n}')
    lines += ["# HELP ghr_target_github_ok 1 if the GitHub API is reachable for the target.",
              "# TYPE ghr_target_github_ok gauge"]
    for tid, st in controller.target_status.items():
        lines.append(f'ghr_target_github_ok{{target="{names.get(tid, tid)}"}} {int(bool(st.get("gh_ok")))}')
    ok, _ = controller.healthy()
    lines += ["# TYPE ghr_controller_healthy gauge", f"ghr_controller_healthy {int(ok)}"]
    disk = controller.system.get("disk") or {}
    if disk:
        lines += ["# TYPE ghr_disk_free_percent gauge", f"ghr_disk_free_percent {disk['free_pct']}"]
    if controller.gh.token_expires_at:
        lines += ["# TYPE ghr_credential_expiry_seconds gauge",
                  f"ghr_credential_expiry_seconds {int(controller.gh.token_expires_at - time.time())}"]
    return PlainTextResponse("\n".join(lines) + "\n")


# --- dashboard --------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC, "index.html"))
