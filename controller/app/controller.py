"""The controller: reconcile loop, housekeeping loop, and self-watchdog.

Reconcile (every `reconcile_interval`, or immediately on a webhook / UI change):
  for each target, join Docker containers with GitHub runner records, run the
  planner, execute its actions (remove / replace / spawn / delete records).

Housekeeping (every minute, each task on its own cadence):
  credential validity + PAT expiry, disk space + pruning, runner-version drift +
  image rebuilds, orphaned containers.

Watchdog: if the reconcile loop stops completing cycles, exit the process so
Docker's restart policy brings up a fresh controller.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
import traceback
from dataclasses import asdict
from typing import Any

from docker.errors import APIError, DockerException

from .config import Env
from .docker_ops import DockerOps
from .github import GitHub, GitHubError, Scope, parse_scope, version_tuple
from .notify import Notifier
from .planner import Action, RunnerView, plan
from .store import Store

log = logging.getLogger("controller")

CRED_CHECK_EVERY = 600
VERSION_CHECK_EVERY = 6 * 3600
EMERGENCY_PRUNE_EVERY = 1800
DEMAND_TTL = 1800


class Controller:
    def __init__(self, env: Env, store: Store):
        self.env = env
        self.store = store
        self.gh = GitHub(env.github_api_url, env.github_pat, env.github_app_id, env.github_app_private_key)
        self.docker = DockerOps(env.runner_image, env.runner_build_context, env.docker_socket)
        self.notifier = Notifier()
        self.wake = threading.Event()
        self._cycle_lock = threading.Lock()
        self.started_at = time.time()

        # In-memory observations, rebuilt from scratch after a controller restart.
        self.track: dict[str, dict[str, float | None]] = {}
        self.target_status: dict[int, dict[str, Any]] = {}
        self.runners: list[dict[str, Any]] = []
        self.failures: dict[int, list[float]] = {}
        self.backoff: dict[int, dict[str, float]] = {}
        self.demand: dict[int, tuple[int, float]] = {}  # job_id -> (target_id, queued_at)

        self.last_cycle_at: float | None = None
        self.last_cycle_ms: float | None = None
        self.last_cycle_error: str | None = None
        self.system: dict[str, Any] = {"credentials": None, "disk": None, "latest_runner_version": None,
                                       "last_prune": store.kv_get("last_prune")}
        self._last_cred_check = 0.0
        self._last_version_check = 0.0
        self._last_emergency_prune = 0.0
        self.base_labels = ["self-hosted", "linux", self._arch()]

    # --- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self.seed_targets()
        for fn, name in ((self._reconcile_loop, "reconcile"), (self._housekeeping_loop, "housekeeping"),
                         (self._watchdog_loop, "watchdog")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def seed_targets(self) -> None:
        """Import ORG_URL / REPO_URL_n from the old compose .env on first boot."""
        if self.store.kv_get("seeded"):
            return
        urls = ([self.env.seed_org_url] if self.env.seed_org_url else []) + self.env.seed_repo_urls()
        existing = {t["url"] for t in self.store.list_targets()}
        for url in urls:
            if url in existing or "<" in url:
                continue
            try:
                scope = parse_scope(url)
            except ValueError:
                log.warning("ignoring unparsable seed URL %s", url)
                continue
            name = scope.path.replace("/", "-")
            self.store.create_target({"name": name, "url": url, "min_idle": 1, "max_runners": 4})
            self.event("info", "target", f"imported target {name} from environment")
        self.store.kv_set("seeded", True)

    def _arch(self) -> str:
        try:
            arch = self.docker.client.info().get("Architecture", "")
        except DockerException:
            arch = ""
        return "arm64" if arch in ("aarch64", "arm64") else "x64"

    def event(self, level: str, kind: str, message: str, target_id: int | None = None,
              runner: str | None = None, alert_key: str | None = None) -> None:
        getattr(log, "warning" if level == "warn" else level)("[%s] %s%s", kind,
                                                               f"{runner}: " if runner else "", message)
        self.store.add_event(level, kind, message, target_id, runner)
        if alert_key:
            s = self.store.settings()
            prefix = {"error": "🔴", "warn": "🟠"}.get(level, "🟢")
            self.notifier.send(s["notify_webhook_url"], alert_key, f"{prefix} [github-runners] {message}",
                               s["notify_cooldown"])

    # --- loops ---------------------------------------------------------------------

    def _reconcile_loop(self) -> None:
        while True:
            try:
                self.reconcile()
            except Exception as exc:  # never let the loop die
                self.last_cycle_error = f"{type(exc).__name__}: {exc}"
                log.error("reconcile failed: %s", traceback.format_exc())
                self.event("error", "controller", f"reconcile cycle failed: {self.last_cycle_error}",
                           alert_key="reconcile-failed")
                self.last_cycle_at = time.time()  # the loop itself is alive
            self.wake.wait(timeout=self.store.settings()["reconcile_interval"] or 20)
            self.wake.clear()

    def _housekeeping_loop(self) -> None:
        time.sleep(3)
        while True:
            try:
                self.housekeeping()
            except Exception:
                log.error("housekeeping failed: %s", traceback.format_exc())
            time.sleep(60)

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(30)
            limit = max(600, 10 * int(self.store.settings()["reconcile_interval"]))
            last = self.last_cycle_at or self.started_at
            if time.time() - last > limit:
                log.critical("reconcile loop stalled for %ss — exiting so Docker restarts the controller",
                             int(time.time() - last))
                self.store.add_event("error", "controller", "reconcile loop stalled; controller restarting")
                os._exit(1)

    def healthy(self) -> tuple[bool, str]:
        limit = max(180, 5 * int(self.store.settings()["reconcile_interval"]))
        last = self.last_cycle_at or self.started_at
        if time.time() - last > limit:
            return False, f"no reconcile cycle for {int(time.time() - last)}s"
        return True, "ok"

    # --- reconcile ---------------------------------------------------------------------

    def reconcile(self) -> None:
        with self._cycle_lock:
            t0 = time.time()
            self._reconcile()
            self.last_cycle_ms = round((time.time() - t0) * 1000)
            self.last_cycle_at = time.time()
            self.last_cycle_error = None

    def _reconcile(self) -> None:
        now = time.time()
        s = self.store.settings()
        targets = self.store.list_targets()
        containers = self.docker.list_managed()
        image = self.docker.image_info()
        target_ids = {t["id"] for t in targets}

        by_target: dict[int, list] = {}
        for c in containers:
            if c.target_id not in target_ids:
                self.docker.remove(c.name)
                self.event("info", "cleanup", "removed container of a deleted target", runner=c.name)
                continue
            by_target.setdefault(c.target_id, []).append(c)

        self._expire_demand(now)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for t in targets:
            rows.extend(self._reconcile_target(t, by_target.get(t["id"], []), image, s, now, seen))
        for name in list(self.track):
            if name not in seen:
                del self.track[name]
        for tid in list(self.target_status):
            if tid not in target_ids:
                del self.target_status[tid]
        self.runners = rows

    def _reconcile_target(self, t: dict[str, Any], containers: list, image: dict | None,
                          s: dict[str, Any], now: float, seen: set[str]) -> list[dict[str, Any]]:
        scope = parse_scope(t["url"])
        prefix = self._name_prefix(t)
        gh_ok, gh_error, records = True, None, {}
        try:
            records = {r["name"]: r for r in self.gh.list_runners(scope) if r["name"].startswith(prefix)}
        except GitHubError as exc:
            gh_ok, gh_error = False, exc.message
            prev = self.target_status.get(t["id"], {})
            if prev.get("gh_ok", True):
                self.event("error", "github", f"{t['name']}: cannot list runners — {exc.message}",
                           t["id"], alert_key=f"gh-list-{t['id']}")

        views: list[RunnerView] = []
        for c in containers:
            seen.add(c.name)
            rec = records.pop(c.name, None)
            tr = self.track.setdefault(c.name, {"first_online": None, "last_online": None, "busy_since": None})
            if rec and rec["status"] == "online":
                if tr["first_online"] is None:
                    tr["first_online"] = now
                    self._clear_backoff(t)
                tr["last_online"] = now
            if rec and rec.get("busy"):
                tr["busy_since"] = tr["busy_since"] or now
            elif rec:
                tr["busy_since"] = None
            views.append(RunnerView(
                name=c.name, container_status=c.status, created_at=c.created_at, started_at=c.started_at,
                exit_code=c.exit_code, oom_killed=c.oom_killed,
                gh_id=rec["id"] if rec else c.gh_runner_id,
                gh_status=rec["status"] if rec else None, gh_busy=bool(rec and rec.get("busy")),
                first_online=tr["first_online"], last_online=tr["last_online"], busy_since=tr["busy_since"],
                outdated=bool(image and c.image_id and c.image_id != image["id"]),
            ))
        if gh_ok:
            for rec in records.values():
                views.append(RunnerView(name=rec["name"], container_status=None, gh_id=rec["id"],
                                        gh_status=rec["status"], gh_busy=bool(rec.get("busy"))))

        blocked_until = self.backoff.get(t["id"], {}).get("until", 0)
        demand = sum(1 for tid, _ in self.demand.values() if tid == t["id"])
        p = plan(t, views, now, s, gh_ok=gh_ok, spawn_blocked=blocked_until > now or image is None,
                 demand=demand)
        by_name = {v.name: v for v in views}
        for action in p.actions:
            try:
                self._execute(action, t, scope, by_name.get(action.name or ""), s)
            except (GitHubError, DockerException) as exc:
                self.event("error", "action", f"{action.kind} failed: {exc}", t["id"], action.name)

        self.target_status[t["id"]] = {
            "gh_ok": gh_ok, "gh_error": gh_error, "counts": p.counts, "demand": demand,
            "backoff_until": blocked_until if blocked_until > now else None,
            "image_missing": image is None, "reconciled_at": now,
        }
        return [{
            **{k: v for k, v in asdict(v).items() if k not in ("created_at",)},
            "created_at": v.created_at or None,
            "state": p.states[v.name],
            "target_id": t["id"],
            "target_name": t["name"],
        } for v in views]

    def _name_prefix(self, t: dict[str, Any]) -> str:
        return f"{self.env.name_prefix}-{t['id']}-"

    def _new_name(self, t: dict[str, Any]) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-")[:30] or "runner"
        return f"{self._name_prefix(t)}{slug}-{secrets.token_hex(3)}"

    def _execute(self, a: Action, t: dict[str, Any], scope: Scope, v: RunnerView | None,
                 s: dict[str, Any]) -> None:
        if a.kind == "spawn":
            for _ in range(a.count):
                if not self._spawn(t, scope):
                    break
            return
        if v is None:
            return
        if a.kind == "delete_record":
            self._delete_record(scope, v.gh_id)
            self.event("info", "cleanup", f"deleted orphaned GitHub runner record", t["id"], v.name)
            return
        # remove
        if a.graceful and v.gh_id:
            # Deregister first: GitHub refuses (422) if a job was just assigned,
            # in which case we leave the runner alone this cycle.
            try:
                self.gh.delete_runner(scope, v.gh_id)
            except GitHubError as exc:
                if exc.status == 422:
                    return
                if exc.status != 404:
                    raise
        message = f"{t['name']}: {a.reason}"
        if a.level != "info":
            # Keep the evidence: the container (and its logs) is about to go.
            tail = [line.split(" ", 1)[-1] for line in self.docker.logs(v.name, tail=4).splitlines() if line.strip()]
            if tail:
                message += " — last output: " + " | ".join(tail)[-400:]
        self.docker.remove(v.name)
        if not a.graceful and v.gh_status is not None:
            self._delete_record(scope, v.gh_id)
        self.track.pop(v.name, None)
        alert = f"{a.reason.split(' ')[0]}-{t['id']}" if a.level in ("warn", "error") else None
        self.event(a.level, "recover" if a.level != "info" else "lifecycle",
                   message, t["id"], v.name, alert_key=alert)
        if a.failure:
            self._record_failure(t, s)

    def _delete_record(self, scope: Scope, gh_id: int | None) -> None:
        if not gh_id:
            return
        try:
            self.gh.delete_runner(scope, gh_id)
        except GitHubError as exc:
            if exc.status not in (404, 422):
                log.warning("could not delete runner record %s: %s", gh_id, exc)

    def _spawn(self, t: dict[str, Any], scope: Scope) -> bool:
        if self.backoff.get(t["id"], {}).get("until", 0) > time.time():
            return False  # a removal earlier in this cycle just tripped the backoff
        name = self._new_name(t)
        labels = list(dict.fromkeys(self.base_labels + t["labels"]))
        try:
            runner_id, jit = self.gh.jit_config(scope, name, labels, t["runner_group"])
        except GitHubError as exc:
            self.event("error", "spawn", f"{t['name']}: could not create JIT runner — {exc.message}",
                       t["id"], name, alert_key=f"spawn-{t['id']}")
            self._record_failure(t, self.store.settings())
            return False
        try:
            self.docker.run_runner(name, t, runner_id, jit)
        except (DockerException, APIError) as exc:
            self._delete_record(scope, runner_id)
            self.docker.remove(name)
            self.event("error", "spawn", f"{t['name']}: could not start container — {exc}",
                       t["id"], name, alert_key=f"spawn-{t['id']}")
            self._record_failure(t, self.store.settings())
            return False
        self.event("info", "lifecycle", f"{t['name']}: started runner", t["id"], name)
        return True

    # --- crash-loop backoff ----------------------------------------------------------------

    def _record_failure(self, t: dict[str, Any], s: dict[str, Any]) -> None:
        now = time.time()
        window = [ts for ts in self.failures.get(t["id"], []) if now - ts < s["crash_window"]]
        window.append(now)
        self.failures[t["id"]] = window
        if len(window) >= s["crash_threshold"]:
            level = self.backoff.get(t["id"], {}).get("level", 0) + 1
            delay = min(60 * 2 ** (level - 1), 1800)
            self.backoff[t["id"]] = {"until": now + delay, "level": level}
            self.failures[t["id"]] = []
            self.event("error", "backoff",
                       f"{t['name']}: {len(window)} failed starts in {s['crash_window']}s — "
                       f"pausing spawns for {delay}s (check the events/logs for the cause)",
                       t["id"], alert_key=f"backoff-{t['id']}")

    def _clear_backoff(self, t: dict[str, Any]) -> None:
        if self.backoff.pop(t["id"], None):
            self.event("info", "backoff", f"{t['name']}: runner came online — backoff cleared", t["id"])
        self.failures.pop(t["id"], None)
        self.notifier.reset(f"backoff-{t['id']}")

    def reset_backoff(self, target_id: int) -> None:
        self.backoff.pop(target_id, None)
        self.failures.pop(target_id, None)

    # --- webhook demand ------------------------------------------------------------------

    def on_workflow_job(self, payload: dict[str, Any]) -> str:
        action = payload.get("action")
        job = payload.get("workflow_job") or {}
        job_id = job.get("id")
        if not job_id:
            return "ignored"
        if action != "queued":
            if self.demand.pop(job_id, None):
                self.wake.set()
            return "released"
        repo = (payload.get("repository") or {}).get("full_name", "").lower()
        owner = repo.split("/")[0]
        wanted = {label.lower() for label in job.get("labels", [])}
        for t in self.store.list_targets():
            if not t["enabled"]:
                continue
            try:
                scope = parse_scope(t["url"])
            except ValueError:
                continue
            if (scope.kind == "repos" and scope.path.lower() != repo) or \
               (scope.kind == "orgs" and scope.owner.lower() != owner):
                continue
            offered = {label.lower() for label in self.base_labels + t["labels"]}
            if wanted <= offered:
                self.demand[job_id] = (t["id"], time.time())
                self.wake.set()
                return f"queued for {t['name']}"
        return "no matching target"

    def _expire_demand(self, now: float) -> None:
        for job_id, (_, ts) in list(self.demand.items()):
            if now - ts > DEMAND_TTL:
                del self.demand[job_id]

    # --- housekeeping --------------------------------------------------------------------

    def housekeeping(self) -> None:
        now = time.time()
        s = self.store.settings()

        if now - self._last_cred_check > CRED_CHECK_EVERY:
            self.check_credentials(s)

        self._check_disk(s, now)

        # First boot anchors the schedule instead of pruning immediately.
        anchor = self.store.kv_get("last_prune") or self.store.kv_get("prune_anchor")
        if anchor is None:
            anchor = now
            self.store.kv_set("prune_anchor", now)
        if s["prune_interval_hours"] and now - anchor > s["prune_interval_hours"] * 3600:
            self.prune(aggressive=False, reason="scheduled")

        if now - self._last_version_check > VERSION_CHECK_EVERY or self.docker.image_info() is None:
            self._last_version_check = now
            self.check_runner_version(s)

    def check_credentials(self, s: dict[str, Any] | None = None) -> dict[str, Any]:
        s = s or self.store.settings()
        self._last_cred_check = time.time()
        prev = self.system.get("credentials") or {}
        cred = self.gh.check_credentials()
        cred["checked_at"] = time.time()
        self.system["credentials"] = cred
        if not cred["ok"]:
            if prev.get("ok", True):
                self.event("error", "credentials", f"GitHub credential check failed: {cred.get('error')}",
                           alert_key="credentials")
        elif prev and not prev.get("ok", True):
            self.event("info", "credentials", "GitHub credentials are valid again")
            self.notifier.reset("credentials")
        expires = cred.get("expires_at")
        if expires:
            days = (expires - time.time()) / 86400
            if days < s["credential_expiry_warn_days"]:
                self.event("warn", "credentials",
                           f"GITHUB_PAT expires in {days:.1f} days — rotate it or switch to a GitHub App",
                           alert_key="pat-expiry")
        return cred

    def _check_disk(self, s: dict[str, Any], now: float) -> None:
        disk = self.docker.disk_usage()
        disk["checked_at"] = now
        self.system["disk"] = disk
        if disk["free_pct"] < s["disk_min_free_pct"] and now - self._last_emergency_prune > EMERGENCY_PRUNE_EVERY:
            self._last_emergency_prune = now
            self.event("warn", "disk", f"Docker disk is {100 - disk['free_pct']:.0f}% full — pruning aggressively",
                       alert_key="disk-low")
            self.prune(aggressive=True, reason="low disk")
            after = self.docker.disk_usage()
            self.system["disk"] = {**after, "checked_at": time.time()}
            if after["free_pct"] < s["disk_min_free_pct"]:
                self.event("error", "disk", f"still only {after['free_pct']}% free after pruning — "
                           "the host needs attention", alert_key="disk-critical")

    def prune(self, aggressive: bool, reason: str) -> dict[str, Any]:
        s = self.store.settings()
        report = self.docker.prune(s["prune_unused_images_hours"], aggressive=aggressive)
        self.store.kv_set("last_prune", time.time())
        self.system["last_prune"] = time.time()
        self.system["last_prune_report"] = report
        gb = report["total_reclaimed"] / 1e9
        self.event("info", "disk", f"prune ({reason}{', aggressive' if aggressive else ''}) reclaimed {gb:.2f} GB")
        return report

    def check_runner_version(self, s: dict[str, Any] | None = None, force_build: bool = False) -> None:
        s = s or self.store.settings()
        try:
            latest = self.gh.latest_runner_version()
            self.system["latest_runner_version"] = latest
        except GitHubError as exc:
            latest = None
            log.warning("could not fetch latest runner version: %s", exc)
        image = self.docker.image_info()
        current = image["runner_version"] if image else None
        if image is None:
            self.event("info", "image", f"runner image {self.env.runner_image} missing — building")
        elif force_build:
            self.event("info", "image", "rebuilding runner image on request")
        elif latest and s["auto_update_runner"] and version_tuple(latest) > version_tuple(current):
            self.event("info", "image", f"runner {latest} released (image has {current}) — rebuilding")
        else:
            return
        ok, output = self.docker.build_image(latest or current)
        if ok:
            new = self.docker.image_info()
            self.event("info", "image", f"built runner image ({(new or {}).get('runner_version')}); "
                       "idle runners will be recycled onto it")
            self.wake.set()
        else:
            tail = output.strip().splitlines()[-3:] if output else []
            self.event("error", "image", "runner image build failed: " + " | ".join(tail),
                       alert_key="image-build")

    # --- operator actions ------------------------------------------------------------------

    def recycle_runner(self, name: str, force: bool = False) -> str:
        """Replace one runner. Without force, a busy runner is left alone."""
        with self._cycle_lock:
            row = next((r for r in self.runners if r["name"] == name), None)
            if row is None:
                return "unknown runner"
            t = self.store.get_target(row["target_id"])
            if t is None:
                return "unknown target"
            scope = parse_scope(t["url"])
            if row["gh_id"] and row["gh_status"] is not None:
                try:
                    self.gh.delete_runner(scope, row["gh_id"])
                except GitHubError as exc:
                    if exc.status == 422 and not force:
                        return "runner is busy — use force to kill the running job"
                    if exc.status not in (404, 422):
                        raise
            if row["container_status"] is not None:
                self.docker.remove(name)
            self.track.pop(name, None)
            self.event("warn" if force else "info", "manual",
                       f"{t['name']}: runner recycled by operator{' (forced)' if force else ''}", t["id"], name)
        self.wake.set()
        return "recycled"

    def purge_target(self, t: dict[str, Any]) -> None:
        """Remove every container and GitHub record belonging to a target (used on delete)."""
        with self._cycle_lock:
            scope = parse_scope(t["url"])
            for c in self.docker.list_managed():
                if c.target_id == t["id"]:
                    self.docker.remove(c.name)
                    self._delete_record(scope, c.gh_runner_id)
            try:
                for rec in self.gh.list_runners(scope):
                    if rec["name"].startswith(self._name_prefix(t)):
                        self._delete_record(scope, rec["id"])
            except GitHubError as exc:
                log.warning("could not list runners while purging %s: %s", t["name"], exc)
            self.runners = [r for r in self.runners if r["target_id"] != t["id"]]
            self.target_status.pop(t["id"], None)

    # --- snapshot for the API ------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        healthy, why = self.healthy()
        targets = []
        for t in self.store.list_targets():
            targets.append({**t, "status": self.target_status.get(t["id"], {})})
        return {
            "controller": {
                "healthy": healthy, "health_detail": why, "started_at": self.started_at,
                "last_cycle_at": self.last_cycle_at, "last_cycle_ms": self.last_cycle_ms,
                "last_cycle_error": self.last_cycle_error, "auth_mode": self.gh.mode,
                "name_prefix": self.env.name_prefix, "base_labels": self.base_labels,
                "webhook_enabled": bool(self.env.webhook_secret),
            },
            "github": {
                "credentials": self.system.get("credentials"),
                "rate_limit_remaining": self.gh.rate_limit_remaining,
                "rate_limit_reset": self.gh.rate_limit_reset,
                "token_expires_at": self.gh.token_expires_at,
            },
            "image": {**(self.docker.image_info() or {"missing": True}),
                      "latest_runner_version": self.system.get("latest_runner_version"),
                      "build": dict(self.docker.build_state)},
            "disk": self.system.get("disk"),
            "last_prune": self.system.get("last_prune"),
            "last_prune_report": self.system.get("last_prune_report"),
            "targets": targets,
            "runners": self.runners,
        }
