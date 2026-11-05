Install the Python libraries:
pip install -r requirements.txt

Install the Playwright browser drivers:
playwright install chromium

Setup a .env file from the env_template file.  You will need to change variables as needed to check your email address and host.  Change the subject line and email body information to reflect incoming order information.

## Optional: route Shopify prints to a different printer

By default, Shopify orders print to the OS default printer — the same one
shared with PuzzleMaster.ca. To send Shopify-only prints to a separate
printer (useful when the two businesses share the same Windows host but
have different stock/supplies workflow), install **SumatraPDF** (free, 6 MB)
from https://www.sumatrapdfreader.org/free-pdf-reader and add to .env:

```
SHOPIFY_TARGET_PRINTER=Leon - Brother HL-L5200DW series Printer
SHOPIFY_SUMATRA_PATH=C:\Program Files\SumatraPDF\SumatraPDF.exe
```

How it works: each order's documents are collected as PDF bytes and merged
to a PDF (`page.pdf()`) and dispatched via `SumatraPDF.exe -print-to
"PrinterName" -silent` so the job lands on the configured printer with
no UI dialog. The original Chrome kiosk `window.print()` path is kept as
the fallback when SumatraPDF is missing or the env var is empty, so
nothing breaks if you choose not to install it.

**Caveat:** When Order Printer Pro renders the preview *in-place*
inside its iframe (instead of a new tab), the iframe's own
`window.print()` still fires under `--kiosk-printing` toward the OS
default printer. Most orders use the new-tab path; if you need
guaranteed 100% reroute, configure Order Printer Pro to
"Open preview in new tab" in Shopify admin.

Merging the two documents into one job needs **PyMuPDF** (`pip install -r
requirements.txt`). Without it (or without PyMuPDF in a rebuilt .exe) the packing
slip and order page still print, but as two separate jobs in the same order.

## Quiet hours instead of a nightly shutdown

Checks only run while the shop is open. `QUIET_DAYS` (default `Mon-Fri`) lists
the days that print at all, and `QUIET_START` / `QUIET_END` (default `18:00` /
`07:45`) bound the nightly pause, so orders are fetched Monday to Friday
between 07:45 and 18:00 and not at all the rest of the time - including all of
Saturday and Sunday. The panel and every automation Chrome session stay open
throughout, so nothing has to be logged into again in the morning. A window may
cross midnight, "Trigger now" still runs a manual check at any hour, and
leaving `QUIET_START`/`QUIET_END` blank switches the feature off entirely. If a
scheduled night-time stop (EmailPrint_Stop / `Stop_OrderPrintAuto.bat`) is still
active, disable that task - otherwise it keeps closing everything down at the
old time.
