"""Tests for quiet_hours: the working-hours schedule and the per-worker gate."""

import os
import unittest
from datetime import datetime
from unittest import mock

import quiet_hours

# 2026-11-04 is a Wednesday; the week runs Wed 4th .. Tue 10th.
WED, THU, FRI, SAT, SUN, MON = 4, 5, 6, 7, 8, 9


def _at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute)


def _no_env():
    return mock.patch.dict(os.environ, {"QUIET_START": "", "QUIET_END": "", "QUIET_DAYS": ""})


class TestConfig(unittest.TestCase):
    def test_defaults_are_working_hours(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            self.assertEqual(quiet_hours.describe_schedule(), "Mon-Fri 07:45-18:00")
            self.assertIsNone(quiet_hours.config_problem())

    def test_single_hour_value_is_accepted(self):
        env = {"QUIET_START": "18", "QUIET_END": "7:45"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(quiet_hours.describe_window(), "18:00-07:45")

    def test_blank_times_switch_the_window_off(self):
        with _no_env():
            self.assertIsNone(quiet_hours.quiet_window())
            self.assertFalse(quiet_hours.in_quiet_hours(_at(SAT, 12, 0)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(WED, 23, 0)))
            self.assertIsNone(quiet_hours.config_problem())

    def test_identical_bounds_switch_the_window_off(self):
        env = {"QUIET_START": "18:00", "QUIET_END": "18:00"}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(quiet_hours.quiet_window())
            self.assertIn("OFF", quiet_hours.config_problem())

    def test_bad_time_is_reported_and_ignored(self):
        env = {"QUIET_START": "six pm", "QUIET_END": "07:45"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("QUIET_START", quiet_hours.config_problem())
            self.assertIsNone(quiet_hours.quiet_window())
            self.assertFalse(quiet_hours.in_quiet_hours(_at(WED, 23, 0)))

    def test_out_of_range_clock_is_rejected(self):
        env = {"QUIET_START": "24:00", "QUIET_END": "07:45"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("QUIET_START", quiet_hours.config_problem())

    def test_bad_day_list_is_reported_and_falls_back_to_every_day(self):
        env = {"QUIET_DAYS": "someday"}
        with mock.patch.dict(os.environ, env):
            self.assertIn("QUIET_DAYS", quiet_hours.config_problem())
            self.assertEqual(quiet_hours.active_days(), frozenset(range(7)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(WED, 23, 0)))

    def test_blank_day_list_removes_the_day_restriction(self):
        env = {"QUIET_DAYS": ""}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(quiet_hours.in_quiet_hours(_at(SAT, 12, 0)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(SUN, 9, 0)))

    def test_explicit_day_lists_are_read(self):
        cases = {
            "Mon-Fri": {0, 1, 2, 3, 4},
            "Sat,Sun": {5, 6},
            "Mon, Wed, Fri": {0, 2, 4},
            "Fri-Mon": {4, 5, 6, 0},
            "monday-tuesday": {0, 1},
        }
        for text, expected in cases.items():
            with mock.patch.dict(os.environ, {"QUIET_DAYS": text}):
                self.assertEqual(quiet_hours.active_days(), frozenset(expected), text)


class TestInQuietHours(unittest.TestCase):
    def test_weekday_boundaries(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            self.assertFalse(quiet_hours.in_quiet_hours(_at(WED, 17, 59)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(WED, 18, 0)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(WED, 23, 59)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(THU, 0, 0)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(THU, 7, 44)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(THU, 7, 45)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(FRI, 12, 0)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(FRI, 18, 0)))

    def test_whole_weekend_is_quiet(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            for hour in (0, 7, 12, 18, 23):
                self.assertTrue(quiet_hours.in_quiet_hours(_at(SAT, hour, 30)), hour)
                self.assertTrue(quiet_hours.in_quiet_hours(_at(SUN, hour, 30)), hour)
            self.assertFalse(quiet_hours.in_quiet_hours(_at(MON, 7, 45)))

    def test_daytime_window_does_not_wrap(self):
        env = {"QUIET_START": "09:00", "QUIET_END": "17:00"}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(quiet_hours.in_quiet_hours(_at(WED, 9, 0)))
            self.assertTrue(quiet_hours.in_quiet_hours(_at(WED, 16, 59)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(WED, 17, 0)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(WED, 8, 0)))
            self.assertFalse(quiet_hours.in_quiet_hours(_at(WED, 23, 0)))


class TestNextResume(unittest.TestCase):
    def test_evening_resumes_next_morning(self):
        self.assertEqual(quiet_hours.next_resume(_at(WED, 19, 0)), _at(THU, 7, 45))

    def test_friday_evening_resumes_monday(self):
        self.assertEqual(quiet_hours.next_resume(_at(FRI, 20, 0)), _at(MON, 7, 45))

    def test_weekend_resumes_monday(self):
        self.assertEqual(quiet_hours.next_resume(_at(SAT, 10, 0)), _at(MON, 7, 45))
        self.assertEqual(quiet_hours.next_resume(_at(SUN, 22, 0)), _at(MON, 7, 45))

    def test_resume_labels(self):
        self.assertEqual(quiet_hours.describe_resume(_at(WED, 19, 0)), "Thu 07:45")
        self.assertEqual(quiet_hours.describe_resume(_at(FRI, 20, 0)), "Mon 07:45")
        self.assertEqual(quiet_hours.describe_resume(_at(SAT, 10, 0)), "Mon 07:45")


class _FakeLogger:
    def __init__(self):
        self.info_lines = []
        self.warning_lines = []

    def info(self, message):
        self.info_lines.append(message)

    def warning(self, message):
        self.warning_lines.append(message)


class TestQuietGate(unittest.TestCase):
    def test_pause_and_resume_are_logged_once_each(self):
        logger = _FakeLogger()
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            gate = quiet_hours.QuietGate(logger, "order checks")

            self.assertFalse(gate.should_pause(_at(WED, 12, 0)))
            self.assertTrue(gate.should_pause(_at(WED, 19, 0)))
            self.assertTrue(gate.should_pause(_at(WED, 20, 0)))
            self.assertTrue(gate.should_pause(_at(SAT, 20, 0)))
            self.assertFalse(gate.should_pause(_at(MON, 8, 0)))

        self.assertEqual(len(logger.info_lines), 2)
        self.assertIn("until Thu 07:45", logger.info_lines[0])
        self.assertIn("Mon-Fri 07:45-18:00", logger.info_lines[0])
        self.assertIn("resuming order checks", logger.info_lines[1])

    def test_pause_starts_friday_evening_and_holds_through_the_weekend(self):
        logger = _FakeLogger()
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            gate = quiet_hours.QuietGate(logger)
            self.assertFalse(gate.should_pause(_at(FRI, 17, 0)))
            self.assertTrue(gate.should_pause(_at(FRI, 18, 30)))
            self.assertTrue(gate.should_pause(_at(SAT, 12, 0)))
            self.assertTrue(gate.should_pause(_at(SUN, 12, 0)))
            self.assertFalse(gate.should_pause(_at(MON, 7, 45)))
            self.assertTrue(gate.take_resumed())

    def test_resume_is_signalled_exactly_once(self):
        logger = _FakeLogger()
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            gate = quiet_hours.QuietGate(logger)
            gate.should_pause(_at(WED, 19, 0))
            self.assertFalse(gate.take_resumed())
            gate.should_pause(_at(THU, 8, 0))
            self.assertTrue(gate.take_resumed())
            self.assertFalse(gate.take_resumed())

    def test_manual_override_is_logged_once(self):
        logger = _FakeLogger()
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("QUIET_START", "QUIET_END", "QUIET_DAYS"):
                os.environ.pop(key, None)
            gate = quiet_hours.QuietGate(logger)
            gate.should_pause(_at(SAT, 10, 0))
            gate.note_manual_override(True)
            gate.note_manual_override(True)
        manual = [line for line in logger.info_lines if "Manual check" in line]
        self.assertEqual(len(manual), 1)

    def test_disabled_window_never_pauses(self):
        logger = _FakeLogger()
        with _no_env():
            gate = quiet_hours.QuietGate(logger)
            for day in (WED, SAT):
                for hour in (0, 7, 18, 23):
                    self.assertFalse(gate.should_pause(_at(day, hour, 30)))
            self.assertFalse(gate.take_resumed())
        self.assertEqual(logger.info_lines, [])

    def test_broken_config_warns_once_at_construction(self):
        logger = _FakeLogger()
        env = {"QUIET_START": "nope", "QUIET_END": "07:45"}
        with mock.patch.dict(os.environ, env):
            gate = quiet_hours.QuietGate(logger)
            self.assertFalse(gate.should_pause(_at(WED, 23, 0)))
        self.assertEqual(len(logger.warning_lines), 1)
        self.assertIn("QUIET_START", logger.warning_lines[0])


if __name__ == "__main__":
    unittest.main()
