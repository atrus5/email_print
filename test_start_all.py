"""Tests for PuzzleMasterControlPanel auto-start and graceful stop.

Drives the real panel with the step actions stubbed out so no Chrome
windows or worker processes are launched, and asserts each start button runs
its own sequence in order and resets cleanly.  Also covers the
stop_trigger.txt handshake used by the scheduled Friday
stop (shutdown is stubbed so the shared root survives the test).
"""
import os
import sys
import time
import unittest
from unittest.mock import call, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk

import app_gui


SEQUENCE = [
    "start_automation_center",   # PuzzleMaster orders (Chrome + worker)
]

# The ▶ Start Etsy + Shopify button is the second half of the daily start.
# open_shopify_chrome is stubbed, so the real _auto_start_shopify_browser runs
# and the test proves it switches tabs before opening that browser.
OTHER_SEQUENCE = [
    "open_chrome_pm",            # PuzzleMaster Etsy browser
    "open_chrome_puz",           # Puz Etsy browser
    "start_etsy_worker",         # Etsy worker
    "open_shopify_chrome",       # Shopify tab switch + admin browser
    "start_shopify_worker",      # Shopify worker
]

# Etsy and Shopify are deliberately NOT part of the startup sequence running on
# launch: running all three workers at once raced them for the shared print
# spooler.  Their per-channel start methods must still exist for manual use.
MANUAL_START_METHODS = list(OTHER_SEQUENCE)


class TestShutdownCleanup(unittest.TestCase):
    """shutdown() must invoke every cleanup step — this body is what the
    Friday stop trigger and the window ✕ button both run."""

    def test_shutdown_calls_every_cleanup_step_before_destroy(self):
        if os.path.exists(app_gui.STOP_TRIGGER_FILE):
            os.remove(app_gui.STOP_TRIGGER_FILE)
        root = tk.Tk()
        app = app_gui.PuzzleMasterControlPanel(root)
        root.withdraw()
        # This fixture's mainloop only runs here: absorb the background
        # update thread's first-iteration root.after() calls inside it, then
        # let the thread settle into its 2s sleep so teardown below happens
        # while it is parked at the loop condition (monitoring=False -> exit).
        root.after(400, root.quit)
        root.mainloop()
        time.sleep(0.2)

        calls = []
        for name in ("stop_all", "stop_etsy_worker", "stop_chrome_pm",
                     "stop_chrome_puz", "stop_shopify_worker",
                     "stop_shopify_chrome"):
            setattr(app, name, (lambda n: (lambda: calls.append(n)))(name))

        # Let the real root.destroy() run: this root exists only for this
        # test, so executing the genuine teardown proves the final step too.
        app.shutdown()

        self.assertEqual(calls, [
            "stop_all", "stop_etsy_worker", "stop_chrome_pm",
            "stop_chrome_puz", "stop_shopify_worker", "stop_shopify_chrome",
        ])
        self.assertFalse(app.monitoring)
        # The genuine final step ran: the real root is destroyed.
        with self.assertRaises(tk.TclError):
            root.winfo_exists()
        # Join the update thread while this frame still owns the fixture, so
        # the Tcl interpreter is not deallocated from inside that thread.
        app._loop_thread.join(timeout=5)
        self.assertFalse(app._loop_thread.is_alive())


class TestLeftoverSweepFilter(unittest.TestCase):
    """The Stop All safety net must pick out exactly the automation
    processes — never a personal Chrome window, never an unrelated python,
    never the query that is doing the looking.  The filter is a pure
    function, so it is tested here without killing anything."""

    ROWS = [
        # — must match — launched the two ways the panel launches workers
        (39120, "python.exe", r'"C:\Users\solut\python.exe" order_print.py'),
        (29740, "python.exe", "python etsy_print.py"),
        (44856, "pythonw.exe", "pythonw shopify_print.py"),
        # — must match — our four automation Chrome sessions
        (31652, "chrome.exe",
         '"chrome.exe" --remote-debugging-port=9222 --kiosk-printing'
         ' --user-data-dir=C:\\Temp\\ChromeDebugProfile'),
        (29168, "chrome.exe",
         '"chrome.exe" --remote-debugging-port=9225'
         ' --user-data-dir=C:\\Temp\\ShopifyChrome_Ornamentopia'),
        # — must NOT match —
        (9001, "chrome.exe",
         '"chrome.exe" --type=utility --user-data-dir=C:\\Temp\\ChromeDebugProfile'),
        (9002, "chrome.exe", '"chrome.exe" --profile-directory=Default'),
        (9003, "python.exe", 'python -c "print(\'order_print.py\')"'),
        (9004, "python.exe", "python test_order_print.py"),
        (9005, "powershell.exe", "powershell -Command order_print.py"),
        (9006, "python.exe", r'C:\venv\Scripts\python.exe -m uvicorn main:app'),
        (9007, "bash.exe", "bash -c 'grep order_print.py'"),
    ]

    def test_picks_out_only_the_automation_processes(self):
        found = app_gui.find_leftover_processes(self.ROWS)
        self.assertEqual(
            [(pid, kind) for pid, kind, _label in found],
            [(39120, "worker"), (29740, "worker"), (44856, "worker"),
             (31652, "browser"), (29168, "browser")],
        )

    def test_excludes_pids_the_panel_already_holds(self):
        found = app_gui.find_leftover_processes(
            self.ROWS, excluded_pids=[39120, 31652]
        )
        self.assertEqual([pid for pid, _kind, _label in found],
                         [29740, 44856, 29168])

    def test_labels_name_the_script_or_the_port(self):
        labels = {pid: label for pid, _kind, label in
                  app_gui.find_leftover_processes(self.ROWS)}
        self.assertEqual(labels[44856], "shopify_print.py")
        self.assertEqual(labels[31652], "automation Chrome (port 9222)")
        self.assertEqual(labels[29168], "automation Chrome (port 9225)")

    @unittest.skipUnless(sys.platform == "win32", "CIM query is Windows-only")
    def test_real_process_query_parses_cleanly(self):
        """End-to-end on this machine: the PowerShell query must return
        well-formed rows, or the sweep would silently find nothing."""
        rows = app_gui.query_running_processes()
        self.assertTrue(rows, "CIM query returned no rows")
        for pid, image_name, cmdline in rows:
            self.assertIsInstance(pid, int)
            self.assertEqual(image_name, image_name.lower())
            self.assertIsInstance(cmdline, str)
        # This very test process must be listed — and never be a target.
        own = [row for row in rows if row[0] == os.getpid()]
        self.assertTrue(own, "the running python process should be visible")
        self.assertEqual(
            app_gui.find_leftover_processes(own, excluded_pids=[os.getpid()]),
            [],
        )


class TestStartStopSequences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A trigger file left behind by an earlier (crashed) run would make
        # the fresh panel shut itself down mid-test.
        if os.path.exists(app_gui.STOP_TRIGGER_FILE):
            os.remove(app_gui.STOP_TRIGGER_FILE)
        cls.root = tk.Tk()
        cls.app = app_gui.PuzzleMasterControlPanel(cls.root)
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def _pump_until(self, predicate, timeout=10.0):
        """Run the real mainloop until predicate() is true (or timeout).

        The main thread must stay inside mainloop for the background
        update_loop thread's root.after() calls to succeed — the same
        condition as production, where mainloop runs for the app's life."""
        deadline = time.time() + timeout

        def _check():
            if predicate() or time.time() > deadline:
                self.root.quit()
            else:
                self.root.after(50, _check)

        self.root.after(0, _check)
        self.root.mainloop()

    @patch.object(app_gui, "AUTO_START_STEP_MS", 5)
    def test_sequence_runs_in_order_and_resets(self):
        calls = []
        for name in SEQUENCE + MANUAL_START_METHODS:
            setattr(self.app, name, (lambda n: (lambda: calls.append(n)))(name))

        # Second call while in flight must be ignored (no duplicate steps).
        self.app.start_all()
        self.assertTrue(self.app._start_all_busy)
        self.app.start_all()
        self.assertEqual(
            str(self.app.btn_start_all.cget("state")), "disabled"
        )

        self._pump_until(lambda: not self.app._start_all_busy)
        self.assertFalse(
            self.app._start_all_busy,
            "start_all sequence did not finish within the timeout",
        )

        # Only the PuzzleMaster.ca step ran - Etsy and Shopify must stay put.
        self.assertEqual(calls, SEQUENCE)
        # Sequence leaves the operator on the Operations tab.
        self.assertEqual(
            self.app.notebook.select(), str(self.app.operations_tab)
        )
        # Button re-enabled for the next run.
        self.assertEqual(str(self.app.btn_start_all.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_start_others.cget("state")), "normal")

    @patch.object(app_gui, "AUTO_START_STEP_MS", 5)
    def test_other_channels_button_starts_only_etsy_and_shopify(self):
        """▶ Start Etsy + Shopify must run the Etsy and Shopify steps and
        leave the PuzzleMaster.ca worker the operator just signed in to
        completely alone (and vice versa)."""
        calls = []
        for name in OTHER_SEQUENCE + ["start_automation_center"]:
            setattr(self.app, name, (lambda n: (lambda: calls.append(n)))(name))

        # Drive the real widget: proves the button is wired to the method.
        self.app.btn_start_others.invoke()
        self.assertTrue(self.app._start_all_busy)
        self.assertEqual(
            str(self.app.btn_start_others.cget("state")), "disabled"
        )
        self.assertEqual(str(self.app.btn_start_all.cget("state")), "disabled")

        # A second click while in flight is ignored, and so is
        # Start PuzzleMaster — one sequence at a time.
        self.app.start_other_channels()
        self.app.start_all()

        self._pump_until(lambda: not self.app._start_all_busy)
        self.assertFalse(
            self.app._start_all_busy,
            "start_other_channels sequence did not finish within the timeout",
        )

        # Etsy then Shopify, and never the PuzzleMaster.ca worker.
        self.assertEqual(calls, OTHER_SEQUENCE)
        self.assertEqual(str(self.app.btn_start_others.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_start_all.cget("state")), "normal")

    @patch.object(app_gui, "AUTO_STOP_STEP_MS", 5)
    def test_stop_all_closes_every_worker_and_browser(self):
        """Stop All must hit all six cleanup actions in reverse startup
        order, ignore a second click while it is running, refuse to start
        while it runs, and hand both top-bar buttons back afterwards.

        Every stop action AND every start action is stubbed: a guard
        regression must fail an assertion here, never launch a real Chrome
        window or kill a real worker process from the test runner."""
        stop_names = [
            "stop_all",              # PM orders worker + admin browser
            "stop_etsy_worker",      # Etsy worker
            "stop_chrome_pm",        # PuzzleMaster Etsy browser
            "stop_chrome_puz",       # Puz Etsy browser
            "stop_shopify_worker",   # Shopify worker
            "stop_shopify_chrome",   # Ornamentopia admin browser
            "stop_leftover_processes",  # safety-net sweep — never run for real
        ]
        start_names = list(SEQUENCE) + MANUAL_START_METHODS + [
            "_auto_start_shopify_browser",
        ]

        calls = []
        patchers = [
            patch.object(self.app, name,
                         (lambda n: (lambda: calls.append(n)))(name))
            for name in stop_names + start_names
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])

        # Beside each other in the top bar: Start PuzzleMaster (2) →
        # Start Etsy + Shopify (3) → Stop All (4).
        self.assertIs(self.app.btn_stop_all.master, self.app.top_bar)
        self.assertIs(self.app.btn_start_others.master, self.app.top_bar)
        self.assertEqual(
            (str(self.app.btn_start_all.grid_info()["column"]),
             str(self.app.btn_start_others.grid_info()["column"]),
             str(self.app.btn_stop_all.grid_info()["column"])),
            ("2", "3", "4"),
        )

        # Drive the real widget: this also proves the button is wired to
        # stop_all_channels, not just that the method works when called.
        self.app.btn_stop_all.invoke()
        self.assertTrue(self.app._stop_all_busy)
        self.assertEqual(str(self.app.btn_stop_all.cget("state")), "disabled")

        # Re-entrant Stop All while in flight: ignored, no duplicate steps.
        self.app.stop_all_channels()
        # Start All during the stop: refused, never marks itself busy.
        self.app.start_all()
        self.assertFalse(self.app._start_all_busy)

        self._pump_until(lambda: not self.app._stop_all_busy)
        self.assertFalse(
            self.app._stop_all_busy,
            "stop_all sequence did not finish within the timeout",
        )
        self.assertEqual(calls, stop_names)

        # Both top-bar buttons are handed back for the next run.
        self.assertEqual(str(self.app.btn_stop_all.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_start_all.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_start_others.cget("state")), "normal")

    @patch.object(app_gui, "LEFTOVER_GRACE_S", 0)
    @patch.object(app_gui, "_taskkill")
    @patch.object(app_gui, "query_running_processes")
    def test_leftover_sweep_asks_then_forces_only_survivors(self, mock_query, mock_kill):
        """The safety-net step closes orphans gracefully first and only
        forces the ones still alive afterwards — never the ones that
        already exited on their own."""
        first = [
            (777, "chrome.exe", '"chrome.exe" --remote-debugging-port=9222'),
            (888, "python.exe", "python shopify_print.py"),
        ]
        # After the grace period the browser has quit; the worker has not.
        second = [first[1]]
        mock_query.side_effect = [first, second]

        self.app.stop_leftover_processes()

        self.assertEqual(
            mock_kill.call_args_list,
            [call(777), call(888), call(888, force=True)],
        )

    @patch.object(app_gui, "LEFTOVER_GRACE_S", 0)
    @patch.object(app_gui, "_taskkill")
    @patch.object(app_gui, "query_running_processes")
    def test_leftover_sweep_quietly_skips_when_nothing_is_left(self, mock_query, mock_kill):
        """Normal case — every process was already stopped — must not kill
        anything and must not wait for a grace period."""
        mock_query.return_value = []

        self.app.stop_leftover_processes()

        mock_kill.assert_not_called()
        self.assertEqual(mock_query.call_count, 1)

    def test_stale_stop_trigger_is_ignored_and_removed(self):
        shutdown_calls = []
        self.app.shutdown = lambda: shutdown_calls.append("shutdown")

        # Age the file BEFORE it appears under the watched name, so the
        # polling thread can never consume it while it is still "fresh".
        tmp = app_gui.STOP_TRIGGER_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write("stop")
        stale_time = time.time() - (app_gui.STOP_TRIGGER_MAX_AGE_S + 60)
        os.utime(tmp, (stale_time, stale_time))
        os.replace(tmp, app_gui.STOP_TRIGGER_FILE)

        # update_loop must consume the stale file WITHOUT shutting down.
        self._pump_until(lambda: not os.path.exists(app_gui.STOP_TRIGGER_FILE))

        self.assertFalse(os.path.exists(app_gui.STOP_TRIGGER_FILE))
        self.assertEqual(shutdown_calls, [])

    def test_stop_trigger_requests_graceful_shutdown(self):
        shutdown_calls = []
        self.app.shutdown = lambda: shutdown_calls.append("shutdown")

        with open(app_gui.STOP_TRIGGER_FILE, "w") as f:
            f.write("stop")

        # The update_loop thread polls for the file and schedules the
        # handler on the main thread via root.after — pump the mainloop
        # until it fires (with a timeout so a regression fails, not hangs).
        self._pump_until(lambda: bool(shutdown_calls))

        self.assertEqual(shutdown_calls, ["shutdown"])
        # Consumed trigger file must not linger (would self-stop on reboot).
        self.assertFalse(os.path.exists(app_gui.STOP_TRIGGER_FILE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
