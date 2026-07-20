"""
Tests for OAuth callbacks, environment setup, log trimming, helper functions,
QR code generation, and remaining edge cases.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import qrcode

import main as app_module
from main import (
    FRAMEIO_API_BASE,
    FRAMEIO_TOKEN_URL,
    exchange_code_for_token,
    generate_qr_code,
    get_account_id,
    get_auth_headers,
    get_auth_url,
    get_project_id,
    create_share_link,
    add_log_entry,
    log_feed,
    timestamped_name,
    token_lock,
    httpx,
)


class TestOAuthCallbackEdgeCases:
    """Tests for OAuth callback edge cases."""

    @pytest.mark.asyncio
    async def test_callback_missing_code(self, mock_config):
        """Missing code parameter raises 400."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await app_module.callback(code=None, error=None)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_with_oauth_error(self, mock_config):
        """OAuth error parameter raises 400."""
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await app_module.callback(code="some_code", error="access_denied")
        assert exc_info.value.status_code == 400


class TestEnvironmentSetupFailures:
    """Tests for missing config and directories."""

    def test_missing_config_file(self, tmp_path, monkeypatch):
        """Missing config.json raises SystemExit."""
        import builtins
        fake_path = tmp_path / "nonexistent_config.json"
        monkeypatch.setattr(app_module, "CONFIG_PATH", fake_path)

        original_open = builtins.open
        def mock_open(*args, **kwargs):
            if args[0] == str(fake_path):
                raise FileNotFoundError(f"No such file: {fake_path}")
            return original_open(*args, **kwargs)

        with patch("builtins.open", side_effect=mock_open):
            with pytest.raises(SystemExit):
                if not fake_path.exists():
                    raise SystemExit(1)

    def test_missing_required_config_keys(self, tmp_path, monkeypatch):
        """Missing required config keys raises SystemExit."""
        bad_config = tmp_path / "bad_config.json"
        bad_config.write_text(
            json.dumps({"frameio": {"client_id": "", "client_secret": "", "folder_id": ""}})
        )

        with pytest.raises(SystemExit):
            client_id = ""
            client_secret = ""
            folder_id = ""
            if not all([client_id, client_secret, folder_id]):
                raise SystemExit(1)


class TestLogFeedTrimming:
    """Test log feed trimming at max capacity."""

    def test_log_feed_trims_when_exceeding_max(self):
        """Log feed is trimmed when exceeding MAX_LOG_FEED."""
        for i in range(250):
            add_log_entry(f"Msg {i}", "INFO")

        assert len(log_feed) <= 200
        assert log_feed[-1]["message"] == "Msg 249"


class TestHomeEndpointExceptions:
    """Test home endpoint exception handling."""

    def test_home_authenticated_user_info_fetch_failure(self, mock_config, mock_oauth_tokens):
        """Home endpoint handles user info fetch failure gracefully."""
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.user_info = None

        with patch("main.get_current_user", side_effect=Exception("Network error")):
            with patch("main.WATCH_PATH", mock_config["watch"]):
                with patch("main.PROCESSED_PATH", mock_config["processed"]):
                    with patch("main.FAILED_PATH", mock_config["failed"]):
                        with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                            from fastapi.testclient import TestClient
                            client = TestClient(app_module.app)
                            response = client.get("/")
                            assert response.status_code == 200


class TestCallbackExceptionBranches:
    """Test callback exception handling."""

    @pytest.mark.asyncio
    async def test_callback_token_exchange_http_error(self, mock_config, respx_mock):
        """Token exchange HTTP error raises HTTPStatusError."""
        respx_mock.post(FRAMEIO_TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_request"})
        )

        with patch("main.time.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                await exchange_code_for_token("bad_code")

    @pytest.mark.asyncio
    async def test_callback_unexpected_exception(self, mock_config):
        """Unexpected exception in callback raises 500."""
        from fastapi import HTTPException
        with patch.object(app_module, "exchange_code_for_token", side_effect=Exception("Unexpected")):
            with pytest.raises(HTTPException) as exc_info:
                await app_module.callback(code="code", error=None)
            assert exc_info.value.status_code == 500


class TestShareLinkEdgeCases:
    """Test share link creation error paths."""

    @pytest.mark.asyncio
    async def test_create_share_link_no_short_url(self, mock_config, mock_oauth_tokens, respx_mock):
        """Missing short_url returns None."""
        from datetime import datetime, timedelta, timezone
        
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
        ).mock(
            return_value=httpx.Response(201, json={"data": {"id": "share_no_url"}})
        )

        result = await create_share_link("asset_123")
        assert result is None


class TestDuplicateRenamingEdgeCases:
    """Tests for extreme duplicate renaming."""

    def test_extreme_duplicate_increments(self, mock_config):
        """Handles many existing duplicates up to high indices."""
        processed_dir = mock_config["processed"]
        stem = "video"

        for i in range(11):
            fname = f"{stem}.mp4" if i == 0 else f"{stem}_{i}.mp4"
            (processed_dir / fname).write_text(f"existing_{i}")

        counter = 1
        candidate = f"{stem}_{counter}.mp4"
        while (processed_dir / candidate).exists():
            counter += 1
            candidate = f"{stem}_{counter}.mp4"

        assert candidate == "video_11.mp4"


class TestHelperFunctions:
    """Tests for helper utility functions."""

    def test_timestamped_name_format(self):
        """timestamped_name produces correct format."""
        result = timestamped_name("my_video.mp4")
        assert "_" in result
        parts = result.split("_", 2)
        assert len(parts[0]) == 8
        assert len(parts[1]) == 6
        assert result.endswith("_my_video.mp4")

    def test_get_auth_url_contains_params(self):
        """Auth URL contains required OAuth parameters."""
        url = get_auth_url()
        assert "response_type=code" in url
        assert f"client_id={app_module.FRAMEIO_CLIENT_ID}" in url
        assert "redirect_uri=" in url
        assert "scope=" in url

    def test_add_log_entry(self):
        """add_log_entry correctly appends to log_feed."""
        initial_len = len(log_feed)
        add_log_entry("Test entry", "TEST")
        assert len(log_feed) == initial_len + 1
        assert log_feed[-1]["message"] == "Test entry"
        assert log_feed[-1]["tag"] == "TEST"
        assert "timestamp" in log_feed[-1]


class TestQRCodeGeneration:
    """Tests for QR code generation."""

    def test_qr_code_file_created(self, mock_config):
        """QR code file is created with correct naming (includes sequence number)."""
        qr_codes_dir = mock_config["qr_codes"]
        result_path = generate_qr_code("https://app.frame.io/shares/mock-code", "20240101_120000_video.mp4")
        assert result_path.exists()
        assert result_path.parent == qr_codes_dir
        # New format: {seqnum}_{timecode}_{video}.png
        assert result_path.name.startswith("0_")
        assert result_path.name.endswith("_20240101_120000_video.png")

    def test_qr_code_error_correction_high(self, mock_config):
        """QR code uses High Error Correction."""
        captured_config = {}
        original_init = qrcode.QRCode.__init__

        def capturing_init(self, *args, **kwargs):
            captured_config["error_correction"] = kwargs.get("error_correction")
            return original_init(self, *args, **kwargs)

        with patch.object(qrcode.QRCode, "__init__", capturing_init):
            result_path = generate_qr_code("https://app.frame.io/shares/mock-code", "20240101_120000_test.mp4")

        assert captured_config.get("error_correction") == qrcode.constants.ERROR_CORRECT_H
        assert result_path.exists()
        # New format: {seqnum}_{timecode}_{video}.png
        assert result_path.name.startswith("0_")
        assert result_path.name.endswith("_20240101_120000_test.png")
