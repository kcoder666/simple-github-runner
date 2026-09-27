import httpx
import pytest

from app.notify import Alert, Notifier, build_request, mask_config, merge_secrets, validate_config
from app.store import Store

DISCORD = "https://discord.com/api/webhooks/123456/abcDEF-ghi_jkl"
SLACK = "https://hooks.slack.com/services/T000/B000/XXXXXXXX"
TG_TOKEN = "123456789:AAHfakefakefakefakefakefake_x"


class Recorder:
    def __init__(self, status=200, body="ok"):
        self.calls = []
        self.status = status
        self.body = body

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        return httpx.Response(self.status, text=self.body)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path))
    s.update_settings({"notify_cooldown": 3600})
    return s


def notifier(store, rec, url="https://gh-runners.example.com"):
    return Notifier(store, url, post=rec, sync=True)


# --- validation / secrets ------------------------------------------------------

@pytest.mark.parametrize("ctype,config", [
    ("discord", {"url": DISCORD}),
    ("discord", {"url": "https://discordapp.com/api/webhooks/1/abc"}),
    ("slack", {"url": SLACK}),
    ("telegram", {"bot_token": TG_TOKEN, "chat_id": "-1001234567890"}),
    ("telegram", {"bot_token": TG_TOKEN, "chat_id": "@mychannel", "thread_id": "42"}),
    ("webhook", {"url": "https://example.com/hook"}),
])
def test_valid_configs(ctype, config):
    assert validate_config(ctype, config)


@pytest.mark.parametrize("ctype,config", [
    ("discord", {"url": SLACK}),
    ("slack", {"url": DISCORD}),
    ("telegram", {"bot_token": "nope", "chat_id": "1"}),
    ("telegram", {"bot_token": TG_TOKEN, "chat_id": "general"}),
    ("webhook", {"url": "ftp://x"}),
    ("email", {"url": "x"}),
])
def test_invalid_configs(ctype, config):
    with pytest.raises(ValueError):
        validate_config(ctype, config)


def test_secrets_masked_and_preserved_on_edit():
    stored = {"bot_token": TG_TOKEN, "chat_id": "-100"}
    masked = mask_config(stored)
    assert TG_TOKEN not in str(masked) and masked["chat_id"] == "-100"
    # Client sends the masked value back unchanged -> keep the real one.
    assert merge_secrets(masked, stored)["bot_token"] == TG_TOKEN
    # Client sends a new value -> use it.
    assert merge_secrets({**masked, "bot_token": "999:new"}, stored)["bot_token"] == "999:new"
    assert DISCORD not in mask_config({"url": DISCORD})["url"]


# --- formatting ------------------------------------------------------------------

def test_discord_payload():
    url, p = build_request("discord", {"url": DISCORD}, Alert("error", "backoff", "boom", target="web"), "https://x")
    assert url == DISCORD
    embed = p["embeds"][0]
    assert "Crash loop" in embed["title"] and embed["description"] == "boom"
    assert embed["fields"][0] == {"name": "Target", "value": "web", "inline": True}
    assert p["allowed_mentions"] == {"parse": []}  # an error message can't @everyone


def test_slack_payload_escapes():
    _, p = build_request("slack", {"url": SLACK}, Alert("warn", "disk", "<!channel> & co"), "https://x")
    text = p["attachments"][0]["blocks"][0]["text"]["text"]
    assert "&lt;!channel&gt; &amp; co" in text
    assert "<https://x|Open dashboard>" in p["attachments"][0]["blocks"][-1]["elements"][0]["text"]


def test_telegram_payload_html_escaped_and_topic():
    cfg = {"bot_token": TG_TOKEN, "chat_id": "-100", "thread_id": "7"}
    url, p = build_request("telegram", cfg, Alert("error", "github", "a <b> & c", target="x<y"))
    assert url == f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    assert p["parse_mode"] == "HTML" and p["message_thread_id"] == 7
    assert "a &lt;b&gt; &amp; c" in p["text"] and "x&lt;y" in p["text"]


def test_resolved_style():
    _, p = build_request("webhook", {"url": "https://h"}, Alert("info", "disk", "ok", resolved=True))
    assert p["resolved"] is True and p["title"] == "Resolved: Disk space"


# --- routing ------------------------------------------------------------------------

def test_level_filtering(store):
    store.create_channel("errors", "webhook", {"url": "https://a"}, "error")
    store.create_channel("warns", "webhook", {"url": "https://b"}, "warn")
    store.create_channel("all", "webhook", {"url": "https://c"}, "info")
    store.create_channel("off", "webhook", {"url": "https://d"}, "info", enabled=False)
    rec = Recorder()
    n = notifier(store, rec)
    n.alert("warn", "disk", "low", "disk-low")
    assert sorted(u for u, _ in rec.calls) == ["https://b", "https://c"]
    rec.calls.clear()
    n.info("controller", "started")
    assert [u for u, _ in rec.calls] == ["https://c"]


def test_cooldown_and_resolve(store):
    store.create_channel("w", "webhook", {"url": "https://a"}, "warn")
    rec = Recorder()
    n = notifier(store, rec)
    n.resolve("disk-low", "fine")           # never alerted -> silent
    assert rec.calls == []
    n.alert("warn", "disk", "low", "disk-low")
    n.alert("warn", "disk", "low", "disk-low")  # within cooldown -> suppressed
    assert len(rec.calls) == 1
    assert n.open_alerts() == ["disk-low"]
    n.resolve("disk-low", "back to 40%")
    assert rec.calls[-1][1]["resolved"] is True
    assert n.open_alerts() == []
    n.alert("warn", "disk", "low again", "disk-low")  # resolution resets the cooldown
    assert len(rec.calls) == 3


def test_resolution_reaches_error_only_channels(store):
    store.create_channel("errors", "webhook", {"url": "https://a"}, "error")
    rec = Recorder()
    n = notifier(store, rec)
    n.alert("error", "backoff", "loop", "backoff-1")
    n.resolve("backoff-1", "cleared")
    assert [p["resolved"] for _, p in rec.calls] == [False, True]


def test_failed_delivery_recorded(store, monkeypatch):
    monkeypatch.setattr("app.notify.time.sleep", lambda s: None)
    ch = store.create_channel("bad", "webhook", {"url": "https://a"}, "warn")
    rec = Recorder(status=404, body='{"message":"Unknown Webhook"}')
    n = notifier(store, rec)
    ok, detail = n.send_test(ch)
    assert not ok and "404" in detail and len(rec.calls) == 1  # 4xx is not retried
    assert n.status[ch["id"]]["ok"] is False


def test_rate_limit_retried(store, monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.notify.time.sleep", sleeps.append)
    ch = store.create_channel("tg", "webhook", {"url": "https://a"}, "warn")
    responses = [httpx.Response(429, json={"parameters": {"retry_after": 3}}), httpx.Response(200)]
    n = Notifier(store, post=lambda u, p: responses.pop(0), sync=True)
    assert n.send_test(ch)[0] is True
    assert sleeps == [3.0]
