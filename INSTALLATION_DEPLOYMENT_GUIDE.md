# Installation & Deployment Guide
**Frame.io V4 QR Code Automation System**

---

## 1. System Requirements & Prerequisites

### Operating System
- **Windows 10/11** (required for standalone `.exe` compilation)
- The application sets `asyncio.WindowsSelectorEventLoopPolicy()` on Windows to avoid ProactorEventLoop shutdown errors.

### Python Version
- **Python 3.11+** (tested with Python 3.12)

### Core Dependencies
Listed in `requirements.txt`. Key libraries:
- **FastAPI** — web server and API
- **Uvicorn** — ASGI server
- **Watchdog** — filesystem watcher for the `watch_folder/`
- **qrcode** — QR code PNG generation
- **httpx** — async HTTP client for Frame.io V4 API
- **cryptography** — SSL certificate auto-generation
- **python-dotenv** — `.env` file loading (development only)
- **PyInstaller** — standalone `.exe` compilation (build-time only)

### Installing Dependencies
```powershell
python -m pip install -r requirements.txt
```

---

## 2. Local Environment Setup (Development)

### Step 1: Clone the Repository
```powershell
git clone <repository-url>
cd frame_io_version_qr_code
```

### Step 2: Install Python Dependencies
```powershell
python -m pip install -r requirements.txt
```

### Step 3: Create Local Environment File
```powershell
copy .env.example .env
```

Edit `.env` and fill in your credentials:
```env
FRAMEIO_CLIENT_ID=your_actual_client_id
FRAMEIO_CLIENT_SECRET=your_actual_client_secret
FRAMEIO_FOLDER_ID=your_actual_folder_id
```

> **Note:** `FRAMEIO_FOLDER_ID` is optional. If left empty, the system will auto-resolve it at runtime via the folder setup wizard.

### Step 4: Run in Development Mode
```powershell
python main.py
```

- The server starts on **`https://localhost:8000`**
- On first run with no `config.json`, the application enters **`setup_mode`**
- In setup mode, the terminal will prompt you for credentials interactively, OR you can skip and use the web wizard.
- The browser opens automatically to `/configure` (setup mode) or the control panel (`/`) if already configured.

---

## 3. The Setup Wizard (First-Run Experience)

### No `config.json`? → Setup Mode
When `config.json` does not exist in the working directory, the application sets `setup_mode = True` and:
1. Prints a first-run CLI prompt in the terminal (dev mode only)
2. Opens the browser to `/configure` automatically

### CLI Prompt (Development Mode Only)
When running `python main.py` without a `config.json`, you will see:
```
============================================================
  Frame.io QR Code Uploader — First-Time Setup
============================================================

No configuration file (config.json) was found.

You can either:
  1. Enter your credentials now (recommended for headless/terminals)
  2. Skip and use the web-based setup wizard instead

Get your credentials from: https://developer.adobe.com/console/

Enter your Frame.io Client ID [or press Enter to use web wizard]:
```
- Enter credentials → `config.json` is written → restart to apply
- Press Enter → browser opens `/configure` for web-based setup

### Web Setup Wizard (`/configure`)
The web wizard at `https://localhost:8000/configure` allows you to:
- Enter **Client ID** and **Client Secret** (required)
- Optionally enter a **Folder ID** (advanced — can be left empty)
- Configure server port, SSL paths, and folder locations

Fields marked with defaults:
- **Server Port:** `8000`
- **Watch Folder:** `data/watch_folder`
- **Processed Folder:** `data/processed_folder`
- **Failed Folder:** `data/failed_folder`
- **QR Codes Folder:** `data/qr_codes`

### Folder Setup Wizard (`/setup-folder`)
After saving credentials and clicking **"Log in with Frame.io"**, the OAuth flow completes. If no `folder_id` is configured, you are redirected to `/setup-folder`.

This is a **3-step guided wizard**:

1. **Select Workspace** — chooses from your Frame.io account workspaces
2. **Select Project** — chooses a project within that workspace
3. **Confirm & Auto-create** — enter a folder name (default: `qr_code_uploader`), then click **"Create / Use Folder & Start System"**

#### How Auto-Creation Works
The backend:
1. Calls `GET /v4/accounts/{aid}/workspaces` — lists workspaces
2. Calls `GET /v4/accounts/{aid}/workspaces/{wid}/projects` — lists projects and their `root_folder_id`
3. Calls `GET /v4/accounts/{aid}/folders/{root_folder_id}/folders` (paginated) — scans existing folders
4. If a folder with the exact name already exists → **reuses it** (case-sensitive match)
5. If not found → `POST /v4/accounts/{aid}/folders/{root_folder_id}/folders` with `{"data": {"name": "..."}}`
6. Saves the resulting `folder_id` and `folder_name` to `config.json`

> **Note:** Frame.io allows duplicate folder names. The wizard scans all pages (up to 100 per page) and uses the first exact case-sensitive match. If you need a specific folder, use the **advanced Folder ID** field in `/configure` instead.

---

## 4. SSL Configuration

The application uses **HTTPS** with auto-generated self-signed certificates.

### Auto-Generation
On startup, `ensure_ssl_certificate(cert_path, key_path)` checks if `cert.pem` and `key.pem` exist. If missing, it:
1. Generates a 2048-bit RSA private key
2. Creates a self-signed X.509 certificate valid for 365 days
3. Sets **Common Name = `localhost`** and Subject Alternative Names for `localhost` and `127.0.0.1`
4. Writes both files to the current working directory

The first launch will be slightly slower while generating the certificate.

### For Deployment
Copy the generated `cert.pem` and `key.pem` alongside the executable, or place them next to `config.json`.

---

## 5. Standalone Compilation (`.exe` Build)

### Overview
`build_exe.py` compiles the application into a single standalone Windows executable using PyInstaller.

### Prerequisites for Building
- Python 3.11+ on Windows
- PyInstaller installed (`pip install pyinstaller==6.4.0` or latest)
- A valid `.env` file in the project root with at least:
  ```env
  FRAMEIO_CLIENT_ID=your_actual_client_id
  FRAMEIO_CLIENT_SECRET=your_actual_client_secret
  ```

> **Important:** `FRAMEIO_FOLDER_ID` is **not required** for building. If omitted, the EXE will guide the user through the folder setup wizard on first login.

### Build Process

#### Option A: Using the Batch Wrapper
Double-click `build_exe.bat` or run:
```powershell
build_exe.bat
```
This will:
1. Check/install PyInstaller
2. Run `python build_exe.py`
3. Pause on completion or failure

#### Option B: Direct Python Execution
```powershell
python build_exe.py
```

### What the Build Script Does
1. **Loads `.env`** — reads `FRAMEIO_*` variables from the local `.env` file
2. **Creates `config_build.json`** — bundles credentials and settings into a temporary JSON file
3. **Runs PyInstaller** with:
   - `--onefile` — single EXE output
   - `--add-data` — bundles `templates/`, `config_build.json`
   - `--hidden-import` — ensures all submodules are included
   - `--console` — console window for logs
4. **Deletes `config_build.json`** — the temporary file is removed after bundling

### Build Enforcement
The build will **fail** if `FRAMEIO_CLIENT_ID` or `FRAMEIO_CLIENT_SECRET` are missing or set to placeholder values (`YOUR_CLIENT_ID`, `YOUR_CLIENT_SECRET`).

### Output
- Executable: `dist/FrameIO_Uploader.exe`

### Deployment to Staff Machines
1. Copy `FrameIO_Uploader.exe` to the target PC
2. Place your `config.json` (with valid credentials) in the **same folder** as the EXE
   - Alternatively, set environment variables `FRAMEIO_CLIENT_ID` and `FRAMEIO_CLIENT_SECRET` on the target machine
3. Run `FrameIO_Uploader.exe`
4. On first launch, the EXE extracts its bundled templates and starts the server

### Runtime Data Folder
When deployed, the EXE creates and uses the following **relative to the EXE location** (unless overridden in `config.json` or `.env`):
```
data/
├── watch_folder/       ← drop MP4 files here
├── processed_folder/   ← successfully uploaded files
├── failed_folder/      ← files that failed after max retries
└── qr_codes/           ← generated PNG QR codes
```

#### QR Codes Folder Details
Contains PNG QR codes. Each file is named: `{timecode}_{seq}_{video}.png` where:
- `{timecode}` is a 14-digit timestamp (YYYYMMDDHHMMSS without underscores)
- `{seq}` is the sequence number (incrementing counter)
- `{video}` is the original video filename stem

For example: `20260717160735_5_myvideo.mp4` represents sequence 5 uploaded on July 17, 2026 at 16:07:35.

These QR codes are:
- Served statically at `https://localhost:8000/qr_codes/`
- Listed in the control panel's QR History table with metadata (sequence, timecode, video name, status)

`config.json`, `cert.pem`, `key.pem`, and `message.txt` (if used) are stored next to the EXE (or in the working directory).

---

## 6. Configuration Reference

### Environment Variables (`FRAMEIO_*`)
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FRAMEIO_CLIENT_ID` | Build-time | `YOUR_CLIENT_ID` | Adobe IMS Client ID |
| `FRAMEIO_CLIENT_SECRET` | Build-time | `YOUR_CLIENT_SECRET` | Adobe IMS Client Secret |
| `FRAMEIO_FOLDER_ID` | No | `""` | Frame.io folder ID (empty = auto-setup) |
| `FRAMEIO_FOLDER_NAME` | No | `qr_code_uploader` | Folder name for auto-creation |
| `FRAMEIO_SERVER_HOST` | No | `0.0.0.0` | Bind address |
| `FRAMEIO_SERVER_PORT` | No | `8000` | Port number |
| `FRAMEIO_SSL_CERTFILE` | No | `cert.pem` | SSL certificate path |
| `FRAMEIO_SSL_KEYFILE` | No | `key.pem` | SSL private key path |
| `FRAMEIO_WATCH_FOLDER` | No | `data/watch_folder` | Watch folder path |
| `FRAMEIO_PROCESSED_FOLDER` | No | `data/processed_folder` | Processed folder path |
| `FRAMEIO_FAILED_FOLDER` | No | `data/failed_folder` | Failed folder path |
| `FRAMEIO_QR_CODES_FOLDER` | No | `data/qr_codes` | QR codes folder path |
| `FRAMEIO_REDIRECT_URI` | No | `https://localhost:8000/callback` | OAuth redirect URI |
| `FRAMEIO_AUTH_URL` | No | Adobe IMS auth URL | OAuth authorization endpoint |
| `FRAMEIO_TOKEN_URL` | No | Adobe IMS token URL | OAuth token endpoint |
| `FRAMEIO_OAUTH_SCOPE` | No | `offline_access,openid,email,profile,additional_info.roles` | OAuth scopes |

### `config.json` Structure

```json
{
  "frameio": {
    "client_id": "your_client_id",
    "client_secret": "your_client_secret",
    "folder_id": "folder_id_here",
    "folder_name": "qr_code_uploader"
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8000,
    "ssl_certfile": "data/cert.pem",
    "ssl_keyfile": "data/key.pem",
    "log_file": "data/automation.log",
    "display_message_path": "data/message.txt"
  },
  "folders": {
    "watch": "data/watch_folder",
    "processed": "data/processed_folder",
    "failed": "data/failed_folder",
    "qr_codes": "data/qr_codes"
  },
  "oauth": {
    "redirect_uri": "https://localhost:8000/callback",
    "auth_url": "https://ims-na1.adobelogin.com/ims/authorize/v2",
    "token_url": "https://ims-na1.adobelogin.com/ims/token/v3",
    "scope": "offline_access,openid,email,profile,additional_info.roles"
  },
  "share": {
    "expiration_days": 7
  }
}
```

---

## 7. Security Notes

- **Never commit** `config.json`, `.env`, `cert.pem`, `key.pem` to version control
- These files are listed in `.gitignore`
- `.env.example` is committed as a template with placeholder values
- The bundled `config_build.json` inside the EXE contains real credentials — do not distribute the EXE publicly

---

## 8. Troubleshooting

### Build fails with "BUILD FAILED: Missing required credentials"
- Ensure `.env` exists in the project root with `FRAMEIO_CLIENT_ID` and `FRAMEIO_CLIENT_SECRET`
- Do not use placeholder values (`YOUR_CLIENT_ID`) in `.env`

### Browser shows "ERR_CONNECTION_REFUSED" or SSL warning
- The app uses a self-signed certificate. Accept the browser warning for `localhost`.
- Ensure port `8000` is not blocked by firewall.
- Check that `cert.pem` and `key.pem` exist in the working directory.

### Setup wizard loops or fails at folder creation
- Verify the user has at least one workspace and one project in their Frame.io account
- Check the browser console and server logs for API error details
- Ensure the Adobe IMS token has `offline_access` scope

### Files not being processed
- Check that the `watch_folder/` exists and the watchdog observer is running (logs: `[WATCHER] Live monitoring started on ...`)
- Ensure the user is logged in (`/status` shows `"authenticated": true`)
- Verify `FRAMEIO_FOLDER_ID` is set (check `config.json` or `/api/setup/status`)

### Queue shows duplicate entries after configuration change
- This was a known issue in earlier versions. It has been fixed in the current version.
- If you still see duplicates, restart the server to clear the in-memory queue.

### QR code generated but shows "Uploading" status
- This is **expected behavior**. The QR code is created before the video upload finishes (fast path).
- The "Uploading" status will change to "Ready" once the S3 upload completes.
- The QR code is fully functional even while the upload is in progress.
