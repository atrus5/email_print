Prints out orders from an email address.  Finds the emails by subject line.  Prints out the orders from link in the email.  Records the email order number and subtotal of order using playwright.  Order is printed from chrome in kiosk mode and prints each order twice with a wait time inbetween printing.  Checks for new orders every 5 minutes
Dashboard for printing has order totals, daily totals, recent orders with order number that has a link to the item on the site, subtotal for each order.  Placed a button to print out orders manually instead of waiting.

Three channels: **PuzzleMaster.ca** (CAD), **Etsy (PuzzleMaster + Puz, USD)**,
and **Ornamentopia** (Shopify, CAD). Each has its own tab in the control
panel. Shopify prints can optionally be routed to a *separate* printer
via SumatraPDF — see `setup.md` for instructions.
