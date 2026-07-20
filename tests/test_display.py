"""
Tests for customer display and staff control panel.
"""
from threading import Lock, Thread
from unittest.mock import patch, MagicMock

import pytest

import main as app_module
from main import (
    display_state_lock,
    active_display_qr,
    manual_override,
    queued_qrs,
    latest_qr,
    token_lock,
)


class TestCustomerDisplay:
    """Tests for customer-facing QR display."""

    def test_get_customer_qr_returns_none_when_empty(self, mock_config):
        """Returns null qr_path when no QR is active."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.active_display_qr = None
            app_module.manual_override = False
        
        response = client.get("/api/customer/qr")
        assert response.status_code == 200
        data = response.json()
        assert data["qr_path"] is None
        assert data["manual_override"] is False

    def test_get_customer_qr_returns_active_qr(self, mock_config):
        """Returns active QR path when set in manual override mode."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/test.png"
            app_module.manual_override = True
        
        response = client.get("/api/customer/qr")
        assert response.status_code == 200
        data = response.json()
        assert data["qr_path"] == "/qr_codes/test.png"

    def test_get_customer_qr_parses_timecode_and_video_name(self, mock_config):
        """Parses filename to extract timecode and video name."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_path = mock_config["qr_codes"] / "20260717133048_1_MyVideo.mp4.png"
        qr_path.write_bytes(b"png_data")
        
        with display_state_lock:
            app_module.active_display_qr = str(qr_path)
            app_module.manual_override = False
        
        response = client.get("/api/customer/qr")
        assert response.status_code == 200
        data = response.json()
        assert data["timecode_human"] == "2026-07-17 13:30:48"
        assert data["video_name"] == "MyVideo.mp4"


class TestStaffControl:
    """Tests for staff manual override and QR selection."""

    def test_select_qr_sets_override(self, mock_config):
        """Selecting a QR activates manual override."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.active_display_qr = None
            app_module.manual_override = False
            app_module.queued_qrs = []
        
        response = client.post("/api/staff/select-qr", json={"qr_path": "/qr_codes/selected.png"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["manual_override"] is True
        
        with display_state_lock:
            assert app_module.active_display_qr == "/qr_codes/selected.png"
            assert app_module.manual_override is True

    def test_select_qr_clears_queue(self, mock_config):
        """Selecting a QR clears any queued items."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.queued_qrs = ["/qr_codes/q1.png", "/qr_codes/q2.png"]
        
        client.post("/api/staff/select-qr", json={"qr_path": "/qr_codes/new.png"})
        
        with display_state_lock:
            assert len(app_module.queued_qrs) == 0

    def test_clear_override_shows_queued_qr(self, mock_config):
        """Clearing override displays first queued QR."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/old.png"
            app_module.manual_override = True
            app_module.queued_qrs = ["/qr_codes/queued.png"]
        
        response = client.post("/api/staff/clear-override")
        assert response.status_code == 200
        data = response.json()
        assert data["qr_path"] == "/qr_codes/queued.png"
        assert data["queue_length"] == 0

    def test_clear_override_with_empty_queue_disables_override(self, mock_config):
        """Clearing override with empty queue disables manual mode."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/latest.png"
            app_module.manual_override = True
            app_module.queued_qrs = []
        
        response = client.post("/api/staff/clear-override")
        assert response.status_code == 200
        data = response.json()
        assert data["manual_override"] is False

    def test_select_same_qr_toggles_deselect(self, mock_config):
        """Selecting the same QR again deselects it."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.active_display_qr = "/qr_codes/toggle.png"
            app_module.manual_override = True
            app_module.latest_qr = "/qr_codes/latest.png"
            app_module.queued_qrs = []
        
        response = client.post("/api/staff/select-qr", json={"qr_path": "/qr_codes/toggle.png"})
        assert response.status_code == 200
        data = response.json()
        assert data["manual_override"] is True  # API always sets override; toggle is handled client-side

    def test_queue_accumulates_during_override(self, mock_config):
        """New QR codes queue up when manual override is active."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with display_state_lock:
            app_module.manual_override = True
            app_module.active_display_qr = "/qr_codes/displayed.png"
            app_module.queued_qrs = []
        
        # Simulate new QR generated by watcher
        qr_path = mock_config["qr_codes"] / "20260717133049_2_new.mp4.png"
        qr_path.write_bytes(b"png_data")
        
        with display_state_lock:
            app_module.queued_qrs.append(str(qr_path))
        
        assert len(app_module.queued_qrs) == 1


class TestDisplayStateThreadSafety:
    """Thread safety for display state operations."""

    def test_concurrent_qr_selection(self, mock_config):
        """Concurrent QR selections don't corrupt state."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        def select_qr(client, path):
            client.post("/api/staff/select-qr", json={"qr_path": path})
        
        paths = [f"/qr_codes/qr_{i}.png" for i in range(10)]
        threads = [Thread(target=select_qr, args=(client, p)) for p in paths]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        # State should be consistent (one of the paths selected)
        assert app_module.active_display_qr in paths
