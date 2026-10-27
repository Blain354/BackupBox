"""LinuxPlatform against a fake udisksctl, and the Pi helpers (LED, power flags)."""

import os
import stat
from pathlib import Path

import pytest

from backupbox import system
from backupbox.devices import BlockDevice
from backupbox.platform import LinuxPlatform, PlatformError

DEV = BlockDevice(
    kname="sdb1", path="/dev/sdb1", disk_kname="sdb", disk_path="/dev/sdb", fstype="exfat",
    label="GOPRO", uuid="AAAA", size=1, mountpoint=None, removable=True, transport="usb", model=None,
)


@pytest.fixture
def fake_udisksctl(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    script = tmp_path / "bin" / "udisksctl"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in\n'
        '  mount) echo "Mounted /dev/sdb1 at /media/backupbox/GOPRO";;\n'
        '  unmount) if [ -n "$FAIL_UNMOUNT" ]; then echo "Error unmounting /dev/sdb1: target is busy" >&2; exit 1; fi;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{script.parent}:{os.environ['PATH']}")
    return log


def test_mount_read_only_and_parse_mountpoint(fake_udisksctl):
    mp = LinuxPlatform().mount(DEV, read_only=True)
    assert mp == "/media/backupbox/GOPRO"
    assert fake_udisksctl.read_text().split() == ["mount", "-b", "/dev/sdb1", "-o", "ro", "--no-user-interaction"]


def test_unmount_error_message_is_surfaced(fake_udisksctl, monkeypatch):
    monkeypatch.setenv("FAIL_UNMOUNT", "1")
    with pytest.raises(PlatformError, match="target is busy"):
        LinuxPlatform().unmount(DEV)


def test_power_off_targets_the_whole_disk(fake_udisksctl):
    LinuxPlatform().power_off(DEV)
    assert "power-off -b /dev/sdb" in fake_udisksctl.read_text()


def test_missing_udisksctl(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(PlatformError, match="udisks2"):
        LinuxPlatform().mount(DEV, read_only=True)


def make_led(tmp_path: Path) -> Path:
    led = tmp_path / "ACT"
    led.mkdir()
    (led / "trigger").write_text("none [mmc0] timer heartbeat default-on\n")
    (led / "brightness").write_text("0")
    return led


def test_led_patterns(tmp_path):
    path = make_led(tmp_path)
    led = system.Led(path)
    led.set("busy")
    assert (path / "trigger").read_text() == "heartbeat"
    led.set("success")
    assert (path / "brightness").read_text() == "1"
    led.set("error")
    led.set("idle")
    assert (path / "trigger").read_text() == "mmc0"


def test_led_without_permission_never_raises(tmp_path):
    led = system.Led(tmp_path / "missing")
    for state in ["busy", "success", "error", "idle"]:
        led.set(state)


def test_power_status_flags(tmp_path, monkeypatch):
    flags = tmp_path / "get_throttled"
    flags.write_text("50005\n")
    monkeypatch.setattr(system, "THROTTLED_SYSFS", flags)
    status = system.power_status()
    assert status["available"]
    assert status["under_voltage_now"] and status["throttled_now"]
    assert status["under_voltage_since_boot"]
    flags.write_text("0\n")
    assert system.power_status()["under_voltage_now"] is False
