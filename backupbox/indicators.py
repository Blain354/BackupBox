"""What the closed box says without a phone: LEDs, buzzer, push button.

States (from the manager):

=========  ======================  =================  =========================
state      ACT LED / GPIO LED      buzzer             meaning
=========  ======================  =================  =========================
idle       SD activity / off       silent             nothing to do
attention  slow blink              one beep on entry  a device waits for you
busy       heartbeat / short blink double beep every  writing: do NOT unplug
                                   ``beep_interval_s``
success    steady on               three short beeps  verified, safe to unplug
error      fast blink              three long beeps   look at the phone
=========  ======================  =================  =========================

Push button (optional): short press starts what is waiting (same as « Tout
transférer »), a 3-second press shuts the box down safely (refused while
writing). Wired to GPIO 3, the same button also wakes the Pi after shutdown.

All GPIO parts are optional and fail-safe: a missing library, pin or permission
only disables that part.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .system import Led

log = logging.getLogger(__name__)

CHIMES: dict[str, list[tuple[float, float]]] = {
    # (on, off) pairs in seconds
    "detected": [(0.08, 0.0)],
    "attention": [(0.08, 0.0)],
    "start": [(0.3, 0.0)],
    "success": [(0.07, 0.08), (0.07, 0.08), (0.07, 0.0)],
    "error": [(0.6, 0.25), (0.6, 0.25), (0.6, 0.0)],
    "refused": [(0.15, 0.1), (0.15, 0.0)],
    "ack": [(0.04, 0.0)],
    "shutdown": [(0.5, 0.2), (0.5, 0.0)],
}
DOUBLE_BEEP = [(0.08, 0.1), (0.08, 0.0)]

STATE_CHIMES = {"attention": "attention", "success": "success", "error": "error"}


class Indicators:
    def __init__(
        self,
        act: Led | None = None,
        *,
        buzzer_pin: int | None = None,
        led_pin: int | None = None,
        button_pin: int | None = None,
        settings: Callable[[], object] | None = None,
        pin_factory=None,
    ) -> None:
        self.act = act or Led(None)
        self.state = "idle"
        self._settings = settings or (lambda: None)
        self._sound_lock = threading.Lock()
        self._busy_stop = threading.Event()
        self._busy_thread: threading.Thread | None = None
        self.buzzer = self.led = self.button = None
        self.on_short_press: Callable[[], None] | None = None
        self.on_long_press: Callable[[], None] | None = None
        self._held = False
        self.events: list[str] = []  # chime history, for tests and diagnostics
        if buzzer_pin is None and led_pin is None and button_pin is None:
            return
        try:
            from gpiozero import LED, Buzzer, Button, Device  # noqa: PLC0415 - optional

            if pin_factory is not None:
                Device.pin_factory = pin_factory
        except Exception as exc:  # ImportError, or no usable pin backend
            log.warning("GPIO indisponible (%s) : buzzer, DEL et bouton désactivés.", exc)
            return
        for attr, factory, pin in (
            ("buzzer", lambda p: Buzzer(p), buzzer_pin),
            ("led", lambda p: LED(p), led_pin),
            ("button", lambda p: Button(p, pull_up=True, hold_time=3, bounce_time=0.05), button_pin),
        ):
            if pin is None:
                continue
            try:
                setattr(self, attr, factory(pin))
            except Exception as exc:
                log.warning("GPIO %s (%s) indisponible : %s", pin, attr, exc)
        if self.button is not None:
            self.button.when_held = self._button_held
            self.button.when_released = self._button_released

    # ----------------------------------------------------------------- state

    def _setting(self, name: str, default):
        settings = self._settings()
        return getattr(settings, name, default) if settings is not None else default

    def set(self, state: str) -> None:
        if state == self.state:
            return
        previous, self.state = self.state, state
        if self._setting("led", True):
            self.act.set(state)
        else:
            self.act.set("idle")
        self._apply_gpio_led(state)
        self._stop_busy()
        if state == "busy":
            self._start_busy()
        elif state in STATE_CHIMES and previous != state:
            self.chime(STATE_CHIMES[state])

    def _apply_gpio_led(self, state: str) -> None:
        if self.led is None:
            return
        try:
            if state == "busy":
                self.led.blink(on_time=0.15, off_time=0.85)
            elif state == "attention":
                self.led.blink(on_time=0.5, off_time=0.5)
            elif state == "error":
                self.led.blink(on_time=0.1, off_time=0.1)
            elif state == "success":
                self.led.on()
            else:
                self.led.off()
        except Exception as exc:
            log.debug("DEL GPIO : %s", exc)

    # ---------------------------------------------------------------- sounds

    def chime(self, name: str) -> None:
        self.events.append(name)
        pattern = CHIMES.get(name)
        if not pattern or self.buzzer is None or not self._setting("buzzer", True):
            return
        threading.Thread(target=self._play, args=(pattern,), name="bb-chime", daemon=True).start()

    def _play(self, pattern: list[tuple[float, float]]) -> None:
        with self._sound_lock:
            for on, off in pattern:
                try:
                    self.buzzer.on()
                    time.sleep(on)
                    self.buzzer.off()
                except Exception:
                    return
                if off:
                    time.sleep(off)

    def _start_busy(self) -> None:
        self._busy_stop.clear()

        def run() -> None:
            while not self._busy_stop.is_set():
                if self.buzzer is not None and self._setting("buzzer", True):
                    self._play(DOUBLE_BEEP)
                interval = max(1, int(self._setting("beep_interval_s", 1)))
                if self._busy_stop.wait(max(interval - 0.26, 0.2)):
                    return

        self._busy_thread = threading.Thread(target=run, name="bb-busy-beep", daemon=True)
        self._busy_thread.start()

    def _stop_busy(self) -> None:
        self._busy_stop.set()
        if self._busy_thread is not None:
            self._busy_thread.join(timeout=2)
            self._busy_thread = None

    # ---------------------------------------------------------------- button

    def _button_held(self) -> None:
        self._held = True
        if self.on_long_press:
            self.on_long_press()

    def _button_released(self) -> None:
        if not self._held and self.on_short_press:
            self.on_short_press()
        self._held = False

    def close(self) -> None:
        self._stop_busy()
        self.act.set("idle")
        for part in (self.buzzer, self.led, self.button):
            if part is not None:
                try:
                    part.close()
                except Exception:
                    pass
