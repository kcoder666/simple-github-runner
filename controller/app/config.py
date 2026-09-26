"""Bootstrap configuration (env) and runtime-tunable settings (stored in the DB).

Env holds secrets and things needed before the DB is open. Everything an operator
might want to tweak without a restart lives in DEFAULT_SETTINGS and is editable
from the dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _read_secret(name: str) -> str:
    """Read NAME, or the file at NAME_FILE (docker secrets style)."""
    value = _env(name)
    path = _env(f"{name}_FILE")
    if not value and path and os.path.exists(path):
        with open(path) as f:
            value = f.read().strip()
    return value


@dataclass
class Env:
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "/data"))
    admin_password: str = field(default_factory=lambda: _read_secret("ADMIN_PASSWORD"))
    secret_key: str = field(default_factory=lambda: _read_secret("SECRET_KEY"))
    github_pat: str = field(default_factory=lambda: _read_secret("GITHUB_PAT"))
    github_app_id: str = field(default_factory=lambda: _env("GITHUB_APP_ID"))
    github_app_private_key: str = field(default_factory=lambda: _read_secret("GITHUB_APP_PRIVATE_KEY"))
    github_api_url: str = field(default_factory=lambda: _env("GITHUB_API_URL", "https://api.github.com").rstrip("/"))
    webhook_secret: str = field(default_factory=lambda: _read_secret("GITHUB_WEBHOOK_SECRET"))
    runner_image: str = field(default_factory=lambda: _env("RUNNER_IMAGE", "custom-github-runner:latest"))
    runner_build_context: str = field(default_factory=lambda: _env("RUNNER_BUILD_CONTEXT", "/app/runner"))
    # Prefix for container and GitHub runner names. Change it if two controllers
    # ever share a Docker host or a GitHub org, so they don't reap each other.
    name_prefix: str = field(default_factory=lambda: _env("RUNNER_NAME_PREFIX", "ghr"))
    docker_socket: str = field(default_factory=lambda: _env("DOCKER_SOCKET_PATH", "/var/run/docker.sock"))
    # Seed targets on first boot (compat with the old compose .env).
    seed_org_url: str = field(default_factory=lambda: _env("ORG_URL"))

    @property
    def auth_mode(self) -> str:
        if self.github_app_id and self.github_app_private_key:
            return "app"
        if self.github_pat:
            return "pat"
        return "none"

    def seed_repo_urls(self) -> list[str]:
        urls = []
        for key, value in sorted(os.environ.items()):
            if key.startswith("REPO_URL_") and value.strip():
                urls.append(value.strip())
        return urls


# Runtime settings. Durations are seconds unless the name says otherwise.
DEFAULT_SETTINGS: dict[str, object] = {
    # How often the reconciler compares desired vs actual state.
    "reconcile_interval": 20,
    # A new runner that never shows up "online" in GitHub within this window is
    # considered wedged and replaced.
    "registration_timeout": 300,
    # A runner that was online and then drops offline (while its container still
    # runs) gets this long to come back before it is replaced.
    "offline_grace": 180,
    # Idle runners are recycled after this age so no long-lived session can rot.
    "idle_max_age": 6 * 3600,
    # Busy runners are killed after this long (0 = never). Self-hosted jobs may run
    # up to 5 days on GitHub's side, so this is a safety net for hung jobs.
    "job_timeout": 12 * 3600,
    # Crash-loop protection: this many failed starts within the window pauses
    # spawning for the target with exponential backoff.
    "crash_threshold": 4,
    "crash_window": 600,
    # Disk: prune aggressively when free space on the Docker data disk drops
    # below this percentage.
    "disk_min_free_pct": 15,
    # Scheduled prune of Docker build cache / dangling images / stopped containers.
    "prune_interval_hours": 24,
    # Also remove *unused* images older than this (0 = only dangling). Container
    # actions leave big images behind; this is the usual monthly disk-fill.
    "prune_unused_images_hours": 168,
    # Rebuild the runner image automatically when actions/runner releases a new
    # version. GitHub refuses jobs to runners that fall too far behind.
    "auto_update_runner": True,
    # Recycle idle runners that are on an outdated image.
    "recycle_outdated": True,
    # Slack/Discord/generic incoming-webhook URL for alerts (empty = off).
    "notify_webhook_url": "",
    # Minimum seconds between two alerts with the same key.
    "notify_cooldown": 6 * 3600,
    # Warn when the credential (PAT) expires within this many days.
    "credential_expiry_warn_days": 14,
    # Max runners spawned per target per cycle (smooths bursts / API usage).
    "spawn_batch": 5,
}


def coerce_setting(key: str, value: object) -> object:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        number = int(float(value))  # type: ignore[arg-type]
        if number < 0:
            raise ValueError(f"{key} must be >= 0")
        return number
    return str(value).strip()
