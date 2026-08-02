"""
battery_monitor.py — battery threshold voice reminders.

Windows has no software API to actually stop/limit charging on this machine
(Lenovo Vantage's "Battery Conservation Mode" talks to a private kernel
driver, not any documented WMI/registry interface — confirmed by direct
before/after diffing). So instead of controlling the charger, Parker WATCHES
the battery and REMINDS the user by voice to unplug/plug in at the
thresholds they set.

Defaults (matches what the user asked for):
  - low threshold  = 20%  → remind to plug in when below this and NOT charging
  - high threshold = None → only reminds to unplug if the user set one
    (e.g. "remind me to unplug at 80%")

Say "stop reminding me to charge" / "charge normally" to clear a threshold.
"""

import time

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    _PSUTIL_OK = False

DEFAULT_LOW = 20      # auto-remind to plug in below this %, unless disabled
_COOLDOWN_S = 600      # don't repeat the same reminder more than once per 10 min


class BatteryMonitor:
    """Stateful battery threshold watcher. Call check() periodically; it
    returns an alert string (for the model to speak) or None."""

    def __init__(self):
        self.low_threshold: int | None = DEFAULT_LOW
        self.high_threshold: int | None = None
        self._last_low_alert = 0.0
        self._last_high_alert = 0.0
        # Remember charging state to only alert on the RELEVANT transition
        # (don't nag every 10 min while plugged in above the high threshold —
        # only when it first crosses).
        self._was_above_high = False
        self._was_below_low = False

    def set_low(self, pct: int | None):
        self.low_threshold = pct

    def set_high(self, pct: int | None):
        self.high_threshold = pct

    def status_text(self) -> str:
        parts = []
        if self.low_threshold is not None:
            parts.append(f"remind to plug in below {self.low_threshold}%")
        if self.high_threshold is not None:
            parts.append(f"remind to unplug above {self.high_threshold}%")
        if not parts:
            return "No battery reminders set."
        return "Battery reminders: " + "; ".join(parts) + "."

    def _battery(self):
        if not _PSUTIL_OK:
            return None
        try:
            return psutil.sensors_battery()
        except Exception:
            return None

    def check(self) -> tuple[str, str] | None:
        """Returns (alert_key, alert_text) if a threshold is crossed and due
        to be spoken, or None. Does NOT mark the alert as sent — call
        mark_sent(alert_key) only after it's actually been spoken, so a
        reminder that couldn't be delivered (Parker was mid-conversation)
        gets retried on the next check() instead of being silently dropped."""
        batt = self._battery()
        if batt is None:
            return None  # no battery (desktop) or psutil unavailable
        pct = batt.percent
        plugged = batt.power_plugged
        now = time.monotonic()

        # Low threshold: remind to plug in when discharging below it.
        if (self.low_threshold is not None and not plugged
                and pct <= self.low_threshold):
            if not self._was_below_low and (now - self._last_low_alert) > _COOLDOWN_S:
                return ("low", (
                    f"[BATTERY_ALERT] Battery is at {pct:.0f}%, below the "
                    f"{self.low_threshold}% threshold, and not charging. "
                    "Tell the user plainly to plug in the charger."
                ))
        elif plugged or (self.low_threshold is None) or pct > self.low_threshold:
            self._was_below_low = False

        # High threshold: remind to unplug when charging above it.
        if (self.high_threshold is not None and plugged
                and pct >= self.high_threshold):
            if not self._was_above_high and (now - self._last_high_alert) > _COOLDOWN_S:
                return ("high", (
                    f"[BATTERY_ALERT] Battery is at {pct:.0f}%, at or above the "
                    f"{self.high_threshold}% threshold, and still charging. "
                    "Tell the user plainly to unplug the charger to protect "
                    "battery health."
                ))
        elif not plugged or (self.high_threshold is None) or pct < self.high_threshold:
            self._was_above_high = False

        return None

    def mark_sent(self, alert_key: str) -> None:
        """Record that the alert was actually spoken, starting its cooldown
        and preventing it from re-firing until the state changes again."""
        now = time.monotonic()
        if alert_key == "low":
            self._was_below_low = True
            self._last_low_alert = now
        elif alert_key == "high":
            self._was_above_high = True
            self._last_high_alert = now


def battery_status(parameters: dict = None, player=None, session_memory=None) -> str:
    """Report current battery % and charging state."""
    if not _PSUTIL_OK:
        return "Sir, I can't read battery status on this machine."
    try:
        batt = psutil.sensors_battery()
    except Exception:
        batt = None
    if batt is None:
        return "Sir, this machine has no battery (or it can't be read)."
    state = "charging" if batt.power_plugged else "on battery"
    return f"Battery is at {batt.percent:.0f}%, currently {state}."
