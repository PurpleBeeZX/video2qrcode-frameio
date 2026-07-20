"""
Tests for new features: setup wizard, customer display API, and staff control panel.
"""

import json
from threading import Lock, Thread
from unittest.mock import MagicMock, patch

import pytest

import main as app_module
from main import (
    display_state_lock,
    active_display_qr,
    latest_qr,
    manual_override,
    queued_qrs,
    token_lock,
    setup_mode,
    config_lock,
    generate_qr_code,
)


class TestSetupWizard:
    """Tests for first-time setup mode and /api/setup endpoints."""

    def test_setup_mode_status_default(self, mock_config):
        """GET /api/setup/status returns setup_mode=false when config is valid."""
        from fastapi.testclient import TestClient
        with patch("main.CONFIG_PATH", mock_config["watch"].parent / "config.json"):
            with patch("main.setup_mode", False):
                client = TestClient(app_module.app)
                response = client.get("/api/setup/status")
                assert response.status_code == 200
                assert response.json() == {"setup_mode": False}

    def test_setup_mode_status_true(self, mock_config):
        """GET /api/setup/status returns setup_mode=true when config is missing."""
        from fastapi.testclient import TestClient
        with patch("main.CONFIG_PATH", None):
            with patch("main.setup_mode", True):
                client = TestClient(app_module.app)
                response = client.get("/api/setup/status")
                assert response.status_code == 200
                assert response.json() == {"setup_mode": True}

    def test_setup_initialize_creates_config_and_folders(self, mock_config, tmp_path):
        """POST /api/setup/initialize writes config.json and creates folders."""
        from fastapi.testclient import TestClient
        import os

        config_path = tmp_path / "new_config.json"
        with patch("main.CONFIG_PATH", config_path):
            with patch("main.setup_mode", True):
                client = TestClient(app_module.app)
                payload = {
                    "client_id": "new_client_id",
                    "client_secret": "new_client_secret",
                    "folder_id": "new_folder_id",
                    "server_port": 8000,
                    "watch_folder": str(mock_config["watch"]),
                    "processed_folder": str(mock_config["processed"]),
                    "failed_folder": str(mock_config["failed"]),
                    "qr_codes_folder": str(mock_config["qr_codes"]),
                }
                response = client.post("/api/setup/initialize", json=payload)
                assert response.status_code == 200
                assert response.json() == {"status": "ok", "message": "Configuration saved successfully"}

                # Verify config.json was written
                assert config_path.exists()
                with open(config_path) as f:
                    saved_config = json.load(f)
                assert saved_config["frameio"]["client_id"] == "new_client_id"
                assert saved_config["frameio"]["client_secret"] == "new_client_secret"
                assert saved_config["frameio"]["folder_id"] == "new_folder_id"

                # Verify folders were created
                for folder_name in ["watch", "processed", "failed", "qr_codes"]:
                    assert saved_config["folders"][folder_name] in [
                        str(mock_config["watch"]),
                        str(mock_config["processed"]),
                        str(mock_config["failed"]),
                        str(mock_config["qr_codes"]),
                    ]

    def test_setup_initialize_missing_fields(self, mock_config):
        """POST /api/setup/initialize with missing fields returns 400."""
        from fastapi.testclient import TestClient
        from fastapi import HTTPException

        with patch("main.CONFIG_PATH", mock_config["watch"].parent / "config.json"):
            with patch("main.setup_mode", True):
                client = TestClient(app_module.app)
                # Missing client_secret and folder_id
                payload = {"client_id": "only_client_id"}
                response = client.post("/api/setup/initialize", json=payload)
                assert response.status_code == 400
                assert "required" in response.json()["detail"].lower()


class TestCustomerDisplayAPI:
    """Tests for /api/customer/qr endpoint."""

    def test_customer_qr_returns_none_when_no_qr(self, mock_config):
        """GET /api/customer/qr returns null qr_path when no QR generated yet."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = None
            app_module.latest_qr = None
            app_module.manual_override = False
            app_module.queued_qrs = []

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        response = client.get("/api/customer/qr")
                        assert response.status_code == 200
                        data = response.json()
                        assert data["qr_path"] is None
                        assert data["manual_override"] is False
                        assert data["queue_length"] == 0

    def test_customer_qr_returns_active_qr(self, mock_config):
        """GET /api/customer/qr returns the active QR URL."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/test.png"
            app_module.latest_qr = "/qr_codes/test.png"
            app_module.manual_override = False
            app_module.queued_qrs = []

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        response = client.get("/api/customer/qr")
                        assert response.status_code == 200
                        data = response.json()
                        assert data["qr_path"] == "/qr_codes/test.png"
                        assert data["manual_override"] is False


class TestStaffControlPanel:
    """Tests for /api/staff/select-qr and /api/staff/clear-override."""

    def test_select_qr_sets_override(self, mock_config):
        """POST /api/staff/select-qr sets manual_override and active_display_qr."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = None
            app_module.manual_override = False
            app_module.queued_qrs = []
            app_module.latest_qr = "/qr_codes/latest.png"

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        response = client.post("/api/staff/select-qr", json={"qr_path": "/qr_codes/selected.png"})
                        assert response.status_code == 200
                        data = response.json()
                        assert data["status"] == "ok"
                        assert data["qr_path"] == "/qr_codes/selected.png"
                        assert data["manual_override"] is True

                        with display_state_lock:
                            assert app_module.active_display_qr == "/qr_codes/selected.png"
                            assert app_module.manual_override is True
                            assert len(app_module.queued_qrs) == 0

    def test_select_qr_clears_queue(self, mock_config):
        """POST /api/staff/select-qr clears any existing queued_qrs."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/old.png"
            app_module.manual_override = True
            app_module.queued_qrs = ["/qr_codes/queued1.png", "/qr_codes/queued2.png"]

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        response = client.post("/api/staff/select-qr", json={"qr_path": "/qr_codes/new.png"})
                        assert response.status_code == 200
                        with display_state_lock:
                            assert app_module.queued_qrs == []

    def test_clear_override_with_empty_queue_shows_latest(self, mock_config):
        """POST /api/staff/clear-override shows latest_qr when queue is empty."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/manual.png"
            app_module.manual_override = True
            app_module.queued_qrs = []
            app_module.latest_qr = "/qr_codes/latest.png"

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        response = client.post("/api/staff/clear-override")
                        assert response.status_code == 200
                        data = response.json()
                        assert data["qr_path"] == "/qr_codes/latest.png"
                        assert data["manual_override"] is False
                        assert data["queue_length"] == 0

    def test_clear_override_with_queued_qr_pops_queue(self, mock_config):
        """POST /api/staff/clear-override pops first queued QR."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/manual.png"
            app_module.manual_override = True
            app_module.queued_qrs = ["/qr_codes/queued1.png", "/qr_codes/queued2.png"]
            app_module.latest_qr = "/qr_codes/latest.png"

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        response = client.post("/api/staff/clear-override")
                        assert response.status_code == 200
                        data = response.json()
                        assert data["qr_path"] == "/qr_codes/queued1.png"
                        assert data["manual_override"] is True  # Still True because queue has more
                        assert data["queue_length"] == 1

    def test_clear_override_second_call_drains_queue(self, mock_config):
        """Second clear-override drains remaining queue and disables override."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/manual.png"
            app_module.manual_override = True
            app_module.queued_qrs = ["/qr_codes/queued1.png"]
            app_module.latest_qr = "/qr_codes/latest.png"

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)
                        # First call pops the last queued item
                        response1 = client.post("/api/staff/clear-override")
                        assert response1.status_code == 200
                        data1 = response1.json()
                        assert data1["qr_path"] == "/qr_codes/queued1.png"
                        assert data1["manual_override"] is False  # Queue is now empty
                        assert data1["queue_length"] == 0


class TestDisplayStateThreadSafety:
    """Tests for thread safety of display state variables."""

    def test_concurrent_display_state_updates(self, mock_config):
        """Concurrent updates to display state are thread-safe."""
        from fastapi.testclient import TestClient

        with display_state_lock:
            app_module.active_display_qr = None
            app_module.latest_qr = None
            app_module.manual_override = False
            app_module.queued_qrs = []

        with patch("main.WATCH_PATH", mock_config["watch"]):
            with patch("main.PROCESSED_PATH", mock_config["processed"]):
                with patch("main.FAILED_PATH", mock_config["failed"]):
                    with patch("main.QR_CODES_PATH", mock_config["qr_codes"]):
                        client = TestClient(app_module.app)

                        results = []
                        errors = []

                        def select_qr(path):
                            try:
                                resp = client.post("/api/staff/select-qr", json={"qr_path": path})
                                results.append(resp.json())
                            except Exception as e:
                                errors.append(e)

                        threads = [
                            Thread(target=select_qr, args=(f"/qr_codes/qr_{i}.png",))
                            for i in range(10)
                        ]
                        for t in threads:
                            t.start()
                        for t in threads:
                            t.join()

                        assert len(errors) == 0, f"Thread errors: {errors}"
                        assert len(results) == 10

    def test_generate_qr_respects_manual_override(self, mock_config):
        """generate_qr_code queues QR when manual_override is True."""
        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/manual.png"
            app_module.latest_qr = "/qr_codes/manual.png"
            app_module.manual_override = True
            app_module.queued_qrs = []

        generate_qr_code("https://app.frame.io/shares/test", "20240101_120000_test.mp4")

        with display_state_lock:
            # New format: {YYYYMMDDhhmmss}_{seq}_{video}.png
            assert app_module.latest_qr.endswith("_0_20240101_120000_test.png")
            assert len(app_module.queued_qrs) == 1
            # active_display_qr should remain unchanged during override
            assert app_module.active_display_qr == "/qr_codes/manual.png"

    def test_generate_qr_updates_display_when_no_override(self, mock_config):
        """generate_qr_code updates active_display_qr when manual_override is False."""
        with display_state_lock:
            app_module.manual_override = False
            app_module.queued_qrs = []

        generate_qr_code("https://app.frame.io/shares/test", "20240101_120000_test2.mp4")

        with display_state_lock:
            # New format: {YYYYMMDDhhmmss}_{seq}_{video}.png
            assert app_module.latest_qr.endswith("_1_20240101_120000_test2.png")
            assert app_module.active_display_qr == app_module.latest_qr
            assert len(app_module.queued_qrs) == 0


class TestFolderAutoCreation:
    """Tests for automatic folder creation at startup and during setup."""

    def test_folders_created_on_startup(self, tmp_path, monkeypatch):
        """Application creates missing folders on startup."""
        import main as app

        # Simulate missing folders
        watch = tmp_path / "watch_missing"
        processed = tmp_path / "processed_missing"
        failed = tmp_path / "failed_missing"
        qr = tmp_path / "qr_missing"

        assert not watch.exists()
        assert not processed.exists()
        assert not failed.exists()
        assert not qr.exists()

        # Create folders as the app does at startup
        for folder in [watch, processed, failed, qr]:
            folder.mkdir(exist_ok=True)

        assert watch.exists()
        assert processed.exists()
        assert failed.exists()
        assert qr.exists()

    def test_setup_initialize_creates_missing_folders(self, mock_config, tmp_path):
        """POST /api/setup/initialize creates folders even if they don't exist."""
        from fastapi.testclient import TestClient

        new_watch = tmp_path / "new_watch"
        new_processed = tmp_path / "new_processed"
        new_failed = tmp_path / "new_failed"
        new_qr = tmp_path / "new_qr"

        config_path = tmp_path / "config.json"
        with patch("main.CONFIG_PATH", config_path):
            with patch("main.setup_mode", True):
                client = TestClient(app_module.app)
                payload = {
                    "client_id": "test_client",
                    "client_secret": "test_secret",
                    "folder_id": "test_folder",
                    "watch_folder": str(new_watch),
                    "processed_folder": str(new_processed),
                    "failed_folder": str(new_failed),
                    "qr_codes_folder": str(new_qr),
                }
                response = client.post("/api/setup/initialize", json=payload)
                assert response.status_code == 200

                # Verify all folders were created
                assert new_watch.exists()
                assert new_processed.exists()
                assert new_failed.exists()
                assert new_qr.exists()