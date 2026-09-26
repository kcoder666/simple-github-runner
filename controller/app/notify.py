"""Outbound alerts to a Slack / Discord / generic incoming webhook, with cooldown."""

from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger("controller.notify")


class Notifier:
    def __init__(self) -> None:
        self._sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def send(self, url: str, key: str, text: str, cooldown: float) -> None:
        """Post `text` unless an alert with the same key went out within `cooldown`."""
        if not url:
            return
        with self._lock:
            last = self._sent.get(key, 0.0)
            if time.time() - last < cooldown:
                return
            self._sent[key] = time.time()
        threading.Thread(target=self._post, args=(url, text), daemon=True).start()

    def reset(self, key: str) -> None:
        with self._lock:
            self._sent.pop(key, None)

    @staticmethod
    def _post(url: str, text: str) -> None:
        # "text" is Slack's field, "content" is Discord's; each ignores the other.
        payload = {"text": text, "content": text[:1900]}
        try:
            httpx.post(url, json=payload, timeout=10).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("alert webhook failed: %s", exc)
