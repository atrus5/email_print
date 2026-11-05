import imaplib
import email
from email.header import decode_header, make_header
import time
import logging
import re
import os
import json
import traceback
import random
import signal
import socket
import uuid
import sys
import subprocess
import tempfile
import shutil
import urllib.request
import logging.handlers
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

import cdp_connect
import quiet_hours

load_dotenv()

# ──────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────
class OrderContextFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.order_id: Optional[str] = None

    def filter(self, record: logging.LogRecord) -> bool:
        record.order_id = self.order_id if self.order_id else "N/A"
        return True

order_context = OrderContextFilter()

def setup_logger(name: str, filename: str, fmt: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        filename, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter(fmt, '%Y-%m-%d %H:%M:%S'))
    handler.addFilter(order_context)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger

system_logger = setup_logger(
    'shopify_system', 'shopify_system_log.txt',
    '%(asctime)s - %(levelname)s - [order_id=%(order_id)s] - %(message)s'
)
success_logger = setup_logger(
    'shopify_success', 'shopify_success_log.txt',
    '%(asctime)s - [order_id=%(order_id)s] - %(message)s'
)

console = logging.StreamHandler()
console.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - [order_id=%(order_id)s] - %(message)s',
    '%Y-%m-%d %H:%M:%S'
))
console.addFilter(order_context)
logging.getLogger('shopify_system').addHandler(console)

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
@dataclass
class ShopConfig:
    """Configuration for the Shopify shop / email account."""
    name: str
    email_user: str
    email_pass: str
    cdp_port: int
    processed_file: str
    status_file: str
    admin_base_url: str    # e.g. https://e55ee8-2.myshopify.com/admin/orders/

@dataclass
class ShopifyAppConfig:
    imap_server: str = field(default_factory=lambda: os.getenv('SHOP_SMTP', 'mail.hostedemail.com'))
    check_interval: int = field(default_factory=lambda: int(os.getenv('CHECK_INTERVAL', '300')))
    max_retries: int = field(default_factory=lambda: int(os.getenv('MAX_RETRIES', '3')))
    new_orders_tags: List[str] = field(default_factory=lambda: ['ShopifyOrder', 'ShopifyOrder'])
    subject_filter: str = 'placed by'
    # Shopify moved merchant notifications to its own sending domain, so new
    # order emails now arrive from store+<id>@t.shopifyemail.com instead of the
    # store address. Comma-separated: any token matching the From wins.
    sender_filter: str = 'hello@ornamentopia.ca, store+81684496668@t.shopifyemail.com'
    admin_domain: str = 'e55ee8-2.myshopify.com'
    print_delay_seconds: float = 2.5
    page_timeout_ms: int = 30000
    # ── Custom printer routing for Shopify-only orders ──────────────────────────
    # Native Shopify print flows (Print packing slips / Print order page) open a
    # preview window whose PDF is dispatched to this printer via SumatraPDF.
    # When empty, prints fall back to the Chromium `window.print()` flow and end
    # up at the OS-default printer (which is shared with PuzzleMaster.ca).
    shopify_target_printer: str = field(
        default_factory=lambda: os.getenv(
            'SHOPIFY_TARGET_PRINTER', 'Leon - Brother HL-L5200DW series Printer'
        )
    )
    shopify_sumatra_path: str = field(
        default_factory=lambda: os.getenv(
            'SHOPIFY_SUMATRA_PATH', r'C:\Program Files\SumatraPDF\SumatraPDF.exe'
        )
    )

CONFIG = ShopifyAppConfig()


# ──────────────────────────────────────────────
# SUMATRA PDF DISCOVERY AND DISPATCH
# ──────────────────────────────────────────────
# SumatraPDF is a 6 MB free Windows PDF viewer whose CLI supports
#   SumatraPDF.exe -print-to "Printer Name" -silent file.pdf
# without ever showing a UI dialog. We use it to route rendered Shopify
# preview PDFs to a designated printer (separate from the OS default).
_SUMATRA_SEARCH_PATHS: List[str] = [
    r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
    # winget install (modern default): %LOCALAPPDATA%\Programs\SumatraPDF\
    os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "Programs", "SumatraPDF", "SumatraPDF.exe"),
    os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "SumatraPDF", "SumatraPDF.exe"),
    os.path.join(os.path.expandvars("%USERPROFILE%"), "SumatraPDF", "SumatraPDF.exe"),
]   


def find_sumatra(configured_path: Optional[str] = None) -> Optional[str]:
    """Locate SumatraPDF.exe by trying the configured path first, then common
    Windows install locations. Returns the absolute path or None if missing.
    Download: https://www.sumatrapdfreader.org/free-pdf-reader (free, 6 MB)."""
    candidates: List[str] = []
    if configured_path:
        candidates.append(configured_path)
    candidates.extend(_SUMATRA_SEARCH_PATHS)
    seen: set = set()
    for raw in candidates:
        norm = os.path.normpath(raw)
        if norm in seen or not norm:
            continue
        seen.add(norm)
        if os.path.isfile(norm):
            return norm
    return None


def print_pdf_via_sumatra(
    pdf_path: str,
    printer_name: str,
    sumatra_exe: str,
    print_settings: str = "",
) -> bool:
    """Send a PDF file to the named printer using `SumatraPDF -print-to -silent`.
    Captures stderr so the operator sees exactly why a print failed rather than
    a silent `window.print()` that ate the job. Returns True on success.
    print_settings: optional SumatraPDF -print-settings string, e.g. "1x,simplex"
    (1 copy, single-sided)."""
    try:
        cmd = [sumatra_exe, "-print-to", printer_name]
        if print_settings:
            cmd += ["-print-settings", print_settings]
        cmd.append("-silent")
        cmd.append(pdf_path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            system_logger.info(
                f"[Shopify] SumatraPDF dispatched '{os.path.basename(pdf_path)}' "
                f"to printer '{printer_name}' (exit 0)."
            )
            return True
        stderr_tail = (result.stderr or "").strip().splitlines()
        snippet = " | ".join(stderr_tail[-3:]) if stderr_tail else "(no stderr)"
        system_logger.warning(
            f"[Shopify] SumatraPDF exited {result.returncode} for printer "
            f"'{printer_name}': {snippet}"
        )
        return False
    except subprocess.TimeoutExpired:
        system_logger.warning(
            f"[Shopify] SumatraPDF timed out (>60s) for printer '{printer_name}'."
        )
        return False
    except FileNotFoundError:
        system_logger.warning(
            f"[Shopify] SumatraPDF missing at {sumatra_exe!r} (was it uninstalled?)."
        )
        return False
    except Exception as e:
        system_logger.warning(
            f"[Shopify] SumatraPDF dispatch failed for printer "
            f"'{printer_name}': {e}"
        )
        return False


# ──────────────────────────────────────────────
# STARTUP DIAGNOSTIC (logged once when this module is imported)
# ──────────────────────────────────────────────
# Surfaces in shopify_system_log.txt on first worker boot so the operator
# can verify routing will actually work — *before* the next order arrives —
# rather than learning at print-time when it's too late to swap printers.
# Placed here (after find_sumatra def) so the name is bound at module load.
_runtime_sumatra = find_sumatra(CONFIG.shopify_sumatra_path)
if not (CONFIG.shopify_target_printer or "").strip():
    system_logger.info(
        "[Shopify] Printer routing: disabled (SHOPIFY_TARGET_PRINTER empty) "
        "\u2192 Shopify orders will go to the OS-default printer."
    )
elif _runtime_sumatra:
    system_logger.info(
        f"[Shopify] Printer routing: target='{CONFIG.shopify_target_printer}' "
        f"via SumatraPDF at {_runtime_sumatra}."
    )
else:
    system_logger.warning(
        f"[Shopify] Printer routing REQUESTED for '{CONFIG.shopify_target_printer}' "
        f"but SumatraPDF not found at configured path "
        f"{CONFIG.shopify_sumatra_path!r} (or any common install dir). "
        f"Install from https://www.sumatrapdfreader.org/free-pdf-reader "
        f"\u2014 until then Shopify orders will fall back to the OS default printer."
    )

# ──────────────────────────────────────────────
# IN-PLACE PRINT INTERCEPTION (Order Printer Pro)
# ──────────────────────────────────────────────
# Order Printer Pro often renders its print preview inside an iframe and calls
# window.print() itself. Under --kiosk-printing that silently goes to the OS
# default printer (shared with PuzzleMaster.ca). We install this hook in every
# frame of the Shopify context BEFORE the OPP modal opens: it replaces
# window.print() with a no-op that flags the top window with the preview URL,
# so Playwright can render that preview to a PDF and dispatch it to the
# configured Shopify printer via SumatraPDF. A restore path re-fires the real
# print as a last-resort fallback.
_OPP_PRINT_HOOK_JS = r"""
(() => {
  if (window.__oppPrintHookInstalled) return;
  window.__oppPrintHookInstalled = true;
  window.__oppOriginalPrint = window.print.bind(window);

  if (window.top === window) {
    // Top frame: record captures from child frames (OPP preview iframes).
    window.__oppPrintCaptured = false;
    window.__oppPrintUrl = '';
    window.__oppCapturedSource = null;
    window.addEventListener('message', (e) => {
      const d = e.data;
      if (d && d.__oppPrintCaptured) {
        window.__oppPrintCaptured = true;
        window.__oppPrintUrl = d.url || '';
        window.__oppCapturedSource = e.source || null;
      }
    });
    window.print = function () {
      window.__oppPrintCaptured = true;
      window.__oppPrintUrl = location.href;
      window.__oppCapturedSource = null;
    };
    // Ask the frame that captured the print to re-fire its ORIGINAL print
    // (last-resort fallback so a failed PDF route still prints the order).
    window.__oppTriggerInPlacePrint = function () {
      if (window.__oppCapturedSource) {
        try { window.__oppCapturedSource.postMessage({ __oppTriggerOriginalPrint: true }, '*'); return; } catch (e) {}
      }
      if (window.__oppOriginalPrint) { window.__oppOriginalPrint(); }
    };
  } else {
    // Child frame (e.g. Order Printer Pro preview iframe): intercept print.
    window.print = function () {
      try { window.top.postMessage({ __oppPrintCaptured: true, url: location.href }, '*'); } catch (e) {}
    };
    window.addEventListener('message', (e) => {
      const d = e.data;
      if (d && d.__oppTriggerOriginalPrint && window.__oppOriginalPrint) {
        window.__oppOriginalPrint();
      }
    });
  }
  // Utility used by the new-tab flow so a fallback window.print() really prints.
  window.__oppRestorePrint = function () {
    window.print = window.__oppOriginalPrint;
  };
})();
"""


# Build shop config from .env
SHOP = ShopConfig(
    name="Ornamentopia",
    email_user=os.getenv('SHOP_USER', ''),
    email_pass=os.getenv('SHOP_PASS', ''),
    cdp_port=9225,
    processed_file='shopify_processed_ornamentopia.json',
    status_file='shopify_status_ornamentopia.json',
    admin_base_url=f'https://{CONFIG.admin_domain}/admin/orders/',
)
SHOPS: List[ShopConfig] = [SHOP]

# ──────────────────────────────────────────────
# IMAP CONNECTION HANDLER
# ──────────────────────────────────────────────
class IMAPConnectionHandler:
    def __init__(self, imap_server: str, email_user: str, email_pass: str, max_retries: int = 3):
        self.imap_server = imap_server
        self.email_user = email_user
        self.email_pass = email_pass
        self.max_retries = max_retries
        self._mail: Optional[imaplib.IMAP4_SSL] = None
        self._connected = False

    def get_connection(self) -> imaplib.IMAP4_SSL:
        if self._mail is not None and self._connected:
            try:
                self._mail.noop()
                return self._mail
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
                system_logger.warning(f"[{self.email_user}] IMAP connection lost, reconnecting...")
                self._connected = False
        return self._reconnect()

    def invalidate(self) -> None:
        """Drop the cached socket so the next get_connection() reconnects."""
        self._connected = False
        self._mail = None

    def mark_processed(self, email_id: str) -> None:
        """Remove the new-order tag(s) and flag the message as Seen.

        Printing an order keeps this worker busy for minutes (Shopify page
        navigation, PDF dispatch and 'printed' tag verification), which is
        easily long enough for the IMAP server to close the idle socket.
        Storing flags on that dead connection raises IMAP4.abort ("an existing
        connection was forcibly closed by the remote host"), so retry once on
        a freshly established connection instead of losing the flag update.
        """
        for attempt in (1, 2):
            mail = self.get_connection()
            try:
                for tag in CONFIG.new_orders_tags:
                    mail.store(email_id, '-FLAGS', tag)
                mail.store(email_id, '+FLAGS', '\\Seen')
                return
            except (imaplib.IMAP4.abort, OSError) as e:
                self.invalidate()
                if attempt == 2:
                    raise
                system_logger.warning(
                    f"[{self.email_user}] Flag update for email {email_id} "
                    f"failed ({e}); reconnecting and retrying..."
                )

    def _reconnect(self) -> imaplib.IMAP4_SSL:
        base_delay = 1.0
        max_delay = 60.0
        for attempt in range(1, self.max_retries + 1):
            try:
                system_logger.info(f"[{self.email_user}] Connecting to IMAP (attempt {attempt}/{self.max_retries})...")
                mail = imaplib.IMAP4_SSL(self.imap_server)
                mail.login(self.email_user, self.email_pass)
                mail.select('INBOX')
                self._mail = mail
                self._connected = True
                system_logger.info(f"[{self.email_user}] IMAP connected.")
                return mail
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as e:
                if attempt < self.max_retries:
                    delay = min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1), max_delay)
                    system_logger.warning(f"[{self.email_user}] IMAP failed: {e}. Retry in {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    system_logger.error(f"[{self.email_user}] IMAP failed after {self.max_retries} attempts: {e}")
                    raise
        raise ConnectionError(f"Failed IMAP connection for {self.email_user}")

    def close(self):
        if self._mail is not None:
            try:
                self._mail.logout()
                system_logger.info(f"[{self.email_user}] IMAP connection closed.")
            except Exception as e:
                system_logger.warning(f"[{self.email_user}] Error closing IMAP: {e}")
            finally:
                self._mail = None
                self._connected = False

# ──────────────────────────────────────────────
# PERSISTENCE HELPERS
# ──────────────────────────────────────────────
def load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return default
    return default

def save_json(path: str, data: Any) -> None:
    """Persist JSON atomically so GUI readers never see a partial file."""
    absolute_path = os.path.abspath(path)
    temp_path = os.path.join(
        os.path.dirname(absolute_path),
        f".{os.path.basename(absolute_path)}.{os.getpid()}.{uuid.uuid4().hex}.tmp",
    )
    try:
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, absolute_path)
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass

def load_processed(shop: ShopConfig) -> List[str]:
    return load_json(shop.processed_file, [])

def save_processed(shop: ShopConfig, order_num: str) -> None:
    orders = load_processed(shop)
    if order_num not in orders:
        orders.append(order_num)
        save_json(shop.processed_file, orders)

# ──────────────────────────────────────────────
# STATUS TRACKING
# ──────────────────────────────────────────────
def load_status(shop: ShopConfig) -> Dict[str, Any]:
    return load_json(shop.status_file, {
        "shop": shop.name,
        "last_check": None,
        "orders_total": 0,
        "daily_counts": {},
        "daily_sales": {},
        "last_order": None,
        "last_error": None,
        "recent_orders": []
    })

def save_status(shop: ShopConfig, status: Dict[str, Any]) -> None:
    save_json(shop.status_file, status)

def record_order_status(shop: ShopConfig, order_num: str,
                        total: Optional[float], order_date: Optional[str],
                        display_status: str = "printed") -> None:
    status = load_status(shop)
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    target_date = order_date if order_date else datetime.now().strftime('%Y-%m-%d')

    daily: Dict[str, int] = status.get("daily_counts", {})
    daily[target_date] = daily.get(target_date, 0) + 1
    cutoff = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
    daily = {d: v for d, v in daily.items() if d >= cutoff}
    status["daily_counts"] = daily
    status["orders_total"] = status.get("orders_total", 0) + 1

    if total is not None:
        daily_sales: Dict[str, float] = status.get("daily_sales", {})
        daily_sales[target_date] = round(daily_sales.get(target_date, 0.0) + total, 2)
        daily_sales = {d: v for d, v in daily_sales.items() if d >= cutoff}
        status["daily_sales"] = daily_sales

    status["last_order"] = {"id": order_num, "time": now_time}
    recent: List[Dict[str, Any]] = status.get("recent_orders", [])
    recent.insert(0, {
        "id": order_num,
        "time": now_time,
        "status": display_status,
        "subtotal": total if total is not None else 0.0,
        "order_date": target_date
    })
    status["recent_orders"] = recent[:20]
    save_status(shop, status)

# ──────────────────────────────────────────────
# EMAIL HELPERS
# ──────────────────────────────────────────────
def extract_order_number_from_subject(subject: str) -> Optional[str]:
    """Pull order number from subject line: '[Ornamentopia] Order #2196 placed by ...'"""
    match = re.search(r'Order\s+#(\d+)', subject, re.IGNORECASE)
    return match.group(1) if match else None

def extract_total_from_body(body_text: str) -> Optional[float]:
    """Pull the order total from the plain-text body, e.g. 'Total ... $125.46 CAD'"""
    match = re.search(r'Total\s*\$?\s*([\d,]+\.\d{2})\s*CAD', body_text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def extract_admin_order_id(body_text: str) -> Optional[str]:
    """Pull the numeric Shopify admin order ID from the 'View order' link."""
    match = re.search(r'myshopify\.com/admin/orders/(\d+)', body_text)
    return match.group(1) if match else None

def extract_order_date(msg: email.message.Message) -> str:
    date_header = msg.get('Date')
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(date_header)
            local_dt = dt.astimezone()
            return local_dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    return datetime.now().strftime('%Y-%m-%d')

def get_email_body_text(msg: email.message.Message) -> str:
    """Extract the plain-text (or HTML fallback) body for regex parsing."""
    if msg.is_multipart():
        text_part = None
        html_part = None
        for part in msg.walk():
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload is None:
                continue
            charset = part.get_content_charset() or 'utf-8'
            try:
                decoded = payload.decode(charset, errors='replace')
            except Exception:
                decoded = payload.decode('utf-8', errors='replace')
            if ctype == 'text/plain' and text_part is None:
                text_part = decoded
            elif ctype == 'text/html' and html_part is None:
                html_part = decoded
        return text_part or html_part or ''
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or 'utf-8'
            return payload.decode(charset, errors='replace') if payload else ''
        except Exception:
            return ''

def _decode_header_value(value: Optional[str]) -> str:
    """Decode an RFC2047 encoded header (=?UTF-8?B?...?=) to plain text.

    Substring filters never match an encoded Subject/From, which would make a
    genuine order notification look foreign, so decode before comparing.
    """
    if not value:
        return ''
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _sender_tokens() -> List[str]:
    """Accepted From substrings, comma-separated in CONFIG.sender_filter."""
    return [t.strip().lower() for t in CONFIG.sender_filter.split(',') if t.strip()]


def is_shopify_order_email(msg: email.message.Message) -> bool:
    """Verify this is a genuine Shopify new-order notification from Ornamentopia."""
    subject = _decode_header_value(msg.get('Subject', ''))
    sender = _decode_header_value(msg.get('From', ''))
    return (CONFIG.subject_filter.lower() in subject.lower() and
            any(token in sender.lower() for token in _sender_tokens()))

# ──────────────────────────────────────────────
# TAGGING
# ──────────────────────────────────────────────
def _apply_printed_tag(page: "Page", order_url: str, order_num: str, shop: ShopConfig) -> None:
    """One attempt at adding the 'printed' tag via the Add tags modal (UI flow).

    The current Shopify admin renders this as a full Polaris modal ('Add tags')
    with a checkbox per tag in the 'Available' list and a Cancel/Save footer.
    Reliability notes learned by live debugging:
      * Do NOT type in the tag-search box: the filtered autocomplete renders a
        different option row whose click does not toggle the checkbox, and the
        footer Save button becomes intermittently undetectable.
      * Click the checkbox INPUT itself (not the option row) and VERIFY/re-click
        until it reads checked -- row clicks have been observed leaving the box
        unchecked or toggling it back off ("checks then unchecks").
      * Click the modal's real 'Save' button. Pressing Enter in the search box
        closes the modal WITHOUT committing, which silently leaves the order
        untagged (the observed "selected but not tagged" symptom).
    """
    try:
        page.goto(order_url, wait_until="domcontentloaded", timeout=CONFIG.page_timeout_ms)

        # Open the Add tags modal. The new admin shows a 'Manage tags' button
        # (older UIs used an 'Edit tags' pencil). Retry until a dialog renders
        # -- the click occasionally reports a timeout while still landing.
        dialog = None
        for _ in range(3):
            try:
                # :visible is required -- Shopify renders hidden duplicate
                # 'Manage tags' buttons (one per section) and DOM order varies
                # between page loads, so an unfiltered .first can pin a hidden
                # one and the click silently never opens the modal.
                mgr = page.locator(
                    'button[aria-label="Manage tags"]:visible, '
                    'button[aria-label="Edit tags"]:visible'
                ).first
                mgr.wait_for(state="visible", timeout=5000)
                mgr.click(timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(900)  # let the modal render
            if page.locator('[role="dialog"]').count() > 0:
                dialog = page.locator('[role="dialog"]').first
                break
        if dialog is None:
            dialog = page.locator('[role="dialog"]').first
            dialog.wait_for(state="visible", timeout=8000)

        # Locate the 'printed' option row INSIDE the dialog (never page-wide:
        # an already-tagged order shows a 'printed' chip behind the modal that
        # would otherwise be clicked through the backdrop). Prefer an explicit
        # option role; fall back to exact-text + climb to the <li> row.
        printed_row = None
        try:
            printed_row = dialog.get_by_role('option', name='printed', exact=True).first
            printed_row.wait_for(state="visible", timeout=5000)
        except Exception:
            printed_row = None
        if printed_row is None:
            try:
                text_el = dialog.locator(':text-is("printed")').first
                text_el.wait_for(state="visible", timeout=5000)
                printed_row = text_el.locator(
                    'xpath=ancestor::li[contains(@class, "OptionList-Option")][1]'
                ).first
                if printed_row.count() == 0:
                    printed_row = text_el
            except Exception:
                # Last resort: the bare exact-text element inside the dialog.
                printed_row = dialog.locator(':text-is("printed")').first

        # Ensure the row's checkbox is CHECKED, verifying after every click.
        # Clicking the checkbox input directly (not the row) and looping until
        # it reads checked absorbs any toggle/re-render quirks.
        checkbox = printed_row.locator('input[type="checkbox"]').first
        checkbox.wait_for(state="visible", timeout=5000)
        for _ in range(4):
            try:
                if checkbox.is_checked():
                    break
                checkbox.click(timeout=3000)
                page.wait_for_timeout(300)
            except Exception:
                page.wait_for_timeout(300)
        if not checkbox.is_checked():
            system_logger.warning(
                f"[{shop.name}] Could not check the 'printed' checkbox for order #{order_num}."
            )

        # Save with the modal's real Save button. NO Enter fallback: Enter in
        # the tag search closes the modal without committing the tags.
        save_btn = dialog.locator('button:has-text("Save")').first
        save_btn.wait_for(state="visible", timeout=5000)
        save_btn.click(timeout=4000)

        # Give the modal a moment to close before the confirmation check.
        page.wait_for_timeout(600)
    except Exception as e:
        system_logger.warning(f"[{shop.name}] Could not tag order #{order_num} as 'printed': {e}")


def _confirm_printed_tag_saved(page: "Page", order_url: str, order_num: str, shop: ShopConfig) -> bool:
    """After clicking save, wait for the 'printed' chip to appear in the
    order's Tags section (server confirmation), then reload the order and
    confirm the tag persists. Returns True only when verified server-side."""
    try:
        # The Add tags modal must actually close after Save. If it is still
        # open, the save did not commit and its open 'printed' text would give
        # a false "chip appeared" reading below.
        for _ in range(12):
            if page.locator('[role="dialog"]').count() == 0:
                break
            page.wait_for_timeout(500)
        if page.locator('[role="dialog"]').count() > 0:
            system_logger.warning(
                f"[{shop.name}] Add tags modal did not close after Save for order #{order_num}."
            )
            return False
        # Server confirmation: the 'printed' chip now appears in the order's
        # Tags section (text engines pierce the section's shadow DOM).
        chip = page.locator(':text-is("printed")').first
        chip.wait_for(state="visible", timeout=8000)
        # Let the underlying save request fully commit before reloading (a
        # reload mid-save could abort the PUT and lose the tag).
        page.wait_for_timeout(2500)
    except Exception:
        system_logger.warning(
            f"[{shop.name}] 'printed' chip did not appear after save for order #{order_num}."
        )
        return False
    try:
        page.goto(order_url, wait_until="domcontentloaded", timeout=CONFIG.page_timeout_ms)
        page.locator(':text-is("printed")').first.wait_for(state="visible", timeout=8000)
        return True
    except Exception:
        system_logger.warning(
            f"[{shop.name}] 'printed' tag missing after reload for order #{order_num}."
        )
        return False


def tag_shopify_order_printed(page: "Page", order_url: str, order_num: str, shop: ShopConfig) -> None:
    """Add the 'printed' tag via the Tags widget and VERIFY the save actually
    persisted (reload + check); retries once if the first attempt didn't stick.
    Non-fatal: logs a warning on failure but does not raise."""
    for attempt in (1, 2):
        try:
            _apply_printed_tag(page, order_url, order_num, shop)
            if _confirm_printed_tag_saved(page, order_url, order_num, shop):
                system_logger.info(
                    f"[{shop.name}] Tagged order #{order_num} as 'printed' (verified)."
                )
                return
            system_logger.warning(
                f"[{shop.name}] Tag save for order #{order_num} not confirmed on "
                f"attempt {attempt}; retrying."
            )
        except Exception as e:
            system_logger.warning(
                f"[{shop.name}] Could not tag order #{order_num} as 'printed' "
                f"(attempt {attempt}): {e}"
            )
    system_logger.warning(
        f"[{shop.name}] Giving up tagging order #{order_num} as 'printed' after 2 attempts."
    )


def merge_pdf_parts(parts: List[bytes]) -> Optional[bytes]:
    """Concatenate validated PDF parts into ONE print job, in the given order.

    Returns None when PyMuPDF is unavailable or a part cannot be read, so the
    caller can fall back to one job per document instead of losing the order.
    """
    parts = [part for part in parts if part]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    try:
        import pymupdf  # PyMuPDF >= 1.24
    except ImportError:
        try:
            import fitz as pymupdf  # older PyMuPDF
        except ImportError:
            system_logger.warning(
                "[Shopify] PyMuPDF is not installed, so the order's documents cannot "
                "be merged into one print job; printing them as separate jobs."
            )
            return None
    try:
        merged = pymupdf.open()
        try:
            for part in parts:
                with pymupdf.open(stream=part, filetype="pdf") as source:
                    merged.insert_pdf(source)
            return merged.tobytes(garbage=3, deflate=True)
        finally:
            merged.close()
    except Exception as e:
        system_logger.warning(f"[Shopify] Merging the order's PDFs failed: {e}")
        return None


def dispatch_pdf_job(
    pdf_body: bytes, printer_name: str, sumatra_exe: str, label: str
) -> bool:
    """Write PDF bytes to a temp file and dispatch them to the named printer."""
    tmp_pdf: Optional[str] = None
    try:
        tmp_fd, tmp_pdf = tempfile.mkstemp(
            prefix="shopify_job_", suffix=".pdf", dir=tempfile.gettempdir()
        )
        os.close(tmp_fd)
        with open(tmp_pdf, "wb") as handle:
            handle.write(pdf_body)
        return print_pdf_via_sumatra(
            tmp_pdf, printer_name, sumatra_exe, print_settings="1x,simplex"
        )
    except Exception as e:
        system_logger.warning(
            f"[Shopify] Could not dispatch {label} to '{printer_name}': {e}"
        )
        return False
    finally:
        if tmp_pdf and os.path.exists(tmp_pdf):
            try:
                os.remove(tmp_pdf)
            except Exception:
                pass

def print_shopify_order(
    context: BrowserContext, order_num: str, admin_order_id: str, shop: ShopConfig,
) -> bool:
    """
    Navigate to the Shopify admin order page and print the order twice via
    Shopify's native flows: 'Print order page' and 'Print packing slips'. Each
    opens a preview window whose PDF is dispatched to the configured Shopify
    printer (single-sided) via SumatraPDF; falls back to window.print() otherwise.
    """
    url = f"{shop.admin_base_url}{admin_order_id}"
    page: Optional[Page] = None
    try:
        page = context.new_page()
        system_logger.info(f"[{shop.name}] Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=CONFIG.page_timeout_ms)

        # Wait for the order toolbar to render instead of burning a fixed 3.5s
        # on every order. Keep a timeout fallback for Shopify layout changes.
        toolbar_menu = page.locator(
            'button[aria-label="More actions"]:visible, '
            'button.menu-activator:not(.icon-only):visible'
        ).first
        try:
            toolbar_menu.wait_for(state="visible", timeout=4000)
        except PlaywrightTimeoutError:
            pass
        # A fresh tab normally starts at the top; this also handles a reused tab.
        page.evaluate("window.scrollTo(0, 0)")
        system_logger.info(f"[{shop.name}] Page loaded for order #{order_num}. Looking for Print dropdown...")

        # ── Step 1: Open the order-toolbar dropdown containing the print        #    options and print via Shopify's NATIVE flows ('Print order page'
        #    then 'Print packing slips'). Order Printer Pro is no longer used: its

        # preview renders in-place behind a blob: URL that Playwright cannot
        # re-render to PDF, so its print job silently vanished. Native previews
        # open a real window whose PDF we dispatch (single-sided) to the configured
        # Shopify printer via SumatraPDF.

        # The order page has MULTIPLE menu buttons. The one we want is in the
        # order header toolbar (next to 'Mark as fulfilled' / 'Create shipping
        # label' / 'Refund' / 'Edit'); its menu contains the Print section with
        # 'Print packing slips' / 'Print order page'. The customer contact card
        # ALSO has a 'More actions' button whose menu only shows 'Edit contact
        # information' / 'Edit shipping address' / 'Remove customer' - clicking
        # that one is the bug we're guarding against.

        def menu_has_print_options(p: Page, wanted_item: Optional[str] = None) -> bool:
            """True if the currently-open dropdown shows the requested print item."""
            checks = (
                '[role="menuitem"]:has-text("Print packing slips")',
                '.menu-item:has-text("Print packing slips")',
                'button:has-text("Print packing slips")',
                '[role="menuitem"]:has-text("Print order page")',
                '[role="menuitem"]:has-text("Order Printer Pro")',
                '[role="menuitem"].section:has-text("Print")',
            )
            if wanted_item:
                checks = (
                    f'.menu-item:visible:has-text("{wanted_item}")',
                    f'[role="menuitem"]:visible:has-text("{wanted_item}")',
                    f'button:visible:has-text("{wanted_item}")',
                )
            for sel in checks:
                loc = p.locator(sel)
                for i in range(loc.count()):
                    try:
                        if loc.nth(i).is_visible():
                            return True
                    except Exception:
                        continue
                # Give the dropdown a moment to render before giving up on it.
                try:
                    p.locator(sel).first.wait_for(state='visible', timeout=800)
                    return True
                except Exception:
                    continue
            return False

        def looks_like_menu_trigger(el) -> bool:
            """True for buttons that plausibly open a menu (kebab / 'More
            actions'), so we never click 'Mark as fulfilled' etc. by accident."""
            try:
                aria = (el.get_attribute('aria-label') or '').lower()
                txt = (el.inner_text(timeout=300) or '').lower().strip()
            except Exception:
                return False
            if 'more action' in aria or 'more action' in txt:
                return True
            if 'menu' in aria or 'menu' in txt:
                return True
            # Icon-only button (no text, no aria, has an SVG) = likely a kebab.
            if not txt and not aria:
                try:
                    if el.locator('svg').count() > 0:
                        return True
                except Exception:
                    pass
            return False

        def open_print_menu(wanted_item: str) -> bool:
            """Open the order toolbar menu and verify it contains print options.
            Returns True only when a print-capable menu is open. Closes wrong
            menus (e.g. the customer-contact 'More actions') with Escape."""
            # Shopify's current order page exposes the toolbar action by name.
            # Try it directly before scanning the much larger page DOM.
            preferred = page.locator('button[aria-label="More actions"]:visible')
            for i in range(preferred.count()):
                candidate = preferred.nth(i)
                try:
                    candidate.click(timeout=2000)
                    page.wait_for_timeout(250)
                except Exception:
                    continue
                if menu_has_print_options(page, wanted_item):
                    system_logger.info(
                        f"[{shop.name}] Opened order menu for order #{order_num}"
                    )
                    return True
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                except Exception:
                    pass

            candidates = []   # fallback locators for older Shopify layouts
            seen_coords = set()

            def add_candidate(el) -> None:
                try:
                    if not el.is_visible():
                        return
                    box = el.bounding_box()
                    key = (round(box['x']), round(box['y'])) if box else None
                    if key is not None:
                        if key in seen_coords:
                            return
                        seen_coords.add(key)
                except Exception:
                    pass
                candidates.append(el)

            # 1) Shopify's current order page uses a generic top toolbar
            #    button with class `menu-activator` for this print menu. It is
            #    distinct from the fulfillment/status action menus below.
            try:
                top_menu = page.locator(
                    'button.menu-activator:not(.icon-only):visible'
                )
                for i in range(top_menu.count()):
                    add_candidate(top_menu.nth(i))
            except Exception:
                pass

            # 2) Buttons inside the order header toolbar. The toolbar is the
            #    compact container (2-8 buttons) that holds 'Mark as fulfilled' /
            #    'Create shipping label' / 'Refund' / 'Edit'. Only keep buttons
            #    that look like menu triggers (kebab / 'More actions').
            try:
                anchor_btns = page.locator(
                    'button:has-text("Mark as fulfilled"), '
                    'button:has-text("Create shipping label"), '
                    'button:has-text("Refund"), '
                    'button:has-text("Edit"), '
                    'button:has-text("Print")'
                )
                for i in range(anchor_btns.count()):
                    anchor = anchor_btns.nth(i)
                    try:
                        container = anchor.locator(
                            'xpath=ancestor::div[count(.//button) > 1 and count(.//button) <= 8][1]'
                        )
                        if container.count() == 0:
                            continue
                        inner_btns = container.locator('button')
                        for j in range(inner_btns.count()):
                            b = inner_btns.nth(j)
                            if looks_like_menu_trigger(b):
                                add_candidate(b)
                    except Exception:
                        continue
            except Exception:
                pass

            # 2) Any button whose aria-label or text mentions "more action".
            try:
                for el in page.locator('button').all():
                    if looks_like_menu_trigger(el):
                        add_candidate(el)
            except Exception:
                pass

            if not candidates:
                return False

            for cand in candidates:
                try:
                    cand.click(timeout=3000)
                    page.wait_for_timeout(250)  # let the dropdown begin opening
                except Exception:
                    continue
                if menu_has_print_options(page, wanted_item):
                    system_logger.info(
                        f"[{shop.name}] Opened order menu (contains print options) for order #{order_num}"
                    )
                    return True
                # Wrong menu (e.g. the customer contact menu) - close it and try the next.
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                except Exception:
                    pass
            return False

        def click_print_item(item_text: str):
            """Click a print menu item ('Print packing slips' / 'Print order
            page') and return the popup Page that eventually loads the print
            document (or None).

            Shopify generates the print document SERVER-SIDE after the click,
            and the flow can open several about:blank windows before the real
            document window arrives. We watch the current attempt for a bounded
            period and return the first one carrying a real, printable URL.
            """
            baseline_pages = list(page.context.pages)
            baseline_urls: Dict[int, str] = {}
            for baseline_page in baseline_pages:
                try:
                    baseline_urls[id(baseline_page)] = (baseline_page.url or "").strip()
                except Exception:
                    baseline_urls[id(baseline_page)] = ""

            def cdp_targets() -> List[Dict[str, Any]]:
                """Read Chrome targets even when its PDF viewer is hidden from Playwright."""
                try:
                    endpoint = f"http://127.0.0.1:{shop.cdp_port}/json/list"
                    with urllib.request.urlopen(endpoint, timeout=1.5) as response:
                        payload = response.read().decode("utf-8", errors="replace")
                    parsed = json.loads(payload)
                    return parsed if isinstance(parsed, list) else []
                except Exception:
                    return []

            baseline_targets = {
                str(target.get("id")): (target.get("url") or "").strip()
                for target in cdp_targets()
                if target.get("id")
            }
            new_pages = []
            attempt_pages = []
            handler = lambda pg: new_pages.append(pg)

            def is_expected_pdf_url(candidate_url: str) -> bool:
                """Accept only this order's packing-slip document."""
                low = (candidate_url or "").lower()
                return (
                    item_text == "Print packing slips"
                    and str(order_num).lower() in low
                    and ("packing_slip" in low or "packing-slip" in low)
                )

            # Only observe popup creation here. Reading response.body() from a
            # Playwright request event can block while Chrome's PDF viewer is
            # still streaming the document, which leaves the visible black
            # viewer page and the mailbox worker stuck together. The dispatcher
            # downloads the final signed URL after it is known to be complete.
            page.context.on("page", handler)

            def close_attempt_pages() -> None:
                for pg in list(new_pages) + list(attempt_pages):
                    try:
                        if pg is not page and not pg.is_closed():
                            pg.close()
                    except Exception:
                        pass

            try:
                # Shopify's native order-page action calls window.print() on
                # the current order page instead of opening a PDF popup. Keep a
                # local marker so that action is rendered by Playwright below.
                if item_text == "Print order page":
                    page.evaluate("""
                        window.__shopifyNativePrintCalled = false;
                        window.__shopifyOriginalPrint = window.print;
                        window.print = function () {
                            window.__shopifyNativePrintCalled = true;
                        };
                        if (window.__oppPrintCaptured !== undefined) {
                            window.__oppPrintCaptured = false;
                        }
                    """)

                # Exact-label matching; section headers like 'Print' are never
                # matched because we target the full item label.
                clicked = False
                for selector in (
                    f'li:not(.section)[role="menuitem"]:visible:has-text("{item_text}") button',
                    f'button:visible:has-text("{item_text}")',
                    f'text="{item_text}"',
                    f'[role="menuitem"]:visible:has-text("{item_text}")',
                ):
                    try:
                        candidate = page.locator(selector).first
                        candidate.wait_for(state='visible', timeout=4000)
                        candidate.click(timeout=4000)
                        clicked = True
                        system_logger.info(f"[{shop.name}] Clicked '{item_text}' via: {selector}")
                        break
                    except Exception:
                        continue
                if not clicked:
                    # JS fallback restricted to VISIBLE menu items only.
                    clicked_txt = page.evaluate(
                        """() => {
                            const items = document.querySelectorAll('[role="menuitem"], .menu-item');
                            for (const item of items) {
                                if (item.classList.contains('section')) continue;
                                if (item.offsetParent === null) continue; // hidden menu
                                const text = (item.textContent || '').toLowerCase();
                                if (text.includes(TARGET)) {
                                    const btn = item.querySelector('button, a') || item;
                                    btn.click();
                                    return item.textContent.trim();
                                }
                            }
                            return null;
                        }""".replace("TARGET", repr(item_text.lower()))
                    )
                    if clicked_txt:
                        clicked = True
                        system_logger.info(f"[{shop.name}] JS clicked '{item_text}': {clicked_txt!r}")
                if not clicked:
                    raise PlaywrightTimeoutError(f"'{item_text}' not found in dropdown")

                # Wait for the document window. Only pages created by this
                # click are eligible; old print windows are deliberately
                # excluded so they cannot satisfy this attempt.

                if item_text == "Print order page":
                    # This action is the browser print flow, not a popup. The
                    # marker is set synchronously by Shopify after the click.
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        try:
                            called = page.evaluate(
                                "!!window.__shopifyNativePrintCalled || !!window.__oppPrintCaptured"
                            )
                        except Exception:
                            called = False
                        if called:
                            system_logger.info(
                                f"[{shop.name}] '{item_text}' requested the native "
                                f"browser print flow for order #{order_num}."
                            )
                            return page, (page.url or "").strip(), None
                        time.sleep(0.5)
                    system_logger.warning(
                        f"[{shop.name}] '{item_text}' did not invoke the browser "
                        f"print flow within 30s for order #{order_num}."
                    )
                    return None

                system_logger.info(
                    f"[{shop.name}] Waiting for '{item_text}' document window "
                    f"for order #{order_num} (server-side generation can take "
                    f"a few minutes)..."
                )
                # Shopify normally creates this PDF within seconds, but a failed
                # generation must not block the entire mailbox worker for minutes.
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    # Chrome's built-in PDF viewer can appear in /json/list but
                    # not as a Playwright Page. Accept only a target created or
                    # navigated by this click and this order, then download its
                    # signed URL without waiting for the black viewer to render.
                    if item_text == "Print packing slips":
                        for target in cdp_targets():
                            target_id = str(target.get("id") or "")
                            target_url = (target.get("url") or "").strip()
                            if not target_id or not is_expected_pdf_url(target_url):
                                continue
                            if (
                                target_id in baseline_targets
                                and target_url == baseline_targets[target_id]
                            ):
                                continue
                            system_logger.info(
                                f"[{shop.name}] '{item_text}' found signed PDF target "
                                f"through Chrome CDP: {target_url[:160]}"
                            )
                            # Close the viewer target immediately. The signed URL
                            # remains valid for the direct download in the
                            # dispatcher, and no black PDF tab is left behind.
                            try:
                                close_endpoint = (
                                    f"http://127.0.0.1:{shop.cdp_port}/json/close/"
                                    f"{target_id}"
                                )
                                urllib.request.urlopen(close_endpoint, timeout=1.5).close()
                            except Exception:
                                pass
                            return page, target_url, None

                    candidates = list(new_pages)
                    for pg in page.context.pages:
                        if pg in candidates:
                            continue
                        try:
                            current_url = (pg.url or "").strip()
                        except Exception:
                            current_url = ""
                        # Shopify may reuse an existing about:blank/PDF tab for
                        # the next print. Include pages whose URL changed after
                        # this click; excluding all baseline Page objects is what
                        # caused the second document to wait forever.
                        if (
                            id(pg) not in baseline_urls
                            or current_url != baseline_urls.get(id(pg), "")
                        ):
                            candidates.append(pg)
                    for candidate_page in candidates:
                        if candidate_page not in attempt_pages:
                            attempt_pages.append(candidate_page)
                    for pg in candidates:
                        try:
                            # A Shopify print popup can navigate to the signed
                            # PDF and close itself immediately. Playwright still
                            # exposes the final URL after close, so read the URL
                            # before rejecting the page as unusable.
                            u = (pg.url or "").strip()
                        except Exception:
                            continue
                        low = u.lower()
                        if not u or "about:blank" in low:
                            continue
                        if "oauth" in low or "authorize" in low:
                            system_logger.warning(
                                f"[{shop.name}] '{item_text}' for order #{order_num} "
                                f"redirected to an app authorization page; not printable "
                                f"from this browser ({u[:120]})."
                            )
                            close_attempt_pages()
                            continue
                        if "admin.shopify.com" in low or "myshopify.com/admin" in low:
                            continue  # admin chrome tab, not the print document
                        if not is_expected_pdf_url(u):
                            continue
                        system_logger.info(
                            f"[{shop.name}] '{item_text}' document window URL: {u[:160]}"
                        )
                        return pg, u, None
                    time.sleep(1)
                system_logger.warning(
                    f"[{shop.name}] '{item_text}' produced no printable document "
                    f"window within 60s for order #{order_num}; moving to the next "
                    f"mail item without blocking the worker."
                )
                close_attempt_pages()
                return None
            except PlaywrightTimeoutError:
                system_logger.warning(
                    f"[{shop.name}] '{item_text}' not found in the dropdown for "
                    f"order #{order_num}."
                )
                close_attempt_pages()
                return None
            finally:
                page.context.remove_listener("page", handler)

        def _legacy_window_print(print_page: Page, item_text: str) -> bool:
            """Original no-custom-printer behaviour: print the document with the
            browser's own print flow, which sends it to the OS-default printer.

            This path cannot be merged into one file, so it is only used when no
            Shopify printer is configured (SHOPIFY_TARGET_PRINTER empty) or
            SumatraPDF is missing.
            """
            try:
                if print_page is page:
                    print_page.evaluate(
                        """
                        if (window.__oppRestorePrint) {
                            window.__oppRestorePrint();
                        } else if (window.__shopifyOriginalPrint) {
                            window.print = window.__shopifyOriginalPrint;
                        }
                        window.print();
                        """
                    )
                else:
                    print_page.evaluate(
                        "window.__oppRestorePrint && window.__oppRestorePrint(); window.print()"
                    )
                time.sleep(CONFIG.print_delay_seconds)
                return True
            except Exception as e:
                system_logger.warning(
                    f"[{shop.name}] window.print() for '{item_text}' on order "
                    f"#{order_num} failed: {e}"
                )
                return False
            finally:
                try:
                    if print_page is not page and not print_page.is_closed():
                        print_page.close()
                except Exception:
                    pass

        def _validated_pdf_bytes(
            print_page: Page, item_text: str,
            pdf_body: Optional[bytes] = None,
            preview_url: Optional[str] = None,
        ) -> Optional[bytes]:
            """Return one native document (packing slip / order page) as VALIDATED
            PDF bytes, without printing it yet.

            Shopify generates the packing slip SERVER-SIDE and opens it at a signed
            shopify-private-shop-assets...googleapis.com/...pdf URL, so it is simply
            downloaded. 'Print order page' has no document of its own: it calls
            window.print() on the live order page, which Playwright renders to PDF.

            Both are checked here - magic bytes, size, pdftotext sanity, and the
            packing-slip/order-page cross-checks - before the caller merges every
            part of the order into a single print job.
            """
            try:
                is_inline_order_page = (
                    item_text == "Print order page" and print_page is page
                )
                preview_url = (preview_url or "").strip()
                if not preview_url:
                    try:
                        preview_url = (print_page.url or "").strip()
                    except Exception:
                        preview_url = ""
                if not is_inline_order_page and (
                    not preview_url or "about:blank" in preview_url.lower()
                ):
                    system_logger.warning(
                        f"[{shop.name}] '{item_text}' document window for order "
                        f"#{order_num} has no real URL; nothing to print."
                    )
                    return None

                if is_inline_order_page:
                    # The order page is a live DOM page, not a PDF: render it.
                    tmp_pdf: Optional[str] = None
                    try:
                        tmp_fd, tmp_pdf = tempfile.mkstemp(
                            prefix=f"shopify_{order_num}_order_page_", suffix=".pdf",
                            dir=tempfile.gettempdir(),
                        )
                        os.close(tmp_fd)
                        print_page.pdf(
                            path=tmp_pdf,
                            format="Letter",
                            print_background=True,
                            margin={
                                "top": "0.5in", "bottom": "0.5in",
                                "left": "0.5in", "right": "0.5in",
                            },
                        )
                        with open(tmp_pdf, "rb") as pdf_file:
                            pdf_body = pdf_file.read()
                        if not pdf_body.startswith(b"%PDF-") or len(pdf_body) < 5000:
                            raise RuntimeError(
                                f"order-page PDF is empty or invalid ({len(pdf_body)} bytes)"
                            )
                        pdftotext = shutil.which("pdftotext")
                        if pdftotext:
                            text_result = subprocess.run(
                                [pdftotext, "-layout", tmp_pdf, "-"],
                                capture_output=True,
                                text=True,
                                timeout=30,
                            )
                            pdf_text = (text_result.stdout or "").strip()
                            if text_result.returncode != 0 or len(pdf_text) < 50:
                                raise RuntimeError(
                                    f"order-page PDF contains too little text "
                                    f"({len(pdf_text)} chars)"
                                )
                            if order_num not in pdf_text:
                                raise RuntimeError(
                                    f"order-page PDF does not contain order #{order_num}"
                                )
                        system_logger.info(
                            f"[{shop.name}] Order #{order_num} '{item_text}' rendered "
                            f"({len(pdf_body)//1024} KB)."
                        )
                        return pdf_body
                    except Exception as e:
                        system_logger.warning(
                            f"[{shop.name}] Order #{order_num} '{item_text}' PDF render "
                            f"failed: {e}"
                        )
                        return None
                    finally:
                        if tmp_pdf and os.path.exists(tmp_pdf):
                            try:
                                os.remove(tmp_pdf)
                            except Exception:
                                pass

                # Server-side document: download the signed PDF and validate it.
                if pdf_body is None:
                    resp = context.request.get(preview_url, timeout=60000)
                    if resp.status != 200:
                        system_logger.warning(
                            f"[{shop.name}] '{item_text}' PDF download for order "
                            f"#{order_num} returned HTTP {resp.status}."
                        )
                        return None
                    body = resp.body()
                    ctype = (resp.headers.get("content-type") or "").lower()
                else:
                    body = pdf_body
                    ctype = "application/pdf"
                low_item = item_text.lower()
                low_url = preview_url.lower()
                if "pdf" not in ctype or not body.startswith(b"%PDF-") or len(body) < 5000:
                    system_logger.warning(
                        f"[{shop.name}] '{item_text}' download for order "
                        f"#{order_num} is not a valid PDF (type={ctype!r}, "
                        f"{len(body)} bytes) - refusing to print."
                    )
                    return None
                # Shopify's packing-slip endpoint is also what stale popup pages
                # exposed during the earlier implementation. Never let that
                # endpoint satisfy the distinct order-page job.
                is_packing_url = "packing_slip" in low_url or "packing-slip" in low_url
                if "order page" in low_item and is_packing_url:
                    system_logger.warning(
                        f"[{shop.name}] '{item_text}' for order #{order_num} resolved "
                        f"to a packing-slip PDF; refusing to print it as an order page."
                    )
                    return None
                if "packing slips" in low_item and not is_packing_url:
                    system_logger.warning(
                        f"[{shop.name}] '{item_text}' for order #{order_num} resolved "
                        f"to an unexpected document URL; refusing to print it."
                    )
                    return None
                # Optional pdftotext verification catches an HTTP 200 PDF that
                # contains no printable text, including blank output.
                pdftotext = shutil.which("pdftotext")
                if pdftotext:
                    tmp_pdf = None
                    try:
                        tmp_fd, tmp_pdf = tempfile.mkstemp(
                            prefix=f"shopify_{order_num}_{item_text[:4]}_", suffix=".pdf",
                            dir=tempfile.gettempdir(),
                        )
                        os.close(tmp_fd)
                        with open(tmp_pdf, "wb") as pdf_file:
                            pdf_file.write(body)
                        text_result = subprocess.run(
                            [pdftotext, "-layout", tmp_pdf, "-"],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        pdf_text = (text_result.stdout or "").strip()
                        if text_result.returncode != 0 or len(pdf_text) < 50:
                            system_logger.warning(
                                f"[{shop.name}] '{item_text}' PDF for order #{order_num} "
                                f"contains too little printable text ({len(pdf_text)} chars); "
                                f"refusing to dispatch it."
                            )
                            return None
                    finally:
                        if tmp_pdf and os.path.exists(tmp_pdf):
                            try:
                                os.remove(tmp_pdf)
                            except Exception:
                                pass
                system_logger.info(
                    f"[{shop.name}] Order #{order_num} '{item_text}' collected "
                    f"({len(body)//1024} KB)."
                )
                return body
            except Exception as e:
                system_logger.warning(
                    f"[{shop.name}] Order #{order_num} '{item_text}' PDF collection "
                    f"failed: {e}"
                )
                return None
            finally:
                try:
                    if print_page is not page and not print_page.is_closed():
                        print_page.close()
                except Exception:
                    pass

        # ── Print settings for this order ──
        # A configured Shopify printer means every part of the order can be
        # collected as PDF bytes and merged into ONE spool job (single-sided).
        # Without one there is no PDF route to merge, so the original
        # per-document window.print() flow (OS-default printer) is kept.
        target_printer = (CONFIG.shopify_target_printer or "").strip()
        sumatra_exe = find_sumatra(CONFIG.shopify_sumatra_path) if target_printer else None
        merged_route = bool(target_printer and sumatra_exe)

        job_parts: List[bytes] = []

        # ── Collect the native documents: packing slip, then order page ──
        # Packing slip FIRST, then the order page (the document with the order
        # total / invoice content). Nothing is printed yet: the whole order is
        # dispatched as one job only once every requested document validated, so
        # a half-printed order cannot be tagged as complete.
        dispatched_urls: set = set()
        native_parts: List[bytes] = []
        successful_items = 0
        for item_text in ("Print packing slips", "Print order page"):
            if not open_print_menu(item_text):

                # Save a screenshot so the next inspection can see the page state.
                try:
                    shot = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug_order_menu.png')
                    page.screenshot(path=shot)
                    system_logger.info(f"[{shop.name}] Saved debug screenshot to {shot}")
                except Exception:
                    pass
                raise PlaywrightTimeoutError(
                    "No order menu containing print options found "
                    f"while trying '{item_text}'"
                )
            preview = click_print_item(item_text)
            if preview is None:
                system_logger.error(
                    f"[{shop.name}] '{item_text}' was not produced for order "
                    f"#{order_num}; stopping before any later document is printed. "
                    f"The order will remain eligible for retry and will not be tagged."
                )
                break
            # Skip if this item returned a document we already printed for this
            # order (both items can resolve to the same signed PDF URL).
            try:
                preview, preview_url, preview_body = preview
            except (TypeError, ValueError):
                system_logger.error(
                    f"[{shop.name}] '{item_text}' returned an invalid preview result "
                    f"for order #{order_num}; stopping the order print."
                )
                break
            dup_url = (preview_url or "").strip()
            if dup_url and dup_url in dispatched_urls:
                system_logger.error(
                    f"[{shop.name}] '{item_text}' resolved to the same document "
                    f"already printed for order #{order_num}; stopping the order print."
                )
                try:
                    if not preview.is_closed():
                        preview.close()
                except Exception:
                    pass
                break
            if merged_route:
                part = _validated_pdf_bytes(
                    preview, item_text, pdf_body=preview_body, preview_url=preview_url
                )
                if part is None:
                    system_logger.error(
                        f"[{shop.name}] '{item_text}' was not produced for order "
                        f"#{order_num}; stopping before any later document is printed. "
                        f"The order will remain eligible for retry and will not be tagged."
                    )
                    break
                native_parts.append(part)
                successful_items += 1
            elif _legacy_window_print(preview, item_text):
                successful_items += 1
            if dup_url:
                dispatched_urls.add(dup_url)
            page.wait_for_timeout(250)  # allow the print action to settle

        documents_ready = (
            len(native_parts) == 2 if merged_route else successful_items == 2
        )
        if not documents_ready:
            system_logger.error(
                f"[{shop.name}] Order #{order_num} print incomplete: "
                f"{successful_items}/2 requested documents "
                f"{'collected' if merged_route else 'dispatched'}; "
                f"order will remain eligible for retry and will not be tagged."
            )
            return False

        if merged_route:
            # Packing slip first, then the order page.
            job_parts.extend(native_parts)
            merged_pdf = merge_pdf_parts(job_parts)
            merged_ok = bool(
                merged_pdf
                and dispatch_pdf_job(
                    merged_pdf, target_printer, sumatra_exe,
                    f"order #{order_num} ({len(job_parts)} documents merged)",
                )
            )
            if merged_ok:
                system_logger.info(
                    f"[{shop.name}] Order #{order_num}: {len(job_parts)} document(s) "
                    f"merged into one {len(merged_pdf)//1024} KB job -> "
                    f"'{target_printer}' (single-sided)."
                )
            else:
                # Never lose an order to a merge/dispatch problem: send the same
                # documents as individual jobs, in the same order.
                system_logger.warning(
                    f"[{shop.name}] Order #{order_num}: the merged job could not be "
                    f"sent; falling back to one job per document."
                )
                for index, part in enumerate(job_parts, 1):
                    sent = dispatch_pdf_job(
                        part, target_printer, sumatra_exe,
                        f"order #{order_num} document {index}/{len(job_parts)}",
                    )
                    system_logger.info(
                        f"[{shop.name}] Order #{order_num} document {index}/"
                        f"{len(job_parts)}: {'sent' if sent else 'FAILED'}."
                    )
        else:
            system_logger.info(
                f"[{shop.name}] Order #{order_num} sent to the OS-default printer "
                f"through the browser (2/2 documents)."
            )

        # ── Step 6: Tag the order as 'printed' in Shopify admin ──
        tag_shopify_order_printed(page, url, order_num, shop)

        return True

    except Exception as e:
        system_logger.error(f"[{shop.name}] Print failed for order #{order_num}: {e}", exc_info=True)
        return False
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass

# ──────────────────────────────────────────────
# PER-SHOP ORDER PROCESSOR
# ──────────────────────────────────────────────
def process_shop_orders(shop: ShopConfig, handler: IMAPConnectionHandler) -> None:
    """Check inbox for new Shopify order notifications and print them."""
    system_logger.info(f"[{shop.name}] Checking for new Shopify orders...")

    try:
        socket.create_connection((CONFIG.imap_server, 993), timeout=5)
    except Exception as e:
        system_logger.warning(f"[{shop.name}] Network check failed: {e}")

    processed_list = load_processed(shop)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    status = load_status(shop)
    status["last_check"] = now_str
    save_status(shop, status)

    try:
        mail = handler.get_connection()
        mail.select("inbox")

        search_query = f'OR KEYWORD "{CONFIG.new_orders_tags[0]}" KEYWORD "{CONFIG.new_orders_tags[1]}"'
        status_code, messages = mail.search(None, search_query)
        if not messages[0]:
            system_logger.info(f"[{shop.name}] No tagged emails found.")
            return

        email_ids = messages[0].split()
        system_logger.info(f"[{shop.name}] Found {len(email_ids)} tagged email(s).")

        # Chrome's remote-debugging endpoint is IPv4 on Windows even when
        # "localhost" resolves to ::1 first, so avoid intermittent refusals.
        cdp_url = f"http://127.0.0.1:{shop.cdp_port}"
        with sync_playwright() as p:
            # Bounded connect: Playwright's own 180 s default stalls the whole
            # poll cycle when Chrome's debug endpoint hangs mid-handshake.
            browser, cdp_short, cdp_message = cdp_connect.connect(
                p, cdp_url, "the Shopify admin browser"
            )
            if browser is None:
                system_logger.error(f"[{shop.name}] {cdp_message}")
                s = load_status(shop)
                s["last_error"] = f"{now_str}: {cdp_short}"
                save_status(shop, s)
                return

            s = load_status(shop)
            if s.get("last_error") is not None:
                s["last_error"] = None
                save_status(shop, s)

            context = browser.contexts[0]

            # Install the window.print interception hook so Order Printer Pro's
            # in-place preview print can be routed to the Shopify printer instead
            # of silently going to the OS default. Applies to every page/frame
            # created from here on (including the OPP preview iframe).
            try:
                context.add_init_script(_OPP_PRINT_HOOK_JS)
                system_logger.info(f"[{shop.name}] Installed in-place print interception hook.")
            except Exception as e:
                system_logger.warning(f"[{shop.name}] Could not install in-place print hook: {e}")

            for e_id in email_ids:
                order_context.order_id = str(uuid.uuid4())[:8]
                try:
                    # Re-validate the socket each iteration: the previous
                    # order's page work can outlast the server's idle timeout,
                    # and a stale connection would fail this fetch too.
                    mail = handler.get_connection()
                    _, msg_data = mail.fetch(e_id, '(RFC822)')
                    msg = email.message_from_bytes(msg_data[0][1])

                    if not is_shopify_order_email(msg):
                        system_logger.info(
                            f"[{shop.name}] Email {e_id} is not a Shopify order notification — "
                            f"skipping. From={_decode_header_value(msg.get('From', ''))!r} "
                            f"Subject={_decode_header_value(msg.get('Subject', ''))!r}"
                        )
                        handler.mark_processed(e_id)
                        continue

                    subject = _decode_header_value(msg.get('Subject', ''))
                    order_num = extract_order_number_from_subject(subject)
                    if not order_num:
                        system_logger.warning(f"[{shop.name}] Could not extract order number from subject: {subject!r}")
                        handler.mark_processed(e_id)
                        continue

                    body_text = get_email_body_text(msg)
                    total = extract_total_from_body(body_text)
                    admin_order_id = extract_admin_order_id(body_text)
                    order_date = extract_order_date(msg)

                    if not admin_order_id:
                        system_logger.warning(f"[{shop.name}] Could not extract admin order ID for order #{order_num}")
                        handler.mark_processed(e_id)
                        continue

                    if order_num in processed_list:
                        system_logger.info(f"[{shop.name}] Order #{order_num} already processed.")
                        handler.mark_processed(e_id)
                        continue

                    success = print_shopify_order(context, order_num, admin_order_id, shop)
                    if success:
                        save_processed(shop, order_num)
                        record_order_status(shop, order_num, total, order_date, display_status="printed")
                        handler.mark_processed(e_id)
                        success_logger.info(f"[{shop.name}] SUCCESS: Shopify order #{order_num} printed.")
                    else:
                        system_logger.error(f"[{shop.name}] Print failed for order #{order_num}. Will retry next cycle.")

                except Exception as e:
                    system_logger.error(f"[{shop.name}] Error on email ID {e_id}: {e}", exc_info=True)
                finally:
                    order_context.order_id = None

    except Exception as e:
        msg_text = f"[{shop.name}] SYSTEM ERROR: {e}\n{traceback.format_exc()}"
        system_logger.error(msg_text)

# ──────────────────────────────────────────────
# GRACEFUL SHUTDOWN
# ──────────────────────────────────────────────
_shutdown_requested = False

def signal_handler(signum: int, frame: Any) -> None:
    global _shutdown_requested
    sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
    system_logger.info(f"Shutdown signal ({sig_name}) received, finishing current cycle...")
    _shutdown_requested = True

# ──────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────
if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    system_logger.info("=== Shopify Order Automation started ===")
    system_logger.info(f"Managing {len(SHOPS)} shop(s): {[s.name for s in SHOPS]}")

    TRIGGER_FILE = "shopify_trigger.txt"
    last_check_time = time.time() - CONFIG.check_interval
    quiet_gate = quiet_hours.QuietGate(system_logger, "Shopify order checks")

    handlers: Dict[str, IMAPConnectionHandler] = {
        shop.name: IMAPConnectionHandler(
            CONFIG.imap_server,
            shop.email_user,
            shop.email_pass,
            CONFIG.max_retries
        )
        for shop in SHOPS
    }

    try:
        while not _shutdown_requested:
            current_time = time.time()
            triggered = os.path.exists(TRIGGER_FILE)

            if not triggered and quiet_gate.should_pause():
                # Quiet hours: fetch nothing overnight, but leave the panel,
                # the browsers and the print setup alive so the morning
                # needs no logins.
                last_check_time = current_time
                time.sleep(1)
                continue
            quiet_gate.note_manual_override(triggered)
            if quiet_gate.take_resumed():
                # First pass after the window closed: check now instead of
                # waiting out another full CHECK_INTERVAL.
                last_check_time = current_time - CONFIG.check_interval

            if triggered or (current_time - last_check_time >= CONFIG.check_interval):
                if triggered:
                    try:
                        os.remove(TRIGGER_FILE)
                    except Exception:
                        pass

                for shop in SHOPS:
                    if _shutdown_requested:
                        break
                    try:
                        process_shop_orders(shop, handlers[shop.name])
                    except Exception as e:
                        system_logger.error(f"Unhandled error processing shop {shop.name}: {e}", exc_info=True)

                last_check_time = time.time()

            time.sleep(1)

    except Exception as e:
        system_logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
    finally:
        for shop in SHOPS:
            handlers[shop.name].close()
        system_logger.info("=== Shopify Order Automation stopped ===")
        time.sleep(1)