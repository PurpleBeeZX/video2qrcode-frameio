"""
Tests for dynamic configuration, settings API, and watchdog hot-reload.
"""

import time
from threading import Lock
from unittest.mock import patch

import httpx
import pytest

import main as app_module
from main import (
    FRAMEIO_API_BASE,
    FRAMEIO_TOKEN_URL,
    WATCH_PATH,
    PROCESSED_PATH,
    FAILED_PATH,
    QR_CODES_PATH,
    token_lock,
)


class TestDynamicConfiguration:
    """Tests for settings API, validation, and watchdog hot-reload."""

    @pytest.fixture
    def client(self, mock_config):
        """Create a TestClient with mocked configuration."""
        from fastapi.testclient import TestClient
        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        yield TestClient(app_module.app)

    def test_update_settings_valid_payload(self, client, mock_config):
        """Valid settings payload updates config."""
        payload = {
            "WATCH_FOLDER": str(mock_config["watch"]),
            "PROCESSED_FOLDER": str(mock_config["processed"]),
            "STABILIZATION_DELAY": 5.0,
            "MAX_RETRIES": 3,
            "SHARE_EXPIRATION_DAYS": 14,
        }
        response = client.post("/api/settings", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["settings"]["STABILIZATION_DELAY"] == 5.0
        assert data["settings"]["MAX_RETRIES"] == 3

    def test_update_settings_invalid_delay(self, client):
        """Negative stabilization delay is rejected."""
        response = client.post("/api/settings", json={"STABILIZATION_DELAY": -1.0})
        assert response.status_code == 400

    def test_update_settings_invalid_retries(self, client):
        """Negative max retries is rejected."""
        response = client.post("/api/settings", json={"MAX_RETRIES": -5})
        assert response.status_code == 400

    def test_update_settings_empty_folder(self, client):
        """Empty folder path is rejected."""
        response = client.post("/api/settings", json={"WATCH_FOLDER": ""})
        assert response.status_code == 400

    def test_update_settings_unknown_key(self, client):
        """Unknown setting key is rejected."""
        response = client.post("/api/settings", json={"UNKNOWN_SETTING": "value"})
        assert response.status_code == 400


class TestCatchUpScanning:
    """Tests for catch-up mode on startup and folder switch."""

    @pytest.mark.asyncio
    async def test_catch_up_processes_existing_files(self, mock_config, mock_oauth_tokens, respx_mock):
        """Pre-existing files are processed on startup."""
        from datetime import datetime, timedelta, timezone
        from threading import Lock
        from pathlib import Path
        
        watch_dir = mock_config["watch"]
        processed_dir = mock_config["processed"]

        # Update dynamic config to point to temp folders
        cfg = app_module.config_instance
        cfg.watch_folder = str(watch_dir)
        cfg.processed_folder = str(processed_dir)
        cfg.failed_folder = str(mock_config["failed"])
        cfg.qr_codes_folder = str(mock_config["qr_codes"])

        # Clean up any leftover files from previous tests
        processing_path = app_module.get_config().processing_path
        for f in processing_path.glob("*.mp4"):
            f.unlink()
        for f in watch_dir.glob("*.mp4"):
            f.unlink()

        for i in range(3):
            fpath = watch_dir / f"preexisting_{i}.mp4"
            fpath.write_bytes(b"x" * 100)

        with token_lock:
            app_module.access_token = mock_oauth_tokens["access_token"]
            app_module.refresh_token = mock_oauth_tokens["refresh_token"]
            app_module.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
            app_module.account_id = "account_123"
            app_module.project_id = "project_123"

        for i in range(3):
            respx_mock.post(
                f"{FRAMEIO_API_BASE}/accounts/account_123/folders/{app_module.FRAMEIO_FOLDER_ID}/files/local_upload"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": f"file-{i}",
                            "name": f"preexisting_{i}.mp4",
                            "file_size": 100,
                            "upload_urls": [
                                {"size": 100, "url": f"https://s3.amazonaws.com/parts/{i}/part_1"}
                            ],
                        }
                    },
                )
            )
            respx_mock.put(f"https://s3.amazonaws.com/parts/{i}/part_1").mock(
                return_value=httpx.Response(200)
            )
            respx_mock.post(
                f"{FRAMEIO_API_BASE}/accounts/account_123/projects/project_123/shares"
            ).mock(
                return_value=httpx.Response(
                    201,
                    json={"data": {"short_url": f"https://app.frame.io/shares/{i}"}},
                )
            )

        processing_lock = Lock()
        with patch("main.time.sleep"):
            await app_module.run_catch_up(processing_lock)

        # Files should remain in watch folder (watch_folder IS the queue)
        # They will only be moved when upload_worker processes them
        remaining = list(watch_dir.glob("*.mp4"))
        assert len(remaining) == 3, f"Files should stay in watch folder (queue): {[f.name for f in remaining]}"
        # Queue should contain all files
        with app_module.upload_queue_lock:
            assert len(app_module.upload_queue) == 3, f"Queue should have 3 items, got {len(app_module.upload_queue)}"
            seq_nums = [entry["seq_num"] for entry in app_module.upload_queue]
            assert seq_nums == [0, 1, 2]
            for entry in app_module.upload_queue:
                assert entry["name"].split("_")[1] == str(entry["seq_num"])

    def test_scan_skips_pending_files(self, mock_config):
        """Periodic scan queues stable files but skips paths already being monitored."""
        watch_dir = mock_config["watch"]
        queue_file = watch_dir / "queued.mp4"
        pending_file = watch_dir / "pending.mp4"
        queue_file.write_bytes(b"x" * 100)
        pending_file.write_bytes(b"x" * 100)

        with app_module._pending_stability_checks_lock:
            app_module._pending_stability_checks.add(str(pending_file.resolve()))

        try:
            with patch("main.time.sleep"):
                queued = app_module._scan_watch_folder_for_new_files()

            assert queued == 1
            with app_module.upload_queue_lock:
                assert [entry["original_name"] for entry in app_module.upload_queue] == ["queued.mp4"]
                assert app_module.upload_queue[0]["name"].split("_")[1] == str(app_module.upload_queue[0]["seq_num"])
        finally:
            with app_module._pending_stability_checks_lock:
                app_module._pending_stability_checks.discard(str(pending_file.resolve()))

    @pytest.mark.asyncio
    async def test_catch_up_empty_folder(self, mock_config):
        """Catch-up on empty folder logs appropriately."""
        from threading import Lock
        
        watch_dir = mock_config["watch"]
        processing_lock = Lock()

        with patch("main.time.sleep"):
            await app_module.run_catch_up(processing_lock)

        assert len(list(watch_dir.glob("*.mp4"))) == 0


class TestServerShutdown:
    """Tests for POST /api/shutdown endpoint."""

    @pytest.fixture
    def client(self, mock_config):
        """Create a TestClient with mocked configuration."""
        from fastapi.testclient import TestClient
        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        yield TestClient(app_module.app)

    def test_shutdown_returns_immediate_response(self, client):
        """Shutdown returns 200 JSON before background thread runs."""
        with patch("os._exit") as mock_exit:
            with patch("logging.shutdown"):
                response = client.post("/api/shutdown")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "shutting_down"

    def test_shutdown_stops_observer(self, client):
        """Shutdown stops the watchdog observer."""
        from unittest.mock import MagicMock
        mock_observer = MagicMock()
        with patch("main._watcher_observer", mock_observer):
            with patch("os._exit"):
                with patch("logging.shutdown"):
                    client.post("/api/shutdown")

        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()