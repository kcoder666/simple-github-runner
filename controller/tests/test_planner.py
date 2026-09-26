from app.config import DEFAULT_SETTINGS
from app.planner import RunnerView, plan

NOW = 1_000_000.0
S = dict(DEFAULT_SETTINGS)


def target(**kw):
    t = {"id": 1, "name": "app", "enabled": True, "min_idle": 2, "max_runners": 5}
    t.update(kw)
    return t


def runner(name, *, status="running", age=60.0, gh="online", busy=False, online_ago=5.0,
           first_online=True, busy_for=None, exit_code=None, **kw):
    started = NOW - age
    return RunnerView(
        name=name, container_status=status, created_at=started, started_at=started,
        exit_code=exit_code, gh_id=hash(name) % 1000 + 1, gh_status=gh, gh_busy=busy,
        first_online=(started + 1) if first_online else None,
        last_online=(NOW - online_ago) if first_online else None,
        busy_since=(NOW - busy_for) if busy_for is not None else None,
        **kw,
    )


def kinds(p):
    return [(a.kind, a.name, a.count) if a.kind == "spawn" else (a.kind, a.name) for a in p.actions]


def spawned(p):
    return sum(a.count for a in p.actions if a.kind == "spawn")


def removed(p):
    return {a.name: a for a in p.actions if a.kind == "remove"}


def test_empty_target_spawns_min_idle():
    assert spawned(plan(target(), [], NOW, S)) == 2


def test_idle_pool_satisfied_does_nothing():
    p = plan(target(), [runner("a"), runner("b")], NOW, S)
    assert p.actions == []
    assert p.states == {"a": "idle", "b": "idle"}


def test_busy_runners_are_backfilled_up_to_max():
    rs = [runner(n, busy=True, busy_for=30) for n in "abcd"]
    # 4 busy, 0 ready, min_idle 2, max 5 -> only 1 more fits.
    assert spawned(plan(target(), rs, NOW, S)) == 1


def test_demand_raises_ready_target():
    p = plan(target(min_idle=0), [], NOW, S, demand=3)
    assert spawned(p) == 3


def test_min_idle_zero_without_demand_spawns_nothing():
    assert spawned(plan(target(min_idle=0), [], NOW, S)) == 0


def test_spawn_batch_caps_burst():
    s = {**S, "spawn_batch": 2}
    assert spawned(plan(target(min_idle=10, max_runners=20), [], NOW, s)) == 2


def test_spawn_blocked_by_backoff():
    assert spawned(plan(target(), [], NOW, S, spawn_blocked=True)) == 0


def test_finished_runner_is_cleaned_up_and_replaced():
    rs = [runner("a"), runner("b", status="exited", exit_code=0)]
    p = plan(target(), rs, NOW, S)
    assert removed(p)["b"].failure is False
    assert removed(p)["b"].level == "info"
    assert spawned(p) == 1


def test_clean_exit_without_connecting_counts_as_failure():
    # Runner.Listener exits 0 on an invalid JIT config; the GitHub record survives.
    rs = [runner("a", status="exited", exit_code=0, first_online=False, gh="offline")]
    a = removed(plan(target(), rs, NOW, S))["a"]
    assert a.failure is True and a.level == "warn"


def test_fast_job_between_polls_is_not_a_failure():
    # Never observed online, but GitHub already dropped the record: a job ran.
    rs = [runner("a", status="exited", exit_code=0, first_online=False, gh=None)]
    assert removed(plan(target(), rs, NOW, S))["a"].failure is False


def test_crash_before_online_counts_as_failure():
    rs = [runner("a", status="exited", exit_code=1, first_online=False, gh=None)]
    a = removed(plan(target(), rs, NOW, S))["a"]
    assert a.failure is True and a.level == "warn"


def test_crash_after_online_is_not_a_crash_loop():
    rs = [runner("a", status="exited", exit_code=1)]
    assert removed(plan(target(), rs, NOW, S))["a"].failure is False


def test_oom_is_reported():
    rs = [runner("a", status="exited", exit_code=0, oom_killed=True)]
    assert "OOM" in removed(plan(target(), rs, NOW, S))["a"].reason


def test_starting_runner_gets_registration_timeout():
    young = runner("a", gh="offline", first_online=False, age=60)
    p = plan(target(min_idle=1), [young], NOW, S)
    assert p.states["a"] == "starting" and p.actions == []


def test_never_online_runner_is_replaced():
    old = runner("a", gh="offline", first_online=False, age=S["registration_timeout"] + 1)
    p = plan(target(min_idle=1), [old], NOW, S)
    assert p.states["a"] == "stale"
    assert removed(p)["a"].failure is True
    assert spawned(p) == 1


def test_offline_blip_within_grace_is_tolerated():
    r = runner("a", gh="offline", online_ago=S["offline_grace"] - 10)
    p = plan(target(min_idle=1), [r], NOW, S)
    assert p.states["a"] == "offline" and p.actions == []


def test_offline_past_grace_is_replaced():
    r = runner("a", gh="offline", online_ago=S["offline_grace"] + 10)
    p = plan(target(min_idle=1), [r], NOW, S)
    assert p.states["a"] == "stale"
    assert removed(p)["a"].failure is False
    assert spawned(p) == 1


def test_record_vanished_while_running_uses_offline_grace():
    r = runner("a", gh=None, online_ago=10)
    assert plan(target(min_idle=1), [r], NOW, S).states["a"] == "offline"


def test_hung_job_is_killed():
    r = runner("a", busy=True, busy_for=S["job_timeout"] + 1)
    p = plan(target(min_idle=0), [r], NOW, S)
    assert p.states["a"] == "stuck"
    assert removed(p)["a"].graceful is False


def test_job_timeout_zero_disables():
    r = runner("a", busy=True, busy_for=10 ** 7)
    assert plan(target(min_idle=0), [r], NOW, {**S, "job_timeout": 0}).states["a"] == "busy"


def test_old_idle_runner_is_recycled_gracefully():
    r = runner("a", age=S["idle_max_age"] + 1)
    p = plan(target(min_idle=1), [r], NOW, S)
    assert removed(p)["a"].graceful is True
    assert spawned(p) == 1


def test_outdated_idle_runner_is_recycled_but_busy_is_not():
    rs = [runner("a", outdated=True), runner("b", busy=True, busy_for=5, outdated=True)]
    p = plan(target(min_idle=1), rs, NOW, S)
    assert set(removed(p)) == {"a"}


def test_disabled_target_drains_ready_keeps_busy():
    rs = [runner("a"), runner("b", gh="offline", first_online=False), runner("c", busy=True, busy_for=5)]
    p = plan(target(enabled=False), rs, NOW, S)
    assert set(removed(p)) == {"a", "b"}
    assert all(a.graceful for a in removed(p).values())
    assert spawned(p) == 0


def test_excess_idle_scaled_down_oldest_first():
    rs = [runner("new", age=10), runner("old", age=500), runner("mid", age=100)]
    p = plan(target(min_idle=1), rs, NOW, S)
    assert set(removed(p)) == {"old", "mid"}


def test_over_max_scales_down_idle_only():
    rs = [runner("a", busy=True, busy_for=5), runner("b", busy=True, busy_for=5), runner("c")]
    p = plan(target(min_idle=1, max_runners=2), rs, NOW, S)
    assert set(removed(p)) == {"c"}
    assert spawned(p) == 0


def test_github_outage_freezes_health_decisions():
    rs = [runner("a", gh=None, first_online=False, age=10 ** 5), runner("b", status="exited", exit_code=0)]
    p = plan(target(), rs, NOW, S, gh_ok=False)
    assert p.states["a"] == "unknown"
    assert set(removed(p)) == {"b"}  # exited containers are still cleaned up
    assert spawned(p) == 0


def test_orphan_record_is_deleted_unless_busy():
    orphan = RunnerView(name="x", container_status=None, gh_id=7, gh_status="offline")
    busy_orphan = RunnerView(name="y", container_status=None, gh_id=8, gh_status="online", gh_busy=True)
    p = plan(target(min_idle=0), [orphan, busy_orphan], NOW, S)
    assert kinds(p) == [("delete_record", "x")]


def test_container_that_never_started_fails():
    r = RunnerView(name="a", container_status="created", created_at=NOW - 300)
    p = plan(target(min_idle=1), [r], NOW, S)
    assert p.states["a"] == "failed" and removed(p)["a"].failure is True
