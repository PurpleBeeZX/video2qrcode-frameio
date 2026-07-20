"""
Tests for upload retry logic, S3 chunked uploads, and malformed API responses.
"""

import httpx
import pytest
from threading import Lock
from unittest.mock import patch

import main as app_module
from main import (
    FRAMEIO_API_BASE,
    process_single_file,
    token_lock,
)


class TestUploadRetries:
    """Tests for upload retry logic."""

    @pytest.mark.asyncio
    async def test_upload_retries_on_failure(self, mock_config, mock_oauth_tokens, respx_mock):
        """Upload retries 5 times then moves to failed_folder."""
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = None  # Will be set during test
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        from datetime import datetime, timedelta, timezone
        app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        watch_dir = mock_config["watch"]
        failed_dir = mock_config["failed"]
        test_file = watch_dir / "test_video.mp4"
        test_file.write_text("dummy video content")

        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
        ).mock(return_value=httpx.Response(500, text="Internal Server Error"))

        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': test_file.name,
                'name': test_file.name,
            })

        assert not test_file.exists()
        assert len(list(failed_dir.glob("*.mp4"))) == 1

    @pytest.mark.asyncio
    async def test_upload_succeeds_after_retry(self, mock_config, mock_oauth_tokens, respx_mock):
        """Upload succeeds after a transient failure."""
        from datetime import datetime, timedelta, timezone

        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"


        watch_dir = mock_config["watch"]
        processed_dir = mock_config["processed"]
        test_file = watch_dir / "retry_video.mp4"
        test_file.write_bytes(b"x" * 100)

        # Mock the initiate_upload POST (first attempt fails, second succeeds)
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
        ).mock(
            side_effect=[
                httpx.Response(500, text="Temporary error"),
                httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "mock-id",
                            "name": "retry_video.mp4",
                            "file_size": 100,
                            "upload_urls": [
                                {
                                    "size": 100,
                                    "url": "https://frameio-uploads-development.s3.amazonaws.com/parts/mock-id/part_1",
                                }
                            ],
                        }
                    },
                ),
            ]
        )
        # Mock the perform_upload PUT
        respx_mock.put(
            "https://frameio-uploads-development.s3.amazonaws.com/parts/mock-id/part_1"
        ).mock(return_value=httpx.Response(200))
        # Mock the share link creation
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
        ).mock(
            return_value=httpx.Response(
                201,
                json={"data": {"short_url": "https://app.frame.io/shares/mock-code"}},
            )
        )

        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': test_file.name,
                'name': test_file.name,
            })

        assert not test_file.exists()
        assert len(list(processed_dir.glob("*.mp4"))) == 1


class TestMalformedAPIResponses:
    """Tests for handling corrupt or missing API response fields."""

    @pytest.mark.asyncio
    async def test_missing_upload_urls(self, mock_config, mock_oauth_tokens, respx_mock):
        """Missing upload_urls causes RuntimeError and file goes to failed_folder."""
        from datetime import datetime, timedelta, timezone

        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        watch_dir = mock_config["watch"]
        failed_dir = mock_config["failed"]
        test_file = watch_dir / "corrupt_upload.mp4"
        test_file.write_bytes(b"test content")

        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"data": {"id": "mock-id", "name": "corrupt_upload.mp4"}},
            )
        )

        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': test_file.name,
                'name': test_file.name,
            })

        # Should move to failed due to missing upload_urls
        assert not test_file.exists()
        assert len(list(failed_dir.glob("*.mp4"))) == 1

    @pytest.mark.asyncio
    async def test_missing_short_url_in_share(self, mock_config, mock_oauth_tokens, respx_mock):
        """Missing short_url logs failure but still moves file to processed."""
        from datetime import datetime, timedelta, timezone

        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        watch_dir = mock_config["watch"]
        processed_dir = mock_config["processed"]
        test_file = watch_dir / "no_share_url.mp4"
        test_file.write_bytes(b"test content")

        # Mock initiate_upload POST
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "id": "mock-id",
                        "name": "no_share_url.mp4",
                        "file_size": 12,
                        "upload_urls": [
                            {"size": 12, "url": "https://s3.amazonaws.com/parts/mock-id/part_1"}
                        ],
                    }
                },
            )
        )
        # Mock perform_upload PUT
        respx_mock.put("https://s3.amazonaws.com/parts/mock-id/part_1").mock(
            return_value=httpx.Response(200)
        )
        # Mock share link creation (returns no short_url)
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
        ).mock(
            return_value=httpx.Response(201, json={"data": {"id": "share-id"}})
        )

        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': test_file.name,
                'name': test_file.name,
            })

        assert not test_file.exists()
        assert len(list(processed_dir.glob("*.mp4"))) == 1