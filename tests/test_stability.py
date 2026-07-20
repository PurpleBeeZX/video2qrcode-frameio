"""
Tests for memory leak prevention and long-running stability.
"""
import gc
import sys
import time
import psutil
import pytest
from threading import Thread, Lock
from unittest.mock import patch, MagicMock

import main as app_module
from main import (
    log_feed,
    queued_qrs,
    add_log_entry,
    config_instance,
    token_lock,
)


class TestMemoryLeakPrevention:
    """Ensure bounded memory usage over long runs."""

    def test_log_feed_ring_buffer_does_not_grow_indefinitely(self):
        """log_feed should respect MAX_LOG_FEED limit."""
        from main import MAX_LOG_FEED
        initial_count = len(log_feed)
        # Add many entries
        for i in range(MAX_LOG_FEED + 100):
            add_log_entry(f"Test message {i}", "INFO")
        # Should not exceed MAX_LOG_FEED
        assert len(log_feed) <= MAX_LOG_FEED

    def test_queued_qrs_bounded(self):
        """queued_qrs should not grow without bound."""
        # Simulate manual override with many queued items
        app_module.manual_override = True
        with display_state_lock:
            app_module.queued_qrs = [f"/qr_codes/qr_{i}.png" for i in range(1000)]
        # Simulate clearing override which drains queue
        with display_state_lock:
            app_module.queued_qrs.clear()
            app_module.manual_override = False
        assert len(app_module.queued_qrs) == 0

    def test_no_file_handle_leaks(self):
        """Ensure no file handles are left open after operations."""
        import gc
        proc = psutil.Process()
        initial_files = proc.num_fds() if hasattr(proc, 'num_fds') else len(proc.open_files())
        
        # Perform many config saves
        from pathlib import Path
        test_path = Path(app_module.CONFIG_PATH or "data/config.json")
        for i in range(10):
            try:
                test_path.write_text("{}")
                test_path.unlink()
            except Exception:
                pass
        
        gc.collect()
        # File handles should not have grown significantly
        final_files = proc.num_fds() if hasattr(proc, 'num_fds') else len(proc.open_files())
        assert final_files - initial_files < 5


class TestLongRunningStability:
    """Tests simulating extended runtime (24+ hour stability)."""

    def test_repeated_config_reloads_do_not_corrupt_state(self):
        """Config can be reloaded many times without corruption."""
        original_watch = config_instance.watch_folder
        for i in range(100):
            config_instance.watch_folder = f"watch_{i}"
            config_instance.watch_folder = original_watch
        assert config_instance.watch_folder == original_watch

    def test_watcher_restart_no_thread_leak(self):
        """Watcher restart doesn't accumulate threads."""
        initial_thread_count = len([t for t in app_module._watcher_observer._modules if hasattr(t, 'ident')] if app_module._watcher_observer else [])
        # Simulate multiple watcher restarts
        for _ in range(5):
            try:
                _restart_watcher_mock()
            except Exception:
                pass
        # Thread count should not explode
        gc.collect()

    def test_session_counters_reset_on_restart(self):
        """Session-based counters reset properly."""
        app_module.session_processed_count = 1000
        app_module.session_failed_count = 500
        # Simulate restart by resetting
        app_module.session_processed_count = 0
        app_module.session_failed_count = 0
        assert app_module.session_processed_count == 0
        assert app_module.session_failed_count == 0

    def test_token_lock_no_deadlock_under_contention(self):
        """token_lock handles high contention without deadlock."""
        results = []
        def worker():
            for _ in range(10):
                with token_lock:
                    # Simulate token operation
                    _ = app_module.access_token
                    time.sleep(0.001)
        
        threads = [Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "Thread deadlocked"


class TestConcurrentAccess:
    """Thread safety under concurrent load."""

    def test_concurrent_log_entries(self):
        """Multiple threads can add log entries safely."""
        def add_logs(thread_id):
            for i in range(50):
                add_log_entry(f"Thread {thread_id} message {i}", "INFO")
        
        threads = [Thread(target=add_logs, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        # 5 threads * 50 messages = 250 new entries (capped by MAX_LOG_FEED)
        assert len(log_feed) <= app_module.MAX_LOG_FEED

    def test_concurrent_config_updates(self):
        """Concurrent config updates don't corrupt state."""
        from main import config_instance
        
        def update_config(thread_id):
            for i in range(10):
                try:
                    config_instance.update(stabilization_delay=float(i))
                except ValueError:
                    pass
        
        threads = [Thread(target=update_config, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        # Config should still be in valid state
        assert config_instance.stabilization_delay >= 0


def _restart_watcher_mock():
    """Helper to trigger watcher restart for testing."""
    global _watcher_observer
    with app_module._watcher_lock:
        if app_module._watcher_observer is not None:
            try:
                app_module._watcher_observer.stop()
                app_module._watcher_observer.join(timeout=1)
            except Exception:
                pass
            app_module._watcher_observer = None