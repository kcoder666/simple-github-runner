"""Minimal GitHub REST client for self-hosted runner management.

Auth is either a PAT or a GitHub App. An App is preferred: installation tokens are
minted on demand and never expire from under you, whereas PATs (especially
fine-grained ones) expire — a classic cause of runners silently dying a month
after setup. For PATs we surface the expiry date GitHub reports in the
`github-authentication-token-expiration` response header.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Scope:
    kind: str  # "repos" | "orgs"
    path: str  # "owner/repo" | "org"

    @property
    def owner(self) -> str:
        return self.path.split("/")[0]

    @property
    def api(self) -> str:
        return f"{self.kind}/{self.path}"


_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def parse_scope(url: str) -> Scope:
    """https://github.com/owner/repo -> repos/owner/repo; https://github.com/org -> orgs/org."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL must look like https://github.com/<org> or https://github.com/<owner>/<repo>")
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if len(parts) not in (1, 2) or not all(_NAME.match(p) for p in parts):
        raise ValueError("URL must look like https://github.com/<org> or https://github.com/<owner>/<repo>")
    return Scope("repos", "/".join(parts)) if len(parts) == 2 else Scope("orgs", parts[0])


class GitHub:
    def __init__(self, api_url: str, pat: str = "", app_id: str = "", app_key: str = ""):
        self.api_url = api_url
        self.pat = pat
        self.app_id = app_id
        self.app_key = app_key
        self.mode = "app" if app_id and app_key else ("pat" if pat else "none")
        self._http = httpx.Client(base_url=api_url, timeout=30.0, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "simple-github-runner-controller",
        })
        self._lock = threading.Lock()
        self._installations: dict[str, int] = {}
        self._install_tokens: dict[int, tuple[str, float]] = {}
        self._groups: dict[tuple[str, str], tuple[int, float]] = {}
        # Observability, surfaced on the dashboard.
        self.token_expires_at: float | None = None
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None

    # --- auth ------------------------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.app_key, algorithm="RS256")

    def _installation_token(self, scope: Scope) -> str:
        with self._lock:
            inst = self._installations.get(scope.owner)
        if inst is None:
            path = f"/orgs/{scope.owner}/installation" if scope.kind == "orgs" else f"/repos/{scope.path}/installation"
            data = self._send("GET", path, token=None, app=True)
            inst = int(data["id"])
            with self._lock:
                self._installations[scope.owner] = inst
        with self._lock:
            cached = self._install_tokens.get(inst)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        data = self._send("POST", f"/app/installations/{inst}/access_tokens", token=None, app=True)
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        with self._lock:
            self._install_tokens[inst] = (data["token"], expires)
        return data["token"]

    def _token_for(self, scope: Scope | None) -> str | None:
        if self.mode == "app":
            return self._installation_token(scope) if scope else None
        return self.pat or None

    # --- transport ----------------------------------------------------------------

    def _send(self, method: str, path: str, token: str | None, app: bool = False,
              json: Any = None, params: dict | None = None) -> Any:
        headers = {}
        if app:
            headers["Authorization"] = f"Bearer {self._app_jwt()}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self._http.request(method, path, headers=headers, json=json, params=params)
        except httpx.HTTPError as exc:
            raise GitHubError(0, f"network error: {exc}") from exc
        self._observe(resp, pat_call=bool(token) and self.mode == "pat")
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message", resp.text)
            except ValueError:
                msg = resp.text
            raise GitHubError(resp.status_code, _explain(resp.status_code, msg))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _observe(self, resp: httpx.Response, pat_call: bool) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
            self.rate_limit_reset = float(resp.headers.get("x-ratelimit-reset", 0)) or None
        expiry = resp.headers.get("github-authentication-token-expiration")
        if pat_call and expiry:
            # e.g. "2026-10-21 12:00:00 UTC" or "2026-10-21 12:00:00 -0700"
            for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
                try:
                    parsed = datetime.strptime(expiry, fmt)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    self.token_expires_at = parsed.timestamp()
                    break
                except ValueError:
                    continue

    def request(self, method: str, scope: Scope, path: str, **kw: Any) -> Any:
        return self._send(method, path, token=self._token_for(scope), **kw)

    # --- runners ---------------------------------------------------------------------

    def list_runners(self, scope: Scope) -> list[dict[str, Any]]:
        runners: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.request("GET", scope, f"/{scope.api}/actions/runners",
                                params={"per_page": 100, "page": page})
            batch = data.get("runners", [])
            runners.extend(batch)
            if len(batch) < 100 or len(runners) >= data.get("total_count", 0):
                return runners
            page += 1

    def delete_runner(self, scope: Scope, runner_id: int) -> None:
        self.request("DELETE", scope, f"/{scope.api}/actions/runners/{runner_id}")

    def runner_group_id(self, scope: Scope, name: str) -> int:
        """Repo runners always use group 1; org runners can target a named group."""
        if scope.kind == "repos" or not name or name.lower() == "default":
            return 1
        key = (scope.path, name.lower())
        cached = self._groups.get(key)
        if cached and time.time() - cached[1] < 3600:
            return cached[0]
        data = self.request("GET", scope, f"/{scope.api}/actions/runner-groups", params={"per_page": 100})
        for group in data.get("runner_groups", []):
            if group["name"].lower() == name.lower():
                self._groups[key] = (int(group["id"]), time.time())
                return int(group["id"])
        raise GitHubError(404, f"runner group '{name}' not found in org {scope.path}")

    def jit_config(self, scope: Scope, name: str, labels: list[str], group: str) -> tuple[int, str]:
        """Create a just-in-time runner. Returns (runner_id, encoded_jit_config)."""
        data = self.request("POST", scope, f"/{scope.api}/actions/runners/generate-jitconfig", json={
            "name": name,
            "runner_group_id": self.runner_group_id(scope, group),
            "labels": labels,
            "work_folder": "_work",
        })
        return int(data["runner"]["id"]), data["encoded_jit_config"]

    # --- health ------------------------------------------------------------------------

    def check_credentials(self) -> dict[str, Any]:
        """Cheap validity probe. For PATs this also refreshes token_expires_at."""
        if self.mode == "none":
            return {"ok": False, "mode": "none", "error": "no GITHUB_PAT or GitHub App configured"}
        try:
            if self.mode == "app":
                app = self._send("GET", "/app", token=None, app=True)
                return {"ok": True, "mode": "app", "identity": app.get("slug") or app.get("name")}
            self._send("GET", "/rate_limit", token=self.pat)
            try:
                user = self._send("GET", "/user", token=self.pat)
                identity = user.get("login")
            except GitHubError:
                identity = None  # some token types can't read /user; rate_limit proved validity
            return {"ok": True, "mode": "pat", "identity": identity, "expires_at": self.token_expires_at}
        except GitHubError as exc:
            return {"ok": False, "mode": self.mode, "error": exc.message}

    def latest_runner_version(self) -> str | None:
        """Latest actions/runner release, e.g. "2.335.1". Unauthenticated is fine at our cadence."""
        token = self.pat if self.mode == "pat" else None
        data = self._send("GET", "/repos/actions/runner/releases/latest", token=token)
        tag = (data or {}).get("tag_name", "")
        return tag.lstrip("v") or None


def _explain(status: int, msg: str) -> str:
    hints = {
        401: "credential is invalid or expired — rotate GITHUB_PAT or check the App key",
        403: "forbidden — check token permissions (repo Administration RW / org Self-hosted runners RW), "
             "SAML SSO authorization, or rate limiting",
        404: "not found — the repo/org does not exist or the credential cannot see it "
             "(for an App: is it installed on this owner?)",
    }
    hint = hints.get(status)
    return f"{msg} ({hint})" if hint else msg


def version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    try:
        return tuple(int(p) for p in version.split("."))
    except ValueError:
        return ()
