"""ntfy push notifications, against a local HTTP server."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from backupbox.notify import Notifier, split_topic_url


def test_split_topic_url():
    assert split_topic_url("https://ntfy.sh/backupbox-g") == ("https://ntfy.sh/", "backupbox-g")
    assert split_topic_url("http://10.0.0.5:8080/bb") == ("http://10.0.0.5:8080/", "bb")
    for bad in ["", "ntfy.sh/x", "https://ntfy.sh/", "https://ntfy.sh/a/b", "ftp://x/y"]:
        assert split_topic_url(bad) is None


def test_notifier_posts_json_with_click_url():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/backupbox-test"
        notifier = Notifier(lambda: url, "http://10.42.0.1/")
        notifier.send("DJI Avata 2 branché", "Sauvegarde dans 30 s", priority="high", tags=("floppy_disk",))
        for _ in range(100):
            if received:
                break
            time.sleep(0.02)
    finally:
        server.shutdown()
    assert received == [
        {
            "topic": "backupbox-test",
            "title": "DJI Avata 2 branché",
            "message": "Sauvegarde dans 30 s",
            "priority": 4,
            "tags": ["floppy_disk"],
            "click": "http://10.42.0.1/",
        }
    ]


def test_notifier_disabled_without_url_and_silent_offline():
    Notifier(lambda: "", "http://10.42.0.1/").send("t", "m")  # nothing configured
    offline = Notifier(lambda: "http://127.0.0.1:9/x", "http://10.42.0.1/", timeout=0.5)
    offline.send("t", "m")  # connection refused: logged, never raised
    time.sleep(0.2)
