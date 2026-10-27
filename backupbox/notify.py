"""Push notifications through ntfy (https://ntfy.sh or self-hosted).

Only works when the Pi reaches the internet (Ethernet, hotel Wi-Fi, or the
phone's hotspot). In the field, on the BackupBox access point, the phone is
alerted by the captive portal instead (see api.py). Sending is best effort, in a
background thread: a failed notification never slows a backup down.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PRIORITIES = {"low": 2, "default": 3, "high": 4, "urgent": 5}


def split_topic_url(url: str) -> tuple[str, str] | None:
    """https://ntfy.sh/my-topic -> ("https://ntfy.sh/", "my-topic")."""
    parts = urlsplit(url)
    topic = parts.path.strip("/")
    if parts.scheme not in {"http", "https"} or not parts.netloc or not topic or "/" in topic:
        return None
    return f"{parts.scheme}://{parts.netloc}/", topic


class Notifier:
    def __init__(self, url: Callable[[], str], click_url: str, timeout: float = 8.0):
        self._url = url
        self.click_url = click_url
        self.timeout = timeout
        self.sent: list[dict] = []  # what was handed over, for tests and diagnostics
        self._queue: queue.Queue = queue.Queue(maxsize=20)
        self._thread: threading.Thread | None = None

    def send(self, title: str, message: str, priority: str = "default", tags: tuple[str, ...] = ()) -> None:
        target = split_topic_url(self._url() or "")
        if target is None:
            return
        base, topic = target
        payload = {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": PRIORITIES.get(priority, 3),
            "tags": list(tags),
            "click": self.click_url,
        }
        self.sent.append(payload)
        try:
            self._queue.put_nowait((base, payload))
        except queue.Full:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="bb-notify", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                base, payload = self._queue.get(timeout=30)
            except queue.Empty:
                return
            request = urllib.request.Request(
                base,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response.read()
            except Exception as exc:  # no internet in the field is the normal case
                log.info("Notification ntfy non envoyée : %s", exc)
