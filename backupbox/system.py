"""Pi-specific helpers: status LED, power supply health, shutdown."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

THROTTLED_SYSFS = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
LED_CANDIDATES = [Path("/sys/class/leds/ACT"), Path("/sys/class/leds/led0")]


class Led:
    """Drives the green ACT LED so the box can be used without a phone.

    busy -> heartbeat, success -> steady on, error -> fast blink, idle -> SD activity.
    Needs write access to ``trigger`` and ``brightness`` (udev rule in deploy/).
    Every failure is swallowed: the LED is a convenience, never a reason to stop.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.state = "idle"
        self._blink_stop = threading.Event()
        self._blink_thread: threading.Thread | None = None
        self._idle_trigger = "mmc0"
        if path is not None:
            current = self._read("trigger")
            if current and "[" in current:
                active = current.split("[", 1)[1].split("]", 1)[0]
                if active not in {"none", "heartbeat", "default-on", "timer"}:
                    self._idle_trigger = active

    @classmethod
    def autodetect(cls, enabled: bool) -> "Led":
        if not enabled:
            return cls(None)
        for candidate in LED_CANDIDATES:
            if (candidate / "trigger").exists():
                return cls(candidate)
        return cls(None)

    def _read(self, name: str) -> str | None:
        try:
            return (self.path / name).read_text().strip()
        except (OSError, TypeError):
            return None

    def _write(self, name: str, value: str) -> None:
        if self.path is None:
            return
        try:
            (self.path / name).write_text(value)
        except OSError as exc:
            log.debug("LED %s=%s impossible: %s", name, value, exc)

    def set(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._stop_blink()
        if state == "busy":
            self._write("trigger", "heartbeat")
        elif state == "success":
            self._write("trigger", "none")
            self._write("brightness", "1")
        elif state == "error":
            self._write("trigger", "none")
            self._start_blink()
        else:
            self._write("trigger", self._idle_trigger)

    def _start_blink(self) -> None:
        if self.path is None:
            return
        self._blink_stop.clear()

        def run() -> None:
            on = False
            while not self._blink_stop.wait(0.15):
                on = not on
                self._write("brightness", "1" if on else "0")

        self._blink_thread = threading.Thread(target=run, name="bb-led", daemon=True)
        self._blink_thread.start()

    def _stop_blink(self) -> None:
        self._blink_stop.set()
        if self._blink_thread is not None:
            self._blink_thread.join(timeout=1)
            self._blink_thread = None


def power_status() -> dict:
    """Raspberry Pi throttling flags. Under-voltage is the usual cause of USB drops."""
    raw: str | None = None
    try:
        raw = THROTTLED_SYSFS.read_text().strip()
    except OSError:
        if shutil.which("vcgencmd"):
            try:
                out = subprocess.run(
                    ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
                ).stdout
                raw = out.strip().split("=", 1)[-1]
            except (OSError, subprocess.SubprocessError):
                raw = None
    if not raw:
        return {"available": False}
    try:
        value = int(raw, 16)
    except ValueError:
        return {"available": False}
    return {
        "available": True,
        "under_voltage_now": bool(value & 0x1),
        "throttled_now": bool(value & 0x4),
        "under_voltage_since_boot": bool(value & 0x10000),
        "throttled_since_boot": bool(value & 0x40000),
        "raw": hex(value),
    }


def clock_synchronized() -> bool | None:
    """True when NTP synchronised the clock (the Pi 4 has no RTC)."""
    if not shutil.which("timedatectl"):
        return None
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out == "yes" if out else None


def power_action(action: str, simulate: bool) -> None:
    if action not in {"poweroff", "reboot"}:
        raise ValueError(action)
    if simulate:
        log.warning("Simulation : %s ignoré", action)
        return
    subprocess.Popen(["systemctl", action, "--no-ask-password"])
