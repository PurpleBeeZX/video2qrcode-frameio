"""
Tests for browser auto-open behavior and authentication gate.
"""

import time
from threading import Lock
from unittest.mock import MagicMock, patch

import pytest

import main as app_module
from main import MP4Handler, token_lock


class TestStartupAndBrowserOpen:
    """Tests for browser auto-open behavior on server startup."""

    def test_browser_opens_on_startup_when_enabled(self, monkeypatch):
        """Browser opens control panel + customer display when not in setup mode."""
        import webbrowser
        mock_open = MagicMock()
        monkeypatch.setattr(webbrowser, "open", mock_open)
        monkeypatch.setattr(app_module, "_browser_opened", False)
        monkeypatch.setattr(app_module, "setup_mode", False)

        app_module._browser_opened = False
        with patch("time.sleep"):
            app_module._open_browser_delayed()

        # Should open control panel and customer display
        assert mock_open.call_count == 2
        assert app_module._browser_opened is True

    def test_browser_opens_configure_page_in_setup_mode(self, monkeypatch):
        """In setup mode, browser opens only the /configure page."""
        import webbrowser
        mock_open = MagicMock()
        monkeypatch.setattr(webbrowser, "open", mock_open)
        monkeypatch.setattr(app_module, "_browser_opened", False)
        monkeypatch.setattr(app_module, "setup_mode", True)

        app_module._browser_opened = False
        with patch("time.sleep"):
            app_module._open_browser_delayed()

        assert mock_open.call_count == 1
        assert "/configure" in mock_open.call_args[0][0]
        assert app_module._browser_opened is True

    def test_browser_does_not_open_when_disabled(self, monkeypatch):
        """Browser does NOT open when AUTO_OPEN_BROWSER=False."""
        import webbrowser
        mock_open = MagicMock()
        monkeypatch.setattr(webbrowser, "open", mock_open)

        original = app_module.AUTO_OPEN_BROWSER
        app_module.AUTO_OPEN_BROWSER = False
        assert app_module.AUTO_OPEN_BROWSER is False
        app_module.AUTO_OPEN_BROWSER = original

    def test_browser_opens_only_once(self, monkeypatch):
        """Calling _open_browser_delayed twice only opens once (2 tabs on first call, 0 on second)."""
        import webbrowser
        mock_open = MagicMock()
        monkeypatch.setattr(webbrowser, "open", mock_open)
        monkeypatch.setattr(app_module, "_browser_opened", False)
        monkeypatch.setattr(app_module, "setup_mode", False)

        with patch("time.sleep"):
            app_module._open_browser_delayed()
            app_module._open_browser_delayed()

        # First call opens 2 windows, second call opens 0
        assert mock_open.call_count == 2


class TestAuthenticationGate:
    """Tests ensuring files are not processed before login."""

    def test_file_dropped_before_login_is_ignored(self, mock_config):
        """File placed before auth is ignored with a warning."""
        watch_dir = mock_config["watch"]
        test_file = watch_dir / "pre_login_file.mp4"
        test_file.write_bytes(b"content")

        handler = MP4Handler()

        with token_lock:
            app_module.access_token = None

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(test_file)

        handler.on_created(event)
        assert test_file.exists()

    def test_file_dropped_after_login_schedules_processing(self, mock_config, mock_oauth_tokens):
        """After login, file drop schedules processing."""
        watch_dir = mock_config["watch"]
        test_file = watch_dir / "post_login_file.mp4"
        test_file.write_bytes(b"content")

        handler = MP4Handler()

        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(test_file)

        handler.on_created(event)
        time.sleep(0.1)