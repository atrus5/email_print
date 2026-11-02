#!/usr/bin/env python3
"""
DEPRECATED.

The Shopify channel has been merged into the main control panel at app_gui.py.
Please launch that script instead:

    python app_gui.py

Open the new 🟒 Shopify Printing tab there to:
  - open or close the Ornamentopia Shopify admin Chrome browser, and
  - start / stop / trigger the shopify_print.py background worker.

This file is kept for backwards compatibility only.
"""
import sys
import time


def main():
    print("=" * 70)
    print("DEPRECATION NOTICE")
    print("=" * 70)
    print()
    print("The standalone Shopify control panel (shopify_gui.py) has been")
    print("merged into the main control panel (app_gui.py).")
    print()
    print("Please launch:")
    print("    python app_gui.py")
    print()
    print("Use the new 🟒  \"Shopify Printing\" tab for Ornamentopia controls.")
    print()
    print("This file will exit in 5 seconds. Press Ctrl+C to quit immediately.")
    print("=" * 70)
    try:
        time.sleep(5)
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
