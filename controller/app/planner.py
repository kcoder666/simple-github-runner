"""Pure decision logic: given what Docker and GitHub report, what should happen?

Kept free of I/O so every recovery rule is unit-testable.

Runner states
-------------
starting  container running, not yet online in GitHub (within registration_timeout)
idle      online, waiting for a job
busy      online, running a job
offline   was online, dropped offline recently (within offline_grace) — a blip
stale     wedged: never came online, or offline past the grace period  -> replace
stuck     busy longer than job_timeout                                 -> replace
exited    container finished (normal after an ephemeral job, or a crash) -> clean up
failed    container was created but never started                      -> replace
orphan    GitHub runner record with no container                       -> delete record
unknown   GitHub state unavailable this cycle (API error) — leave alone
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LIVE = ("starting", "idle", "busy", "offline")
READY = ("starting", "idle", "offline")


@dataclass
class RunnerView:
    name: str
    container_status: str | None          # None -> no container (GitHub-only record)
    created_at: float = 0.0
    started_at: float | None = None
    exit_code: int | None = None
    oom_killed: bool = False
    gh_id: int | None = None
    gh_status: str | None = None           # "online" | "offline" | None (no record)
    gh_busy: bool = False
    first_online: float | None = None
    last_online: float | None = None
    busy_since: float | None = None
    outdated: bool = False


@dataclass
class Action:
    kind: str                  # "remove" | "spawn" | "delete_record"
    reason: str
    name: str | None = None
    level: str = "info"
    graceful: bool = False     # delete the GitHub record first; skip if the runner turned busy
    failure: bool = False      # counts toward crash-loop backoff
    count: int = 0


@dataclass
class Plan:
    states: dict[str, str] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def classify(r: RunnerView, now: float, s: dict[str, Any], gh_ok: bool) -> str:
    if r.container_status is None:
        return "orphan"
    if r.container_status in ("exited", "dead"):
        return "exited"
    age = now - (r.started_at or r.created_at)
    if r.container_status == "created":
        return "failed" if now - r.created_at > 120 else "starting"
    if not gh_ok:
        return "unknown"
    if r.container_status != "running":  # paused / restarting / removing
        return "stale" if age > s["registration_timeout"] else "starting"
    if r.gh_status == "online":
        if not r.gh_busy:
            return "idle"
        timeout = s["job_timeout"]
        if timeout and r.busy_since is not None and now - r.busy_since > timeout:
            return "stuck"
        return "busy"
    if r.last_online is None:
        return "starting" if age < s["registration_timeout"] else "stale"
    return "offline" if now - r.last_online < s["offline_grace"] else "stale"


def plan(target: dict[str, Any], runners: list[RunnerView], now: float, s: dict[str, Any],
         gh_ok: bool = True, spawn_blocked: bool = False, demand: int = 0) -> Plan:
    """demand: queued jobs known (via webhook) to be waiting for this target."""
    p = Plan()
    for r in runners:
        p.states[r.name] = classify(r, now, s, gh_ok)

    def remove(r: RunnerView, reason: str, level: str = "info", graceful: bool = False,
               failure: bool = False) -> None:
        p.actions.append(Action("remove", reason, r.name, level, graceful, failure))

    for r in runners:
        state = p.states[r.name]
        if state == "orphan":
            if not r.gh_busy:
                p.actions.append(Action("delete_record", "GitHub runner without a container", r.name))
        elif state == "exited":
            if r.exit_code == 0 and r.first_online is None and r.gh_status is not None:
                # The runner exits 0 even on a rejected/invalid config. After a
                # real job GitHub deletes the ephemeral record; a record that is
                # still there on a runner we never saw online means it never
                # connected.
                remove(r, "exited without ever connecting to GitHub", "warn", failure=True)
            elif r.exit_code == 0 and not r.oom_killed:
                remove(r, "finished")
            else:
                why = "killed by the OOM killer (raise the memory limit)" if r.oom_killed else f"exit code {r.exit_code}"
                # Only failures before the runner ever came online count as a
                # crash loop; a job that died mid-run is the workflow's business.
                remove(r, f"exited unexpectedly: {why}", "warn", failure=r.first_online is None)
        elif state == "failed":
            remove(r, "container never started", "error", failure=True)
        elif state == "stale":
            if r.last_online is None:
                remove(r, "never came online within registration_timeout", "warn", failure=True)
            else:
                remove(r, "went offline and did not recover within offline_grace", "warn")
        elif state == "stuck":
            remove(r, "busy longer than job_timeout — job presumed hung", "warn")
        elif state == "idle":
            age = now - (r.started_at or r.created_at)
            if s["idle_max_age"] and age > s["idle_max_age"]:
                remove(r, "idle past idle_max_age — recycling", graceful=True)
            elif s.get("recycle_outdated") and r.outdated:
                remove(r, "running an outdated runner image — recycling", graceful=True)

    removing = {a.name for a in p.actions if a.kind == "remove"}
    live = [r for r in runners if p.states[r.name] in LIVE and r.name not in removing]
    ready = [r for r in live if p.states[r.name] in READY]
    unknown = [r for r in runners if p.states[r.name] == "unknown"]

    if not target["enabled"]:
        # Drain: let busy runners finish, release everything else.
        for r in ready:
            remove(r, "target disabled — draining", graceful=True)
    elif gh_ok:
        max_runners = target["max_runners"]
        # Ready runners wanted: the warm pool, or more if jobs are queued (this
        # is what lets min_idle=0 scale from zero when webhooks are configured).
        min_idle = max(target["min_idle"], demand)
        excess = max(len(ready) - min_idle, len(live) - max_runners, 0)
        if excess:
            idle = sorted((r for r in ready if p.states[r.name] == "idle"),
                          key=lambda r: r.started_at or r.created_at)
            for r in idle[:excess]:
                remove(r, "scaling down", graceful=True)
        want = min(min_idle - len(ready), max_runners - len(live), int(s.get("spawn_batch", 5)))
        if want > 0 and not spawn_blocked:
            p.actions.append(Action("spawn", "keeping min_idle ready runners", count=want))

    states = [p.states[r.name] for r in runners]
    p.counts = {k: states.count(k) for k in set(states)}
    p.counts["live"] = len(live) + len(unknown)
    return p
