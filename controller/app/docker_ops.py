"""Docker operations: runner containers, the runner image, disk hygiene."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import docker
from docker.errors import APIError, ImageNotFound, NotFound

MANAGED = "ghr.managed"


@dataclass
class Container:
    name: str
    id: str
    status: str  # created | running | exited | dead | paused | restarting | removing
    target_id: int | None
    gh_runner_id: int | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    exit_code: int | None
    image_id: str
    oom_killed: bool


def _ts(value: str | None) -> float | None:
    if not value or value.startswith("0001-"):
        return None
    # Docker gives RFC3339 with nanoseconds; trim to micros for fromisoformat.
    value = value.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        frac, _, tz = rest.partition("+")
        value = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class DockerOps:
    def __init__(self, image: str, build_context: str, socket_path: str):
        self.client = docker.from_env(timeout=60)
        self.image = image
        self.build_context = build_context
        self.socket_path = socket_path
        self.build_lock = threading.Lock()
        self.build_state: dict[str, Any] = {"building": False, "last_result": None, "log_tail": ""}

    # --- containers --------------------------------------------------------------

    def list_managed(self) -> list[Container]:
        out = []
        for c in self.client.containers.list(all=True, filters={"label": f"{MANAGED}=true"}):
            attrs = c.attrs
            state = attrs.get("State", {})
            labels = attrs.get("Config", {}).get("Labels", {}) or {}
            out.append(Container(
                name=c.name,
                id=c.id,
                status=state.get("Status", "unknown"),
                target_id=_int(labels.get("ghr.target")),
                gh_runner_id=_int(labels.get("ghr.runner_id")),
                created_at=_ts(attrs.get("Created")) or time.time(),
                started_at=_ts(state.get("StartedAt")),
                finished_at=_ts(state.get("FinishedAt")),
                exit_code=state.get("ExitCode"),
                image_id=attrs.get("Image", ""),
                oom_killed=bool(state.get("OOMKilled")),
            ))
        return out

    def run_runner(self, name: str, target: dict[str, Any], gh_runner_id: int, jitconfig: str) -> None:
        kwargs: dict[str, Any] = {
            "name": name,
            "hostname": name[:63],
            "detach": True,
            "init": True,  # tini as PID 1: reaps zombies, forwards signals
            "environment": {"RUNNER_JITCONFIG": jitconfig},
            "labels": {
                MANAGED: "true",
                "ghr.target": str(target["id"]),
                "ghr.target_name": target["name"],
                "ghr.runner_id": str(gh_runner_id),
            },
            # The controller owns lifecycle: no Docker restarts, we replace instead.
            "restart_policy": {"Name": "no"},
            "log_config": {"Type": "json-file", "Config": {"max-size": "20m", "max-file": "2"}},
        }
        if target.get("docker_access") and os.path.exists(self.socket_path):
            kwargs["volumes"] = {"/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"}}
            # Let the unprivileged runner user use the socket without chgrp-ing
            # the host's socket.
            kwargs["group_add"] = [str(os.stat(self.socket_path).st_gid)]
        if target.get("cpus"):
            kwargs["nano_cpus"] = int(float(target["cpus"]) * 1e9)
        if target.get("memory"):
            kwargs["mem_limit"] = target["memory"]
        self.client.containers.run(self.image, **kwargs)

    def remove(self, name: str) -> None:
        try:
            self.client.containers.get(name).remove(force=True, v=True)
        except NotFound:
            pass

    def logs(self, name: str, tail: int = 300) -> str:
        try:
            raw = self.client.containers.get(name).logs(tail=tail, timestamps=True)
            return raw.decode("utf-8", errors="replace")
        except NotFound:
            return ""

    # --- image ---------------------------------------------------------------------

    def image_info(self) -> dict[str, Any] | None:
        try:
            img = self.client.images.get(self.image)
        except ImageNotFound:
            return None
        labels = img.labels or {}
        return {
            "id": img.id,
            "tag": self.image,
            "runner_version": labels.get("ghr.runner_version"),
            "created": img.attrs.get("Created"),
            "size": img.attrs.get("Size"),
        }

    def build_image(self, runner_version: str | None) -> tuple[bool, str]:
        """Build the runner image with the docker CLI (BuildKit). Blocking."""
        if not self.build_lock.acquire(blocking=False):
            return False, "a build is already running"
        self.build_state.update(building=True, started_at=time.time(), version=runner_version)
        try:
            cmd = ["docker", "build", "--pull", "-t", self.image, "--label", "ghr.managed_image=true"]
            if runner_version:
                cmd += ["--build-arg", f"RUNNER_VERSION={runner_version}"]
            cmd.append(self.build_context)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            output = (proc.stdout + proc.stderr)[-6000:]
            ok = proc.returncode == 0
            self.build_state.update(last_result="ok" if ok else "failed", log_tail=output, finished_at=time.time())
            return ok, output
        except (OSError, subprocess.SubprocessError) as exc:
            self.build_state.update(last_result="failed", log_tail=str(exc), finished_at=time.time())
            return False, str(exc)
        finally:
            self.build_state["building"] = False
            self.build_lock.release()

    # --- disk ------------------------------------------------------------------------

    @staticmethod
    def disk_usage() -> dict[str, float]:
        # The controller's own root is an overlay on the Docker data disk, so
        # statvfs("/") reports the space runners and images actually compete for.
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total": total, "free": free, "free_pct": round(100.0 * free / total, 1) if total else 0.0}

    def prune(self, unused_images_hours: int, aggressive: bool = False) -> dict[str, Any]:
        """Reclaim space. Only touches things Docker itself deems unused.

        Volumes are deliberately left alone: runner containers are removed with
        their anonymous volumes, and anything else on the host isn't ours.
        """
        reclaimed = 0
        report: dict[str, Any] = {}
        steps = [
            ("containers", lambda: self.client.containers.prune(filters={"label": f"{MANAGED}=true"})),
            ("dangling_images", lambda: self.client.images.prune(filters={"dangling": True})),
            ("build_cache", lambda: self.client.api.prune_builds(
                **({} if aggressive else {"filters": {"until": "72h"}}))),
        ]
        hours = 24 if aggressive and unused_images_hours else unused_images_hours
        if hours:
            steps.append(("unused_images", lambda: self.client.images.prune(
                # Never prune our own runner image, even when no runner uses it.
                filters={"dangling": False, "until": f"{hours}h", "label!": "ghr.managed_image=true"})))
        for key, step in steps:
            try:
                result = step() or {}
                space = int(result.get("SpaceReclaimed") or 0)
                reclaimed += space
                report[key] = space
            except APIError as exc:
                report[key] = f"error: {exc.explanation or exc}"
        report["total_reclaimed"] = reclaimed
        return report


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
