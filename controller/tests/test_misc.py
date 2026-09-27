import threading
from types import SimpleNamespace

import pytest

from app.controller import Controller
from app.github import Scope, parse_scope, version_tuple
from app.store import Store


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/acme", Scope("orgs", "acme")),
    ("https://github.com/acme/", Scope("orgs", "acme")),
    ("https://github.com/acme/app", Scope("repos", "acme/app")),
    ("https://github.com/acme/app.git", Scope("repos", "acme/app")),
    ("https://ghe.example.com/acme/app", Scope("repos", "acme/app")),
])
def test_parse_scope(url, expected):
    assert parse_scope(url) == expected


@pytest.mark.parametrize("url", ["github.com/acme", "https://github.com/", "https://github.com/a/b/c",
                                 "https://github.com/a b"])
def test_parse_scope_rejects(url):
    with pytest.raises(ValueError):
        parse_scope(url)


def test_version_tuple_ordering():
    assert version_tuple("2.336.0") > version_tuple("2.335.10") > version_tuple("2.335.1")
    assert version_tuple(None) == ()


def test_store_roundtrip(tmp_path):
    store = Store(str(tmp_path))
    t = store.create_target({"name": "app", "url": "https://github.com/a/b", "labels": "gpu,big"})
    assert t["labels"] == ["gpu", "big"] and t["enabled"] is True and t["min_idle"] == 1
    store.update_target(t["id"], {"min_idle": 3, "enabled": False})
    t = store.get_target(t["id"])
    assert t["min_idle"] == 3 and t["enabled"] is False
    assert store.update_settings({"job_timeout": "60", "auto_update_runner": "false"})["job_timeout"] == 60
    assert store.settings()["auto_update_runner"] is False
    with pytest.raises(KeyError):
        store.update_settings({"nope": 1})
    store.add_event("warn", "recover", "hello", t["id"], "r1")
    assert store.events(target_id=t["id"])[0]["message"] == "hello"


def _fake_controller(tmp_path, targets):
    store = Store(str(tmp_path))
    for t in targets:
        store.create_target(t)
    return SimpleNamespace(store=store, base_labels=["self-hosted", "linux", "x64"], demand={},
                           wake=threading.Event())


def _job(action, job_id=1, repo="acme/app", labels=("self-hosted",)):
    return {"action": action, "repository": {"full_name": repo},
            "workflow_job": {"id": job_id, "labels": list(labels)}}


def test_webhook_demand_matches_repo_and_labels(tmp_path):
    fake = _fake_controller(tmp_path, [
        {"name": "other", "url": "https://github.com/acme/other"},
        {"name": "gpu", "url": "https://github.com/acme/app", "labels": "gpu"},
    ])
    assert Controller.on_workflow_job(fake, _job("queued", labels=("self-hosted", "GPU"))) == "queued for gpu"
    assert fake.demand[1][0] == 2
    assert fake.wake.is_set()
    Controller.on_workflow_job(fake, _job("in_progress"))
    assert fake.demand == {}


def test_webhook_org_target_and_label_mismatch(tmp_path):
    fake = _fake_controller(tmp_path, [{"name": "org", "url": "https://github.com/acme"}])
    assert Controller.on_workflow_job(fake, _job("queued", repo="acme/x")) == "queued for org"
    assert Controller.on_workflow_job(fake, _job("queued", 2, labels=("ubuntu-latest",))) == "no matching target"
    assert Controller.on_workflow_job(fake, _job("queued", 3, repo="other/x")) == "no matching target"


def test_webhook_prefers_repo_target_over_org(tmp_path):
    # "CremiAI" sorts before the repo target, but the repo's own pool should win.
    fake = _fake_controller(tmp_path, [
        {"name": "CremiAI", "url": "https://github.com/CremiAI"},
        {"name": "frontend", "url": "https://github.com/CremiAI/frontend"},
    ])
    assert Controller.on_workflow_job(fake, _job("queued", repo="CremiAI/frontend")) == "queued for frontend"
    # Other repos in the org still go to the org pool.
    assert Controller.on_workflow_job(fake, _job("queued", 2, repo="CremiAI/other")) == "queued for CremiAI"
    # The org and repo webhooks both deliver the same job: counted once.
    Controller.on_workflow_job(fake, _job("queued", repo="CremiAI/frontend"))
    assert sum(1 for tid, _ in fake.demand.values() if tid == 2) == 1
