# User Operation Manual
**Frame.io V4 QR Code Automation System**

---

## 1. System Overview

The Frame.io QR Code Automation System watches a folder on your computer for new MP4 video files. When a file is detected, it automatically:

1. **Renames** the file with a timestamp prefix to ensure uniqueness
2. **Initiates** upload to Frame.io via the V4 API (creates asset placeholder)
3. **Creates** a public share link with configurable expiration (default: 7 days)
4. **Generates** a QR code PNG linking to that share (fast path — before S3 upload completes)
5. **Uploads** the file bytes to S3 (background operation)
6. **Moves** the original file to a processed folder on success

A **customer-facing display screen** can show the latest QR code on a second monitor for easy scanning.

### Data Flow
```
[Watch Folder] → [Initiate Upload] → [Create Share Link] → [Generate QR Code] → [Customer Screen]
                       ↓                       ↓
                [Move to processing]   [fast path: QR ready before upload finishes]
                       ↓                       
                [S3 Upload (background)]
                       ↓
             [retry on failure (max_retries)]
                       ↓
             [Failed Folder] (after exhausting retries)
                       ↓
            [Processed Folder] (on success)
```

**Note:** The QR code is generated as soon as the share link is created (typically within a few seconds), while the actual video file upload continues in the background. This means staff can display the QR code before the upload finishes.

---

## 2. Launching the System

### Starting the Application
Run the executable (`FrameIO_Uploader.exe`) or `python main.py`. The server starts on **`https://localhost:8000`**.

- On **first launch** with no `config.json`, the browser opens to the **Setup Wizard** (`/configure`).
- After configuration is complete, the browser opens to the **Staff Control Panel** (`/`).
- A second tab **"Open Customer Display"** opens automatically if enabled.

### First-Run CLI Prompt (Development Mode Only)

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

**Option 1: Enter credentials interactively**
- Enter your Frame.io Client ID
- Enter your Client Secret
- Optionally enter a Folder ID (or press Enter to configure later)
- Enter a target folder name (default: `qr_code_uploader`)
- The system writes `config.json` and prompts you to restart

**Option 2: Skip to web wizard**
- Press Enter at the Client ID prompt
- The browser opens to `/configure` for web-based setup

---

## 3. Logging In

### Using the Staff Control Panel

1. Click **"Log in with Frame.io"** button (top right of control panel)
2. You are redirected to **Adobe IMS** (Frame.io's authentication provider)
3. Sign in with your Adobe/Facebook/Google credentials linked to Frame.io
4. You are redirected back to the control panel, now authenticated

**What happens after login:**
- If `folder_id` is configured → you go directly to the control panel (`/`)
- If `folder_id` is missing → you are redirected to the **Folder Setup Wizard** (`/setup-folder`)

**Note:** Tokens are refreshed automatically when they expire (default: 24 hours). You will be prompted to re-login only if the refresh fails.

---

## 4. Using the Staff Control Panel

Access the control panel at: **`https://localhost:8000`**

### 4.1 Connection Status Card

**Location:** Top of the page

**States:**
- **System Active - Logged in as [Name]** (green) — fully operational, uploads are running
- **⏳ Waiting for Staff Login** (yellow) — not authenticated; uploads are paused

**Actions:**
- Click **"Log in with Frame.io"** to authenticate
- Click **"Open Customer Display"** to open the customer screen in a new tab

---

### 4.2 Quick Stats

**Location:** Left column, below the status card

Three stat cards show live counters:
- **Processed** — number of files successfully uploaded since the server started (resets to 0 on restart)
- **QR Codes** — total number of QR code PNG files in the qr_codes folder (persists across restarts)
- **Failed** — number of files that failed after all retries since the server started (resets to 0 on restart)

**Note:** These are display-only values that update every 2 seconds.

---

### 4.3 Upload Queue & Currently Uploading

**Location:** Right sidebar, below Quick Stats

#### Currently Uploading
Shows the file currently being processed:
- **File name** (with timestamp prefix added by the system)
- **Original file size**
- **Status badge** — "Uploading" (yellow, pulsing indicator)
- **QR code thumbnail** — appears once the QR is generated

**When does a file appear here?**
- After the file is detected and moved to `processing_folder`
- During both Frame.io asset creation AND S3 byte upload
- Clears automatically when processing completes

#### Upload Queue
Shows files waiting to be processed:
- **File name** (with timestamp prefix)
- **Original file size** (human-readable format)
- **Status badge** — "Queuing" (orange, pulsing indicator)

**Behavior:**
- Files appear here after the stabilization delay (default: 2 seconds)
- Only one file is processed at a time (sequential queue)
- The queue empties as files complete processing
- Max retries (default: 5) with exponential backoff on failure

**Note:** The queue is display-only and does not accept manual reordering. Files are processed in the order they are detected.

---

### 4.4 QR Code Library

**Location:** Left column, below Activity Log

The **📋 Uploaded Videos (QR History)** panel shows **all generated QR codes** (no limit), most recent first.

**QR Code Table Columns:**
- **Seq** — sequence number (incrementing counter for each upload)
- **Timecode** — human-readable timestamp (e.g., `2026/07/03 15:22:28`)
- **Video Name** — original video filename without `.mp4` extension
- **Status** — current upload state (Ready/Uploading/Queuing)
- **Action** — "Display" or "Deselect" button

**Status Badges:**
- **Ready** (green) — upload completed, QR code available
- **Uploading** (yellow, pulsing) — currently being uploaded to Frame.io
- **Queuing** (orange, pulsing) — waiting to start upload

**QR Code Actions:**
- **Click "Display"** on a QR code → shows it on the customer screen (activates Manual Override)
- **Click "Deselect"** (or click the same QR again) → returns to automatic mode
- The selected QR code is **highlighted with blue left border and background** in the table
- Customer screen shows the sequence number, video name, and timecode below the QR code

**Search QR Codes:**
Use the search boxes above the table to filter QR codes by:
- **Sequence** — e.g., `5` for a specific number, `5-10` for a range, or `>10` for numbers greater than 10
- **Timecode** — e.g., `20260706_135800` or `2026-07-06 13:58:00`
- **Video Name** — partial match on the original filename

The search updates the table in real-time as you type.

**Note:** Because the QR code is generated before the S3 upload finishes, you may see a QR code with "Uploading" status. This is expected — the QR is ready to use immediately.

---

### 4.5 Manual Override & Queue

**Purpose:** Lock a specific QR code on the customer screen instead of showing the latest one.

**Default Behavior:**
- The customer screen **automatically** shows the latest QR code
- When a new QR is generated, it replaces the previous one

**To activate Manual Override:**
1. Find the desired QR in the **QR Code Library**
2. Click it — the system activates **Manual Override**
3. A **🔒 Manual Screen Lock Active** banner appears at the top
4. The selected QR stays on screen until you release it
5. The button text changes from "Display" to "Deselect"

**Behavior during Override:**
- New uploads are **queued** in the background (stored in memory)
- The queued QR codes are not displayed immediately
- The log shows: `[DISPLAY] QR queued (manual override active)`
- The status banner turns yellow with a lock icon

**To release the screen:**
1. Click the **"Release Screen"** button in the yellow banner
2. If the queue has more items, the next queued QR is shown automatically
3. If the queue is empty, the **latest QR** is shown and auto-mode resumes
4. The banner disappears and auto-mode is restored

**Note:** You can also deselect by clicking the same QR code again in the library.

---

### 4.6 Display Message Control

**Location:** Below the Incomplete Config Warning banner, above the Main Content Grid

**Purpose:** Display a custom message on the customer screen (below the "Scan to Download" header).

**Default Message:** "Point your camera at the QR code to access your video"

**How to use:**
1. Type your custom message in the text input field
2. Click **"Update Display"** button to save the message
3. The message appears immediately on the customer display screen
4. A green checkmark "✓ Message updated!" confirms successful save

**Message Persistence:**
- The message is saved to `data/message.txt` (configurable via settings)
- Survives server restarts
- If the file is deleted or missing, the default message is shown

**Example Use Cases:**
- Welcome messages: "Welcome to our exhibition! Scan to view today's featured videos"
- Event-specific announcements: "Today's special preview — scan to watch"
- Instructions: "Please scan the QR code below to access your video"

**Note:** Changes take effect immediately. The customer screen polls for updates every 1.5 seconds.

---

### 4.7 Shutdown

**Location:** Bottom of the control panel

**Action:**
1. Click the **🛑 Shutdown Server** button
2. Confirm in the dialog ("Are you sure you want to stop the exhibition server?")
3. The button shows "Shutting down..." during shutdown
4. The page displays: "Server has shut down safely. You can now close this browser window."

**What happens:**
- The file watcher stops cleanly
- All background threads are terminated
- The server process exits

**Note:** You can restart the server by running the executable or `python main.py` again.

---

## 5. Customer Display Screen

**Location:** Opens at `https://localhost:8000/customer` (second monitor/tab)

This is a **read-only display** for visitors. It shows:
- The current QR code (centered on screen)
- The video name and upload time below the QR code
- Status indicator (ready, locked, or waiting)

**Staff cannot interact with this screen** — it is controlled entirely from the Staff Control Panel.

---

## 6. Configuration Page

**Access:** Click **"⚙️ Configure"** button or navigate to `https://localhost:8000/configure`

This page allows you to modify system settings. All changes take effect immediately.

### 6.1 Frame.io Settings

| Field | Required | Description |
|-------|----------|-------------|
| **Client ID** | Yes | Adobe IMS Client ID from your Frame.io developer app |
| **Client Secret** | Yes | Adobe IMS Client Secret |
| **Folder ID** | No | Advanced — leave empty to use folder wizard |

**Notes:**
- Changing Client ID or Secret requires re-authentication
- Leaving Folder ID empty enables the folder setup wizard after login

### 6.2 Server Settings

| Field | Default | Description |
|-------|---------|-------------|
| **Host** | `0.0.0.0` | Bind address (`0.0.0.0` for all interfaces) |
| **Port** | `8000` | Server port number |
| **SSL Cert** | `data/cert.pem` | Path to SSL certificate file |
| **SSL Key** | `data/key.pem` | Path to SSL private key file |
| **Log File** | `data/automation.log` | Path to application log file |
| **Display Message File Path** | `data/message.txt` | Path to the file storing the display message |

**Notes:**
- Changing host/port requires a server restart
- The server must restart to bind to the new port
- SSL certificates are auto-generated if missing
- Log file is appended to on restart

### 6.3 Folder Settings

| Field | Default | Description |
|-------|---------|-------------|
| **Watch Folder** | `data/watch_folder` | Drop MP4 files here for processing |
| **Processed Folder** | `data/processed_folder` | Successfully uploaded files are moved here |
| **Failed Folder** | `data/failed_folder` | Files that failed after all retries |
| **QR Codes Folder** | `data/qr_codes` | Generated QR code PNGs are saved here |

**Notes:**
- All folder paths can be absolute or relative
- Relative paths are resolved from the application directory
- Changing the Watch Folder triggers an immediate catch-up scan

### 6.4 Share Settings (Expiration)

| Field | Default | Description |
|-------|---------|-------------|
| **Share Expiration (days)** | `7` | Number of days until Frame.io share links expire |

**Behavior:**
- This setting controls the `expiration` field when creating share links via the Frame.io V4 API
- The expiration is set at share creation time and cannot be modified afterward
- Lower values (e.g., 1-3 days) are recommended for exhibition environments where links should expire quickly
- The value is saved to `config.json` and persists across restarts

**Example:** Setting to `3` means QR codes will stop working after 3 days.

### 6.5 Saving Changes

Click **"Save Configuration"** to:
- Write settings to `config.json`
- Reload all configuration values
- Restart the file watcher if folder paths changed

**Immediate effects:**
- The file watcher restarts automatically with the new settings
- All other changes take effect immediately
- **Note:** Saving configuration does NOT re-scan the watch folder (prevents duplicate queue entries)

**Example scenario:**
If you change the Watch Folder from `data/watch_folder` to `/mnt/usb/videos`:
1. The watcher stops monitoring the old folder
2. The watcher starts monitoring `/mnt/usb/videos`
3. New files dropped in the new folder will be detected automatically

---

## 7. Folder Setup Wizard

**Access:** Automatically redirects to `/setup-folder` after login if no `folder_id` is configured

This is a **3-step guided wizard** to set up your Frame.io upload destination:

### Step 1: Select Workspace
- Choose from your Frame.io account workspaces
- Click **"Next"** to proceed

### Step 2: Select Project
- Choose a project within the selected workspace
- Click **"Next"** to proceed

### Step 3: Confirm & Auto-create
- Enter a folder name (default: `qr_code_uploader`)
- Click **"Create / Use Folder & Start System"**

**What the wizard does:**
1. Scans existing folders for a matching name (case-sensitive)
2. If found → reuses the existing folder
3. If not found → creates a new folder
4. Saves the `folder_id` to `config.json`
5. Starts processing files automatically

**Note:** You can skip this wizard by entering a Folder ID directly in the Configuration page.

---

## 8. The Folder Workflow

### Watch Folder
**Path:** `data/watch_folder/` (relative to the application directory)

**What to do:**
- Drop `.mp4` files into this folder
- The system detects new files automatically via the watchdog observer
- Files must be **fully written** before processing (the system waits for a stabilization delay of 2.0 seconds by default)

**What happens next:**
1. The file is renamed with a timestamp prefix: `YYYYMMDD_HHMMSS_originalname.mp4`
2. Upload begins to Frame.io
3. On success → moved to `processed_folder/`
4. On failure after max retries (default: 5) → moved to `failed_folder/`

---

### Processed Folder
**Path:** `data/processed_folder/`

Contains files that were **successfully uploaded**. The filename retains the timestamp prefix added during upload.

---

### Failed Folder
**Path:** `data/failed_folder/`

Contains files that **failed to upload** after exhausting all retry attempts. Check the Activity Log for the error reason before re-uploading manually.

---

### QR Codes Folder
**Path:** `data/qr_codes/`

Contains PNG QR codes. Each file is named: `{timecode}_{seq}_{video}.png` where:
- `{timecode}` is a 14-digit timestamp (YYYYMMDDHHMMSS without underscores)
- `{seq}` is the sequence number (incrementing counter)
- `{video}` is the original video filename stem

For example: `20260717160735_5_video.mp4` represents sequence 5 uploaded on July 17, 2026 at 16:07:35.

These are served statically at `https://localhost:8000/qr_codes/` and displayed in the control panel.

---

## 9. Known Limitations

- **Authentication required:** Files dropped before logging in are **ignored** with a warning. Log in first.
- **MP4 only:** The watcher only processes `.mp4` files. Other file types are ignored.
- **Single folder:** The current implementation uploads to **one Frame.io folder** per deployment.
- **Case-sensitive folder matching:** When auto-creating folders, the name match is case-sensitive (`QR Code Uploader` ≠ `qr code uploader`).
- **Self-signed SSL:** The browser will show a warning for the self-signed certificate. This is expected for local HTTPS.

---

## 10. Troubleshooting

### "No folder configured. Redirecting to folder setup."
- This is normal if you skipped the Folder ID during setup.
- Complete the `/setup-folder` wizard after logging in.

### Files are not being processed
- Ensure you are **logged in** (green status in control panel)
- Check that MP4 files are fully written to disk before dropping
- Verify the watchdog observer started: look for `[WATCHER] Live monitoring started on ...`
- Check `data/watch_folder/` exists and is writable

### Upload fails with 401 Unauthorized
- Your OAuth token may have expired. Refresh happens automatically — wait or re-login.
- Verify `client_id` and `client_secret` are correct in `config.json`

### QR code not showing on customer screen
- Ensure a QR code has been generated (check library and logs)
- If Manual Override is active, click **Release Screen** to return to auto-mode
- Check the browser console on the customer page for errors

### Port 8000 already in use
- Stop the other application using the port, or change the port in `/configure` and restart