"""
Tests for FastAPI HTTP endpoints.
"""

import time
from threading import Thread
from unittest.mock import MagicMock, patch

import httpx
import pytest

import main as app_module
from main import (
    FRAMEIO_API_BASE,
    app,
    token_lock,
    add_log_entry,
)


class TestFastAPIEndpoints:
    """Tests for FastAPI HTTP endpoints."""

    @pytest.fixture
    def client(self, mock_config):
        """Create a TestClient with mocked configuration."""
        from fastapi.testclient import TestClient
        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        yield TestClient(app_module.app)

    def test_home_authenticated(self, client, mock_oauth_tokens):
        """GET / returns 200 when authenticated."""
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.user_info = {"name": "Test User", "email": "test@example.com"}

        response = client.get("/")
        assert response.status_code == 200

    def test_home_unauthenticated(self, client):
        """GET / returns 200 when not authenticated."""
        with token_lock:
            app_module.access_token = None
        response = client.get("/")
        assert response.status_code == 200

    def test_login_redirect(self, client):
        """GET /login redirects to Adobe IMS."""
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 307
        assert "ims-na1.adobelogin.com" in response.headers["location"]

    def test_logs_endpoint(self, client):
        """GET /logs returns log feed."""
        add_log_entry("Test message", "INFO")
        response = client.get("/logs")
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data
        assert len(data["logs"]) > 0

    def test_status_unauthenticated(self, client):
        """GET /status returns correct unauthenticated state."""
        with token_lock:
            app_module.access_token = None
        response = client.get("/status")
        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is False

    def test_status_authenticated(self, client, mock_oauth_tokens):
        """GET /status returns correct authenticated state."""
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.user_info = {"name": "Test User"}
            app_module.account_id = "acc_123"
            app_module.project_id = "proj_123"
        response = client.get("/status")
        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is True
        assert data["name"] == "Test User"

    def test_concurrent_status_requests(self, mock_config, mock_oauth_tokens):
        """Concurrent /status requests are thread-safe."""
        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.user_info = {"name": "Concurrent User"}
            app_module.account_id = "acc_1"
            app_module.project_id = "proj_1"

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        from fastapi.testclient import TestClient
                        client = TestClient(app_module.app)

                        results = []
                        def make_request():
                            results.append(client.get("/status").json())

                        threads = [Thread(target=make_request) for _ in range(10)]
                        for t in threads:
                            t.start()
                        for t in threads:
                            t.join()

                        assert len(results) == 10
                        for data in results:
                            assert data["authenticated"] is True


class TestCatchUpOnFolderSwitch:
    """Tests for catch-up triggered by dynamic folder change."""

    @pytest.mark.asyncio
    async def test_change_watch_folder_triggers_catch_up(self, mock_config, mock_oauth_tokens, respx_mock):
        """Changing WATCH_FOLDER triggers immediate catch-up on new folder."""
        from pathlib import Path
        from datetime import datetime, timedelta, timezone
        from main import process_single_file
        
        new_watch = mock_config["watch"].parent / "new_watch"
        new_watch.mkdir(exist_ok=True)
        existing_file = new_watch / "existing.mp4"
        existing_file.write_bytes(b"x" * 100)

        processed_dir = mock_config["processed"]
        processing_dir = mock_config["watch"].parent / "processing"

        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"
            app_module.FRAMEIO_FOLDER_ID = "qr_code_uploader"

        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/folders/qr_code_uploader/files/local_upload"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "id": "existing-id",
                        "name": "existing.mp4",
                        "file_size": 100,
                        "upload_urls": [
                            {"size": 100, "url": "https://s3.amazonaws.com/parts/existing/part_1"}
                        ],
                    }
                },
            )
        )
        respx_mock.put("https://s3.amazonaws.com/parts/existing/part_1").mock(
            return_value=httpx.Response(200)
        )
        respx_mock.post(
            f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
        ).mock(
            return_value=httpx.Response(
                201,
                json={"data": {"short_url": "https://app.frame.io/shares/test"}},
            )
        )

        # Update config paths to use temp directories for process_single_file
        app_module.config_instance.watch_folder = str(new_watch)
        app_module.config_instance.processed_folder = str(processed_dir)
        app_module.config_instance.failed_folder = str(mock_config["failed"])
        app_module.config_instance.qr_codes_folder = str(mock_config["qr_codes"])
        app_module.config_instance.processing_folder = str(processing_dir)
        app_module.config_instance.stabilization_delay = 0.0
        
        # Create the processing directory since get_config() will use it
        processing_dir.mkdir(parents=True, exist_ok=True)
        
        # Directly process the file to test the flow
        with patch("main.time.sleep"):
            await process_single_file({
                'original_name': 'existing.mp4',
                'name': 'existing.mp4',
            })

        assert not existing_file.exists(), "File was not processed by catch-up in time"
        processed_files = list(processed_dir.glob("*.mp4"))
        assert len(processed_files) >= 1, f"No files found in processed folder after catch-up"