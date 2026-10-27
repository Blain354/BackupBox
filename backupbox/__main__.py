"""Entry point: ``python -m backupbox`` (systemd) or ``python -m backupbox --simulate DIR``."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from . import __version__
from .api import create_app
from .manager import Manager
from .platform import LinuxPlatform, SimPlatform
from .settings import StateStore
from .system import Led


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
    parser.add_argument("--poll", type=float, default=float(os.environ.get("BACKUPBOX_POLL", "2")))
    parser.add_argument("--log-level", default=os.environ.get("BACKUPBOX_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("backupbox")

    store = StateStore(Path(args.state_dir))
    if args.simulate:
        platform = SimPlatform(Path(args.simulate))
        led = Led(None)
        log.warning("Mode simulation : périphériques simulés dans %s", args.simulate)
    else:
        platform = LinuxPlatform()
        led = Led.autodetect(store.settings.led)
    manager = Manager(platform, store, simulate=bool(args.simulate), poll_interval=args.poll, led=led)
    manager.start()
    log.info("BackupBox %s sur http://%s:%d", __version__, args.host, args.port)
    try:
        uvicorn.run(create_app(manager), host=args.host, port=args.port, log_level="warning")
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
