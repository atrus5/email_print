import imaplib
import email
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
import logging.handlers
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

import cdp_connect

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
    'etsy_system', 'etsy_system_log.txt',
    '%(asctime)s - %(levelname)s - [order_id=%(order_id)s] - %(message)s'
)
success_logger = setup_logger(
    'etsy_success', 'etsy_success_log.txt',
    '%(asctime)s - [order_id=%(order_id)s] - %(message)s'
)

console = logging.StreamHandler()
console.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - [order_id=%(order_id)s] - %(message)s',
    '%Y-%m-%d %H:%M:%S'
))
console.addFilter(order_context)
logging.getLogger('etsy_system').addHandler(console)

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
@dataclass
class ShopConfig:
    """Configuration for one Etsy shop / email account."""
    name: str                   # Human label e.g. "PuzzleMaster" or "Puz"
    email_user: str
    email_pass: str
    cdp_port: int               # 9223 or 9224
    processed_file: str         # separate JSON per shop
    status_file: str            # separate status JSON per shop

@dataclass
class EtsyAppConfig:
    imap_server: str = field(default_factory=lambda: os.getenv('IMAP_SERVER', 'mail.hostedemail.com'))
    check_interval: int = field(default_factory=lambda: int(os.getenv('CHECK_INTERVAL', '300')))
    max_retries: int = field(default_factory=lambda: int(os.getenv('MAX_RETRIES', '3')))
    new_orders_tags: List[str] = field(default_factory=lambda: ['EtsyOrder', 'EtsyOrder'])
    fraud_tag: str = '$label3'
    subject_filter: str = 'You made a sale on Etsy'
    sender_filter: str = 'transaction@etsy.com'
    order_url_base: str = 'https://www.etsy.com/your/orders/'
    print_delay_seconds: float = 2.5
    page_timeout_ms: int = 30000

CONFIG = EtsyAppConfig()

# Build shop list from .env
SHOPS: List[ShopConfig] = [
    ShopConfig(
        name="PuzzleMaster",
        email_user=os.getenv('EMAIL_USER', ''),
        email_pass=os.getenv('EMAIL_PASS', ''),
        cdp_port=9223,
        processed_file='etsy_processed_puzzlemaster.json',
        status_file='etsy_status_puzzlemaster.json',
    ),
    ShopConfig(
        name="Puz",
        email_user=os.getenv('EMAIL_USER_2', ''),
        email_pass=os.getenv('EMAIL_PASS_2', ''),
        cdp_port=9224,
        processed_file='etsy_processed_puz.json',
        status_file='etsy_status_puz.json',
    ),
]

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

        Printing an order keeps this worker busy for minutes (Etsy page
        navigation and the print dialog), which is easily long enough for the
        IMAP server to close the idle socket. Storing flags on that dead
        connection raises IMAP4.abort ("an existing connection was forcibly
        closed by the remote host"), so retry once on a freshly established
        connection instead of losing the flag update.
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
# PERSISTENCE HELPERS (per-shop files)
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

def save_processed(shop: ShopConfig, order_id: str) -> None:
    orders = load_processed(shop)
    if order_id not in orders:
        orders.append(order_id)
        save_json(shop.processed_file, orders)

# ──────────────────────────────────────────────
# STATUS TRACKING (per-shop, mirrors order_print.py structure)
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
                        subtotal: Optional[float], order_date: Optional[str],
                        display_status: str = "printed") -> None:
    status = load_status(shop)
    now_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    target_date = order_date if order_date else datetime.now().strftime('%Y-%m-%d')

    if display_status != "spam":
        daily: Dict[str, int] = status.get("daily_counts", {})
        daily[target_date] = daily.get(target_date, 0) + 1
        cutoff = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        daily = {d: v for d, v in daily.items() if d >= cutoff}
        status["daily_counts"] = daily
        status["orders_total"] = status.get("orders_total", 0) + 1

        if subtotal is not None:
            daily_sales: Dict[str, float] = status.get("daily_sales", {})
            daily_sales[target_date] = round(daily_sales.get(target_date, 0.0) + subtotal, 2)
            daily_sales = {d: v for d, v in daily_sales.items() if d >= cutoff}
            status["daily_sales"] = daily_sales

    status["last_order"] = {"id": order_num, "time": now_time}
    recent: List[Dict[str, Any]] = status.get("recent_orders", [])
    recent.insert(0, {
        "id": order_num,
        "time": now_time,
        "status": display_status,
        "subtotal": subtotal if subtotal is not None else 0.0,
        "order_date": target_date
    })
    status["recent_orders"] = recent[:20]
    save_status(shop, status)

# ──────────────────────────────────────────────
# EMAIL HELPERS
# ──────────────────────────────────────────────
def extract_order_number_from_subject(subject: str) -> Optional[str]:
    """Pull order number from subject line: 'Order #4083610972'"""
    match = re.search(r'Order\s+#(\d+)', subject, re.IGNORECASE)
    return match.group(1) if match else None

def extract_subtotal_from_subject(subject: str) -> Optional[float]:
    """Pull total from subject line: '[US$30.64, Order #...]'"""
    match = re.search(r'US\$\s*([\d,]+\.?\d*)', subject, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def extract_order_date(msg: email.message.Message) -> str:
    date_header = msg.get('Date')
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(date_header)
            local_dt = dt.astimezone()  # convert to system local timezone
            return local_dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    return datetime.now().strftime('%Y-%m-%d')

def is_etsy_order_email(msg: email.message.Message) -> bool:
    """Verify this is a genuine Etsy sale notification."""
    subject = msg.get('Subject', '')
    sender = msg.get('From', '')
    return (CONFIG.subject_filter.lower() in subject.lower() and
            CONFIG.sender_filter.lower() in sender.lower())

# ──────────────────────────────────────────────
# BROWSER / PRINTING
# ──────────────────────────────────────────────
def print_etsy_order(context: BrowserContext, order_num: str, shop: ShopConfig) -> bool:
    """
    Navigate to the Etsy order page, click '...' -> Print,
    then click 'Print Order(s)' in the modal. Both packing slip
    and order receipt are checked by default — we leave them as-is.
    """
    url = f"{CONFIG.order_url_base}{order_num}"
    page: Optional[Page] = None
    try:
        page = context.new_page()
        system_logger.info(f"[{shop.name}] Navigating to {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=CONFIG.page_timeout_ms)

        # Give Etsy's React/JS a moment to finish rendering after DOM load
        time.sleep(3)
        system_logger.info(f"[{shop.name}] Page loaded for order #{order_num}. Looking for Order actions button...")

        # ── Step 1: Click the 'Order actions' button ──
        more_btn = page.locator(
            'button:has-text("Order actions"), '
            'button[aria-label*="Order actions" i], '
            'button[data-testid*="order-actions" i], '
            'button:has(span.wt-icon--base.etsy-icon)'
        ).first

        # --- LOCALIZED EXCEPTION HANDLING FOR THE BLOCKING OVERLAY ---
        try:
            more_btn.wait_for(state="visible", timeout=15000)
            # Try a standard natural click first
            more_btn.click(timeout=5000)
        except Exception as click_err:
            system_logger.warning(
                f"[{shop.name}] Standard click intercepted or timed out for order #{order_num}. "
                f"Error details: {click_err}"
            )
            
            # FALLBACK: focus the button and activate it via keyboard, since a
            # force=True pointer click bypasses the JS handler that expands the
            # menu (leaving items at tabindex="-1"/hidden)
            system_logger.info(f"[{shop.name}] Attempting keyboard-activation fallback...")
            more_btn.focus()
            more_btn.press("Enter")
            page.wait_for_timeout(500)
            if more_btn.get_attribute("aria-expanded") != "true":
                more_btn.press("Space")
        # --- END OF EXCEPTION HANDLING ---

        system_logger.info(f"[{shop.name}] Step 1 OK: Clicked 'Order actions' for order #{order_num}")

        # ── Step 2: Click 'Print' in the dropdown ──
        print_item = page.locator(
            'a:has-text("Print"), '
            'button:has-text("Print"), '
            '[role="menuitem"]:has-text("Print")'
        ).first
        try:
            print_item.wait_for(state="visible", timeout=10000)
        except Exception:
            count = page.locator(
                'a:has-text("Print"), button:has-text("Print"), [role="menuitem"]:has-text("Print")'
            ).count()
            system_logger.error(
                f"[{shop.name}] Step 2 FAILED: 'Print' menu item not visible after 10s "
                f"(matched {count} element(s))"
            )
            raise
        # Pointer clicks on menu items are blocked by the same overlay
        # ("panel-body" intercepts pointer events), so activate via keyboard
        try:
            print_item.click(timeout=5000)
        except Exception as click_err:
            system_logger.warning(
                f"[{shop.name}] Standard click on 'Print' intercepted for order #{order_num}: {click_err}"
            )
            print_item.focus()
            print_item.press("Enter")
        system_logger.info(f"[{shop.name}] Step 2 OK: Clicked Print menu item for order #{order_num}")

        # ── Step 3: Wait for the print modal ──
        print_orders_btn = page.locator(
            'button:has-text("Print Order(s)"), '
            'button:has-text("Print Orders")'
        ).first
        try:
            print_orders_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            count = page.locator(
                'button:has-text("Print Order(s)"), button:has-text("Print Orders")'
            ).count()
            system_logger.error(
                f"[{shop.name}] Step 3 FAILED: Print modal button not visible after 15s "
                f"(matched {count} element(s))"
            )
            raise

        system_logger.info(f"[{shop.name}] Step 3 OK: Print modal open for order #{order_num}. Clicking Print Order(s)...")

        # Clicking 'Print Order(s)' opens the printable document in a new tab
        # and calls window.print() there - capture that popup so we can wait
        # for it to load and confirm the print dialog actually opens.
        try:
            with page.context.expect_page(timeout=15000) as popup_info:
                print_orders_btn.click()
            print_page = popup_info.value
            print_page.wait_for_load_state("load", timeout=15000)
            system_logger.info(f"[{shop.name}] Print document loaded in new tab for order #{order_num}.")

            # Ctrl+P as a keypress doesn't reach Chrome's browser-level
            # shortcuts via CDP. Call window.print() directly instead.
            # With --kiosk-printing this silently prints to the default
            # printer (no dialog).
            print_page.wait_for_timeout(500)
            print_page.evaluate("window.print()")
            system_logger.info(f"[{shop.name}] Called window.print() for order #{order_num}.")

            # Give the print job a moment to be handed off to the spooler
            time.sleep(CONFIG.print_delay_seconds)

            print_page.close()
        except PlaywrightTimeoutError:
            system_logger.warning(
                f"[{shop.name}] No new tab detected after clicking Print Order(s) for order #{order_num}; "
                f"assuming print happened in-place."
            )
            time.sleep(CONFIG.print_delay_seconds)
        system_logger.info(f"[{shop.name}] Order #{order_num} sent to spooler.")
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
    """Check inbox for one shop and print any new Etsy orders."""
    system_logger.info(f"[{shop.name}] Checking for new Etsy orders...")

    # Health check: network
    try:
        socket.create_connection((CONFIG.imap_server, 993), timeout=5)
    except Exception as e:
        system_logger.warning(f"[{shop.name}] Network check failed: {e}")

    processed_list = load_processed(shop)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Update last_check timestamp
    status = load_status(shop)
    status["last_check"] = now_str
    save_status(shop, status)

    try:
        mail = handler.get_connection()
        mail.select("inbox")

        # Search for emails tagged as new orders (same tags as PuzzleMaster system)
        search_query = f'OR KEYWORD "{CONFIG.new_orders_tags[0]}" KEYWORD "{CONFIG.new_orders_tags[1]}"'
        status_code, messages = mail.search(None, search_query)
        if not messages[0]:
            system_logger.info(f"[{shop.name}] No tagged emails found.")
            return

        email_ids = messages[0].split()
        system_logger.info(f"[{shop.name}] Found {len(email_ids)} tagged email(s).")

        # Connect to this shop's dedicated Chrome instance. Use the explicit
        # IPv4 loopback: on Windows, "localhost" can resolve to ::1 first while
        # Chrome's debugging endpoint is listening only on 127.0.0.1.
        cdp_url = f"http://127.0.0.1:{shop.cdp_port}"
        with sync_playwright() as p:
            # Bounded connect: Playwright's own 180 s default stalls the whole
            # poll cycle when Chrome's debug endpoint hangs mid-handshake.
            browser, cdp_short, cdp_message = cdp_connect.connect(
                p, cdp_url, f"the {shop.name} Etsy browser"
            )
            if browser is None:
                system_logger.error(f"[{shop.name}] {cdp_message}")
                s = load_status(shop)
                s["last_error"] = f"{now_str}: {cdp_short}"
                save_status(shop, s)
                return

            # A successful connection means the previous overnight browser
            # failure is no longer current; do not leave a stale error in the UI.
            s = load_status(shop)
            if s.get("last_error") is not None:
                s["last_error"] = None
                save_status(shop, s)

            context = browser.contexts[0]

            for e_id in email_ids:
                order_context.order_id = str(uuid.uuid4())[:8]
                try:
                    # Re-validate the socket each iteration: the previous
                    # order's page work can outlast the server's idle timeout,
                    # and a stale connection would fail this fetch too.
                    mail = handler.get_connection()
                    _, flag_data = mail.fetch(e_id, '(FLAGS)')
                    flags = flag_data[0].decode()

                    _, msg_data = mail.fetch(e_id, '(RFC822)')
                    msg = email.message_from_bytes(msg_data[0][1])

                    # Only process genuine Etsy sale notifications
                    if not is_etsy_order_email(msg):
                        system_logger.info(f"[{shop.name}] Email {e_id} is not an Etsy sale notification — skipping.")
                        handler.mark_processed(e_id)
                        continue

                    subject = msg.get('Subject', '')
                    order_num = extract_order_number_from_subject(subject)
                    if not order_num:
                        system_logger.warning(f"[{shop.name}] Could not extract order number from subject: {subject!r}")
                        handler.mark_processed(e_id)
                        continue

                    subtotal = extract_subtotal_from_subject(subject)
                    order_date = extract_order_date(msg)

                    # Skip fraud-flagged emails
                    if CONFIG.fraud_tag in flags:
                        if order_num not in processed_list:
                            system_logger.info(f"[{shop.name}] Order #{order_num} flagged as fraud. Skipping.")
                            record_order_status(shop, order_num, subtotal, order_date, display_status="spam")
                            save_processed(shop, order_num)
                        handler.mark_processed(e_id)
                        continue

                    # Skip already-processed orders
                    if order_num in processed_list:
                        system_logger.info(f"[{shop.name}] Order #{order_num} already processed.")
                        handler.mark_processed(e_id)
                        continue

                    # Print the order
                    success = print_etsy_order(context, order_num, shop)
                    if success:
                        save_processed(shop, order_num)
                        record_order_status(shop, order_num, subtotal, order_date, display_status="printed")
                        handler.mark_processed(e_id)
                        success_logger.info(f"[{shop.name}] SUCCESS: Etsy order #{order_num} printed.")
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

    system_logger.info("=== Etsy Order Automation started ===")
    system_logger.info(f"Managing {len(SHOPS)} shop(s): {[s.name for s in SHOPS]}")

    TRIGGER_FILE = "etsy_trigger.txt"
    last_check_time = time.time() - CONFIG.check_interval  # Run immediately on first start

    # One IMAP handler per shop (different email credentials)
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

            if triggered or (current_time - last_check_time >= CONFIG.check_interval):
                if triggered:
                    try:
                        os.remove(TRIGGER_FILE)
                    except Exception:
                        pass

                # Process each shop in sequence
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
        system_logger.info("=== Etsy Order Automation stopped ===")
        time.sleep(1)