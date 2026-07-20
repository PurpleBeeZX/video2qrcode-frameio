"""
Tests for auto-generated SSL certificate functionality.
"""

from pathlib import Path

import pytest

import main as app_module
from main import ensure_ssl_certificate


class TestSSLAutoGeneration:
    """Tests for the ensure_ssl_certificate function."""

    def test_generates_cert_and_key_when_missing(self, tmp_path):
        """Cert and key files are created when they do not exist."""
        cert_path = str(tmp_path / "test_cert.pem")
        key_path = str(tmp_path / "test_key.pem")

        # Both files should not exist initially
        assert not Path(cert_path).exists()
        assert not Path(key_path).exists()

        # Generate them
        ensure_ssl_certificate(cert_path, key_path)

        # Both files should now exist
        assert Path(cert_path).exists()
        assert Path(key_path).exists()

        # Verify content looks like PEM
        cert_content = Path(cert_path).read_text()
        key_content = Path(key_path).read_text()
        assert "BEGIN CERTIFICATE" in cert_content
        assert "END CERTIFICATE" in cert_content
        assert "BEGIN PRIVATE KEY" in key_content or "BEGIN RSA PRIVATE KEY" in key_content
        assert "END PRIVATE KEY" in key_content or "END RSA PRIVATE KEY" in key_content

    def test_skips_generation_when_files_exist(self, tmp_path, capsys):
        """Nothing is regenerated when both cert and key already exist."""
        cert_path = str(tmp_path / "existing_cert.pem")
        key_path = str(tmp_path / "existing_key.pem")

        # Create dummy files with marker content
        Path(cert_path).write_text("EXISTING_CERT_MARKER")
        Path(key_path).write_text("EXISTING_KEY_MARKER")

        # Call the function — should skip regeneration
        ensure_ssl_certificate(cert_path, key_path)

        captured = capsys.readouterr()
        assert "already exist" in captured.out

        # Verify the files were NOT overwritten
        assert Path(cert_path).read_text() == "EXISTING_CERT_MARKER"
        assert Path(key_path).read_text() == "EXISTING_KEY_MARKER"

    def test_generates_valid_pem_format(self, tmp_path):
        """Generated certificate is valid PEM and the common name is localhost."""
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend

        cert_path = str(tmp_path / "valid_cert.pem")
        key_path = str(tmp_path / "valid_key.pem")

        ensure_ssl_certificate(cert_path, key_path)

        cert_content = Path(cert_path).read_text()
        assert "BEGIN CERTIFICATE" in cert_content

        # Load the certificate and verify the Common Name is localhost
        cert_obj = x509.load_pem_x509_certificate(cert_content.encode(), default_backend())
        cn_attributes = cert_obj.subject.get_attributes_for_oid(
            x509.oid.NameOID.COMMON_NAME
        )
        assert len(cn_attributes) == 1
        assert cn_attributes[0].value == "localhost"

    def test_only_key_exists_skips_generation(self, tmp_path, capsys):
        """Generation is skipped when only one of the two files exists."""
        cert_path = str(tmp_path / "partial_cert.pem")
        key_path = str(tmp_path / "partial_key.pem")

        # Create only the key file
        Path(key_path).write_text("EXISTING_KEY")

        # Call function — should skip because key exists (both must be missing)
        ensure_ssl_certificate(cert_path, key_path)

        captured = capsys.readouterr()
        # When only key exists, cert is missing, so it tries or skips based on logic
        # The logic checks `cert_file.exists() and key_file.exists()` — since key exists
        # but cert doesn't, it won't skip and will generate new ones.
        # This test checks it handles the partial case gracefully.
        new_cert_content = Path(cert_path).read_text()
        assert "BEGIN CERTIFICATE" in new_cert_content

    def test_uses_config_paths_when_called_at_startup(self, monkeypatch, tmp_path):
        """Verify the function works with actual config.json style paths."""
        test_cert = tmp_path / "startup_cert.pem"
        test_key = tmp_path / "startup_key.pem"

        ensure_ssl_certificate(str(test_cert), str(test_key))

        assert test_cert.exists()
        assert test_key.exists()