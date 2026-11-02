import imaplib
import email
import time
import logging
import re
import os
import json
import smtplib
import traceback
import random
import shutil
import socket
import signal
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
from typing import List, Dict, Any, Optional, Tuple, TextIO
from dataclasses import dataclass, field
import sys
import uuid
import logging.handlers

import cdp_connect

load_dotenv()

# ──────────────────────────────────────────────
# LOGGING SETUP WITH ROTATION AND CORRELATION IDS
# ──────────────────────────────────────────────
class OrderContextFilter(logging.Filter):
    """Adds a correlation ID to log records for order tracking."""
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
    
    # Rotating file handler: 5MB max, keep 3 backups
    handler = logging.handlers.RotatingFileHandler(
        filename, 
        maxBytes=5*1024*1024, 
        backupCount=3,
        encoding='utf-8'
    )
    handler.setFormatter(logging.Formatter(fmt, '%Y-%m-%d %H:%M:%S'))
    handler.addFilter(order_context)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger

system_logger = setup_logger('system', 'system_log.txt', '%(asctime)s - %(levelname)s - [order_id=%(order_id)s] - %(message)s')
success_logger = setup_logger('success', 'success_log.txt', '%(asctime)s - [order_id=%(order_id)s] - %(message)s')

console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - [order_id=%(order_id)s] - %(message)s', '%Y-%m-%d %H:%M:%S'))
console.addFilter(order_context)
logging.getLogger('system').addHandler(console)

# ──────────────────────────────────────────────
# CONFIGURATION DATACLASSES
# ──────────────────────────────────────────────
@dataclass
class EmailConfig:
    """Email server and authentication configuration."""
    imap_server: str = field(default_factory=lambda: os.getenv('IMAP_SERVER', 'mail.hostedemail.com'))
    email_user: str = field(default_factory=lambda: os.getenv('EMAIL_USER', ''))
    email_pass: str = field(default_factory=lambda: os.getenv('EMAIL_PASS', ''))
    alert_email: str = field(default_factory=lambda: os.getenv('ALERT_EMAIL', ''))
    smtp_server: str = field(default_factory=lambda: os.getenv('SMTP_SERVER', 'smtp.gmail.com'))
    smtp_port: int = field(default_factory=lambda: int(os.getenv('SMTP_PORT', '587')))
    smtp_user: str = field(default_factory=lambda: os.getenv('SMTP_USER', ''))
    smtp_pass: str = field(default_factory=lambda: os.getenv('SMTP_PASS', ''))
    sender_filter: str = field(default_factory=lambda: os.getenv('SENDER_FILTER', 'info@puzzlemaster.ca'))
    subject_filter: str = field(default_factory=lambda: os.getenv('SUBJECT_FILTER', 'Retail Order #'))

@dataclass
class AppConfig:
    """Application-wide configuration settings."""
    email: EmailConfig = field(default_factory=EmailConfig)
    admin_base_url: str = field(default_factory=lambda: os.getenv('ADMIN_BASE_URL', 'https://www.puzzlemaster.ca/admin/order.php?id='))
    cdp_url: str = field(default_factory=lambda: os.getenv('CDP_URL', 'http://127.0.0.1:9222'))
    check_interval: int = field(default_factory=lambda: int(os.getenv('CHECK_INTERVAL', '300')))
    max_retries: int = field(default_factory=lambda: int(os.getenv('MAX_RETRIES', '3')))
    order_ttl_days: int = field(default_factory=lambda: int(os.getenv('ORDER_TTL_DAYS', '30')))
    print_copies: int = field(default_factory=lambda: int(os.getenv('PRINT_COPIES', '2')))
    json_file: str = 'processed_orders.json'
    retry_file: str = 'retry_queue.json'
    fraud_tag: str = '$label3'
    status_file: str = 'status.json'
    new_orders_tags: List[str] = field(default_factory=lambda: ['new_orders', 'NewOrder'])
    skip_gift_certificates_only: bool = True
    page_timeout_ms: int = 60000
    print_delay_seconds: float = 2.0
    chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    log_file_path: str = 'order_print.log'

# Global configuration instance
CONFIG = AppConfig()

# ──────────────────────────────────────────────
# SINGLE-INSTANCE GUARD (TCP port lock)
# ──────────────────────────────────────────────
# If two order_print.py workers are alive concurrently they will both pass the
# in-memory dedup check before either writes to processed_orders.json, so each
# new order ends up getting printed by BOTH workers. With PRINT_COPIES=2 each
# worker prints 2 copies, totaling 4 physical prints per order (the bug the
# user just hit). We bind TCP port LOCK_PORT below to a localhost socket —
# the OS kernel guarantees the bind() is atomic, so exactly one worker holds
# the port; any second worker's bind() raises OSError and we exit cleanly.
# We chose a TCP port lock (instead of a PID file) for two reasons:
#   1. socket is already imported here for the network-health probe — no new
#      dependency (no psutil).
#   2. There is no stale lock to clean up: when a worker dies (crash, Task
#      Manager End Process, kill -9), the kernel releases the port
#      automatically, so the next launch always succeeds.
LOCK_PORT = 49193  # High unregistered port; unlikely to clash with anything else.


def acquire_singleton_lock(logger: logging.Logger):
    """Try to bind 127.0.0.1:LOCK_PORT as our single-instance guard.

    Returns the bound socket on success — the caller MUST keep it referenced
    for the lifetime of the process so the OS keeps the port held. Returns
    None and logs an actionable error if another worker is already alive.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError as e:
        # Try to identify the offending PID so the operator has a one-shot
        # fix command. Windows-only: this whole codebase is Windows-targeted,
        # so we use `netstat -ano` and parse the PID column off the LISTENING
        # row for our lock port. Best-effort; if netstat fails we still
        # abort with a clear fallback message.
        other_pid = None
        if sys.platform == "win32":
            try:
                proc = subprocess.run(
                    ["netstat", "-ano"], capture_output=True, text=True, timeout=5
                )
                for line in proc.stdout.splitlines():
                    if f":{LOCK_PORT} " in line and "LISTENING" in line:
                        last = line.split()
                        if last and last[-1].isdigit():
                            other_pid = last[-1]
                            break
            except Exception:
                pass

        if other_pid:
            detail = (
                f"Conflicting worker PID: {other_pid} -> "
                f"run `taskkill /F /PID {other_pid}` to clear."
            )
        else:
            detail = (
                "Open Task Manager and End Process on the python.exe "
                "order_print.py row, or run `taskkill /F /PID <pid>`."
            )

        logger.error(
            f"ABORT: another order_print.py worker is already running "
            f"(TCP 127.0.0.1:{LOCK_PORT} is in use, "
            f"errno={getattr(e, 'errno', 'N/A')}). Refusing to start a "
            f"second instance -- running two workers would race the "
            f"dedup check and print every new order twice (4 physical "
            f"prints with PRINT_COPIES=2). {detail}"
        )
        try:
            sock.close()
        except Exception:
            pass
        return None
    logger.info(
        f"OK: singleton lock acquired on 127.0.0.1:{LOCK_PORT} (pid={os.getpid()})."
    )
    return sock


def release_singleton_lock(sock) -> None:
    """Release the single-instance guard. Safe to call on None."""
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        pass


# ──────────────────────────────────────────────
# IMAP CONNECTION HANDLER WITH RETRY LOGIC
# ──────────────────────────────────────────────
class IMAPConnectionHandler:
    """Manages persistent IMAP connection with exponential backoff retry logic."""
    
    def __init__(self, imap_server: str, email_user: str, email_pass: str, max_retries: int = 3):
        self.imap_server = imap_server
        self.email_user = email_user
        self.email_pass = email_pass
        self.max_retries = max_retries
        self._mail: imaplib.IMAP4_SSL | None = None
        self._connected = False
    
    def get_connection(self) -> imaplib.IMAP4_SSL:
        """Get active IMAP connection, reconnecting if necessary."""
        if self._mail is not None and self._connected:
            try:
                # Test connection with NOOP
                self._mail.noop()
                return self._mail
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
                system_logger.warning("IMAP connection lost, attempting reconnect...")
                self._connected = False
        
        return self._reconnect()
    
    def invalidate(self) -> None:
        """Drop the cached socket so the next get_connection() reconnects."""
        self._connected = False
        self._mail = None
    
    def mark_processed(self, email_id: str) -> None:
        """Remove the new-order tag(s) and flag the message as Seen.

        Printing an order keeps this worker busy for minutes (admin page
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
                    f"Flag update for email {email_id} failed ({e}); "
                    f"reconnecting and retrying..."
                )
    
    def _reconnect(self) -> imaplib.IMAP4_SSL:
        """Establish new IMAP connection with exponential backoff."""
        base_delay = 1.0
        max_delay = 60.0
        
        for attempt in range(1, self.max_retries + 1):
            try:
                system_logger.info(f"Connecting to IMAP server {self.imap_server} (attempt {attempt}/{self.max_retries})...")
                
                mail = imaplib.IMAP4_SSL(self.imap_server)
                mail.login(self.email_user, self.email_pass)
                mail.select('INBOX')
                
                self._mail = mail
                self._connected = True
                
                system_logger.info("IMAP connection established successfully.")
                return mail
                
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as e:
                if attempt < self.max_retries:
                    # Exponential backoff with jitter
                    delay = min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1), max_delay)
                    system_logger.warning(f"IMAP connection failed: {e}. Retrying in {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    system_logger.error(f"IMAP connection failed after {self.max_retries} attempts: {e}")
                    raise
        
        raise ConnectionError("Failed to establish IMAP connection")
    
    def close(self):
        """Close IMAP connection gracefully."""
        if self._mail is not None:
            try:
                self._mail.logout()
                system_logger.info("IMAP connection closed.")
            except Exception as e:
                system_logger.warning(f"Error closing IMAP connection: {e}")
            finally:
                self._mail = None
                self._connected = False

# ──────────────────────────────────────────────
# STATUS TRACKING
# ──────────────────────────────────────────────
def load_status() -> Dict[str, Any]:
    if os.path.exists(CONFIG.status_file):
        with open(CONFIG.status_file, 'r', encoding='utf-8') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return {
        "last_check": None,
        "orders_total": 0,
        "daily_counts": {},
        "daily_sales": {},
        "last_order": None,
        "last_error": None,
        "skipped_fraud": 0,
        "skipped_gc": 0,
        "recent_orders": []
    }

def save_status(status: Dict[str, Any]) -> None:
    save_json(CONFIG.status_file, status)

def record_order_status(order_num: str, subtotal: Optional[float] = None, order_date: Optional[str] = None, display_status: str = "printed") -> None:
    status = load_status()
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
    save_status(status)

# ──────────────────────────────────────────────
# AUTOMATED HOUSEKEEPING
# ──────────────────────────────────────────────
def run_monthly_housekeeping() -> None:
    global system_logger, success_logger
    housekeeping_marker = "last_cleanup_month.txt"
    current_month_str = datetime.now().strftime("%Y-%m")

    if os.path.exists(housekeeping_marker):
        with open(housekeeping_marker, 'r', encoding='utf-8') as f:
            last_month = f.read().strip()
        if last_month == current_month_str:
            return  # Already ran housecleaning for this month
            
    system_logger.info("=== Starting Automated Monthly Housekeeping ===")

    # 1. Optimize status.json
    if os.path.exists(CONFIG.status_file):
        try:
            with open(CONFIG.status_file, 'r', encoding='utf-8') as f:
                data: Dict[str, Any] = json.load(f)
            if "recent_orders" in data:
                data["recent_orders"] = data["recent_orders"][:20]
            save_json(CONFIG.status_file, data)
            system_logger.info(" -> status.json cache optimized successfully.")
        except Exception as e:
            system_logger.error(f"Housekeeping failed to optimize status.json: {e}")

    # 2. Archive flat files
    log_files = ['system_log.txt', 'success_log.txt']
    archive_dir = 'log_archives'
    os.makedirs(archive_dir, exist_ok=True)
    
    last_month_date = datetime.now().replace(day=1) - timedelta(days=1)
    archive_suffix = last_month_date.strftime("%Y_%m")

    for log_file in log_files:
        if os.path.exists(log_file):
            try:
                # Safely close handlers so Windows releases file locks
                for handler in logging.getLogger().handlers + system_logger.handlers + success_logger.handlers:
                    if isinstance(handler, logging.FileHandler) and handler.baseFilename.endswith(log_file):
                        handler.close()
                
                archive_name = os.path.join(archive_dir, f"{log_file.replace('.txt', '')}_{archive_suffix}.txt")
                if os.path.getsize(log_file) > 0:
                    os.rename(log_file, archive_name)
                open(log_file, 'w', encoding='utf-8').close() # Create fresh empty file
            except Exception as e:
                system_logger.warning(f"Could not archive {log_file}: {e}")

    # Re-establish clean active file logging hooks
    system_logger = setup_logger('system', 'system_log.txt', '%(asctime)s - %(levelname)s - %(message)s')
    success_logger = setup_logger('success', 'success_log.txt', '%(asctime)s - %(message)s')
    logging.getLogger('system').addHandler(console)
    
    system_logger.info("Fresh empty log streams established for the current month cycle.")
    with open(housekeeping_marker, 'w', encoding='utf-8') as f:
        f.write(current_month_str)
    system_logger.info("=== Monthly Housekeeping Cycle Completed ===")

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

def load_processed_orders() -> List[str]:
    return load_json(CONFIG.json_file, [])

def save_processed_order(order_id: str) -> None:
    orders = load_processed_orders()
    if order_id not in orders:
        orders.append(order_id)
        save_json(CONFIG.json_file, orders)

def load_retry_queue() -> List[Dict[str, Any]]:
    return load_json(CONFIG.retry_file, [])

def save_retry_queue(queue: List[Dict[str, Any]]) -> None:
    save_json(CONFIG.retry_file, queue)

def add_to_retry_queue(order_num: str, reason: str) -> None:
    queue = load_retry_queue()
    if not any(item['order'] == order_num for item in queue):
        queue.append({
            "order": order_num,
            "reason": reason,
            "attempts": 0,
            "added": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        save_retry_queue(queue)
        system_logger.warning(f"Added order #{order_num} to retry queue. Reason: {reason}")

# ──────────────────────────────────────────────
# EMAIL PARSING
# ──────────────────────────────────────────────
def get_html_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                return payload.decode(errors='replace') if payload else ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors='replace')
    return ""

def extract_order_number(soup: BeautifulSoup) -> Optional[str]:
    admin_link = next(
        (link['href'] for link in soup.find_all('a', href=True) if "puzzlemaster.ca/admin" in link['href']),
        None
    )
    if not admin_link:
        return None
    match = re.search(r'id=(\d+)', admin_link)
    return match.group(1) if match else None

def extract_subtotal(email_text: str) -> Optional[float]:
    match = re.search(r'Subtotal:\s*\$?([\d,]+\.?\d*)', email_text)
    if match:
        return float(match.group(1).replace(',', ''))
    return None

def extract_order_date(msg: email.message.Message, email_text: str) -> str:
    # 1. Try to get the date from the email headers (most reliable for "timestamp of the email")
    date_header = msg.get('Date')
    if date_header:
        try:
            dt = email.utils.parsedate_to_datetime(date_header)
            local_dt = dt.astimezone()  # convert to system local timezone
            return local_dt.strftime('%Y-%m-%d')
        except Exception:
            pass
            
    # 2. Fallback to parsing the email body text
    # Check for ISO numeric format first (e.g., "Order Date: 2024-05-20")
    match_iso = re.search(r'(?:Order\s+)?Date:\s*(\d{4}-\d{2}-\d{2})', email_text, re.IGNORECASE)
    if match_iso:
        return match_iso.group(1)

    # Check for text format (e.g., "January 5, 2024" or "Jan 5 2024")
    match_text = re.search(r'(?:Order\s+)?Date:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})', email_text, re.IGNORECASE)
    if match_text:
        date_str = match_text.group(1).replace(',', '').strip()
        for fmt in ['%B %d %Y', '%b %d %Y', '%Y-%m-%d']:
            try:
                return datetime.strptime(date_str, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
                
    # 3. Final fallback to current date (only if header and body parsing both fail)
    # We now log a warning so you can see exactly which emails are failing to parse
    system_logger.warning(f"Failed to parse date from email. Defaulting to today. Snippet: {email_text[:150]}")
    return datetime.now().strftime('%Y-%m-%d')

def is_gift_certificate_only(soup: BeautifulSoup, email_text: str) -> bool:
    gc_pattern = r"\(\d+\s*x\)\s*[$]\s*[\d,.]+\s*Gift Certificate\s+\(Size\s*:\s*[\d,.]+\)\s*:\s*at\s*[$][\d,.]+\s*each"
    all_item_pattern = r"\(\d+\s*x\)"
    gc_matches = re.findall(gc_pattern, email_text, re.IGNORECASE)
    all_item_markers = re.findall(all_item_pattern, email_text)
    return len(gc_matches) >= 1 and len(all_item_markers) == len(gc_matches)

# ──────────────────────────────────────────────
# EMAIL ALERT
# ──────────────────────────────────────────────
def send_alert(subject: str, body: str) -> None:
    if not all([ALERT_EMAIL, SMTP_USER, SMTP_PASS]):
        return
    try:
        msg = MIMEText(body)
        msg['Subject'] = f"[PuzzleMaster Bot] {subject}"
        msg['From'] = SMTP_USER
        msg['To'] = ALERT_EMAIL
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_EMAIL, msg.as_string())
        system_logger.info(f"Alert sent: {subject}")
    except Exception as e:
        system_logger.error(f"Failed to send alert email: {e}")

# ──────────────────────────────────────────────
# BROWSER / PRINTING
# ──────────────────────────────────────────────
class BrowserPageContext:
    """Context manager for browser page lifecycle management."""
    
    def __init__(self, context: BrowserContext, order_num: str, logger: logging.Logger):
        self.context = context
        self.order_num = order_num
        self.logger = logger
        self.page: Optional[Page] = None
    
    def __enter__(self) -> Page:
        self.page = self.context.new_page()
        return self.page
    
    def __exit__(self, exc_type: Optional[type], exc_val: Optional[Exception], exc_tb: Optional[Any]) -> bool:
        if self.page:
            try:
                self.page.close()
                self.logger.debug(f"Page for order #{self.order_num} closed successfully.")
            except Exception as e:
                self.logger.warning(f"Error closing page for order #{self.order_num}: {e}")
        return False  # Don't suppress exceptions

def hide_affiliate(page: Page) -> None:
    page.evaluate("""
    () => {
        document.querySelectorAll('strong').forEach(tag => {
            if (tag.innerText.includes('Affiliate:')) {
                tag.style.display = 'none';
                const next = tag.nextElementSibling;
                if (next && next.tagName === 'A') next.style.display = 'none';
            }
        });
    }
    """)

def print_order_page(context: BrowserContext, order_num: str) -> bool:
    try:
        with BrowserPageContext(context, order_num, system_logger) as page:
            url = f"{CONFIG.admin_base_url}{order_num}"
            page.goto(url, wait_until="networkidle", timeout=30000)
            hide_affiliate(page)
            
            for copy_num in range(1, CONFIG.print_copies + 1):
                page.evaluate("window.print()")
                system_logger.info(f"Order #{order_num} — copy {copy_num}/{CONFIG.print_copies} sent to spooler.")
                if copy_num < CONFIG.print_copies:
                    time.sleep(CONFIG.print_delay_seconds)
                    
            time.sleep(CONFIG.print_delay_seconds)
            return True
    except Exception as e:
        system_logger.error(f"Print failed for order #{order_num}: {e}")
        return False

# ──────────────────────────────────────────────
# MAIN ORDER PROCESSOR
# ──────────────────────────────────────────────
def process_orders(handler: IMAPConnectionHandler) -> None:
    """Process new orders with health checks and correlation IDs."""
    # Health Check: Disk Space
    try:
        total, used, free = shutil.disk_usage(".")
        if free < 100 * 1024 * 1024:  # Less than 100MB free
            system_logger.warning(f"Low disk space: {free // (1024*1024)}MB remaining")
    except Exception as e:
        system_logger.warning(f"Could not check disk space: {e}")
    
    # Health Check: Network connectivity
    try:
        socket.create_connection((CONFIG.email.imap_server, 993), timeout=5)
    except Exception as e:
        system_logger.warning(f"Network connectivity check failed: {e}")
    
    processed_list = load_processed_orders()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    current_status = load_status()
    current_status["last_check"] = now_str
    save_status(current_status)

    try:
        mail = handler.get_connection()
        mail.select("inbox")

        search_query = f'OR KEYWORD "{CONFIG.new_orders_tags[0]}" KEYWORD "{CONFIG.new_orders_tags[1]}"'
        status, messages = mail.search(None, search_query)
        if not messages[0]:
            return

        email_ids = messages[0].split()

        with sync_playwright() as p:
            # Bounded connect: Playwright's own 180 s default stalls the whole
            # poll cycle when Chrome's debug endpoint hangs mid-handshake.
            browser, cdp_short, cdp_message = cdp_connect.connect(
                p, CONFIG.cdp_url, "the PuzzleMaster admin browser"
            )
            if browser is None:
                system_logger.error(cdp_message)
                s = load_status()
                s["last_error"] = f"{now_str}: {cdp_short}"
                save_status(s)
                return

            s = load_status()
            if s.get("last_error") is not None:
                s["last_error"] = None
                save_status(s)

            context = browser.contexts[0]
            
            for e_id in email_ids:
                # Set correlation ID for this order cycle
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
                    html_content = get_html_body(msg)
                    soup = BeautifulSoup(html_content, 'html.parser')
                    email_text = soup.get_text()

                    subtotal = extract_subtotal(email_text)
                    order_date = extract_order_date(msg, email_text)
                    order_num = extract_order_number(soup)

                    if not order_num:
                        continue

                    if CONFIG.fraud_tag in flags:
                        if order_num not in processed_list:
                            system_logger.info(f"Order #{order_num} flagged as fraud/spam. Skipping and logging.")
                            s = load_status()
                            s["skipped_fraud"] = s.get("skipped_fraud", 0) + 1
                            save_status(s)
                            record_order_status(order_num, subtotal, order_date, display_status="spam")
                            save_processed_order(order_num)
                            processed_list.append(order_num)
                        handler.mark_processed(e_id)
                        continue

                    if order_num in processed_list:
                        handler.mark_processed(e_id)
                        continue

                    if is_gift_certificate_only(soup, email_text):
                        system_logger.info(f"Order #{order_num} is GC only. Recording and skipping print.")
                        s = load_status()
                        s["skipped_gc"] = s.get("skipped_gc", 0) + 1
                        save_status(s)
                        record_order_status(order_num, subtotal, order_date, display_status="gift_card")
                        save_processed_order(order_num)
                        processed_list.append(order_num)
                        handler.mark_processed(e_id)
                        continue

                    success = print_order_page(context, order_num)
                    if success:
                        save_processed_order(order_num)
                        # Keep the in-cycle dedup list in sync: processed_orders.json is
                        # only read once per cycle, so a second email carrying this same
                        # order would otherwise print it again (see order #1836947).
                        processed_list.append(order_num)
                        record_order_status(order_num, subtotal, order_date, display_status="printed")
                        handler.mark_processed(e_id)
                        success_logger.info(f"SUCCESS: Order #{order_num} printed.")
                    else:
                        add_to_retry_queue(order_num, "Print dialog failure")

                except Exception as e:
                    system_logger.error(f"Error on email ID {e_id}: {e}", exc_info=True)
                finally:
                    # Clear correlation ID after processing each email
                    order_context.order_id = None

    except Exception as e:
        msg = f"SYSTEM ERROR: {e}\n{traceback.format_exc()}"
        system_logger.error(msg)
        send_alert("Automation crashed", msg)

# ──────────────────────────────────────────────
# CORE LOOP INTERFACE ENGINE
# ──────────────────────────────────────────────
# Global shutdown flag
_shutdown_requested = False

def signal_handler(signum: int, frame: Any) -> None:
    """Handle shutdown signals gracefully."""
    global _shutdown_requested
    sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM" if signum == signal.SIGTERM else str(signum)
    system_logger.info(f"Shutdown signal ({sig_name}) received, finishing current cycle...")
    _shutdown_requested = True

if __name__ == "__main__":
    # Single-instance guard: refuse to start if another order_print.py is alive
    # locally. Cheap, atomic, and self-cleaning — see LOCK_PORT comment above.
    lock_socket = acquire_singleton_lock(system_logger)
    if lock_socket is None:
        # Brief pause so an operator who manually launched this sees the error
        # in the log before the terminal window / scrolled text scrolls away.
        time.sleep(2)
        sys.exit(1)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    system_logger.info("=== PuzzleMaster Order Automation started ===")
    TRIGGER_FILE = "trigger.txt"
    last_check_time = time.time()

    # Initialize IMAP connection handler
    handler = IMAPConnectionHandler(
        CONFIG.email.imap_server, 
        CONFIG.email.email_user, 
        CONFIG.email.email_pass, 
        CONFIG.max_retries
    )
    
    try:
        while not _shutdown_requested:
            try:
                run_monthly_housekeeping()
            except Exception as e:
                system_logger.error(f"Housekeeping exception caught: {e}")

            current_time = time.time()
            if (current_time - last_check_time >= CONFIG.check_interval) or os.path.exists(TRIGGER_FILE):
                if os.path.exists(TRIGGER_FILE):
                    try: 
                        os.remove(TRIGGER_FILE)
                    except Exception: 
                        pass
                process_orders(handler)
                last_check_time = time.time()
            
            time.sleep(1)  # Prevent CPU spinning
    
    except Exception as e:
        system_logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
    finally:
        handler.close()
        release_singleton_lock(lock_socket)
        system_logger.info("=== PuzzleMaster Order Automation stopped ===")
        time.sleep(1)