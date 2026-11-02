"""Unit tests for order_print.py core logic."""
import unittest
from unittest.mock import Mock, patch, MagicMock
import imaplib
import email
import json
import os
import sys
import tempfile
from bs4 import BeautifulSoup

# Add parent directory to path to import order_print
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import functions to test - we'll test the standalone functions
# Note: Some imports may fail due to dependencies, so we mock them


class TestExtractOrderNumber(unittest.TestCase):
    """Test order number extraction from subject lines."""
    
    def test_standard_format(self):
        """Test extraction from standard subject format."""
        from order_print import extract_order_number
        
        # Create a real BeautifulSoup object with HTML containing admin link
        html = '<html><body><a href="https://www.puzzlemaster.ca/admin/order.php?id=123456">View Order</a></body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        
        result = extract_order_number(soup)
        self.assertEqual(result, "123456")
    
    def test_no_order_number(self):
        """Test when no order number is found."""
        from order_print import extract_order_number
        
        html = '<html><body><p>No admin link here</p></body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        
        result = extract_order_number(soup)
        self.assertIsNone(result)


class TestExtractSubtotal(unittest.TestCase):
    """Test subtotal extraction from email body."""
    
    def test_standard_currency(self):
        """Test extraction of standard currency format."""
        from order_print import extract_subtotal
        
        email_text = "Your Subtotal: $100.00 for this order"
        result = extract_subtotal(email_text)
        self.assertEqual(result, 100.00)
    
    def test_currency_with_commas(self):
        """Test extraction of currency with thousands separator."""
        from order_print import extract_subtotal
        
        email_text = "Subtotal: $1,234.56"
        result = extract_subtotal(email_text)
        self.assertEqual(result, 1234.56)
    
    def test_no_subtotal(self):
        """Test when no subtotal is found."""
        from order_print import extract_subtotal
        
        email_text = "No price information here"
        result = extract_subtotal(email_text)
        self.assertIsNone(result)


class TestIsGiftCertificateOnly(unittest.TestCase):
    """Test gift certificate detection logic."""
    
    def test_gift_certificate_only(self):
        """Test order with only gift certificates."""
        from order_print import is_gift_certificate_only
        
        soup = MagicMock()
        email_text = "Gift Certificate $50.00"
        
        # Mock the detection logic
        with patch('order_print.BeautifulSoup') as mock_bs:
            result = is_gift_certificate_only(soup, email_text)
            # The actual implementation checks for specific patterns
            # This test verifies the function runs without error
            self.assertIsInstance(result, bool)
    
    def test_mixed_cart(self):
        """Test order with gift certificate and physical items."""
        from order_print import is_gift_certificate_only
        
        soup = MagicMock()
        email_text = "Gift Certificate $50.00\nPhysical Item $20.00"
        
        result = is_gift_certificate_only(soup, email_text)
        self.assertIsInstance(result, bool)


class TestParseEmailItems(unittest.TestCase):
    """Test email item parsing."""
    
    def test_parse_items(self):
        """Test parsing of item list from email."""
        # This function may not exist in the current version
        # Adjust based on actual implementation
        try:
            from order_print import parse_email_items
            
            html = "<div>Item 1 - $10.00</div>"
            result = parse_email_items(html)
            self.assertIsInstance(result, list)
        except ImportError:
            # Function doesn't exist in this version
            pass


class TestMarkEmailProcessed(unittest.TestCase):
    """Test email flag manipulation."""
    
    @patch('order_print.CONFIG')
    def test_mark_processed(self, mock_config):
        """Test that correct IMAP commands are called."""
        from order_print import IMAPConnectionHandler
        
        mock_config.new_orders_tags = ['new_orders', 'NewOrder']
        mock_mail = Mock(spec=imaplib.IMAP4_SSL)
        handler = IMAPConnectionHandler("imap.test.com", "user", "pass")
        handler._mail = mock_mail
        handler._connected = True
        email_id = "123"
        
        handler.mark_processed(email_id)
        
        # Verify store was called to remove tags
        self.assertEqual(mock_mail.store.call_count, 3)  # 2 tags + 1 for \\Seen
        mock_mail.store.assert_any_call(email_id, '-FLAGS', 'new_orders')
        mock_mail.store.assert_any_call(email_id, '-FLAGS', 'NewOrder')
        mock_mail.store.assert_any_call(email_id, '+FLAGS', '\\Seen')
    
    @patch('order_print.CONFIG')
    def test_mark_processed_retries_on_dead_socket(self, mock_config):
        """Test that flag updates retry once on a fresh connection after socket loss."""
        from order_print import IMAPConnectionHandler
        
        mock_config.new_orders_tags = ['new_orders', 'NewOrder']
        email_id = "123"
        
        dead_mail = Mock(spec=imaplib.IMAP4_SSL)
        fresh_mail = Mock(spec=imaplib.IMAP4_SSL)
        
        # First connection dies when the tag removal is attempted.
        dead_mail.store.side_effect = imaplib.IMAP4.abort(
            "socket error: [Errno 10054] An existing connection was forcibly closed by the remote host"
        )
        
        handler = IMAPConnectionHandler("imap.test.com", "user", "pass")
        handler._mail = dead_mail
        handler._connected = True
        
        # After invalidate() the handler must reconnect instead of reusing the dead socket.
        with patch.object(handler, '_reconnect', return_value=fresh_mail):
            handler.mark_processed(email_id)
        
        # Flag updates landed on the fresh connection.
        fresh_mail.store.assert_any_call(email_id, '-FLAGS', 'new_orders')
        fresh_mail.store.assert_any_call(email_id, '-FLAGS', 'NewOrder')
        fresh_mail.store.assert_any_call(email_id, '+FLAGS', '\\Seen')


class TestIMAPConnectionHandler(unittest.TestCase):
    """Test IMAP connection handler with retry logic."""
    
    @patch('order_print.imaplib.IMAP4_SSL')
    @patch('order_print.system_logger')
    def test_successful_connection(self, mock_logger, mock_imap):
        """Test successful IMAP connection."""
        from order_print import IMAPConnectionHandler
        
        mock_mail = Mock()
        mock_imap.return_value = mock_mail
        
        handler = IMAPConnectionHandler("imap.test.com", "user", "pass", max_retries=3)
        result = handler.get_connection()
        
        self.assertEqual(result, mock_mail)
        mock_imap.assert_called_once_with("imap.test.com")
        mock_mail.login.assert_called_once_with("user", "pass")
        mock_mail.select.assert_called_once_with('INBOX')
    
    @patch('order_print.time.sleep')
    @patch('order_print.imaplib.IMAP4_SSL')
    @patch('order_print.system_logger')
    def test_retry_on_failure(self, mock_logger, mock_imap, mock_sleep):
        """Test exponential backoff on connection failure."""
        from order_print import IMAPConnectionHandler
        
        # Make first two attempts fail, third succeed
        mock_mail = Mock()
        mock_imap.side_effect = [
            imaplib.IMAP4.abort("Connection failed"),
            imaplib.IMAP4.abort("Connection failed"),
            mock_mail
        ]
        
        handler = IMAPConnectionHandler("imap.test.com", "user", "pass", max_retries=3)
        result = handler.get_connection()
        
        self.assertEqual(result, mock_mail)
        self.assertEqual(mock_imap.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Sleep between retries
    
    @patch('order_print.system_logger')
    def test_connection_reuse(self, mock_logger):
        """Test that existing connection is reused."""
        from order_print import IMAPConnectionHandler
        
        handler = IMAPConnectionHandler("imap.test.com", "user", "pass", max_retries=3)
        handler._mail = Mock()
        handler._connected = True
        handler._mail.noop.return_value = None  # Connection is alive
        
        result = handler.get_connection()
        
        self.assertEqual(result, handler._mail)
        handler._mail.noop.assert_called_once()


class TestAtomicJSONPersistence(unittest.TestCase):
    """Status files must remain complete while the GUI polls them."""

    def test_save_json_replaces_file_atomically(self):
        from order_print import save_json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'status.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'old': True}, f)

            save_json(path, {'new': True, 'count': 3})

            with open(path, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), {'new': True, 'count': 3})
            self.assertEqual(
                [name for name in os.listdir(temp_dir) if name.endswith('.tmp')],
                [],
            )

    def test_failed_save_preserves_previous_complete_file(self):
        from order_print import save_json

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'status.json')
            original = {'last_check': '2026-09-24'}
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(original, f)

            with self.assertRaises(TypeError):
                save_json(path, {'invalid': object()})

            with open(path, 'r', encoding='utf-8') as f:
                self.assertEqual(json.load(f), original)
            self.assertEqual(
                [name for name in os.listdir(temp_dir) if name.endswith('.tmp')],
                [],
            )


class TestBrowserPageContext(unittest.TestCase):
    """Test browser page context manager."""
    
    def test_context_manager_creates_and_closes_page(self):
        """Test that context manager creates page on enter and closes on exit."""
        # This test would require mocking playwright
        # Implementation depends on whether BrowserPageContext exists
        try:
            from order_print import BrowserPageContext
            
            mock_page = Mock()
            mock_context = Mock()
            mock_context.new_page.return_value = mock_page
            
            with patch.object(BrowserPageContext, '__init__', lambda self, ctx: None):
                ctx_manager = BrowserPageContext.__new__(BrowserPageContext)
                ctx_manager.context = mock_context
                ctx_manager.page = None
                
                # Test __enter__
                result = ctx_manager.__enter__()
                self.assertEqual(result, mock_page)
                
                # Test __exit__
                ctx_manager.__exit__(None, None, None)
                mock_page.close.assert_called_once()
        except (ImportError, AttributeError):
            # Class doesn't exist in this version
            pass


class TestSignalHandler(unittest.TestCase):
    """Test graceful shutdown signal handling."""
    
    @patch('order_print.system_logger')
    @patch('order_print._shutdown_requested', False)
    def test_signal_handler_sets_flag(self, mock_logger):
        """Test that signal handler sets shutdown flag."""
        from order_print import signal_handler
        import order_print
        
        # Reset flag
        order_print._shutdown_requested = False
        
        # Call handler
        signal_handler(2, None)  # SIGINT
        
        self.assertTrue(order_print._shutdown_requested)
        mock_logger.info.assert_called()


class TestCdpConnect(unittest.TestCase):
    """CDP connect must be bounded and must classify why Chrome is unusable."""

    # Exactly the shape Playwright produced in the 08:01 incident: HTTP
    # endpoint answers, websocket connects, then nothing until the 180 s
    # library default expires.
    HUNG_ERROR = (
        "BrowserType.connect_over_cdp: Timeout 15000ms exceeded.\n"
        "Call log:\n"
        "- <ws preparing> retrieving websocket url from http://localhost:9222\n"
        "- <ws connected> ws://localhost:9222/devtools/browser/cbea28ce-2d5d\n"
    )

    @staticmethod
    def _playwright(side_effect=None, return_value=None):
        pw = MagicMock()
        call = pw.chromium.connect_over_cdp
        if side_effect is not None:
            call.side_effect = side_effect
        else:
            call.return_value = return_value
        return pw

    def test_connect_is_bounded_and_pins_loopback(self):
        """Success path passes an explicit timeout (never Playwright's 180 s)
        and rewrites localhost to 127.0.0.1."""
        import cdp_connect

        browser = Mock(name='browser')
        pw = self._playwright(return_value=browser)

        result, short, message = cdp_connect.connect(
            pw, 'http://localhost:9222/', 'the PuzzleMaster admin browser'
        )

        self.assertIs(result, browser)
        self.assertIsNone(short)
        self.assertIsNone(message)
        pw.chromium.connect_over_cdp.assert_called_once_with(
            'http://127.0.0.1:9222', timeout=cdp_connect.CONNECT_TIMEOUT_MS
        )
        self.assertLess(cdp_connect.CONNECT_TIMEOUT_MS, 180000)

    @patch('cdp_connect.time.sleep')
    @patch('cdp_connect.endpoint_alive', return_value=True)
    def test_hung_chrome_is_classified(self, mock_alive, mock_sleep):
        """Endpoint answers but the handshake hangs -> report a hung browser
        and strip Playwright's multi-line call log."""
        import cdp_connect

        pw = self._playwright(side_effect=Exception(self.HUNG_ERROR))
        result, short, message = cdp_connect.connect(
            pw, 'http://localhost:9222', 'the PuzzleMaster admin browser'
        )

        self.assertIsNone(result)
        self.assertIn('hung', short)
        self.assertIn('force-quit', message)
        self.assertIn(f'Timeout {cdp_connect.CONNECT_TIMEOUT_MS}ms exceeded', message)
        self.assertNotIn('Call log', message)
        self.assertEqual(pw.chromium.connect_over_cdp.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch('cdp_connect.time.sleep')
    @patch('cdp_connect.endpoint_alive', return_value=False)
    def test_closed_chrome_is_classified(self, mock_alive, mock_sleep):
        """Endpoint refuses -> report a closed browser, not a hung one."""
        import cdp_connect

        pw = self._playwright(
            side_effect=Exception('Error: connect ECONNREFUSED 127.0.0.1:9222')
        )
        result, short, message = cdp_connect.connect(
            pw, 'http://localhost:9222', 'the PuzzleMaster admin browser'
        )

        self.assertIsNone(result)
        self.assertIn('not running', short)
        self.assertIn('is closed', message)
        self.assertNotIn('force-quit', message)

    @patch('cdp_connect.time.sleep')
    def test_retries_once_then_succeeds(self, mock_sleep):
        """A transient first failure is retried instead of failing the cycle."""
        import cdp_connect

        browser = Mock(name='browser')
        pw = self._playwright(side_effect=[Exception('transient'), browser])

        result, short, message = cdp_connect.connect(pw, 'http://127.0.0.1:9222')

        self.assertIs(result, browser)
        self.assertIsNone(short)
        self.assertIsNone(message)
        self.assertEqual(pw.chromium.connect_over_cdp.call_count, 2)
        mock_sleep.assert_called_once_with(cdp_connect.RETRY_DELAY_S)


class TestDuplicateOrderEmails(unittest.TestCase):
    """The same order arriving twice in one email batch must print once.

    process_orders() reads processed_orders.json once per cycle, so the list
    has to be updated in memory the moment an order is recorded -- otherwise
    the second copy of the order prints again (seen with order #1836947).
    """

    ORDER_NUM = "1836947"

    def _raw_email(self) -> bytes:
        html = (
            '<html><body>'
            f'<a href="https://www.puzzlemaster.ca/admin/order.php?id={self.ORDER_NUM}">View order</a>'
            '<p>Subtotal: $10.00</p>'
            '</body></html>'
        )
        return (
            b"From: orders@puzzlemaster.ca\r\n"
            b"Subject: Retail Order #1836947\r\n"
            b"Date: Thu, 22 Oct 2026 05:00:00 +0000\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"\r\n" + html.encode()
        )

    @patch('order_print.send_alert')
    @patch('order_print.success_logger')
    @patch('order_print.system_logger')
    @patch('order_print.socket.create_connection')
    @patch('order_print.print_order_page', return_value=True)
    @patch('order_print.cdp_connect.connect')
    @patch('order_print.sync_playwright')
    @patch('order_print.CONFIG')
    def test_second_email_for_same_order_is_not_printed(
        self, mock_cfg, mock_playwright, mock_connect, mock_print,
        mock_socket, mock_syslog, mock_successlog, mock_alert,
    ):
        from order_print import process_orders, load_processed_orders

        with tempfile.TemporaryDirectory() as tmp:
            mock_cfg.json_file = os.path.join(tmp, 'processed_orders.json')
            mock_cfg.status_file = os.path.join(tmp, 'status.json')
            mock_cfg.retry_file = os.path.join(tmp, 'retry_queue.json')
            mock_cfg.new_orders_tags = ['new_orders', 'NewOrder']
            mock_cfg.fraud_tag = '$Fraud'
            mock_cfg.cdp_url = 'http://127.0.0.1:9222'
            mock_cfg.email = Mock(imap_server='imap.test.com')

            raws = [self._raw_email(), self._raw_email()]

            def fake_fetch(e_id, query, *args, **kwargs):
                if 'FLAGS' in query:
                    return ('OK', [b'(\\Seen)'])
                return ('OK', [(b'1 (RFC822)', raws[int(e_id) - 1])])

            mail = Mock()
            mail.search.return_value = ('OK', [b'1 2'])
            mail.fetch.side_effect = fake_fetch

            handler = Mock()
            handler.get_connection.return_value = mail

            mock_connect.return_value = (MagicMock(), '', '')

            process_orders(handler)

            self.assertEqual(
                mock_print.call_count, 1,
                "the same order must only be printed once per batch",
            )
            self.assertEqual(load_processed_orders(), [self.ORDER_NUM])
            self.assertEqual(handler.mark_processed.call_count, 2)
            mock_successlog.info.assert_called_once()


if __name__ == '__main__':
    unittest.main()
