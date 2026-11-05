"""Quiet hours - only fetch orders while the shop is open.

The panel used to be shut down at night, which meant logging back into every
storefront browser the next morning.  Instead the three workers stay up and
simply stop fetching orders outside working hours, so the Chrome sessions, the
panel and the print setup are all still there when checks resume.

Configured through .env:

    QUIET_DAYS=Mon-Fri    # days that print at all; every other day is silent
    QUIET_START=18:00     # local time the checks stop (24h "HH:MM")
    QUIET_END=07:45       # local time the checks start again

With the defaults nothing is fetched from 18:00 to 07:45 on weekdays, and not
at all on Saturday or Sunday.  Leaving QUIET_START and QUIET_END blank switches
the feature off (check around the clock); leaving QUIET_DAYS blank removes the
day restriction.  The window may wrap past midnight.  An unparseable value is
reported once at worker start and the window is ignored, so a typo can never
stop orders from printing.
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta

DEFAULT_START = "18:00"
DEFAULT_END = "07:45"
DEFAULT_DAYS = "Mon-Fri"

# Monday = 0, matching datetime.weekday().
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_clock(raw: str | None) -> time | None:
    """Turn "18:00" / "18" into a time, or None when it isn't a clock time."""
    text = (raw or "").strip()
    if not text:
        return None
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text) if minute_text else 0
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _parse_days(raw: str | None) -> frozenset[int] | None:
    """"Mon-Fri" / "Mon,Wed,Fri" / "Sat-Sun" -> weekday numbers.

    An empty value means "every day".  None means the text could not be read.
    """
    text = (raw or "").strip()
    if not text:
        return frozenset(range(7))
    days: set[int] = set()
    for chunk in text.replace(";", ",").split(","):
        part = chunk.strip().lower().replace(" ", "")
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            try:
                start = DAY_NAMES.index(first[:3])
                end = DAY_NAMES.index(last[:3])
            except ValueError:
                return None
            if start <= end:
                days.update(range(start, end + 1))
            else:                      # "Fri-Mon" wraps around the week
                days.update(range(start, 7))
                days.update(range(0, end + 1))
        else:
            try:
                days.add(DAY_NAMES.index(part[:3]))
            except ValueError:
                return None
    return frozenset(days) if days else None


def quiet_window() -> tuple[time, time] | None:
    """(stop, resume) local times, or None when quiet hours are switched off."""
    start = _parse_clock(os.getenv("QUIET_START", DEFAULT_START))
    end = _parse_clock(os.getenv("QUIET_END", DEFAULT_END))
    if start is None or end is None or start == end:
        return None
    return start, end


def active_days() -> frozenset[int]:
    """Weekdays that print at all; a broken QUIET_DAYS falls back to every day."""
    return _parse_days(os.getenv("QUIET_DAYS", DEFAULT_DAYS)) or frozenset(range(7))


def config_problem() -> str | None:
    """One-line description of a broken configuration, or None when it is fine."""
    start_raw = os.getenv("QUIET_START", DEFAULT_START)
    end_raw = os.getenv("QUIET_END", DEFAULT_END)
    days_raw = os.getenv("QUIET_DAYS", DEFAULT_DAYS)
    if not start_raw.strip() and not end_raw.strip():
        return None
    start = _parse_clock(start_raw)
    end = _parse_clock(end_raw)
    if start is None:
        return f"QUIET_START={start_raw!r} is not an HH:MM time - quiet hours are OFF."
    if end is None:
        return f"QUIET_END={end_raw!r} is not an HH:MM time - quiet hours are OFF."
    if start == end:
        return "QUIET_START equals QUIET_END - quiet hours are OFF."
    if _parse_days(days_raw) is None:
        return (
            f"QUIET_DAYS={days_raw!r} is not a day list - "
            "checking every day instead."
        )
    return None


def in_quiet_hours(now: datetime | None = None) -> bool:
    """True when the clock is outside working hours and nothing should be fetched."""
    window = quiet_window()
    if window is None:
        return False
    moment = now or datetime.now()
    if moment.weekday() not in active_days():
        return True                    # weekend / non-working day: silent all day
    current = moment.time()
    start, end = window
    if start < end:
        return start <= current < end
    # Window wraps midnight (18:00 -> 07:45).
    return current >= start or current < end


def next_resume(now: datetime | None = None) -> datetime | None:
    """When checks start again, or None when quiet hours are off."""
    window = quiet_window()
    if window is None:
        return None
    moment = now or datetime.now()
    _start, end = window
    days = active_days()
    for offset in range(8):            # a full week plus today
        day = (moment + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate = datetime.combine(day, end)
        if candidate > moment:
            return candidate
    return None


def describe_days() -> str:
    """'Mon-Fri', 'Mon-Sun' or an explicit list for the log lines."""
    days = active_days()
    if len(days) == 7:
        return "Mon-Sun"
    if days == frozenset(range(5)):
        return "Mon-Fri"
    return ",".join(DAY_NAMES[day].capitalize() for day in sorted(days))


def describe_window() -> str:
    """'18:00-07:45', or 'disabled' when there is no window."""
    window = quiet_window()
    if window is None:
        return "disabled"
    return f"{window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')}"


def describe_schedule() -> str:
    """'Mon-Fri 07:45-18:00' - the hours that actually print."""
    window = quiet_window()
    if window is None:
        return "always"
    return f"{describe_days()} {window[1].strftime('%H:%M')}-{window[0].strftime('%H:%M')}"


def describe_resume(now: datetime | None = None) -> str:
    """'07:45' today/next day, 'Mon 07:45' across a weekend."""
    moment = now or datetime.now()
    resume = next_resume(moment)
    if resume is None:
        return "the next working morning"
    if resume.date() == moment.date():
        return resume.strftime("%H:%M")
    return resume.strftime("%a %H:%M")


class QuietGate:
    """Quiet-hours state for one worker loop.

    The loop asks should_pause() once a second; each transition is logged
    exactly once, so a whole weekend does not fill system_log.txt with tens of
    thousands of identical lines.
    """

    def __init__(self, logger, what: str = "order checks"):
        self.logger = logger
        self.what = what
        self._paused = False
        self._resumed = False
        self._override_logged = False
        problem = config_problem()
        if problem:
            self.logger.warning(problem)

    def should_pause(self, now: datetime | None = None) -> bool:
        """True while the loop must skip its scheduled check."""
        if quiet_window() is None:
            return False
        if not in_quiet_hours(now):
            if self._paused:
                self._paused = False
                self._resumed = True
                self._override_logged = False
                self.logger.info(
                    f"Quiet hours over - resuming {self.what} "
                    f"(printing {describe_schedule()})."
                )
            return False
        if not self._paused:
            self._paused = True
            self.logger.info(
                f"Quiet hours: pausing {self.what} until "
                f"{describe_resume(now)} (printing {describe_schedule()}; "
                "panel and browsers stay open)."
            )
        return True

    def note_manual_override(self, manual_trigger: bool) -> None:
        """Log once when an operator asks for a check inside the window."""
        if manual_trigger and self._paused and not self._override_logged:
            self._override_logged = True
            self.logger.info(
                "Manual check requested during quiet hours - running it anyway."
            )

    def take_resumed(self) -> bool:
        """True exactly once after a pause ended, so the loop can check at once."""
        resumed, self._resumed = self._resumed, False
        return resumed
