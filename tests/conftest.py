"""
Shared fixtures for all test modules.
"""

import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import main as app_module
from main import (
    access_token,
    token_lock,
    log_feed,
    add_log_entry,
    app,
    MP4Handler,
)


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all module-level global state before each test."""
    with token_lock:
        app_module.access_token = None
        app_module.refresh_token = None
        app_module.token_expires_at = None
        app_module.user_info = None
        app_module.account_id = None
        app_module.project_id = None
    with app_module.upload_queue_lock:
        app_module.upload_queue.clear()
    with app_module.upload_status_lock:
        app_module.upload_status.clear()
    with app_module.sequence_lock:
        app_module.current_sequence_number = 0
    with app_module._pending_stability_checks_lock:
        app_module._pending_stability_checks.clear()
    with app_module.display_state_lock:
        app_module.active_display_qr = None
        app_module.latest_qr = None
        app_module.manual_override = False
        app_module.queued_qrs = []
    app_module.current_upload_name = None
    app_module.session_processed_count = 0
    app_module.session_failed_count = 0
    log_feed.clear()
    # Reset dynamic config paths to defaults to ensure test isolation
    app_module.config_instance.watch_folder = "watch_folder"
    app_module.config_instance.processed_folder = "processed_folder"
    app_module.config_instance.failed_folder = "failed_folder"
    app_module.config_instance.qr_codes_folder = "qr_codes"


@pytest.fixture
def mock_config(tmp_path, monkeypatch):
    """Override config paths to use temporary directories."""
    test_watch = tmp_path / "watch"
    test_processed = tmp_path / "processed"
    test_failed = tmp_path / "failed"
    test_qr = tmp_path / "qr_codes"
    test_templates = tmp_path / "templates"

    for folder in [test_watch, test_processed, test_failed, test_qr, test_templates]:
        folder.mkdir(parents=True, exist_ok=True)

    # Create processing folder for file moves
    test_processing = tmp_path / "processing"
    test_processing.mkdir(parents=True, exist_ok=True)
    
    monkeypatch.setattr(app_module, "WATCH_PATH", test_watch)
    monkeypatch.setattr(app_module, "PROCESSED_PATH", test_processed)
    monkeypatch.setattr(app_module, "FAILED_PATH", test_failed)
    monkeypatch.setattr(app_module, "QR_CODES_PATH", test_qr)
    monkeypatch.setattr(app_module, "TEMPLATES_PATH", test_templates)

    # Also update the DynamicConfig instance so get_config() returns temp paths
    app_module.config_instance.watch_folder = str(test_watch)
    app_module.config_instance.processed_folder = str(test_processed)
    app_module.config_instance.failed_folder = str(test_failed)
    app_module.config_instance.qr_codes_folder = str(test_qr)
    app_module.config_instance.processing_folder = str(test_processing)

    return {
        "watch": test_watch,
        "processed": test_processed,
        "failed": test_failed,
        "qr_codes": test_qr,
        "templates": test_templates,
    }


@pytest.fixture
def mock_oauth_tokens():
    """Simulate successful OAuth token exchange."""
    return {
        "access_token": "mock_v4_access_token_12345",
        "refresh_token": "mock_v4_refresh_token_67890",
        "expires_in": 3600,
        "token_type": "Bearer",
    }


@pytest.fixture
def auth_headers(mock_oauth_tokens):
    """Return authorization headers with mock token."""
    return {"Authorization": f"Bearer {mock_oauth_tokens['access_token']}"}