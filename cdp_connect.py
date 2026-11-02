"""Bounded, self-diagnosing CDP connection for the print workers.

`connect_over_cdp()` waits up to 180000 ms by default. Chrome's HTTP debug
endpoint can answer normally while its browser process is wedged (stuck print
spooler, resume from sleep, native dialog blocking the UI thread): Playwright
retrieves the websocket URL, connects, and then sits at "<ws connected>"
until the full 180 s expires, stalling the worker's whole poll cycle behind
it (this is the `CDP connection to http://localhost:9222 failed` incident).

This module bounds the handshake, retries once, and classifies the failure so
the operator knows what to do:

  * endpoint not answering  -> Chrome is closed; reopen it.
  * endpoint answers but the websocket handshake times out -> Chrome is hung;
    force-quit it and reopen it.

Each worker builds its own `cdp_url`, so the module is deliberately free of
worker-specific config.
"""

import os
import time
import urllib.request
from typing import Any, Optional, Tuple

# Bounded so one dead or hung Chrome can never stall a poll cycle anywhere
# near the library default of 180000 ms. Override with CDP_CONNECT_TIMEOUT_MS.
CONNECT_TIMEOUT_MS = int(os.getenv('CDP_CONNECT_TIMEOUT_MS', '15000'))
CONNECT_ATTEMPTS = 2
RETRY_DELAY_S = 2.0
PROBE_TIMEOUT_S = 5.0


def normalize_url(cdp_url: str) -> str:
    """Trim the URL and pin the loopback address.

    On Windows "localhost" can resolve to ::1 first while Chrome's debugging
    socket listens only on IPv4; the Etsy/Shopify workers already pin
    127.0.0.1 for exactly that reason.
    """
    url = (cdp_url or '').strip().rstrip('/')
    return url.replace('://localhost', '://127.0.0.1', 1)


def endpoint_alive(cdp_url: str) -> bool:
    """True if Chrome's /json/version answers within the probe timeout."""
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=PROBE_TIMEOUT_S) as response:
            response.read(4096)
        return True
    except Exception:
        return False


def connect(playwright: Any, cdp_url: str, browser_label: str = "the admin browser"
            ) -> Tuple[Optional[Any], Optional[str], Optional[str]]:
    """Attach to Chrome over CDP with a bounded timeout.

    Returns ``(browser, short_reason, log_message)``. On success the last two
    entries are None; on failure ``browser`` is None, ``short_reason`` is a
    one-line value suitable for status.json's ``last_error``, and
    ``log_message`` is the full operator-facing explanation.
    """
    url = normalize_url(cdp_url)
    last_error: Optional[Exception] = None
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            browser = playwright.chromium.connect_over_cdp(url, timeout=CONNECT_TIMEOUT_MS)
            return browser, None, None
        except Exception as e:
            last_error = e
            if attempt < CONNECT_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY_S)

    # Keep only the first line: the rest of Playwright's message is the
    # multi-line "Call log" of websocket steps that made the original error
    # unreadable.
    detail = str(last_error).splitlines()[0].strip() if last_error else "unknown error"
    timed_out = 'timeout' in detail.lower()
    alive = endpoint_alive(url)

    if timed_out and alive:
        short = f"Chrome hung at {url} (DevTools unresponsive)"
        message = (
            f"CDP connection to {url} failed. The endpoint answers HTTP but its DevTools "
            f"never completed the handshake, so {browser_label} is hung: force-quit it and "
            f"reopen it. ({detail})"
        )
    elif alive:
        short = f"Chrome not attachable at {url}"
        message = (
            f"CDP connection to {url} failed. The debug endpoint answers but Playwright "
            f"could not attach; reopen {browser_label}. ({detail})"
        )
    else:
        short = f"Chrome not running at {url}"
        message = (
            f"CDP connection to {url} failed. The debug endpoint is not answering, so "
            f"{browser_label} is closed: reopen it. ({detail})"
        )
    return None, short, message
