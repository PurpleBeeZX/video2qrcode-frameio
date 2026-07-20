"""
Tests for QR code history parsing, timecode handling, and search filtering.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from unittest.mock import MagicMock

import main as app_module
from main import (
    get_qr_history,
)


class TestTimecodeParsing:
    """Tests for timecode extraction and formatting."""

    def test_standard_timecode_parsing(self, mock_config):
        """Standard YYYYMMDDHHMMSS format parses correctly."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        # Create a mock QR file
        qr_dir = mock_config["qr_codes"]
        test_file = qr_dir / "20260706135800_test_video.mp4.png"
        test_file.write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        qr = data["qr_codes"][0]
        assert qr["timecode_human"] == "2026-07-06 13:58:00"
        assert qr["video_name"] == "test_video.mp4"

    def test_hyphen_separator_timecode(self, mock_config):
        """Timecode with hyphen separator works."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        test_file = qr_dir / "2026_0706_135800_test.mp4.png"
        test_file.write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1

    def test_malformed_timecode_fallback(self, mock_config):
        """Malformed timecodes fall back to raw string."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        test_file = qr_dir / "BADTIME_test.mp4.png"
        test_file.write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        # Malformed timecode: first 14 chars are taken as raw timecode
        assert data["qr_codes"][0]["timecode_human"] == "BADTIME_test.mp"

    def test_short_filename_skipped(self, mock_config):
        """Files shorter than 14 chars are skipped."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        test_file = qr_dir / "short.mp4.png"
        test_file.write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 0

    def test_png_extension_stripped_from_video_name(self, mock_config):
        """Double .mp4.png extension is handled correctly."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        test_file = qr_dir / "20260706135800_video.mp4.png"
        test_file.write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert data["qr_codes"][0]["video_name"] == "video.mp4"


class TestSearchFiltering:
    """Tests for timecode and video name search."""

    def test_timecode_search_exact_match(self, mock_config):
        """Exact timecode match works."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_video1.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_video2.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?timecode=20260706135800")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        assert data["qr_codes"][0]["video_name"] == "video1.mp4"

    def test_timecode_search_flexible_separators(self, mock_config):
        """Timecode search ignores separators."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_video1.mp4.png").write_bytes(b"png_data")
        
        # Test various formats
        for search in ["20260706135800", "2026/07/06 13:58:00"]:
            response = client.get(f"/api/qr-history?timecode={search}")
            assert response.status_code == 200
            data = response.json()
            assert len(data["qr_codes"]) == 1, f"Failed for search: {search}"

    def test_timecode_search_with_spaces(self, mock_config):
        """Timecode search ignores spaces."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_video1.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?timecode=20260706 135800")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1

    def test_video_name_search_partial_match(self, mock_config):
        """Video name search is case-insensitive substring match."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_MyVideo_Test.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_OtherVideo.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?video_name=myvideo")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        assert "MyVideo" in data["qr_codes"][0]["video_name"]

    def test_combined_timecode_and_video_search(self, mock_config):
        """Both filters can be applied simultaneously."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_target.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_other.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?timecode=20260706135800&video_name=target")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1

    def test_search_no_matches(self, mock_config):
        """Search with no matches returns empty list."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        response = client.get("/api/qr-history?timecode=19990101000000")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 0

    def test_results_sorted_by_filename_descending(self, mock_config):
        """Results are sorted newest first."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_old.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_new.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 2
        # Filenames are sorted in reverse order
        assert "20260706135801" in data["qr_codes"][0]["filename"]
        assert "20260706135800" in data["qr_codes"][1]["filename"]


class TestQRHistoryEdgeCases:
    """Edge cases for QR history."""

    def test_empty_qr_codes_directory(self, mock_config):
        """Empty QR codes directory returns empty list."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert data["qr_codes"] == []

    def test_nonexistent_qr_codes_directory(self, mock_config):
        """Nonexistent QR codes directory returns empty list."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        with patch("main.get_config") as mock_get_config:
            mock_cfg = MagicMock()
            mock_cfg.qr_codes_path = Path("/nonexistent/path")
            mock_get_config.return_value = mock_cfg
            
            response = client.get("/api/qr-history")
            assert response.status_code == 200
            data = response.json()
            assert data["qr_codes"] == []

    def test_filename_without_mp4_extension(self, mock_config):
        """QR files without .mp4 in name are handled."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        test_file = qr_dir / "20260706135800_justvideo.png"
        test_file.write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        assert data["qr_codes"][0]["video_name"] == "justvideo"


class TestSequenceNumberParsing:
    """Tests for sequence number extraction and persistence."""

    def test_new_format_with_sequence_number(self, mock_config):
        """New format includes sequence number in second segment."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_1_target.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_2_other.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 2
        assert data["qr_codes"][0]["seqnum"] == 2
        assert data["qr_codes"][1]["seqnum"] == 1

    def test_backward_compatible_old_format(self, mock_config):
        """Old format without sequence number is handled gracefully."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_old_style.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        # 20260706 would be parsed as a 14-char timecode? No - this filename has underscore between timecode parts
        # The format "20260706135800_old_style.mp4.png" => 14 first chars = "20260706135800" which is valid
        # parts[1] would be "old" - not a digit, so seqnum is None
        assert data["qr_codes"][0]["seqnum"] is None
        assert data["qr_codes"][0]["video_name"] == "old_style.mp4"

    def test_mixed_formats_together(self, mock_config):
        """When timecode-first format is used, seqnum is extracted from second segment."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_old.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_1_new.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 2
        seqnums = [e["seqnum"] for e in data["qr_codes"]]
        assert None in seqnums  # "old" is not a digit
        assert 1 in seqnums    # "1" is a digit


class TestSequenceNumberFiltering:
    """Tests for sequence number filter."""

    def test_seqnum_exact_match(self, mock_config):
        """Exact sequence number match filters correctly."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_1_a.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_2_b.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135802_3_c.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?seqnum=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        assert data["qr_codes"][0]["seqnum"] == 2
        assert data["qr_codes"][0]["video_name"] == "b.mp4"

    def test_seqnum_range_filter(self, mock_config):
        """Range filter (min-max) works."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_1_a.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_2_b.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135802_3_c.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135803_4_d.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?seqnum=2-3")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 2
        seqnums = {e["seqnum"] for e in data["qr_codes"]}
        assert seqnums == {2, 3}

    def test_seqnum_greater_than(self, mock_config):
        """>N filters correctly (strictly greater than)."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_1_a.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_2_b.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135802_3_c.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?seqnum=>2")
        assert response.status_code == 200
        data = response.json()
        # >2 means strictly greater than 2, so only 3 should match
        assert len(data["qr_codes"]) == 1
        assert data["qr_codes"][0]["seqnum"] == 3

    def test_seqnum_less_than(self, mock_config):
        """<N filters correctly (strictly less than)."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_1_a.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_2_b.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135802_3_c.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?seqnum=<2")
        assert response.status_code == 200
        data = response.json()
        # <2 means strictly less than 2, so only 1 should match
        assert len(data["qr_codes"]) == 1
        assert data["qr_codes"][0]["seqnum"] == 1

    def test_seqnum_skips_files_without_sequence(self, mock_config):
        """When seqnum filter is active, files without sequence numbers are skipped."""
        from fastapi.testclient import TestClient
        client = TestClient(app_module.app)
        
        qr_dir = mock_config["qr_codes"]
        (qr_dir / "20260706135800_old.mp4.png").write_bytes(b"png_data")
        (qr_dir / "20260706135801_1_new.mp4.png").write_bytes(b"png_data")
        
        response = client.get("/api/qr-history?seqnum=1")
        assert response.status_code == 200
        data = response.json()
        assert len(data["qr_codes"]) == 1
        assert data["qr_codes"][0]["seqnum"] == 1


class TestExtractSequenceNumber:
    """Unit tests for the sequence number extraction helper."""

    def test_extracts_valid_sequence(self):
        """Valid sequence number from second segment is extracted."""
        seq = app_module.extract_sequence_number("20260706135800_3_video.mp4.png")
        assert seq == 3

    def test_missing_sequence_returns_none(self):
        """Returns None when no sequence number is present."""
        seq = app_module.extract_sequence_number("20260706135800_video.mp4.png")
        assert seq is None

    def test_single_digit_sequence(self):
        """Single digit sequence is handled."""
        seq = app_module.extract_sequence_number("20260706135800_5_video.mp4.png")
        assert seq == 5

    def test_multi_digit_sequence(self):
        """Multi-digit sequence is handled."""
        seq = app_module.extract_sequence_number("20260706135800_123_video.mp4.png")
        assert seq == 123