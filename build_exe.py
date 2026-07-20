"""
Build standalone executable for Frame.io QR Code Uploader.
Run: python build_exe.py

This script reads FRAMEIO_* environment variables from a local .env file
(if present) and bundles them as config_build.json inside the EXE so the
compiled artifact starts pre-configured without any plaintext secrets in
the public repository.
"""
import json
import os
import shutil
import subprocess
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Get the directory where this script is located
script_dir = os.path.dirname(os.path.abspath(__file__))

# Load developer's private .env for build-time defaults (if available)
env_path = os.path.join(script_dir, ".env")
if load_dotenv and os.path.exists(env_path):
    load_dotenv(env_path)
elif os.path.exists(env_path):
    print("[BUILD] WARNING: python-dotenv not installed; .env file will be ignored.")
    print("[BUILD] Install with: pip install python-dotenv")

# Build a temporary config_build.json from environment variables so the
# compiled EXE can ship with pre-configured defaults without any hardcoded
# credentials in source control.
build_config = {
    "frameio": {
        "client_id": os.getenv("FRAMEIO_CLIENT_ID", "YOUR_CLIENT_ID"),
        "client_secret": os.getenv("FRAMEIO_CLIENT_SECRET", "YOUR_CLIENT_SECRET"),
        "folder_id": os.getenv("FRAMEIO_FOLDER_ID", ""),
    },
    "server": {
        "host": os.getenv("FRAMEIO_SERVER_HOST", "0.0.0.0"),
        "port": int(os.getenv("FRAMEIO_SERVER_PORT", "8000")),
        "ssl_certfile": os.getenv("FRAMEIO_SSL_CERTFILE", "data/cert.pem"),
        "ssl_keyfile": os.getenv("FRAMEIO_SSL_KEYFILE", "data/key.pem"),
        "log_file": os.getenv("FRAMEIO_LOG_FILE", "data/automation.log"),
    },
    "folders": {
        "templates": "templates",
        "watch": os.getenv("FRAMEIO_WATCH_FOLDER", "data/watch_folder"),
        "processed": os.getenv("FRAMEIO_PROCESSED_FOLDER", "data/processed_folder"),
        "failed": os.getenv("FRAMEIO_FAILED_FOLDER", "data/failed_folder"),
        "qr_codes": os.getenv("FRAMEIO_QR_CODES_FOLDER", "data/qr_codes"),
    },
    "oauth": {
        "redirect_uri": os.getenv("FRAMEIO_REDIRECT_URI", "https://localhost:8000/callback"),
        "auth_url": os.getenv("FRAMEIO_AUTH_URL", "https://ims-na1.adobelogin.com/ims/authorize/v2"),
        "token_url": os.getenv("FRAMEIO_TOKEN_URL", "https://ims-na1.adobelogin.com/ims/token/v3"),
        "scope": os.getenv("FRAMEIO_OAUTH_SCOPE", "offline_access,openid,email,profile,additional_info.roles"),
    },
}

bundled_config_path = os.path.join(script_dir, "config_build.json")
with open(bundled_config_path, "w", encoding="utf-8") as f:
    json.dump(build_config, f, indent=2)

# ---------------------------------------------------------------------------
# Enforce credentials — fail the build if real secrets are not provided.
# This prevents accidentally shipping an EXE that requires manual setup.
# NOTE: FRAMEIO_FOLDER_ID is NOT required here — it is auto-resolved at
# runtime via the folder setup wizard (workspace/project selection).
# ---------------------------------------------------------------------------
MISSING = []
if build_config["frameio"]["client_id"] in ("", "YOUR_CLIENT_ID"):
    MISSING.append("FRAMEIO_CLIENT_ID")
if build_config["frameio"]["client_secret"] in ("", "YOUR_CLIENT_SECRET"):
    MISSING.append("FRAMEIO_CLIENT_SECRET")

if MISSING:
    print()
    print("=" * 50)
    print(" BUILD FAILED: Missing required credentials")
    print("=" * 50)
    print()
    print("The following environment variable(s) must be set in your .env file:")
    for var in MISSING:
        print(f"   • {var}")
    print()
    print("Create a .env file in the project root with:")
    print()
    print("  FRAMEIO_CLIENT_ID=your_actual_client_id")
    print("  FRAMEIO_CLIENT_SECRET=your_actual_client_secret")
    print()
    print("Then re-run: python build_exe.py")
    print()
    # Clean up the temp config_build.json
    if os.path.exists(bundled_config_path):
        os.remove(bundled_config_path)
    sys.exit(1)
else:
    print("[BUILD] ✓ FRAMEIO_CLIENT_ID and FRAMEIO_CLIENT_SECRET found.")
    if build_config["frameio"]["folder_id"] in ("", "YOUR_FOLDER_ID"):
        print("[BUILD]   (FRAMEIO_FOLDER_ID not set — will be auto-resolved at runtime)")
    else:
        print("[BUILD]   (FRAMEIO_FOLDER_ID provided — will be used directly)")

# PyInstaller command as a list (avoids shell quoting issues)
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--name", "FrameIO_Uploader",
    "--add-data", f"templates{os.pathsep}templates",
    "--add-data", f"requirements.txt{os.pathsep}.",
    "--add-data", f"config_build.json{os.pathsep}.",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.loops.asyncio",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.http.h11_impl",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.protocols.websockets.wsproto",
    "--hidden-import", "uvicorn.middleware.debug",
    "--hidden-import", "uvicorn.middleware.proxy_headers",
    "--hidden-import", "uvicorn.middleware.wsgi",
    "--hidden-import", "cryptography.hazmat.backends.openssl",
    "--hidden-import", "cryptography.hazmat.primitives.asymmetric",
    "--hidden-import", "cryptography.hazmat.primitives.serialization",
    "--hidden-import", "cryptography.x509",
    "--hidden-import", "jinja2",
    "--hidden-import", "jinja2.ext",
    "--hidden-import", "qrcode",
    "--hidden-import", "httpx",
    "--hidden-import", "watchdog.observers",
    "--hidden-import", "watchdog.events",
    "--collect-all", "watchfiles",
    "--console",
    "--log-level", "DEBUG",
    "main.py",
]

print("=" * 50)
print(" Building Frame.io QR Code Uploader")
print("=" * 50)
print()

# Clean previous builds
for d in ["dist", "build"]:
    p = os.path.join(script_dir, d)
    if os.path.exists(p):
        print(f"[BUILD] Removing {d}...")
        shutil.rmtree(p, ignore_errors=True)

spec_file = os.path.join(script_dir, "FrameIO_Uploader.spec")
if os.path.exists(spec_file):
    os.remove(spec_file)

print("[BUILD] Starting PyInstaller build...")
print("[BUILD] This may take several minutes...")
print()

result = subprocess.run(cmd, cwd=script_dir, capture_output=False)

# Clean up temporary build config so it doesn't end up in source control
if os.path.exists(bundled_config_path):
    os.remove(bundled_config_path)

if result.returncode != 0:
    print()
    print("[ERROR] PyInstaller build failed!")
    sys.exit(1)

print()
print("[BUILD] Build successful!")
print()
print("=" * 50)
print(" Output: dist\\FrameIO_Uploader.exe")
print("=" * 50)
print()
print("The executable is: dist\\FrameIO_Uploader.exe")
print()
print("To deploy to another machine:")
print("  1. Copy FrameIO_Uploader.exe to the target PC")
print("  2. Place config.json (with valid credentials) in the same folder")
print("  3. Run FrameIO_Uploader.exe -- no Python needed!")
print()
print("NOTE: First launch will be slower as it extracts itself.")
print()
