"""US-25/US-26: buzzer, GPIO LED and button, on gpiozero's mock pins."""

import time

import pytest

gpiozero = pytest.importorskip("gpiozero")
from gpiozero.pins.mock import MockFactory  # noqa: E402

from backupbox.indicators import Indicators  # noqa: E402
from backupbox.settings import Settings  # noqa: E402


@pytest.fixture
def factory():
    f = MockFactory()
    yield f
    f.reset()


def make(factory, **settings):
    s = Settings(**settings)
    return Indicators(buzzer_pin=17, led_pin=27, button_pin=3, pin_factory=factory, settings=lambda: s), s


def buzzer_toggles(factory, pin=17):
    return sum(1 for _, state in factory.pin(pin).states if state)


def test_busy_double_beeps_until_done(factory):
    ind, _ = make(factory, beep_interval_s=1)
    factory.pin(17).clear_states()
    ind.set("busy")
    time.sleep(1.4)
    ind.set("success")
    time.sleep(0.5)
    beeps = buzzer_toggles(factory)
    # two double beeps (t=0 and t=1 s) + three success beeps
    assert beeps == 7
    assert factory.pin(27).state  # success: LED steady on
    ind.close()


def test_buzzer_setting_silences_everything(factory):
    ind, s = make(factory, buzzer=False)
    factory.pin(17).clear_states()
    ind.set("busy")
    time.sleep(0.3)
    ind.set("error")
    time.sleep(0.2)
    assert buzzer_toggles(factory) == 0
    assert ind.events == ["error"]
    ind.close()


def test_button_short_and_long_press(factory):
    ind, _ = make(factory)
    calls = []
    ind.on_short_press = lambda: calls.append("short")
    ind.on_long_press = lambda: calls.append("long")
    button = factory.pin(3)
    button.drive_low()
    time.sleep(0.1)
    button.drive_high()
    time.sleep(0.1)
    assert calls == ["short"]
    ind.button.hold_time = 0.2
    button.drive_low()
    time.sleep(0.4)
    button.drive_high()
    time.sleep(0.1)
    assert calls == ["short", "long"]
    ind.close()


def test_without_gpio_pins_nothing_breaks():
    ind = Indicators()
    for state in ["attention", "busy", "success", "error", "idle"]:
        ind.set(state)
    ind.chime("start")
    ind.close()
