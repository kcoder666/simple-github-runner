"""Alert delivery to notification channels: Discord, Slack, Telegram, generic webhook.

An *alert* has a key (e.g. "backoff-3"). The same key is not re-sent within the
cooldown, and when the underlying problem clears the controller calls
`resolve(key)`, which sends a single "resolved" message — but only if that key
actually alerted, so recoveries of things nobody was told about stay quiet.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

log = logging.getLogger("controller.notify")

LEVELS = {"info": 0, "warn": 1, "error": 2}
CHANNEL_TYPES = ("discord", "slack", "telegram", "webhook")
SECRET_FIELDS = ("url", "bot_token")

TITLES = {
    "recover": "Runner replaced",
    "github": "GitHub API problem",
    "spawn": "Runner failed to start",
    "backoff": "Crash loop — spawning paused",
    "credentials": "GitHub credentials",
    "disk": "Disk space",
    "image": "Runner image",
    "controller": "Controller",
    "capacity": "Runner capacity",
    "test": "Test notification",
}
STYLE = {  # emoji, colour
    "error": ("🔴", 0xCF222E),
    "warn": ("🟠", 0xD29922),
    "info": ("🔵", 0x2F6FEB),
    "resolved": ("✅", 0x1A7F37),
}


@dataclass
class Alert:
    level: str               # info | warn | error
    kind: str
    message: str
    key: str | None = None
    target: str | None = None
    runner: str | None = None
    resolved: bool = False
    ts: float = field(default_factory=time.time)

    @property
    def title(self) -> str:
        base = TITLES.get(self.kind, self.kind.capitalize())
        return f"Resolved: {base}" if self.resolved else base

    @property
    def style(self) -> tuple[str, int]:
        return STYLE["resolved" if self.resolved else self.level]


# --- validation -----------------------------------------------------------------------

def validate_config(ctype: str, config: dict[str, Any]) -> dict[str, Any]:
    if ctype not in CHANNEL_TYPES:
        raise ValueError(f"type must be one of {', '.join(CHANNEL_TYPES)}")
    if ctype == "telegram":
        token = str(config.get("bot_token", "")).strip()
        chat = str(config.get("chat_id", "")).strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("bot_token must look like 123456:ABC… (from @BotFather)")
        if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]{4,}", chat):
            raise ValueError("chat_id must be a numeric chat id (e.g. -1001234567890) or @channelname")
        out = {"bot_token": token, "chat_id": chat}
        if str(config.get("thread_id", "")).strip():
            if not str(config["thread_id"]).strip().isdigit():
                raise ValueError("thread_id must be numeric")
            out["thread_id"] = str(config["thread_id"]).strip()
        return out
    url = str(config.get("url", "")).strip()
    patterns = {
        "discord": r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+",
        "slack": r"https://hooks\.slack\.com/(?:services|triggers|workflows)/\S+",
        "webhook": r"https?://\S+",
    }
    if not re.fullmatch(patterns[ctype], url):
        example = {"discord": "https://discord.com/api/webhooks/<id>/<token>",
                   "slack": "https://hooks.slack.com/services/…",
                   "webhook": "https://example.com/hook"}[ctype]
        raise ValueError(f"url must look like {example}")
    return {"url": url}


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """Hide secrets for display: keep enough to recognise the destination."""
    out = dict(config)
    for key in SECRET_FIELDS:
        value = out.get(key)
        if value:
            out[key] = _mask(value)
    return out


def merge_secrets(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Keep stored secrets when the client sends back the masked value (or nothing)."""
    merged = dict(new)
    for key in SECRET_FIELDS:
        if key in old and (not merged.get(key) or merged.get(key) == _mask(old[key])):
            merged[key] = old[key]
    return merged


def _mask(value: str) -> str:
    if value.startswith("http"):
        m = re.match(r"(https?://[^/]+)/", value)
        return f"{m.group(1) if m else ''}/…{value[-4:]}"
    return f"…{value[-4:]}"


# --- formatting -------------------------------------------------------------------------

def _context(alert: Alert) -> list[tuple[str, str]]:
    ctx = []
    if alert.target:
        ctx.append(("Target", alert.target))
    if alert.runner:
        ctx.append(("Runner", alert.runner))
    return ctx


def build_request(ctype: str, config: dict[str, Any], alert: Alert, public_url: str = "") -> tuple[str, dict]:
    """Return (url, json_payload) for one channel."""
    emoji, colour = alert.style
    ctx = _context(alert)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(alert.ts))

    if ctype == "discord":
        embed: dict[str, Any] = {
            "title": f"{emoji} {alert.title}",
            "description": alert.message[:4000],
            "color": colour,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(alert.ts)),
            "footer": {"text": "GitHub Runner Controller"},
        }
        if ctx:
            embed["fields"] = [{"name": k, "value": v[:1000], "inline": True} for k, v in ctx]
        if public_url:
            embed["url"] = public_url
        return config["url"], {"username": "GitHub Runners", "embeds": [embed],
                               "allowed_mentions": {"parse": []}}

    if ctype == "slack":
        details = "  ·  ".join(f"*{k}:* {_slack_escape(v)}" for k, v in ctx)
        footer = f"{stamp}" + (f"  ·  <{public_url}|Open dashboard>" if public_url else "")
        text = f"{emoji} *{alert.title}*\n{_slack_escape(alert.message)}"
        return config["url"], {
            "text": f"{emoji} {alert.title}: {alert.message}"[:3000],  # notification preview
            "attachments": [{
                "color": f"#{colour:06x}",
                "blocks": [b for b in (
                    {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": details}]} if details else None,
                    {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]},
                ) if b],
            }],
        }

    if ctype == "telegram":
        lines = [f"{emoji} <b>{html.escape(alert.title)}</b>", html.escape(alert.message)]
        if ctx:
            lines.append(" · ".join(f"<b>{html.escape(k)}:</b> <code>{html.escape(v)}</code>" for k, v in ctx))
        footer = html.escape(stamp)
        if public_url:
            footer += f' · <a href="{html.escape(public_url, quote=True)}">dashboard</a>'
        lines.append(f"<i>{footer}</i>")
        payload: dict[str, Any] = {"chat_id": config["chat_id"], "text": "\n".join(lines)[:4096],
                                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if config.get("thread_id"):
            payload["message_thread_id"] = int(config["thread_id"])
        return f"https://api.telegram.org/bot{config['bot_token']}/sendMessage", payload

    # generic webhook
    return config["url"], {
        "level": alert.level, "resolved": alert.resolved, "kind": alert.kind, "title": alert.title,
        "message": alert.message, "target": alert.target, "runner": alert.runner, "key": alert.key,
        "timestamp": alert.ts, "dashboard_url": public_url or None,
        "text": f"{emoji} {alert.title}: {alert.message}",
    }


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- delivery ----------------------------------------------------------------------------

Poster = Callable[[str, dict], httpx.Response]


def _default_post(url: str, payload: dict) -> httpx.Response:
    return httpx.post(url, json=payload, timeout=15)


class Notifier:
    def __init__(self, store: Any, public_url: str = "", post: Poster | None = None, sync: bool = False):
        self.store = store
        self.public_url = public_url.rstrip("/")
        self._post = post or _default_post
        self._sync = sync
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
        self._lock = threading.Lock()
        self._sent: dict[str, float] = {}     # key -> last sent
        self._open: dict[str, tuple[str, str]] = {}  # key -> (kind, level) of unresolved alerts
        self.status: dict[int, dict[str, Any]] = {}  # channel id -> last delivery result

    # public API ------------------------------------------------------------------------

    def alert(self, level: str, kind: str, message: str, key: str, target: str | None = None,
              runner: str | None = None) -> None:
        cooldown = float(self.store.settings().get("notify_cooldown", 0))
        with self._lock:
            if time.time() - self._sent.get(key, 0.0) < cooldown:
                return
            self._sent[key] = time.time()
            self._open[key] = (kind, level)
        self._dispatch(Alert(level, kind, message, key, target, runner))

    def resolve(self, key: str, message: str, target: str | None = None) -> None:
        with self._lock:
            opened = self._open.pop(key, None)
            self._sent.pop(key, None)
        if opened is not None:
            kind, level = opened
            # Same routing level as the original alert, so whoever got the
            # alert also gets the all-clear.
            self._dispatch(Alert(level, kind, message, key, target, resolved=True))

    def info(self, kind: str, message: str) -> None:
        """Informational broadcast (e.g. controller started); only to channels set to 'info'."""
        self._dispatch(Alert("info", kind, message))

    def send_test(self, channel: dict[str, Any]) -> tuple[bool, str]:
        alert = Alert("info", "test", "Notifications from the GitHub Runner Controller will arrive here.",
                      target=channel["name"])
        return self._deliver(channel, alert)

    def open_alerts(self) -> list[str]:
        with self._lock:
            return sorted(self._open)

    def flush(self, timeout: float = 10.0) -> None:
        """Best-effort wait for queued deliveries (used before the watchdog exits)."""
        self._pool.shutdown(wait=True, cancel_futures=False)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")

    # internals ---------------------------------------------------------------------------

    def _dispatch(self, alert: Alert) -> None:
        rank = LEVELS[alert.level]
        for ch in self.store.list_channels():
            if not ch["enabled"]:
                continue
            if rank < LEVELS[ch["min_level"]]:
                continue
            if self._sync:
                self._deliver(ch, alert)
            else:
                self._pool.submit(self._deliver, ch, alert)

    def _deliver(self, ch: dict[str, Any], alert: Alert) -> tuple[bool, str]:
        url, payload = build_request(ch["type"], ch["config"], alert, self.public_url)
        detail = ""
        for attempt in range(3):
            try:
                resp = self._post(url, payload)
                if resp.status_code < 300:
                    ok, detail = True, "delivered"
                    break
                detail = f"HTTP {resp.status_code}: {_short(resp.text)}"
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(_retry_after(resp, attempt))
                    continue
                ok = False
                break
            except httpx.HTTPError as exc:
                detail = f"network error: {type(exc).__name__}"
                time.sleep(2 ** attempt)
        else:
            ok = False
        self.status[ch["id"]] = {"ok": ok, "detail": detail, "at": time.time(), "title": alert.title}
        if not ok:
            log.warning("notification to channel %s (%s) failed: %s", ch["name"], ch["type"], detail)
        return ok, detail


def _short(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")[:200]


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        data = resp.json()
        hint = data.get("retry_after") or (data.get("parameters") or {}).get("retry_after")
        if hint:
            return min(float(hint), 30.0)
    except ValueError:
        pass
    header = resp.headers.get("retry-after")
    try:
        return min(float(header), 30.0) if header else float(2 ** attempt)
    except ValueError:
        return float(2 ** attempt)
