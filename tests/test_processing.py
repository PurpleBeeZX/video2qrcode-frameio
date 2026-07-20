"""
Tests for file system race conditions, token expiry/refresh, and S3 chunked uploads.
"""

import time
import pytest
from threading import Lock
from unittest.mock import MagicMock, patch

import httpx
import main as app_module
from main import (
    FRAMEIO_API_BASE,
    FRAMEIO_TOKEN_URL,
    process_single_file,
    refresh_access_token,
    token_lock,
)


class TestFileSystemRaceConditions:
    """Tests for handling files deleted during processing."""

    def test_file_deleted_during_stabilization(self, mock_config):
        """File deleted during stabilization is handled gracefully."""
        watch_dir = mock_config["watch"]
        test_file = watch_dir / "fleeting.mp4"
        test_file.write_bytes(b"temporary content")

        handler = app_module.MP4Handler()

        event = MagicMock()
        event.is_directory = False
        event.src_path = str(test_file)

        handler.on_created(event)
        test_file.unlink()
        delay = app_module.get_config().stabilization_delay
        time.sleep(delay + 1)

        assert len(list(watch_dir.glob("*.mp4"))) == 0
        assert len(list(mock_config["processed"].glob("*.mp4"))) == 0

    def test_unsupported_file_types_ignored(self, mock_config):
        """Non-MP4 files are ignored by the watcher."""
        watch_dir = mock_config["watch"]
        handler = app_module.MP4Handler()

        txt_file = watch_dir / "notes.txt"
        png_file = watch_dir / "image.png"
        txt_file.write_text("some text")
        png_file.write_bytes(b"PNGDATA")

        txt_event = MagicMock()
        txt_event.is_directory = False
        txt_event.src_path = str(txt_file)

        png_event = MagicMock()
        png_event.is_directory = False
        png_event.src_path = str(png_file)

        handler.on_created(txt_event)
        handler.on_created(png_event)
        delay = app_module.get_config().stabilization_delay
        time.sleep(delay + 1)

        assert txt_file.exists()
        assert png_file.exists()
        assert len(list(mock_config["processed"].glob("*.mp4"))) == 0


class TestTokenExpiryAndRefresh:
    """Tests for token expiry and refresh logic."""

    @pytest.mark.asyncio
    async def test_token_refresh_on_401_during_upload(self, mock_config, mock_oauth_tokens, respx_mock):
        """Token refresh happens automatically on 401."""
        from datetime import datetime, timedelta, timezone
        
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        watch_dir = mock_config["watch"]
        test_file = watch_dir / "expire_test.mp4"
        test_file.write_bytes(b"x" * 100)

        respx_mock.post(FRAMEIO_TOKEN_URL).mock(
            side_effect=[httpx.Response(200, json=mock_oauth_tokens)]
        )
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
        ).mock(
            side_effect=[
                httpx.Response(401, text="Unauthorized"),
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "file-after-refresh",
                            "name": "expire_test.mp4",
                            "file_size": 100,
                            "upload_urls": [
                                {
                                    "size": 100,
                                    "url": "https://s3.amazonaws.com/parts/file-after-refresh/part_1",
                                }
                            ],
                        }
                    },
                ),
            ]
        )
        respx_mock.put("https://s3.amazonaws.com/parts/file-after-refresh/part_1").mock(
            return_value=httpx.Response(200)
        )
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "data": {
                        "id": "share-refresh",
                        "short_url": "https://app.frame.io/shares/refresh",
                    }
                },
            )
        )

        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': test_file.name,
                'name': test_file.name,
            })

        assert not test_file.exists()
        assert len(list(mock_config["processed"].glob("*.mp4"))) == 1

    @pytest.mark.asyncio
    async def test_refresh_token_failure(self, mock_config, respx_mock):
        """Refresh token failure returns None and keeps old token."""
        from datetime import datetime, timedelta, timezone
        
        with token_lock:
            app_module.access_token = "expired_token"
            app_module.refresh_token = "bad_refresh_token"
            app_module.token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        respx_mock.post(FRAMEIO_TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )

        result = await refresh_access_token()
        assert result is None
        with token_lock:
            assert app_module.access_token == "expired_token"


class TestS3ChunkedUploadRetries:
    """Tests for S3 chunked upload scenarios."""

    @pytest.mark.asyncio
    async def test_s3_chunked_upload_succeeds(self, mock_config, mock_oauth_tokens, respx_mock):
        """Chunked upload with multiple parts succeeds."""
        from datetime import datetime, timedelta, timezone
        
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        watch_dir = mock_config["watch"]
        test_file = watch_dir / "chunked.mp4"
        test_file.write_bytes(b"x" * 300)

        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "id": "chunked-file",
                        "name": "chunked.mp4",
                        "file_size": 300,
                        "upload_urls": [
                            {"size": 100, "url": f"https://s3.amazonaws.com/chunked/part_{i}"}
                            for i in range(1, 4)
                        ],
                    }
                },
            )
        )
        for i in range(1, 4):
            respx_mock.put(f"https://s3.amazonaws.com/chunked/part_{i}").mock(
                return_value=httpx.Response(200)
            )
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
        ).mock(
            return_value=httpx.Response(
                201,
                json={"data": {"short_url": "https://app.frame.io/shares/chunked"}},
            )
        )

        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': test_file.name,
                'name': test_file.name,
            })

        assert not test_file.exists()
        assert len(list(mock_config["processed"].glob("*.mp4"))) == 1
