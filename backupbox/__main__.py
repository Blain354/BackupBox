"""Entry point: ``python -m backupbox`` (systemd) or ``python -m backupbox --simulate DIR``.

Hardware options come from the environment (``/etc/default/backupbox`` on the Pi):

=============================  ==============================================
BACKUPBOX_BUFFER_DIR           local buffer used when no SSD is plugged in
BACKUPBOX_BUFFER_RESERVE_GB    space always left free there (default 10)
BACKUPBOX_BUZZER_PIN           GPIO (BCM) of an active buzzer
BACKUPBOX_LED_PIN              GPIO (BCM) of a status LED
BACKUPBOX_BUTTON_PIN           GPIO (BCM) of a push button (3 = also wakes the Pi)
BACKUPBOX_PORTAL_URL           address the phone is sent to (http://10.42.0.1/)
BACKUPBOX_HOSTNAMES            names served without captive-portal redirect
=============================  ==============================================
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from . import __version__
from .api import create_app
from .indicators import Indicators
from .manager import Manager, buffer_reserve_from_env
from .notify import Notifier
from .platform import LinuxPlatform, SimPlatform
from .settings import StateStore
from .system import Led

DEFAULT_PORTAL = "http://10.42.0.1/"


def _pin(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        logging.getLogger("backupbox").warning("%s=%r ignoré : numéro de GPIO attendu", name, value)
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="backupbox", description="BackupBox web service")
    parser.add_argument("--host", default=os.environ.get("BACKUPBOX_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BACKUPBOX_PORT", "80")))
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("BACKUPBOX_STATE_DIR") or os.environ.get("STATE_DIRECTORY") or "/var/lib/backupbox",
        help="settings and history (systemd StateDirectory)",
    )
    parser.add_argument(
        "--simulate",
        default=os.environ.get("BACKUPBOX_SIMULATE"),
        metavar="DIR",
        help="fake devices: every sub-folder of DIR is a device (see backupbox.simctl)",
    )
    parser.add_argument(
        "--buffer-dir",
        default=os.environ.get("BACKUPBOX_BUFFER_DIR") or None,
        help="local buffer on the Pi, used when no SSD is plugged in",
    )
    parser.add_argument("--poll", type=float, default=float(os.environ.get("BACKUPBOX_POLL", "2")))
    parser.add_argument("--log-level", default=os.environ.get("BACKUPBOX_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("backupbox")

    store = StateStore(Path(args.state_dir))
    portal_url = os.environ.get("BACKUPBOX_PORTAL_URL", DEFAULT_PORTAL)
    hostnames = os.environ.get("BACKUPBOX_HOSTNAMES", "")
    if args.simulate:
        platform = SimPlatform(Path(args.simulate))
        indicators = Indicators(Led(None), settings=lambda: store.settings)
        log.warning("Mode simulation : périphériques simulés dans %s", args.simulate)
    else:
        platform = LinuxPlatform()
        indicators = Indicators(
            Led.autodetect(True),
            buzzer_pin=_pin("BACKUPBOX_BUZZER_PIN"),
            led_pin=_pin("BACKUPBOX_LED_PIN"),
            button_pin=_pin("BACKUPBOX_BUTTON_PIN"),
            settings=lambda: store.settings,
        )
    manager = Manager(
        platform,
        store,
        simulate=bool(args.simulate),
        poll_interval=args.poll,
        led=indicators,
        buffer_dir=Path(args.buffer_dir) if args.buffer_dir else None,
        buffer_reserve=buffer_reserve_from_env(os.environ.get("BACKUPBOX_BUFFER_RESERVE_GB")),
        notifier=Notifier(lambda: store.settings.ntfy_url, portal_url),
    )
    manager.start()
    log.info("BackupBox %s sur http://%s:%d", __version__, args.host, args.port)
    try:
        uvicorn.run(
            create_app(manager, portal_url=portal_url, extra_hostnames=hostnames),
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
