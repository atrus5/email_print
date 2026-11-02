#!/usr/bin/env python3
"""
PuzzleMaster order-automation control panel.

Four tabs:
  • Welcome    – daily startup instructions
  • Operations – printing buttons + recent-order grids (what has printed)
  • Shopify    – Shopify printing controls and recent orders
  • Analytics  – per-channel sales & order totals

A small heartbeat pill in the top bar shows today's aggregate order count so the operator
can see at a glance if the system is alive regardless of which tab is selected. The live
terminal console stays anchored at the bottom of the window for worker output.
"""

import sys
import os
import json
import hashlib
import re
import subprocess
import threading
import time
import logging
import tkinter as tk
import webbrowser
from tkinter import scrolledtext, ttk
from datetime import datetime, timedelta

# Anchor the working directory to the app's own folder.  Task Scheduler
# launches with cwd = C:\Windows\System32, and this app (and the worker
# processes it spawns as `python order_print.py`) resolves app_gui.log,
# status/trigger JSON files, logo.ico and the workers relative to cwd.
# Without this the frozen exe cannot even create its log file when the
# Monday scheduled task starts it.
APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
)
os.chdir(APP_DIR)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app_gui.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

STATUS_FILE = 'status.json'
TRIGGER_FILE = 'trigger.txt'
ETSY_TRIGGER_FILE = 'etsy_trigger.txt'
SHOPIFY_TRIGGER_FILE = 'shopify_trigger.txt'
STOP_TRIGGER_FILE = 'stop_trigger.txt'
STOP_TRIGGER_MAX_AGE_S = 300   # ignore a stale file left by a crashed bat
SHOPIFY_STATUS_FILE = 'shopify_status_ornamentopia.json'
MAX_LOG_LINES = 500

# ── Auto-start sequence ─────────────────────────────────────────────────────
# On launch the panel starts the PuzzleMaster.ca channel only: admin Chrome
# (port 9222) + order_print.py worker.  The ▶ Start Etsy + Shopify button then
# starts the other two channels once that login has gone through, because
# running all three workers at once raced them for the shared print spooler and
# orders came out interleaved or with pages missing.
# Launch with --no-autostart to open the panel idle instead.
AUTO_START_INITIAL_MS = 2500   # let the window paint before launching
AUTO_START_STEP_MS = 3000      # pause between actions (Chrome cold-start)
# Stop All runs the same one-action-per-tick pattern, but needs only a short
# beat between steps: each worker/browser stop already waits on its own exit.
AUTO_STOP_STEP_MS = 1000

# ── Shopify custom-printer routing (mirrors ShopifyAppConfig in shopify_print.py) ──
# Read once at app-startup so the Shopify tab can label which printer is targeted.
# If SHOPIFY_TARGET_PRINTER is empty OR SumatraPDF is missing, prints still work
# (they just go to the OS-default printer shared with PuzzleMaster.ca orders).
SHOPIFY_TARGET_PRINTER = os.getenv(
    'SHOPIFY_TARGET_PRINTER', 'Leon - Brother HL-L5200DW series Printer'
)
SHOPIFY_SUMATRA_PATH = os.getenv(
    'SHOPIFY_SUMATRA_PATH', r'C:\Program Files\SumatraPDF\SumatraPDF.exe'
)
SHOPIFY_ROUTING_ACTIVE = bool(os.path.isfile(SHOPIFY_SUMATRA_PATH))

# Chrome persists the silent/kiosk print destination inside each profile's
# Preferences (print_preview_sticky_settings.appState.recentDestinations). The
# Shopify Chrome uses a DEDICATED profile (separate from Etsy's), so we can pin
# that profile's saved printer to SHOPIFY_TARGET_PRINTER without affecting the
# Etsy/PuzzleMaster instances -- and without touching the OS default printer.
# Without this, `window.print()` under --kiosk-printing silently goes to whatever
# printer was last saved in the Shopify profile (e.g. the Etsy/Allan unit).
SHOPIFY_PROFILE_DIR = r'C:\Temp\ShopifyChrome_Ornamentopia'


def set_profile_print_destination(profile_dir, printer_name):
    """Rewrite a Chrome profile's saved print destination so silent/kiosk
    prints from that profile go to `printer_name`. Best-effort; never raises.
    Must be called while Chrome is NOT running for that profile, otherwise
    Chrome overwrites the file from its in-memory copy on shutdown."""
    try:
        prefs_path = os.path.join(profile_dir, 'Default', 'Preferences')
        if not os.path.exists(prefs_path):
            return
        with open(prefs_path, 'r', encoding='utf-8-sig') as f:
            prefs = json.load(f)
        dest = {
            'id': printer_name,
            'displayName': printer_name,
            'origin': 'local',
            'extensionId': '',
            'icon': 'print-preview:print',
        }
        app_state = json.dumps({'version': 2, 'recentDestinations': [dest]})
        # Modern Chrome reads it under 'printing'; older under the root key.
        prefs.setdefault('printing', {}).setdefault('print_preview_sticky_settings', {})['appState'] = app_state
        prefs.setdefault('print_preview_sticky_settings', {})['appState'] = app_state
        with open(prefs_path, 'w', encoding='utf-8') as f:
            json.dump(prefs, f)
        print(f"[Shopify] Pinned Chrome profile print destination -> {printer_name}")
    except Exception as e:
        print(f"[Shopify] Warning: could not pin Shopify profile printer: {e}")



# Order channels. (display_name, status_file, currency)
# Currencies are kept per-channel: do not sum across currencies.
CHANNELS = [
    ("PuzzleMaster.ca (CAD)",  STATUS_FILE,                          "CAD"),
    ("Etsy – PuzzleMaster (USD)", "etsy_status_puzzlemaster.json",  "USD"),
    ("Etsy – Puz (USD)",          "etsy_status_puz.json",           "USD"),
    ("Ornamentopia (CAD)",          "shopify_status_ornamentopia.json", "CAD"),
]

# Backwards-compat dict used by the Etsy code paths (workers are still keyed by shop)
ETSY_STATUS_FILES = {
    "PuzzleMaster": "etsy_status_puzzlemaster.json",
    "Puz":          "etsy_status_puz.json",
}

# Analytics period columns
PERIODS = ["Today", "Last 7 Days", "This Month", "This Year", "All Time"]

def graceful_chrome_shutdown(process, label):
    """Ask Chrome to quit cleanly so it saves session state and won't
    show the 'restore pages' bubble on next launch.
    Falls back to terminate() if it doesn't exit within 5 seconds."""
    if process is None:
        return
    try:
        if sys.platform == "win32":
            import signal as _signal
            process.send_signal(_signal.CTRL_CLOSE_EVENT)
        else:
            process.terminate()
        process.wait(timeout=5)
        logger.debug(f"{label} closed gracefully.")
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


# ── Leftover-process sweep (Stop All's safety net) ─────────────────────────
# A panel that crashed — or a SECOND panel instance that never spawned the
# workers itself — leaves the three worker scripts and the automation Chrome
# sessions running with nobody holding their handle, so the panel's own stop
# methods would no-op and 'stop everything' would stop nothing.  This sweep
# catches those.  Patterns mirror Stop_OrderPrintAuto.bat, but a process is
# only eligible when BOTH its image name and its command line match:
#   • python/pythonw whose command line STARTS one of the three worker
#     scripts (as an argument or a path).  A test module like
#     test_order_print.py, or a `python -c "('order_print.py')"` snippet,
#     does not qualify — the character before the name would be _ ( ' or (.
#   • chrome.exe carrying --remote-debugging-port=9222-9225.  Only the
#     browser's main process has that flag (Chrome children get --type=…
#     without it), so closing it takes its whole session with it, and
#     personal Chrome windows — which have no such port — are untouched.
WORKER_IMAGE_NAMES = frozenset({"python.exe", "pythonw.exe", "python3.exe"})
WORKER_SCRIPT_RE = re.compile(r"(?<![\w.'=:(-])(?:order_print|etsy_print|shopify_print)\.py\b")
BROWSER_IMAGE_NAME = "chrome.exe"
BROWSER_PORT_RE = re.compile(r"--remote-debugging-port=922[2-5](?:\s|$)")
LEFTOVER_GRACE_S = 4   # time given to quit gracefully before taskkill /F


def query_running_processes():
    """Return [(pid, image_name_lower, command_line)] for every process on
    the machine, or [] when the query is unavailable.  Filtering happens in
    Python (not PowerShell) so the query's own command line can never match
    the patterns it is asked to find."""
    ps = ("Get-CimInstance Win32_Process | ForEach-Object { "
          "$_.ProcessId.ToString() + [char]9 + $_.Name + [char]9 + "
          "$_.CommandLine }")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        logger.warning(f"Leftover sweep could not list processes: {e}")
        return []
    rows = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].strip().isdigit():
            rows.append((int(parts[0]), parts[1].strip().lower(), parts[2] or ""))
    return rows


def find_leftover_processes(rows, excluded_pids=()):
    """Pure filter: pick the automation workers/browsers out of `rows`.

    `excluded_pids` are processes this panel already holds a handle for —
    those are stopped through the graceful path instead.  Returns
    [(pid, kind, label)] with kind in {'worker', 'browser'}.
    """
    excluded = set(excluded_pids)
    excluded.add(os.getpid())
    found = []
    for pid, image_name, cmdline in rows:
        if pid in excluded:
            continue
        if image_name in WORKER_IMAGE_NAMES and WORKER_SCRIPT_RE.search(cmdline):
            script = WORKER_SCRIPT_RE.search(cmdline).group(0)
            found.append((pid, "worker", script))
        elif image_name == BROWSER_IMAGE_NAME and BROWSER_PORT_RE.search(cmdline):
            port = BROWSER_PORT_RE.search(cmdline).group(0).split("=")[-1].strip()
            found.append((pid, "browser", f"automation Chrome (port {port})"))
    return found


def _taskkill(pid, force=False):
    """taskkill /PID <pid> posts WM_CLOSE (graceful — Chrome saves its
    session); with /F it forces the process down.  Non-zero exit codes are
    normal and harmless: they just mean the process already exited."""
    args = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
    try:
        subprocess.run(
            args, capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        logger.debug(f"taskkill {pid}{' /F' if force else ''} could not run: {e}")


class PuzzleMasterControlPanel:
    def __init__(self, root):
        self.root = root
        self.root.title("Automatic Order Printing")
        try:
            self.root.iconbitmap("logo.ico")
        except Exception:
            logger.debug("Could not load logo.ico, using default icon")

        self.root.geometry("1100x850")
        self.root.minsize(900, 700)
        self.root.state('zoomed')

        self.is_dark_mode = False
        self.automation_process = None
        self.chrome_process = None
        self._start_all_busy = False
        self._stop_all_busy = False
        self._shutting_down = False

        # Etsy processes
        self.etsy_process = None
        self.etsy_chrome_pm = None   # Chrome for PuzzleMaster Etsy (port 9223)
        self.etsy_chrome_puz = None  # Chrome for Puz Etsy (port 9224)
        self.shopify_process = None  # shopify_print.py worker
        self.shopify_chrome = None   # Chrome for Ornamentopia Shopify (port 9225)

        self.monitoring = True
        self.last_status_hash = ""
        self.last_etsy_hashes = {"PuzzleMaster": "", "Puz": ""}
        self.last_shopify_status_hash = ""

        # Catppuccin Latte (light) + Mocha (dark)
        self.themes = {
            "light": {
                "bg": "#eff1f5", "surface": "#ffffff", "text": "#4c4f69",
                "text_dim": "#6c6f85",
                "primary": "#1e66f5", "primary_hover": "#209fb5", "accent": "#df8e1d",
                "border": "#ccd0da", "log_bg": "#1e1e2e", "log_text": "#a6e3a1",
                "stat_card": "#e6e9ef", "stat_lbl": "#6c6f85",
                "tab_bg": "#e6e9ef",
                "etsy_card": "#fde8d8", "etsy_accent": "#fe640b",
                "disabled_bg": "#ccd0da", "disabled_fg": "#9ca0b0",
            },
            "dark": {
                "bg": "#1e1e2e", "surface": "#313244", "text": "#cdd6f4",
                "text_dim": "#a6adc8",
                "primary": "#89b4fa", "primary_hover": "#74c7ec", "accent": "#f9e2af",
                "border": "#45475a", "log_bg": "#11111b", "log_text": "#a6e3a1",
                "stat_card": "#181825", "stat_lbl": "#a6adc8",
                "tab_bg": "#313244",
                "etsy_card": "#2a1f1a", "etsy_accent": "#fab387",
                "disabled_bg": "#45475a", "disabled_fg": "#6c7086",
            }
        }

        if self.detect_system_dark_mode():
            self.is_dark_mode = True
            self.current_theme = self.themes["dark"]
        else:
            self.current_theme = self.themes["light"]

        self.root.grid_columnconfigure(0, weight=1)
        # Row 0: top bar (fixed)
        # Row 1: notebook (expandable)
        # Row 2: log console (fixed)
        self.root.grid_rowconfigure(1, weight=1)

        self.build_ui()
        self.apply_theme()

        self._loop_thread = threading.Thread(target=self.update_loop, daemon=True)
        self._loop_thread.start()

    def detect_system_dark_mode(self):
        if sys.platform == "win32":
            try:
                import winreg
                registry = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
                key = winreg.OpenKey(registry, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return value == 0
            except Exception as e:
                logger.debug(f"Could not detect Windows dark mode setting: {e}")
        return False

    # ── TOP-LEVEL UI BUILDER ──────────────────────────────────────────────────
    def build_ui(self):
        # ── TOP BAR ────────────────────────────────────────────────────────
        self.top_bar = tk.Frame(self.root, height=70)
        self.top_bar.grid(row=0, column=0, sticky="ew", padx=25, pady=(15, 8))
        self.top_bar.grid_columnconfigure(0, weight=1)
        self.top_bar.grid_columnconfigure(1, weight=0)
        self.top_bar.grid_columnconfigure(2, weight=0)
        self.top_bar.grid_columnconfigure(3, weight=0)
        self.top_bar.grid_columnconfigure(4, weight=0)
        self.top_bar.grid_columnconfigure(5, weight=0)

        self.lbl_title = tk.Label(self.top_bar, text="Order Printing Control Center",
                                  font=("Arial", 20, "bold"))
        self.lbl_title.grid(row=0, column=0, sticky="w")

        # Heartbeat pill – always visible, shows today's combined order count
        self.heartbeat_frame = tk.Frame(self.top_bar)
        self.heartbeat_frame.grid(row=0, column=1, sticky="e", padx=(0, 12))
        self.heartbeat_icon = tk.Label(self.heartbeat_frame, text="📅",
                                       font=("Arial", 12, "bold"))
        self.heartbeat_icon.pack(side="left", padx=(10, 4), pady=6)
        self.heartbeat_label = tk.Label(
            self.heartbeat_frame,
            text="Today: — orders",
            font=("Arial", 11, "bold"),
        )
        self.heartbeat_label.pack(side="left", padx=(0, 10), pady=6)

        self.btn_start_all = tk.Button(
            self.top_bar, text="🎛 Start PuzzleMaster", font=("Arial", 11, "bold"),
            command=self.start_all, relief="flat", padx=12, pady=6)
        self.btn_start_all.grid(row=0, column=2, sticky="e", padx=(0, 12))

        # Second half of the daily start, kept separate on purpose: the
        # operator starts PuzzleMaster, signs in, and only then starts Etsy and
        # Shopify — printing all three at once floods the shared spooler.
        self.btn_start_others = tk.Button(
            self.top_bar, text="▶ Start Etsy + Shopify",
            font=("Arial", 11, "bold"),
            command=self.start_other_channels, relief="flat", padx=12, pady=6)
        self.btn_start_others.grid(row=0, column=3, sticky="e", padx=(0, 12))

        # Mirror of the start buttons: one click stops every worker and closes
        # every browser window without closing the panel itself.
        self.btn_stop_all = tk.Button(
            self.top_bar, text="🛑 Stop All", font=("Arial", 11, "bold"),
            command=self.stop_all_channels, relief="flat", padx=12, pady=6)
        self.btn_stop_all.grid(row=0, column=4, sticky="e", padx=(0, 12))

        self.btn_theme = tk.Button(self.top_bar, text="🌓 Toggle Light/Dark",
                                   font=("Arial", 11),
                                   command=self.toggle_theme, relief="flat",
                                   padx=12, pady=6)
        self.btn_theme.grid(row=0, column=5, sticky="e")

        # ── NOTEBOOK (Welcome / Operations / Shopify / Analytics) ───────────
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=25, pady=(0, 6))

        self.welcome_tab = tk.Frame(self.notebook)
        self.operations_tab = tk.Frame(self.notebook)
        self.shopify_tab = tk.Frame(self.notebook)
        self.analytics_tab = tk.Frame(self.notebook)
        self.notebook.add(self.welcome_tab,    text="  Welcome  ")
        self.notebook.add(self.operations_tab, text="  🖨   Operations  ")
        self.notebook.add(self.shopify_tab,    text="  🛒   Shopify Printing  ")
        self.notebook.add(self.analytics_tab,  text="  📊   Analytics  ")

        self._build_welcome_tab(self.welcome_tab)
        self._build_operations_tab(self.operations_tab)
        self._build_shopify_tab(self.shopify_tab)
        self._build_analytics_tab(self.analytics_tab)

        # ── LIVE LOG CONSOLE (always visible at the bottom) ───────────────
        self.log_frame = tk.LabelFrame(
            self.root,
            text=" Live Terminal Engine Output Console ",
            font=("Arial", 13, "bold"),
            padx=15, pady=10,
        )
        self.log_frame.grid(row=2, column=0, sticky="ew", padx=25, pady=(4, 15))
        self.log_frame.grid_columnconfigure(0, weight=1)

        self.log_area = scrolledtext.ScrolledText(self.log_frame, height=9,
                                                  font=("Consolas", 14))
        self.log_area.grid(row=0, column=0, sticky="ew")
        self.log_area.insert(tk.END,
                             "[System Panel initialized. "
                             "Ready to launch environment workers...]\n")
        self.log_area.configure(state="disabled")

        self.log_area.tag_configure("info",  foreground="#34d399")
        self.log_area.tag_configure("error", foreground="#f87171")
        self.log_area.tag_configure("warn",  foreground="#fbbf24")

        self.btn_clear_log = tk.Button(self.log_frame, text="🗑 Clear Log",
                                       font=("Arial", 10),
                                       command=self.clear_log, relief="flat",
                                       padx=8, pady=4)
        self.btn_clear_log.grid(row=1, column=0, sticky="e", pady=(5, 0))

    # ── WELCOME TAB (operator startup instructions) ───────────────────────────
    def _build_welcome_tab(self, parent):
        """Build the first-run guide for the normal daily startup sequence."""
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        self.welcome_canvas = tk.Canvas(parent, highlightthickness=0, bd=0)
        self.welcome_canvas.grid(row=0, column=0, sticky="nsew")
        self.welcome_scrollbar = tk.Scrollbar(
            parent, orient="vertical", command=self.welcome_canvas.yview,
        )
        self.welcome_scrollbar.grid(row=0, column=1, sticky="ns")
        self.welcome_canvas.configure(yscrollcommand=self.welcome_scrollbar.set)

        self.welcome_content = tk.Frame(self.welcome_canvas)
        self.welcome_window = self.welcome_canvas.create_window(
            (0, 0), window=self.welcome_content, anchor="nw",
        )
        self.welcome_content.bind(
            "<Configure>",
            lambda _event: self.welcome_canvas.configure(
                scrollregion=self.welcome_canvas.bbox("all")
            ),
        )
        self.welcome_canvas.bind(
            "<Configure>",
            lambda event: self.welcome_canvas.itemconfigure(
                self.welcome_window, width=event.width,
            ),
        )

        self.welcome_title = tk.Label(
            self.welcome_content,
            text="Welcome to Automatic Order Printing",
            font=("Arial", 22, "bold"), anchor="w",
        )
        self.welcome_title.pack(fill="x", padx=28, pady=(24, 4))

        self.welcome_intro = tk.Label(
            self.welcome_content,
            text="Use the sequence below when starting the program. Complete each browser step, then press Alt+Tab to return to this control panel.",
            font=("Arial", 12), anchor="w", justify="left", wraplength=900,
        )
        self.welcome_intro.pack(fill="x", padx=28, pady=(0, 20))

        self.welcome_section_frames = []
        self.welcome_section_titles = []
        self.welcome_section_bodies = []

        def add_section(title, body):
            section = tk.LabelFrame(
                self.welcome_content, text=f"  {title}  ",
                font=("Arial", 14, "bold"), padx=18, pady=12,
            )
            section.pack(fill="x", padx=28, pady=(0, 14))
            body_label = tk.Label(
                section, text=body, font=("Arial", 12),
                anchor="w", justify="left", wraplength=880,
            )
            body_label.pack(fill="x")
            self.welcome_section_frames.append(section)
            self.welcome_section_titles.append(section)
            self.welcome_section_bodies.append(body_label)

        add_section(
            "1. Start PuzzleMaster.ca and Etsy",
            "1. Open the Operations tab.\n"
            "2. Under PuzzleMaster.ca Orders, click Start.\n"
            "3. Press Alt+Tab to return to this program.\n"
            "4. Under Etsy Orders, click Open PuzzleMaster Etsy Browser. Log in if needed.\n"
            "5. Press Alt+Tab to return to this program.\n"
            "6. Click Open Puz Etsy Browser. Log in if needed.\n"
            "7. Press Alt+Tab to return to this program.\n"
            "8. Click Start Etsy Worker.\n"
            "9. Click Check For Orders to check PuzzleMaster.ca.\n"
            "10. Click Check Etsy Orders to check both Etsy stores."
        )
        add_section(
            "2. Start Shopify printing",
            "1. Open the Shopify Printing tab.\n"
            "2. Click Open Shopify Admin Browser and log in if needed.\n"
            "3. Press Alt+Tab to return to this program.\n"
            "4. Click Start Shopify Worker.\n"
            "5. Click Check Shopify Orders.\n"
            "6. Shopify prints the order page first, then the packing slip."
        )
        add_section(
            "3. View order totals",
            "Open the Analytics tab to see order counts and totals for each channel. "
            "Available views include Today, Last 7 Days, This Month, This Year, and All Time. "
            "Currencies remain separated by channel."
        )
        add_section(
            "Daily reminders",
            "Keep the required browser windows open and logged in before starting workers. "
            "Use Alt+Tab whenever a browser opens so you can continue with the control panel. "
            "Do not click a Start button more than once; the status line shows when a worker is running."
        )

    # ── OPERATIONS TAB (printing buttons + recent orders) ─────────────────────
    def _build_operations_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)   # PM.ca section expands
        parent.grid_rowconfigure(3, weight=1)   # Etsy section expands

        # Section: PuzzleMaster.ca
        self.mid_frame = tk.Frame(parent)
        self.mid_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self.mid_frame.grid_columnconfigure(0, weight=4)
        self.mid_frame.grid_columnconfigure(1, weight=5)
        self.mid_frame.grid_rowconfigure(0, weight=1)

        self.ctrl_frame = tk.Frame(self.mid_frame)
        self.ctrl_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 15))

        self.lbl_pm_section = tk.Label(self.ctrl_frame,
                                       text="PuzzleMaster.ca Orders",
                                       font=("Arial", 13, "bold"))
        self.lbl_pm_section.pack(fill="x", pady=(0, 8))

        self.btn_start_print = tk.Button(self.ctrl_frame, text="🚀 Start",
                                         font=("Arial", 14, "bold"),
                                         height=2,
                                         command=self.start_automation_center,
                                         relief="groove")
        self.btn_start_print.pack(fill="x", pady=8)

        self.btn_manual_trigger = tk.Button(self.ctrl_frame,
                                             text="🔄 Check For Orders",
                                             font=("Arial", 13),
                                             height=2,
                                             command=self.trigger_now,
                                             state="disabled")
        self.btn_manual_trigger.pack(fill="x", pady=8)

        self.btn_stop = tk.Button(self.ctrl_frame, text="🛑 Stop",
                                  font=("Arial", 14, "bold"),
                                  bg="#0007cc", fg="white",
                                  activebackground="#0007cc",
                                  activeforeground="white",
                                  height=2, command=self.stop_all,
                                  relief="groove", state="disabled")
        self.btn_stop.pack(fill="x", pady=25)

        self.lbl_pm_worker_status = tk.Label(self.ctrl_frame,
                                             text="⚫  Worker: Stopped",
                                             font=("Arial", 11, "bold"),
                                             anchor="w")
        self.lbl_pm_worker_status.pack(fill="x", pady=(0, 2))

        self.lbl_pm_chrome_status = tk.Label(self.ctrl_frame,
                                             text="⚫  Browser: Closed",
                                             font=("Arial", 10),
                                             anchor="w")
        self.lbl_pm_chrome_status.pack(fill="x", pady=(0, 2))

        self.orders_frame = tk.LabelFrame(
            self.mid_frame,
            text=" Recent PuzzleMaster Order Logs ",
            font=("Arial", 13, "bold"), padx=15, pady=15,
        )
        self.orders_frame.grid(row=0, column=1, sticky="nsew", padx=(15, 0))
        self.orders_frame.grid_columnconfigure(0, weight=1)
        self.orders_frame.grid_rowconfigure(0, weight=1)

        columns = ("order_id", "date", "subtotal", "status")
        self.order_grid = ttk.Treeview(self.orders_frame, columns=columns,
                                       show="headings", selectmode="browse")

        self.order_grid.heading("order_id", text="Order ID")
        self.order_grid.heading("date",     text="Placement Date")
        self.order_grid.heading("subtotal", text="Subtotal")
        self.order_grid.heading("status",   text="Processing Status")
        self.order_grid.column("order_id", width=110, anchor="center")
        self.order_grid.column("date",     width=140, anchor="center")
        self.order_grid.column("subtotal", width=110, anchor="e")
        self.order_grid.column("status",   width=210, anchor="w")
        self.order_grid.grid(row=0, column=0, sticky="nsew")
        self._attach_context_menu(self.order_grid, "pm_ca")

        # Divider between PM.ca and Etsy sections
        self.etsy_divider = tk.Frame(parent, height=3)
        self.etsy_divider.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 5))

        # Section: Etsy (both shops)
        self.etsy_frame = tk.Frame(parent)
        self.etsy_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.etsy_frame.grid_columnconfigure(0, weight=4)
        self.etsy_frame.grid_columnconfigure(1, weight=5)
        self.etsy_frame.grid_rowconfigure(0, weight=1)

        self.etsy_ctrl_frame = tk.Frame(self.etsy_frame)
        self.etsy_ctrl_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 15))

        self.lbl_etsy_section = tk.Label(self.etsy_ctrl_frame, text="Etsy Orders",
                                         font=("Arial", 13, "bold"))
        self.lbl_etsy_section.pack(fill="x", pady=(0, 8))

        self.lbl_etsy_pm_browser = tk.Label(
            self.etsy_ctrl_frame,
            text="PuzzleMaster Etsy  (info@puzzlemaster.ca)",
            font=("Arial", 10, "bold"), anchor="w",
        )
        self.lbl_etsy_pm_browser.pack(fill="x", pady=(0, 2))

        self.btn_open_chrome_pm = tk.Button(
            self.etsy_ctrl_frame, text="🌐 Open PuzzleMaster Etsy Browser",
            font=("Arial", 11), height=1,
            command=self.open_chrome_pm, relief="groove",
        )
        self.btn_open_chrome_pm.pack(fill="x", pady=(0, 2))

        self.btn_stop_chrome_pm = tk.Button(
            self.etsy_ctrl_frame, text="✖ Close PuzzleMaster Browser",
            font=("Arial", 10), height=1,
            command=self.stop_chrome_pm, relief="groove", state="disabled",
        )
        self.btn_stop_chrome_pm.pack(fill="x", pady=(0, 10))

        self.lbl_etsy_puz_browser = tk.Label(
            self.etsy_ctrl_frame,
            text="Puz Etsy  (info@puz.ca)",
            font=("Arial", 10, "bold"), anchor="w",
        )
        self.lbl_etsy_puz_browser.pack(fill="x", pady=(0, 2))

        self.btn_open_chrome_puz = tk.Button(
            self.etsy_ctrl_frame, text="🌐 Open Puz Etsy Browser",
            font=("Arial", 11), height=1,
            command=self.open_chrome_puz, relief="groove",
        )
        self.btn_open_chrome_puz.pack(fill="x", pady=(0, 2))

        self.btn_stop_chrome_puz = tk.Button(
            self.etsy_ctrl_frame, text="✖ Close Puz Browser",
            font=("Arial", 10), height=1,
            command=self.stop_chrome_puz, relief="groove", state="disabled",
        )
        self.btn_stop_chrome_puz.pack(fill="x", pady=(0, 10))

        self.btn_etsy_start = tk.Button(
            self.etsy_ctrl_frame, text="🚀 Start Etsy Worker",
            font=("Arial", 14, "bold"),
            height=2, command=self.start_etsy_worker, relief="groove",
        )
        self.btn_etsy_start.pack(fill="x", pady=(4, 4))

        self.btn_etsy_trigger = tk.Button(
            self.etsy_ctrl_frame, text="🔄 Check Etsy Orders",
            font=("Arial", 13), height=2,
            command=self.etsy_trigger_now, state="disabled",
        )
        self.btn_etsy_trigger.pack(fill="x", pady=4)

        self.btn_etsy_stop = tk.Button(
            self.etsy_ctrl_frame, text="🛑 Stop Etsy Worker",
            font=("Arial", 14, "bold"),
            bg="#0007cc", fg="white",
            activebackground="#0007cc", activeforeground="white",
            height=2, command=self.stop_etsy_worker,
            relief="groove", state="disabled",
        )
        self.btn_etsy_stop.pack(fill="x", pady=(4, 0))

        self.lbl_etsy_worker_status = tk.Label(self.etsy_ctrl_frame,
                                               text="⚫  Worker: Stopped",
                                               font=("Arial", 11, "bold"),
                                               anchor="w")
        self.lbl_etsy_worker_status.pack(fill="x", pady=(6, 2))

        self.lbl_etsy_pm_chrome_status = tk.Label(self.etsy_ctrl_frame,
                                                  text="⚫  PM Browser: Closed",
                                                  font=("Arial", 10),
                                                  anchor="w")
        self.lbl_etsy_pm_chrome_status.pack(fill="x", pady=(0, 2))

        self.lbl_etsy_puz_chrome_status = tk.Label(self.etsy_ctrl_frame,
                                                   text="⚫  Puz Browser: Closed",
                                                   font=("Arial", 10),
                                                   anchor="w")
        self.lbl_etsy_puz_chrome_status.pack(fill="x", pady=(0, 2))

        # Etsy order log — two columns side by side (one per shop)
        self.etsy_logs_frame = tk.Frame(self.etsy_frame)
        self.etsy_logs_frame.grid(row=0, column=1, sticky="nsew", padx=(15, 0))
        self.etsy_logs_frame.grid_columnconfigure(0, weight=1)
        self.etsy_logs_frame.grid_columnconfigure(1, weight=1)
        self.etsy_logs_frame.grid_rowconfigure(0, weight=1)

        self.etsy_pm_frame = tk.LabelFrame(
            self.etsy_logs_frame, text=" Etsy – PuzzleMaster ",
            font=("Arial", 12, "bold"), padx=10, pady=10,
        )
        self.etsy_pm_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.etsy_pm_frame.grid_columnconfigure(0, weight=1)
        self.etsy_pm_frame.grid_rowconfigure(0, weight=1)
        self.etsy_pm_grid = self._make_etsy_grid(self.etsy_pm_frame)
        self._attach_context_menu(self.etsy_pm_grid, "etsy_pm")

        self.etsy_puz_frame = tk.LabelFrame(
            self.etsy_logs_frame, text=" Etsy – Puz ",
            font=("Arial", 12, "bold"), padx=10, pady=10,
        )
        self.etsy_puz_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.etsy_puz_frame.grid_columnconfigure(0, weight=1)
        self.etsy_puz_frame.grid_rowconfigure(0, weight=1)
        self.etsy_puz_grid = self._make_etsy_grid(self.etsy_puz_frame)
        self._attach_context_menu(self.etsy_puz_grid, "etsy_puz")

    def _make_etsy_grid(self, parent: tk.Widget) -> ttk.Treeview:
        """Create a small order treeview for one Etsy shop."""
        cols = ("order_id", "date", "total", "status")
        grid = ttk.Treeview(parent, columns=cols, show="headings",
                            selectmode="browse", height=6)
        grid.heading("order_id", text="Order #")
        grid.heading("date",     text="Date")
        grid.heading("total",    text="Total")
        grid.heading("status",   text="Status")
        grid.column("order_id", width=100, anchor="center")
        grid.column("date",     width=100, anchor="center")
        grid.column("total",    width=80,  anchor="e")
        grid.column("status",   width=130, anchor="w")
        grid.grid(row=0, column=0, sticky="nsew")
        return grid

    # ── RIGHT-CLICK CONTEXT MENU ON ORDER GRIDS ───────────────────────────────
    # Right-clicking a row in any of the three order logs pops a small menu with:
    #   • Open order in browser  (deep-links into the channel's admin view)
    #   • Copy order ID          (handy for paste into the worker trigger box)
    # Right-clicking empty grid space does nothing — no menu pops up.
    def _admin_url_for(self, channel: str, order_id: str):
        """Return the deep-link admin URL for one (channel, order) pair, or None."""
        if not order_id:
            return None
        if channel == "pm_ca":
            return f"https://www.puzzlemaster.ca/admin/order.php?id={order_id}"
        if channel in ("etsy_pm", "etsy_puz"):
            # Etsy shop-manager orders list; the operator clicks into the order next.
            return f"https://www.etsy.com/your/orders/{order_id}"
        if channel == "shopify_ornamentopia":
            return f"https://e55ee8-2.myshopify.com/admin/orders/{order_id}"
        return None

    def _channel_label(self, channel: str) -> str:
        return {
            "pm_ca":    "PuzzleMaster.ca",
            "etsy_pm":  "Etsy\u2013PuzzleMaster",
            "etsy_puz": "Etsy\u2013Puz",
            "shopify_ornamentopia": "Ornamentopia (Shopify)",
        }.get(channel, channel)

    def _open_order_in_browser(self, channel: str, order_id_display):
        """order_id_display is the raw cell value (already prefixed with '#').
        Strip the '#' and open the channel's admin URL for the order."""
        raw = str(order_id_display or "").lstrip("#").strip()
        if not raw:
            self.append_log("[UI] No order selected - cannot open admin URL.")
            return
        url = self._admin_url_for(channel, raw)
        if not url:
            self.append_log(f"[UI] No admin URL known for channel {channel!r}.")
            return
        try:
            webbrowser.open(url)
            self.append_log(
                f"[UI] Opened {self._channel_label(channel)} order #{raw} -> {url}"
            )
        except Exception as e:
            logger.error(f"Error opening admin URL for {channel} #{raw}: {e}", exc_info=True)
            self.append_log(f"[ERROR] Failed to open browser for order #{raw}: {e}")

    def _attach_context_menu(self, grid: ttk.Treeview, channel: str):
        """Bind <Button-3> on the grid to a popup with: open admin URL / copy order ID.
        Right-clicking on empty area is a no-op (no menu shown)."""
        menu = tk.Menu(self.root, tearoff=0)

        def open_action():
            iid = grid.focus()
            if not iid:
                return
            order_id = grid.item(iid, "values")[0]
            self._open_order_in_browser(channel, order_id)

        def copy_action():
            iid = grid.focus()
            if not iid:
                return
            raw = str(grid.item(iid, "values")[0] or "").lstrip("#").strip()
            if not raw:
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(raw)
            self.append_log(
                f"[UI] Copied {self._channel_label(channel)} order ID {raw} to clipboard."
            )

        menu.add_command(label="\U0001f310  Open order in browser", command=open_action)
        menu.add_command(label="\U0001f4cb  Copy order ID",          command=copy_action)

        def on_right_click(event):
            row = grid.identify_row(event.y)
            if not row:
                # Right-clicked on empty area - do nothing.
                return
            grid.selection_set(row)
            grid.focus(row)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        grid.bind("<Button-3>", on_right_click)


    # ── SHOPIFY PRINTING TAB (Ornamentopia) ─────────────────────────────────────
    def _build_shopify_tab(self, parent):
        """Build the Shopify printing tab: Chrome open/close, worker
        controls, and a recent-order grid with right-click context menu."""
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        mid = tk.Frame(parent)
        mid.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        mid.grid_columnconfigure(0, weight=4)
        mid.grid_columnconfigure(1, weight=5)
        mid.grid_rowconfigure(0, weight=1)

        ctrl = tk.Frame(mid)
        ctrl.grid(row=0, column=0, sticky="nsew", padx=(0, 15))

        self.lbl_shopify_section = tk.Label(
            ctrl, text="Ornamentopia (Shopify)", font=("Arial", 13, "bold"))
        self.lbl_shopify_section.pack(fill="x", pady=(0, 8))

        self.lbl_shopify_browser = tk.Label(
            ctrl, text="Ornamentopia Shopify  (hello@ornamentopia.ca)",
            font=("Arial", 10, "bold"), anchor="w")
        self.lbl_shopify_browser.pack(fill="x", pady=(0, 2))

        self.btn_open_shopify_chrome = tk.Button(
            ctrl, text="🌐 Open Shopify Admin Browser",
            font=("Arial", 11), height=1,
            command=self.open_shopify_chrome, relief="groove")
        self.btn_open_shopify_chrome.pack(fill="x", pady=(0, 2))

        self.btn_stop_shopify_chrome = tk.Button(
            ctrl, text="✖ Close Browser",
            font=("Arial", 10), height=1,
            command=self.stop_shopify_chrome, relief="groove", state="disabled")
        self.btn_stop_shopify_chrome.pack(fill="x", pady=(0, 10))

        self.btn_shopify_start = tk.Button(
            ctrl, text="🚀 Start Shopify Worker",
            font=("Arial", 14, "bold"),
            height=2, command=self.start_shopify_worker, relief="groove")
        self.btn_shopify_start.pack(fill="x", pady=(4, 4))

        self.btn_shopify_trigger = tk.Button(
            ctrl, text="🔄 Check Shopify Orders",
            font=("Arial", 13), height=2,
            command=self.shopify_trigger_now, state="disabled")
        self.btn_shopify_trigger.pack(fill="x", pady=4)

        self.btn_shopify_stop = tk.Button(
            ctrl, text="🛑 Stop Shopify Worker",
            font=("Arial", 14, "bold"),
            bg="#0007cc", fg="white",
            activebackground="#0007cc", activeforeground="white",
            height=2, command=self.stop_shopify_worker,
            relief="groove", state="disabled")
        self.btn_shopify_stop.pack(fill="x", pady=(4, 0))

        self.lbl_shopify_worker_status = tk.Label(
            ctrl, text="⚫  Worker: Stopped",
            font=("Arial", 11, "bold"), anchor="w")
        self.lbl_shopify_worker_status.pack(fill="x", pady=(6, 2))

        self.lbl_shopify_chrome_status = tk.Label(
            ctrl, text="⚫  Browser: Closed",
            font=("Arial", 10), anchor="w")
        self.lbl_shopify_chrome_status.pack(fill="x", pady=(0, 2))

        # Routed-printer status line. Mirrors shopify_print.CONFIG; if the
        # defaults here drift from ShopifyAppConfig in shopify_print.py the
        # label will lie — keep both files in sync when editing.
        if SHOPIFY_ROUTING_ACTIVE:
            printer_text = (
                f"\U0001f5a8  Routed to: {SHOPIFY_TARGET_PRINTER}  "
                f"\u2192 SumatraPDF"
            )
        elif not SHOPIFY_TARGET_PRINTER:
            printer_text = (
                "\U0001f5a8  Printer: OS default  "
                "(set SHOPIFY_TARGET_PRINTER .env to route elsewhere)"
            )
        else:
            printer_text = (
                f"\U0001f5a8  Printer: OS default  "
                f"(install SumatraPDF @ {SHOPIFY_SUMATRA_PATH} "
                f"to enable routing \u2192 {SHOPIFY_TARGET_PRINTER})"
            )
        self.lbl_shopify_printer = tk.Label(
            ctrl, text=printer_text,
            font=("Arial", 9), anchor="w", justify="left",
            wraplength=380,
        )
        self.lbl_shopify_printer.pack(fill="x", pady=(0, 4))

        orders_frame = tk.LabelFrame(
            mid, text=" Recent Shopify Order Logs ",
            font=("Arial", 13, "bold"), padx=15, pady=15)
        orders_frame.grid(row=0, column=1, sticky="nsew", padx=(15, 0))
        orders_frame.grid_columnconfigure(0, weight=1)
        orders_frame.grid_rowconfigure(0, weight=1)

        columns = ("order_id", "date", "total", "status")
        self.shopify_grid = ttk.Treeview(
            orders_frame, columns=columns, show="headings", selectmode="browse")
        self.shopify_grid.heading("order_id", text="Order #")
        self.shopify_grid.heading("date", text="Placement Date")
        self.shopify_grid.heading("total", text="Total")
        self.shopify_grid.heading("status", text="Processing Status")
        self.shopify_grid.column("order_id", width=110, anchor="center")
        self.shopify_grid.column("date", width=140, anchor="center")
        self.shopify_grid.column("total", width=110, anchor="e")
        self.shopify_grid.column("status", width=210, anchor="w")
        self.shopify_grid.grid(row=0, column=0, sticky="nsew")
        self._attach_context_menu(self.shopify_grid, "shopify_ornamentopia")

    # ── SHOPIFY ACTIONS ─────────────────────────────────────────────────
    def open_shopify_chrome(self):
        """Open Chrome for the Ornamentopia Shopify admin (port 9225)."""
        if self.shopify_chrome is not None:
            return
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        # Pin the Shopify profile's silent-print destination to the Brothers
        # Shopify unit BEFORE Chrome starts (Chrome reads Preferences at launch;
        # the file must not be edited while that Chrome is open).
        set_profile_print_destination(SHOPIFY_PROFILE_DIR, SHOPIFY_TARGET_PRINTER)
        self.append_log("[Shopify] Opening Ornamentopia Shopify admin browser on port 9225...")
        if os.path.exists(chrome_path):
            self.shopify_chrome = subprocess.Popen([
                chrome_path,
                "--remote-debugging-port=9225",
                "--kiosk-printing",
                "--user-data-dir=C:\\Temp\\ShopifyChrome_Ornamentopia",
                "--window-title=Shopify - Ornamentopia (hello@ornamentopia.ca)",
                "--disable-session-crashed-bubble",
                "--disable-infobars",
                "--hide-crash-restore-bubble",
                "https://e55ee8-2.myshopify.com/admin/orders",
            ], creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            self.append_log(" -> Browser open. Log into the Ornamentopia Shopify admin (hello@ornamentopia.ca) now.")
        else:
            self.append_log(" -> WARNING: Chrome not found at default path.")
        self.apply_theme()

    def stop_shopify_chrome(self):
        if self.shopify_chrome:
            graceful_chrome_shutdown(self.shopify_chrome, "Ornamentopia Shopify Chrome")
            self.shopify_chrome = None
            # Re-pin after Chrome has fully closed: a graceful shutdown flushes
            # Chrome's in-memory print destination back to Preferences, which can
            # overwrite our pin. Re-apply so the next launch still targets Brother.
            set_profile_print_destination(SHOPIFY_PROFILE_DIR, SHOPIFY_TARGET_PRINTER)
            self.append_log("[Shopify] Browser (port 9225) closed cleanly.")
        self.apply_theme()

    def start_shopify_worker(self):
        if self.shopify_process is None:
            self.append_log("[Shopify Engine] Starting Shopify order printing worker...")
            py_exe = sys.executable if not getattr(sys, 'frozen', False) else "python"
            self.shopify_process = subprocess.Popen(
                [py_exe, "shopify_print.py"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            threading.Thread(target=self.pipe_reader, args=(self.shopify_process,), daemon=True).start()
            self.apply_theme()

    def shopify_trigger_now(self):
        with open(SHOPIFY_TRIGGER_FILE, 'w') as f:
            f.write("trigger")
        self.append_log("[Shopify Engine] Requesting immediate Shopify email sync...")

    def stop_shopify_worker(self):
        if self.shopify_process:
            self.shopify_process.terminate()
            try:
                self.shopify_process.wait(timeout=5)
            except Exception:
                try:
                    self.shopify_process.kill()
                except Exception:
                    pass
            self.shopify_process = None
            self.append_log("[Shopify Panel] Shopify printing worker stopped.")
        self.apply_theme()

    # ── ANALYTICS TAB (per-channel × period matrix) ───────────────────────────
    def _build_analytics_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        self.analytics_title = tk.Label(
            parent,
            text="Per-Channel Sales & Order Totals",
            font=("Arial", 18, "bold"),
        )
        self.analytics_title.grid(row=0, column=0, sticky="w",
                                  padx=20, pady=(15, 5))

        self.analytics_subtitle = tk.Label(
            parent,
            text="Totals are kept per channel — currencies are not summed.",
            font=("Arial", 14),
        )
        self.analytics_subtitle.grid(row=0, column=0, sticky="e",
                                     padx=20, pady=(17, 5))

        # Table
        self.analytics_table = tk.Frame(parent)
        self.analytics_table.grid(row=1, column=0, sticky="nsew", padx=20, pady=5)
        # Column 0 = channel name (fixed), columns 1..N = period columns (uniform)
        self.analytics_table.grid_columnconfigure(0, weight=0, minsize=230)
        for c in range(1, len(PERIODS) + 1):
            self.analytics_table.grid_columnconfigure(c, weight=1, uniform="period")
        for r in range(len(CHANNELS)):
            self.analytics_table.grid_rowconfigure(r, weight=1)

        # Header row
        self.analytics_header_labels = []
        corner = tk.Label(
            self.analytics_table, text="Channel",
            font=("Arial", 15, "bold"), padx=12, pady=10, anchor="w",
        )
        corner.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        self.analytics_header_labels.append(corner)
        for c, label in enumerate(PERIODS, start=1):
            hdr = tk.Label(
                self.analytics_table, text=label,
                font=("Arial", 15, "bold"), padx=12, pady=10, anchor="center",
            )
            hdr.grid(row=0, column=c, sticky="nsew", padx=2, pady=2)
            self.analytics_header_labels.append(hdr)

        # Channel rows
        self.analytics_cells = {}
        self.analytics_channel_labels = []
        for r, (channel_name, _path, currency) in enumerate(CHANNELS, start=1):
            name_lbl = tk.Label(
                self.analytics_table, text=channel_name,
                font=("Arial", 15, "bold"), padx=12, pady=10, anchor="w",
            )
            name_lbl.grid(row=r, column=0, sticky="nsew", padx=2, pady=2)
            self.analytics_channel_labels.append(name_lbl)

            for c, _label in enumerate(PERIODS, start=1):
                cell = tk.Frame(self.analytics_table)
                cell.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)
                orders_lbl = tk.Label(cell, text="—",
                                      font=("Arial", 16, "bold"),
                                      anchor="center")
                orders_lbl.pack(side="top", pady=(10, 0))
                sales_lbl = tk.Label(
                    cell, text="—",
                    font=("Arial", 14), anchor="center",
                )
                sales_lbl.pack(side="top", pady=(0, 10))
                self.analytics_cells[(r - 1, c - 1)] = {
                    "frame": cell, "orders": orders_lbl,
                    "sales": sales_lbl,
                }

        # Skipped counters
        self.skipped_frame = tk.LabelFrame(
            parent, text=" All-time skipped counters ",
            font=("Arial", 16, "bold"), padx=15, pady=10,
        )
        self.skipped_frame.grid(row=2, column=0, sticky="ew",
                                padx=20, pady=(10, 20))
        self.skipped_frame.grid_columnconfigure(0, weight=1)
        self.skipped_frame.grid_columnconfigure(1, weight=1)

        self.lbl_skipped_fraud = tk.Label(
            self.skipped_frame, text="🚫 Fraud skipped: —",
            font=("Arial", 15, "bold"), anchor="w",
        )
        self.lbl_skipped_fraud.grid(row=0, column=0, sticky="w", padx=5)

        self.lbl_skipped_gc = tk.Label(
            self.skipped_frame, text="🎁 Gift-card-only skipped: —",
            font=("Arial", 15, "bold"), anchor="w",
        )
        self.lbl_skipped_gc.grid(row=0, column=1, sticky="w", padx=5)

    # ── THEME ─────────────────────────────────────────────────────────────────
    def apply_theme(self):
        theme = self.current_theme
        self.root.configure(bg=theme["bg"])
        self.top_bar.configure(bg=theme["bg"])
        self.lbl_title.configure(bg=theme["bg"], fg=theme["text"])

        # Heartbeat pill
        self.heartbeat_frame.configure(bg=theme["stat_card"], highlightthickness=0)
        self.heartbeat_icon.configure(bg=theme["stat_card"], fg=theme["primary"])
        self.heartbeat_label.configure(bg=theme["stat_card"], fg=theme["text"])

        # Notebook + tab backgrounds
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "TNotebook",
            background=theme["bg"],
            borderwidth=0,
            tabmargins=(8, 6, 8, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=theme["tab_bg"],
            foreground=theme["text"],
            padding=(20, 10),
            font=("Arial", 11, "bold"),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", theme["primary"]),
                        ("active", theme["border"])],
            foreground=[("selected", "white"),
                        ("active", theme["text"])],
        )

        # Welcome tab contents
        self.welcome_tab.configure(bg=theme["bg"])
        self.welcome_canvas.configure(bg=theme["bg"])
        self.welcome_content.configure(bg=theme["bg"])
        self.welcome_title.configure(bg=theme["bg"], fg=theme["text"])
        self.welcome_intro.configure(bg=theme["bg"], fg=theme["text_dim"])
        for section, body_label in zip(
            self.welcome_section_frames, self.welcome_section_bodies
        ):
            section.configure(bg=theme["surface"], fg=theme["text"])
            body_label.configure(bg=theme["surface"], fg=theme["text"])

        # Operations tab contents
        self.operations_tab.configure(bg=theme["bg"])
        self.mid_frame.configure(bg=theme["bg"])
        self.ctrl_frame.configure(bg=theme["bg"])
        self.lbl_pm_section.configure(bg=theme["bg"], fg=theme["text"])
        self.orders_frame.configure(bg=theme["surface"], fg=theme["text"],
                                     bd=1, relief="solid")

        self.etsy_divider.configure(bg=theme["border"])
        self.etsy_frame.configure(bg=theme["bg"])
        self.etsy_ctrl_frame.configure(bg=theme["bg"])
        self.lbl_etsy_section.configure(bg=theme["bg"], fg=theme["etsy_accent"])
        self.lbl_etsy_pm_browser.configure(bg=theme["bg"], fg=theme["text"])
        self.lbl_etsy_puz_browser.configure(bg=theme["bg"], fg=theme["text"])
        self.etsy_logs_frame.configure(bg=theme["bg"])

        disabled_bg = theme["disabled_bg"]
        disabled_fg = theme["disabled_fg"]
        if self.etsy_chrome_pm is not None:
            self.btn_open_chrome_pm.configure(bg=disabled_bg, fg=disabled_fg,
                                              state="disabled")
            self.btn_stop_chrome_pm.configure(bg="#cc0000", fg="white",
                                              state="normal")
        else:
            self.btn_open_chrome_pm.configure(bg=theme["primary"], fg="white",
                                              state="normal")
            self.btn_stop_chrome_pm.configure(bg=disabled_bg, fg=disabled_fg,
                                              state="disabled")
        if self.etsy_chrome_puz is not None:
            self.btn_open_chrome_puz.configure(bg=disabled_bg, fg=disabled_fg,
                                              state="disabled")
            self.btn_stop_chrome_puz.configure(bg="#cc0000", fg="white",
                                              state="normal")
        else:
            self.btn_open_chrome_puz.configure(bg=theme["primary"], fg="white",
                                              state="normal")
            self.btn_stop_chrome_puz.configure(bg=disabled_bg, fg=disabled_fg,
                                              state="disabled")
        self.etsy_pm_frame.configure(bg=theme["etsy_card"], fg=theme["etsy_accent"])
        self.etsy_puz_frame.configure(bg=theme["etsy_card"], fg=theme["etsy_accent"])

        for lbl in (self.lbl_pm_worker_status, self.lbl_pm_chrome_status,
                    self.lbl_etsy_worker_status, self.lbl_etsy_pm_chrome_status,
                    self.lbl_etsy_puz_chrome_status):
            lbl.configure(bg=theme["bg"])
        self.btn_clear_log.configure(bg=theme["surface"], fg=theme["stat_lbl"],
                                     activebackground=theme["border"])

        # Treeview styling (light/dark bands)
        style.configure(
            "Treeview",
            background=theme["surface"], fieldbackground=theme["surface"],
            foreground=theme["text"], rowheight=36, font=("Arial", 13),
        )
        style.configure(
            "Treeview.Heading",
            background=theme["stat_card"], foreground=theme["text"],
            bordercolor=theme["border"], font=("Arial", 13, "bold"),
        )
        style.map("Treeview.Heading", background=[('active', theme["border"])])

        if self.is_dark_mode:
            for grid in (self.order_grid, self.etsy_pm_grid, self.etsy_puz_grid, self.shopify_grid):
                grid.tag_configure("printed",     background="#1e293b", foreground="#f8fafc")
                grid.tag_configure("printed_alt", background="#0f172a", foreground="#f8fafc")
                grid.tag_configure("spam",        background="#4c1d1d", foreground="#fca5a5")
        else:
            for grid in (self.order_grid, self.etsy_pm_grid, self.etsy_puz_grid, self.shopify_grid):
                grid.tag_configure("printed",     background="#ffffff", foreground="#1a365d")
                grid.tag_configure("printed_alt", background="#f8fafc", foreground="#1a365d")
                grid.tag_configure("spam",        background="#fee2e2", foreground="#991b1b")

        # Log frame
        self.log_frame.configure(bg=theme["bg"], fg=theme["text"])
        self.log_area.configure(bg=theme["log_bg"], fg=theme["log_text"],
                                insertbackground=theme["log_text"])
        self.btn_theme.configure(bg=theme["surface"], fg=theme["text"],
                                 activebackground=theme["border"])
        self.btn_start_all.configure(bg=theme["primary"], fg="#ffffff",
                                     activebackground=theme["primary_hover"])
        self.btn_start_others.configure(bg=theme["primary"], fg="#ffffff",
                                        activebackground=theme["primary_hover"])
        self.btn_stop_all.configure(bg="#cc0000", fg="#ffffff",
                                    activebackground="#e03030",
                                    activeforeground="#ffffff")

        # Operations button states
        if self.automation_process is not None:
            self.btn_start_print.configure(bg=disabled_bg, fg=disabled_fg,
                                           state="disabled")
            self.btn_manual_trigger.configure(bg=theme["surface"],
                                              fg=theme["text"], state="normal")
            self.btn_stop.configure(bg="#cc0000", fg="white", state="normal")
        else:
            self.btn_start_print.configure(
                bg=theme["primary"], fg="white",
                activebackground=theme["primary_hover"], state="normal",
            )
            self.btn_manual_trigger.configure(bg=disabled_bg, fg=disabled_fg,
                                              state="disabled")
            self.btn_stop.configure(bg=disabled_bg, fg=disabled_fg,
                                    state="disabled")

        if self.etsy_process is not None:
            self.btn_etsy_start.configure(bg=disabled_bg, fg=disabled_fg,
                                         state="disabled")
            self.btn_etsy_trigger.configure(bg=theme["surface"],
                                            fg=theme["text"], state="normal")
            self.btn_etsy_stop.configure(bg="#cc0000", fg="white", state="normal")
        else:
            self.btn_etsy_start.configure(
                bg=theme["etsy_accent"], fg="white",
                activebackground=theme["border"], state="normal",
            )
            self.btn_etsy_trigger.configure(bg=disabled_bg, fg=disabled_fg,
                                            state="disabled")
            self.btn_etsy_stop.configure(bg=disabled_bg, fg=disabled_fg,
                                        state="disabled")

        # Worker/browser status indicators
        running_fg = "#22c55e"
        stopped_fg = theme["stat_lbl"]
        if self.automation_process is not None:
            self.lbl_pm_worker_status.configure(text="🟢  Worker: Running",
                                                fg=running_fg)
        else:
            self.lbl_pm_worker_status.configure(text="⚫  Worker: Stopped",
                                                fg=stopped_fg)
        if self.chrome_process is not None:
            self.lbl_pm_chrome_status.configure(text="🟢  Browser: Open",
                                                fg=running_fg)
        else:
            self.lbl_pm_chrome_status.configure(text="⚫  Browser: Closed",
                                                fg=stopped_fg)
        if self.etsy_process is not None:
            self.lbl_etsy_worker_status.configure(text="🟢  Worker: Running",
                                                  fg=running_fg)
        else:
            self.lbl_etsy_worker_status.configure(text="⚫  Worker: Stopped",
                                                  fg=stopped_fg)
        if self.etsy_chrome_pm is not None:
            self.lbl_etsy_pm_chrome_status.configure(text="🟢  PM Browser: Open",
                                                    fg=running_fg)
        else:
            self.lbl_etsy_pm_chrome_status.configure(text="⚫  PM Browser: Closed",
                                                    fg=stopped_fg)
        if self.etsy_chrome_puz is not None:
            self.lbl_etsy_puz_chrome_status.configure(text="🟢  Puz Browser: Open",
                                                     fg=running_fg)
        else:
            self.lbl_etsy_puz_chrome_status.configure(text="⚫  Puz Browser: Closed",
                                                     fg=stopped_fg)

        # Analytics tab styling
        self.analytics_tab.configure(bg=theme["bg"])
        self.analytics_table.configure(bg=theme["surface"])
        self.analytics_title.configure(bg=theme["bg"], fg=theme["text"])
        self.analytics_subtitle.configure(bg=theme["bg"], fg=theme["text_dim"])
        self.skipped_frame.configure(bg=theme["surface"], fg=theme["text"])
        self.lbl_skipped_fraud.configure(bg=theme["surface"], fg=theme["text"])
        self.lbl_skipped_gc.configure(bg=theme["surface"], fg=theme["text"])

        for lbl in self.analytics_header_labels:
            lbl.configure(bg=theme["stat_card"], fg=theme["text"])
        for lbl in self.analytics_channel_labels:
            lbl.configure(bg=theme["bg"], fg=theme["text"])
        for cell in self.analytics_cells.values():
            cell["frame"].configure(bg=theme["surface"])
            cell["orders"].configure(bg=theme["surface"], fg=theme["primary"])
            cell["sales"].configure(bg=theme["surface"], fg=theme["text_dim"])

        # Shopify Printing tab styling (labels follow tab bg)
        self.shopify_tab.configure(bg=theme["bg"])
        self.lbl_shopify_section.configure(bg=theme["bg"], fg=theme["text"])
        self.lbl_shopify_browser.configure(bg=theme["bg"], fg=theme["text"])
        # Printer-routing status line: green when routed, muted otherwise.
        if SHOPIFY_ROUTING_ACTIVE:
            self.lbl_shopify_printer.configure(bg=theme["bg"], fg=theme["primary"])
        else:
            self.lbl_shopify_printer.configure(
                bg=theme["bg"], fg=theme["text_dim"]
            )

        if self.shopify_chrome is not None:
            self.btn_open_shopify_chrome.configure(bg=disabled_bg, fg=disabled_fg, state="disabled")
            self.btn_stop_shopify_chrome.configure(bg="#cc0000", fg="white", state="normal")
        else:
            self.btn_open_shopify_chrome.configure(bg=theme["primary"], fg="white", state="normal")
            self.btn_stop_shopify_chrome.configure(bg=disabled_bg, fg=disabled_fg, state="disabled")

        if self.shopify_process is not None:
            self.btn_shopify_start.configure(bg=disabled_bg, fg=disabled_fg, state="disabled")
            self.btn_shopify_trigger.configure(bg=theme["surface"], fg=theme["text"], state="normal")
            self.btn_shopify_stop.configure(bg="#cc0000", fg="white", state="normal")
        else:
            self.btn_shopify_start.configure(
                bg=theme["primary"], fg="white",
                activebackground=theme["primary_hover"], state="normal",
            )
            self.btn_shopify_trigger.configure(bg=disabled_bg, fg=disabled_fg, state="disabled")
            self.btn_shopify_stop.configure(bg=disabled_bg, fg=disabled_fg, state="disabled")

        if self.shopify_process is not None:
            self.lbl_shopify_worker_status.configure(text="🟢  Worker: Running", fg=running_fg)
        else:
            self.lbl_shopify_worker_status.configure(text="⚫  Worker: Stopped", fg=stopped_fg)
        if self.shopify_chrome is not None:
            self.lbl_shopify_chrome_status.configure(text="🟢  Browser: Open", fg=running_fg)
        else:
            self.lbl_shopify_chrome_status.configure(text="⚫  Browser: Closed", fg=stopped_fg)

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        self.current_theme = self.themes["dark"] if self.is_dark_mode else self.themes["light"]
        self.apply_theme()

    # ── LOGGING ───────────────────────────────────────────────────────────────
    def append_log(self, text):
        self.log_area.configure(state="normal")
        tag = None
        if "ERROR" in text or "failed" in text:
            tag = "error"
        elif "INFO" in text or "SUCCESS" in text:
            tag = "info"
        elif "warning" in text:
            tag = "warn"
        self.log_area.insert(tk.END, f"{text}\n", tag)
        line_count = int(self.log_area.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_area.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log_area.see(tk.END)
        self.log_area.configure(state="disabled")

    def clear_log(self):
        self.log_area.configure(state="normal")
        self.log_area.delete("1.0", tk.END)
        self.log_area.configure(state="disabled")

    # ── ONE-SHOT STARTUP SEQUENCES ────────────────────────────────────────────
    def start_all(self):
        """Start the PuzzleMaster.ca channel: admin Chrome + printing worker.

        Only PuzzleMaster.ca is started here — the operator signs in to the
        admin session, then hits ▶ Start Etsy + Shopify.  The step's method is
        idempotent (it no-ops when already running), so a partly started system
        still converges.  Re-entrant calls are ignored while a sequence is in
        flight."""
        self._start_sequence(
            steps=[
                ("PuzzleMaster orders: Chrome + printing worker",
                 self.start_automation_center),
            ],
            intro=("[AutoStart] Starting PuzzleMaster.ca printing only "
                   "(Etsy and Shopify are started by hand)..."),
            done=("[AutoStart] ✅ PuzzleMaster.ca printing started. If the "
                  "browser asks for a login, sign in once — the profile "
                  "remembers the session. Then hit ▶ Start Etsy + Shopify "
                  "when you are ready."),
        )

    def start_other_channels(self):
        """Start the Etsy (both shops) and Shopify channels.

        The second half of the daily start, deliberately separate from
        start_all: the operator starts PuzzleMaster.ca first, signs in, and only
        then lets the other channels print — all three workers printing at once
        left orders interleaved or missing pages.  PuzzleMaster.ca is left
        running untouched, and each step is idempotent, so a partly started
        system still converges."""
        self._start_sequence(
            steps=[
                ("PuzzleMaster Etsy browser (port 9223)",
                 self.open_chrome_pm),
                ("Puz Etsy browser (port 9224)",
                 self.open_chrome_puz),
                ("Etsy printing worker",
                 self.start_etsy_worker),
                ("Shopify tab + Ornamentopia admin browser (port 9225)",
                 self._auto_start_shopify_browser),
                ("Shopify printing worker",
                 self.start_shopify_worker),
            ],
            intro=("[AutoStart] Starting Etsy (both shops) and Shopify "
                   "— PuzzleMaster.ca keeps running..."),
            done=("[AutoStart] ✅ Etsy and Shopify started. If any browser "
                  "asks for a login, sign in once — the profile remembers "
                  "the session."),
        )

    def _start_sequence(self, steps, intro, done):
        """Run a startup step list one action per mainloop tick.

        Both start buttons share this so their guards, button states and step
        logging can never drift apart."""
        if self._start_all_busy:
            self.append_log("[AutoStart] A startup sequence is already in progress...")
            return
        if self._stop_all_busy:
            self.append_log("[AutoStart] Stop sequence still in progress — wait for it to finish.")
            return
        self._start_all_busy = True
        self.btn_start_all.configure(state="disabled")
        self.btn_start_others.configure(state="disabled")
        self.btn_stop_all.configure(state="disabled")
        self.notebook.select(self.operations_tab)
        self.append_log(intro)
        self.root.after(0, self._run_start_all_step, steps, 0, done)

    def _run_start_all_step(self, steps, index, done_msg=None):
        if index >= len(steps):
            self._start_all_busy = False
            self.btn_start_all.configure(state="normal")
            self.btn_start_others.configure(state="normal")
            self.btn_stop_all.configure(state="normal")
            self.append_log(done_msg or "[AutoStart] ✅ Startup sequence complete.")
            return
        label, action = steps[index]
        self.append_log(f"[AutoStart] Step {index + 1}/{len(steps)} — {label}")
        action()
        self.root.after(AUTO_START_STEP_MS, self._run_start_all_step,
                        steps, index + 1, done_msg)

    def _auto_start_shopify_browser(self):
        """Switch to the Shopify tab first, then open the admin browser —
        the same order the operator follows by hand."""
        self.notebook.select(self.shopify_tab)
        self.open_shopify_chrome()

    # ── ONE-SHOT FULL STOP SEQUENCE (mirror of Start All) ─────────────────────
    def stop_all_channels(self):
        """Stop every printing worker and close every browser window in one
        click, leaving the panel itself open for the next Start All.

        Steps: PuzzleMaster orders (worker + admin browser) → Etsy worker →
        PuzzleMaster Etsy browser → Puz Etsy browser → Shopify worker →
        Shopify admin browser — the exact reverse of the startup order.  Each
        step's method is idempotent (it no-ops when already stopped), so a
        partly stopped system still converges to fully stopped.  One action is
        scheduled per tick so a graceful Chrome shutdown gets its own mainloop
        turn instead of freezing the window for the whole sequence.  Re-entrant
        calls, and calls while a startup sequence is in flight, are ignored."""
        if self._stop_all_busy:
            self.append_log("[AutoStop] Full stop sequence already in progress...")
            return
        if self._start_all_busy:
            self.append_log("[AutoStop] Startup sequence still running — wait for it to finish.")
            return
        self._stop_all_busy = True
        self.btn_stop_all.configure(state="disabled")
        self.btn_start_all.configure(state="disabled")
        self.btn_start_others.configure(state="disabled")
        self.append_log("[AutoStop] Stopping every worker and closing every browser...")

        steps = [
            ("PuzzleMaster orders worker + admin browser (port 9222)",
             self.stop_all),
            ("Etsy printing worker",
             self.stop_etsy_worker),
            ("PuzzleMaster Etsy browser (port 9223)",
             self.stop_chrome_pm),
            ("Puz Etsy browser (port 9224)",
             self.stop_chrome_puz),
            ("Shopify printing worker",
             self.stop_shopify_worker),
            ("Ornamentopia admin browser (port 9225)",
             self.stop_shopify_chrome),
            ("Anything left over from an earlier session",
             self.stop_leftover_processes),
        ]
        self.root.after(0, self._run_stop_all_step, steps, 0)

    def _run_stop_all_step(self, steps, index):
        if index >= len(steps):
            self._stop_all_busy = False
            self.btn_stop_all.configure(state="normal")
            self.btn_start_all.configure(state="normal")
            self.btn_start_others.configure(state="normal")
            self.append_log(
                "[AutoStop] ✅ All workers stopped and all browsers closed. "
                "The panel stays open — hit 🎛 Start PuzzleMaster when you are ready."
            )
            self.apply_theme()
            return
        label, action = steps[index]
        self.append_log(f"[AutoStop] Step {index + 1}/{len(steps)} — {label}")
        action()
        self.root.after(AUTO_STOP_STEP_MS, self._run_stop_all_step,
                        steps, index + 1)

    def stop_leftover_processes(self):
        """Final Stop All step: terminate workers/browsers that are still
        running but no longer belong to this panel — the result of a panel
        that crashed, or of a second panel instance that never spawned them
        itself.  Without this, Stop All from a freshly opened panel would
        find every handle None and silently stop nothing.

        Asks nicely first (taskkill without /F posts WM_CLOSE, so Chrome
        saves its session and workers unwind), then forces only whatever is
        still alive after LEFTOVER_GRACE_S.  Processes this panel holds a
        handle for are excluded — they were already stopped gracefully by
        the earlier steps."""
        held = [p for p in (
            self.automation_process, self.etsy_process, self.shopify_process,
            self.chrome_process, self.etsy_chrome_pm, self.etsy_chrome_puz,
            self.shopify_chrome,
        ) if p is not None]
        held_pids = [p.pid for p in held]

        found = find_leftover_processes(query_running_processes(), held_pids)
        if not found:
            self.append_log(" -> Nothing left over from earlier sessions.")
            return

        for pid, _kind, label in found:
            self.append_log(f" -> Found leftover {label} (PID {pid}); asking it to close...")
            _taskkill(pid)

        time.sleep(LEFTOVER_GRACE_S)

        survivors = {pid for pid, _kind, _label in
                     find_leftover_processes(query_running_processes(), held_pids)}
        for pid, _kind, label in found:
            if pid in survivors:
                self.append_log(f" -> {label} did not close in time (PID {pid}); forcing it shut.")
                _taskkill(pid, force=True)
            else:
                self.append_log(f" -> {label} closed.")

    # ── GRACEFUL STOP (scheduled Friday shutdown) ────────────────────────────
    def _graceful_shutdown_requested(self):
        """Entry point for the stop_trigger.txt handshake (main thread)."""
        if self._shutting_down:
            return
        self._shutting_down = True
        # Second removal attempt: a lingering trigger file would otherwise
        # self-stop the app on its next launch.
        try:
            os.remove(STOP_TRIGGER_FILE)
        except OSError:
            pass
        self.append_log(
            "[AutoStop] stop_trigger.txt received — shutting every channel "
            "down cleanly..."
        )
        self.shutdown()

    def shutdown(self):
        """Clean full shutdown: stop every worker, close every browser.
        Used by both the window's ✕ button and the scheduled Friday stop."""
        self.monitoring = False
        self.stop_all()
        self.stop_etsy_worker()
        self.stop_chrome_pm()
        self.stop_chrome_puz()
        self.stop_shopify_worker()
        self.stop_shopify_chrome()
        self.root.destroy()

    # ── PUZZLEMASTER ACTIONS ──────────────────────────────────────────────────
    def start_automation_center(self):
        if self.chrome_process is None:
            self.append_log("[Environment] Booting dedicated Chrome engine over port 9222...")
            chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            if os.path.exists(chrome_path):
                self.chrome_process = subprocess.Popen([
                    chrome_path,
                    "--remote-debugging-port=9222",
                    "--kiosk-printing",
                    "--user-data-dir=C:\\Temp\\ChromeDebugProfile",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                    "--hide-crash-restore-bubble",
                    "https://www.puzzlemaster.ca/admin/"
                ], creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                self.append_log(" -> Chrome environment connected. Go to puzzlemaster.ca/admin and enter login now.")
            else:
                self.append_log(" -> WARNING: Chrome executable not found at typical path. Please open manually.")

        if self.automation_process is None:
            self.append_log("[Core Engine] Initializing background printing worker loops...")
            py_exe = sys.executable if not getattr(sys, 'frozen', False) else "python"
            self.automation_process = subprocess.Popen(
                [py_exe, "order_print.py"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            threading.Thread(target=self.pipe_reader, args=(self.automation_process,), daemon=True).start()
            self.apply_theme()

    def trigger_now(self):
        with open(TRIGGER_FILE, 'w') as f:
            f.write("trigger")
        self.append_log("[Core Engine] Requesting immediate manual email sync...")

    def stop_all(self):
        self.append_log("[System Panel] Shutting down PuzzleMaster background components safely...")
        if self.automation_process:
            self.automation_process.terminate()
            try:
                self.automation_process.wait(timeout=5)
            except Exception:
                self.automation_process.kill()
            self.automation_process = None
            self.append_log(" -> Order Printing background worker loop stopped.")
        if self.chrome_process:
            graceful_chrome_shutdown(self.chrome_process, "PuzzleMaster Chrome")
            self.chrome_process = None
            self.append_log(" -> PuzzleMaster Chrome session closed cleanly.")
        self.apply_theme()

    # ── ETSY ACTIONS ──────────────────────────────────────────────────────────
    def open_chrome_pm(self):
        """Open Chrome for the PuzzleMaster Etsy account (port 9223)."""
        if self.etsy_chrome_pm is not None:
            return
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        self.append_log("[Etsy] Opening PuzzleMaster Etsy browser on port 9223...")
        if os.path.exists(chrome_path):
            self.etsy_chrome_pm = subprocess.Popen([
                chrome_path,
                "--remote-debugging-port=9223",
                "--kiosk-printing",
                "--user-data-dir=C:\\Temp\\EtsyChrome_PuzzleMaster",
                "--window-title=Etsy - PuzzleMaster (info@puzzlemaster.ca)",
                "--disable-session-crashed-bubble",
                "--disable-infobars",
                "--hide-crash-restore-bubble",
                "https://www.etsy.com/your/orders/sold"
            ], creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            self.append_log(" -> Browser open. Log into the PuzzleMaster Etsy account (info@puzzlemaster.ca) now.")
        else:
            self.append_log(" -> WARNING: Chrome not found at default path.")
        self.apply_theme()

    def stop_chrome_pm(self):
        """Close the PuzzleMaster Etsy Chrome window."""
        if self.etsy_chrome_pm:
            graceful_chrome_shutdown(self.etsy_chrome_pm, "PuzzleMaster Etsy Chrome")
            self.etsy_chrome_pm = None
            self.append_log("[Etsy] PuzzleMaster Etsy browser (port 9223) closed cleanly.")
        self.apply_theme()

    def open_chrome_puz(self):
        """Open Chrome for the Puz Etsy account (port 9224)."""
        if self.etsy_chrome_puz is not None:
            return
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        self.append_log("[Etsy] Opening Puz Etsy browser on port 9224...")
        if os.path.exists(chrome_path):
            self.etsy_chrome_puz = subprocess.Popen([
                chrome_path,
                "--remote-debugging-port=9224",
                "--kiosk-printing",
                "--user-data-dir=C:\\Temp\\EtsyChrome_Puz",
                "--window-title=Etsy - Puz (info@puz.ca)",
                "--disable-session-crashed-bubble",
                "--disable-infobars",
                "--hide-crash-restore-bubble",
                "https://www.etsy.com/your/orders/sold"
            ], creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            self.append_log(" -> Browser open. Log into the Puz Etsy account (info@puz.ca) now.")
        else:
            self.append_log(" -> WARNING: Chrome not found at default path.")
        self.apply_theme()

    def stop_chrome_puz(self):
        """Close the Puz Etsy Chrome window."""
        if self.etsy_chrome_puz:
            graceful_chrome_shutdown(self.etsy_chrome_puz, "Puz Etsy Chrome")
            self.etsy_chrome_puz = None
            self.append_log("[Etsy] Puz Etsy browser (port 9224) closed cleanly.")
        self.apply_theme()

    def start_etsy_worker(self):
        """Start the etsy_print.py background worker."""
        if self.etsy_process is None:
            self.append_log("[Etsy Engine] Starting Etsy order printing worker...")
            py_exe = sys.executable if not getattr(sys, 'frozen', False) else "python"
            self.etsy_process = subprocess.Popen(
                [py_exe, "etsy_print.py"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            threading.Thread(target=self.pipe_reader, args=(self.etsy_process,), daemon=True).start()
            self.apply_theme()

    def etsy_trigger_now(self):
        with open(ETSY_TRIGGER_FILE, 'w') as f:
            f.write("trigger")
        self.append_log("[Etsy Engine] Requesting immediate Etsy email sync...")

    def stop_etsy_worker(self):
        """Stop the etsy_print.py worker only (leaves browsers open)."""
        if self.etsy_process:
            self.etsy_process.terminate()
            try:
                self.etsy_process.wait(timeout=5)
            except Exception:
                self.etsy_process.kill()
            self.etsy_process = None
            self.append_log("[Etsy Panel] Etsy printing worker stopped.")
        self.apply_theme()

    # ── SUBPROCESS OUTPUT READER ──────────────────────────────────────────────
    def pipe_reader(self, process):
        for line in iter(process.stdout.readline, ''):
            if line:
                self.root.after(0, self.append_log, line.strip())
        process.stdout.close()

    def _reap_dead_browsers(self):
        """Notice Chrome windows that exited while the panel stayed open.

        Popen objects are not notified when Chrome closes itself (sleep,
        crash, or a closed window).  Poll them so the panel changes back to
        Closed and the operator can reopen the correct browser; the worker
        will keep the tagged emails and retry on the next cycle.
        """
        browser_specs = (
            ("chrome_process", "PuzzleMaster admin browser"),
            ("etsy_chrome_pm", "PuzzleMaster Etsy browser"),
            ("etsy_chrome_puz", "Puz Etsy browser"),
            ("shopify_chrome", "Ornamentopia Shopify browser"),
        )
        dead = []
        for attr, label in browser_specs:
            process = getattr(self, attr, None)
            if process is not None and process.poll() is not None:
                setattr(self, attr, None)
                dead.append((label, process.returncode))
        if dead:
            self.root.after(0, self._report_dead_browsers, dead)

    def _report_dead_browsers(self, dead):
        for label, returncode in dead:
            self.append_log(
                f"[Browser] {label} exited unexpectedly (code {returncode}). "
                "Reopen it; pending tagged emails will be retried."
            )
        self.apply_theme()

    # ── STATUS UPDATE LOOP ────────────────────────────────────────────────────
    def update_loop(self):
        """Background thread: reads status files and dispatches UI updates to the main thread."""
        while self.monitoring:
            # Graceful-stop handshake: the scheduled Friday stop writes this
            # file and waits for the panel to close itself cleanly — the same
            # cleanup path as the window's ✕ button.
            if os.path.exists(STOP_TRIGGER_FILE):
                stale = True
                try:
                    age = time.time() - os.path.getmtime(STOP_TRIGGER_FILE)
                    stale = age > STOP_TRIGGER_MAX_AGE_S
                    os.remove(STOP_TRIGGER_FILE)
                except OSError:
                    pass
                if not stale:
                    self.root.after(0, self._graceful_shutdown_requested)
                    break

            self._reap_dead_browsers()

            # PuzzleMaster.recent_orders grid
            if os.path.exists(STATUS_FILE):
                try:
                    with open(STATUS_FILE, 'rb') as f:
                        file_bytes = f.read()
                    current_hash = hashlib.md5(file_bytes).hexdigest()
                    if current_hash != self.last_status_hash:
                        data = json.loads(file_bytes.decode('utf-8'))
                        self.last_status_hash = current_hash
                        today_str = datetime.now().strftime('%Y-%m-%d')
                        orders = data.get("recent_orders", [])[:10]
                        self.root.after(0, self._update_pm_grid, orders, today_str)
                except Exception as e:
                    logger.error(f"Error processing PM status: {e}", exc_info=True)

            # Etsy recent_orders grids (one per shop)
            for shop_name, status_file in ETSY_STATUS_FILES.items():
                if not os.path.exists(status_file):
                    continue
                try:
                    with open(status_file, 'rb') as f:
                        file_bytes = f.read()
                    current_hash = hashlib.md5(file_bytes).hexdigest()
                    if current_hash == self.last_etsy_hashes[shop_name]:
                        continue
                    data = json.loads(file_bytes.decode('utf-8'))
                    self.last_etsy_hashes[shop_name] = current_hash
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    orders = data.get("recent_orders", [])[:8]
                    self.root.after(0, self._update_etsy_grid, shop_name, orders, today_str)
                except Exception as e:
                    logger.error(f"Error processing Etsy status for {shop_name}: {e}", exc_info=True)

            # Shopify recent_orders grid
            if os.path.exists(SHOPIFY_STATUS_FILE):
                try:
                    with open(SHOPIFY_STATUS_FILE, 'rb') as f:
                        file_bytes = f.read()
                    current_hash = hashlib.md5(file_bytes).hexdigest()
                    if current_hash != self.last_shopify_status_hash:
                        data = json.loads(file_bytes.decode('utf-8'))
                        self.last_shopify_status_hash = current_hash
                        today_str = datetime.now().strftime('%Y-%m-%d')
                        orders = data.get("recent_orders", [])[:10]
                        self.root.after(0, self._update_shopify_grid, orders, today_str)
                except Exception as e:
                    logger.error(f"Error processing Shopify status: {e}", exc_info=True)

            # Per-channel analytics + heartbeat
            channel_metrics = self._compute_channel_stats()
            self.root.after(0, self._update_heartbeat, channel_metrics)
            self.root.after(0, self._apply_analytics,  channel_metrics)

            time.sleep(2)

    def _compute_channel_stats(self):
        """Single pass over every channel's status file.

        Returns: dict {<channel_name>: {
            today_orders, today_sales,
            week_orders, week_sales,
            month_orders, month_sales,
            year_orders, year_sales,
            alltime_orders, alltime_sales,
            fraud_skipped, gc_skipped, last_check
        }}
        Week is a rolling 7-day window inclusive of today.
        Month / Year are calendar-month / calendar-year prefixes.
        Totals are kept per-channel — never summed across currencies."""
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        week_start = (now - timedelta(days=6)).strftime('%Y-%m-%d')
        month_prefix = now.strftime('%Y-%m')
        year_prefix = now.strftime('%Y')

        result = {}
        for channel_name, status_path, _currency in CHANNELS:
            m = {
                "today_orders":   0, "today_sales":   0.0,
                "week_orders":    0, "week_sales":    0.0,
                "month_orders":   0, "month_sales":   0.0,
                "year_orders":    0, "year_sales":    0.0,
                "alltime_orders": 0, "alltime_sales": 0.0,
                "fraud_skipped":  0, "gc_skipped":    0,
                "last_check":     "—",
            }
            if os.path.exists(status_path):
                try:
                    with open(status_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    daily_counts = data.get("daily_counts", {}) or {}
                    daily_sales  = data.get("daily_sales",  {}) or {}
                    for date_str, count in daily_counts.items():
                        m["alltime_orders"] += count
                        if date_str == today_str:
                            m["today_orders"] += count
                        if date_str >= week_start:
                            m["week_orders"] += count
                        if date_str.startswith(month_prefix):
                            m["month_orders"] += count
                        if date_str.startswith(year_prefix):
                            m["year_orders"] += count
                    for date_str, amount in daily_sales.items():
                        m["alltime_sales"] += amount
                        if date_str == today_str:
                            m["today_sales"] += amount
                        if date_str >= week_start:
                            m["week_sales"] += amount
                        if date_str.startswith(month_prefix):
                            m["month_sales"] += amount
                        if date_str.startswith(year_prefix):
                            m["year_sales"] += amount
                    m["fraud_skipped"] = data.get("skipped_fraud", 0) or 0
                    m["gc_skipped"]    = data.get("skipped_gc",    0) or 0
                    m["last_check"]    = data.get("last_check", "—") or "—"
                except Exception as e:
                    logger.error(f"Error reading {status_path}: {e}", exc_info=True)
            result[channel_name] = m
        return result

    def _update_heartbeat(self, channel_metrics):
        """Persistent top-bar label: today's PuzzleMaster.ca (retail) order count.
        Etsy and Shopify are deliberately excluded - see the Analytics tab for those.
        The "active" color is also driven by the retail channel only."""
        pm_metrics = channel_metrics.get(CHANNELS[0][0], {})
        today_orders = pm_metrics.get("today_orders", 0)
        self.heartbeat_label.configure(text=f"Today: {today_orders:,} orders")
        has_recent = (
            pm_metrics.get("today_orders", 0) > 0
            or (pm_metrics.get("last_check", "—") not in ("—", ""))
        )
        try:
            runnable = bool(
                self.last_status_hash != ""
                or self.last_etsy_hashes["PuzzleMaster"] != ""
                or self.last_etsy_hashes["Puz"] != ""
                or self.last_shopify_status_hash != ""
            )
        except Exception:
            runnable = False
        self.heartbeat_frame.configure(highlightthickness=0)
        self.heartbeat_label.configure(
            fg=self.current_theme["text"] if (has_recent or runnable)
            else self.current_theme["text_dim"]
        )

    def _apply_analytics(self, channel_metrics):
        """Update the per-channel × period analytics labels on the main thread."""
        total_fraud = 0
        total_gc = 0
        for channel_idx, (channel_name, _path, _currency) in enumerate(CHANNELS):
            m = channel_metrics.get(channel_name, {})
            values = [
                (m.get("today_orders",   0), m.get("today_sales",   0.0)),
                (m.get("week_orders",    0), m.get("week_sales",    0.0)),
                (m.get("month_orders",   0), m.get("month_sales",   0.0)),
                (m.get("year_orders",    0), m.get("year_sales",    0.0)),
                (m.get("alltime_orders", 0), m.get("alltime_sales", 0.0)),
            ]
            for period_idx, (orders, sales) in enumerate(values):
                cell = self.analytics_cells.get((channel_idx, period_idx))
                if cell is None:
                    continue
                cell["orders"].configure(text=f"{orders:,}" if orders else "0")
                cell["sales"].configure(text=f"${sales:,.2f}")
            total_fraud += m.get("fraud_skipped", 0)
            total_gc    += m.get("gc_skipped",    0)

        self.lbl_skipped_fraud.configure(text=f"🚫 Fraud skipped: {total_fraud:,}")
        self.lbl_skipped_gc.configure(text=f"🎁 Gift-card-only skipped: {total_gc:,}")

    # ── ORDER GRID POPULATION (Operations tab) ───────────────────────────────
    def _update_pm_grid(self, orders, today_str):
        for row in self.order_grid.get_children():
            self.order_grid.delete(row)
        for idx, ord_info in enumerate(orders):
            raw_status = ord_info.get('status', 'printed')
            if raw_status == "gift_card":
                display_status = "Skipped (Gift Card)"
                row_tag = "gift_card"
            elif raw_status == "spam":
                display_status = "Skipped (Spam/Fraud)"
                row_tag = "spam"
            else:
                display_status = "Printed Successfully"
                row_tag = "printed_alt" if idx % 2 == 0 else "printed"
            self.order_grid.insert("", tk.END,
                values=(
                    f"#{ord_info.get('id')}",
                    ord_info.get('order_date', today_str),
                    f"${ord_info.get('subtotal', 0.0):,.2f}",
                    display_status,
                ),
                tags=(row_tag,),
            )

    def _update_etsy_grid(self, shop_name, orders, today_str):
        grid = self.etsy_pm_grid if shop_name == "PuzzleMaster" else self.etsy_puz_grid
        for row in grid.get_children():
            grid.delete(row)
        for idx, ord_info in enumerate(orders):
            raw_status = ord_info.get('status', 'printed')
            if raw_status == "spam":
                display_status = "Skipped (Fraud)"
                row_tag = "spam"
            else:
                display_status = "Printed"
                row_tag = "printed_alt" if idx % 2 == 0 else "printed"
            grid.insert("", tk.END,
                values=(
                    f"#{ord_info.get('id')}",
                    ord_info.get('order_date', today_str),
                    f"${ord_info.get('subtotal', 0.0):,.2f}",
                    display_status,
                ),
                tags=(row_tag,),
            )

    def _update_shopify_grid(self, orders, today_str):
        """Refresh the Shopify recent-order grid on the main thread."""
        for row in self.shopify_grid.get_children():
            self.shopify_grid.delete(row)
        for idx, ord_info in enumerate(orders):
            raw_status = ord_info.get('status', 'printed')
            if raw_status == "spam":
                display_status = "Skipped (Fraud)"
                row_tag = "spam"
            else:
                display_status = "Printed Successfully"
                row_tag = "printed_alt" if idx % 2 == 0 else "printed"
            self.shopify_grid.insert("", tk.END,
                values=(
                    f"#{ord_info.get('id')}",
                    ord_info.get('order_date', today_str),
                    f"${ord_info.get('subtotal', 0.0):,.2f}",
                    display_status,
                ),
                tags=(row_tag,),
            )

if __name__ == "__main__":
    root = tk.Tk()
    app = PuzzleMasterControlPanel(root)

    # Auto-start PuzzleMaster.ca printing shortly after launch so the
    # scheduled Monday 7am task needs no manual clicking.  Etsy and Shopify
    # are started by hand.  Pass --no-autostart to open idle.
    if "--no-autostart" not in sys.argv:
        root.after(AUTO_START_INITIAL_MS, app.start_all)

    def on_close():
        app.shutdown()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
