'''
Frame.io V4 QR Code Automation System
FastAPI web app with OAuth 2.0 (Adobe IMS), folder watcher, and QR code generation.
'''

import asyncio
import ipaddress
import json
import logging
import math
import os
import shutil
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Optional

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

# ---------------------------------------------------------------------------
# Configuration — env vars → config.json → bundled defaults → setup mode
# ---------------------------------------------------------------------------

def resource_path(*parts: str) -> Path:
    '''Return the absolute path to a bundled resource.

    When running as a PyInstaller onefile executable, data files live inside
    the temporary _MEIxxxxxx extraction folder; fall back to the project root
    when running in dev mode (or when the resource is found next to the exe).'''
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    return base.joinpath(*parts)


def _load_dotenv_if_available() -> None:
    '''Load .env file in dev mode only if python-dotenv is installed.'''
    if getattr(sys, 'frozen', False):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / '.env')
    except ImportError:
        pass


def _load_file_if_exists(path_str: str) -> Optional[str]:
    '''Read a file and return its text content, or None if missing.'''
    p = Path(path_str)
    if p.exists():
        try:
            return p.read_text(encoding='utf-8')
        except Exception:
            return None
    return None


def find_config_path() -> Optional[Path]:
    '''Locate config.json.

    Search order (first match wins):
      1. Environment variable FRAMEIO_CONFIG_PATH (user-specified path).
      2. data/config.json next to the running executable (for persistent EXE config).
      3. config.json next to the running executable.
      4. config.json in current working directory.
      5. Bundled config_build.json (sys._MEIPASS) for PyInstaller onefile runs.
    '''
    env_path = os.getenv('FRAMEIO_CONFIG_PATH')
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p.resolve()

    exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    data_dir = exe_dir / 'data'
    candidates = [
        data_dir / 'config.json',
        exe_dir / 'config.json',
        Path('./config.json'),
    ]
    # In frozen mode, also check the bundled MEIPASS for a default config
    if getattr(sys, 'frozen', False):
        candidates.append(resource_path('config_build.json'))

    for p in candidates:
        if p.exists():
            return p.resolve()
    return None


def _build_config_from_env() -> dict:
    '''Construct a minimal CFG dict from environment variables.'''
    return {
        'frameio': {
            'client_id': os.getenv('FRAMEIO_CLIENT_ID', ''),
            'client_secret': os.getenv('FRAMEIO_CLIENT_SECRET', ''),
            'folder_id': os.getenv('FRAMEIO_FOLDER_ID', ''),
            'folder_name': os.getenv('FRAMEIO_FOLDER_NAME', 'qr_code_uploader'),
        },
        'server': {
            'host': os.getenv('FRAMEIO_SERVER_HOST', '0.0.0.0'),
            'port': int(os.getenv('FRAMEIO_SERVER_PORT', '8000')),
            'ssl_certfile': 'data/cert.pem',
            'ssl_keyfile': 'data/key.pem',
            'log_file': os.getenv('FRAMEIO_LOG_FILE', 'data/automation.log'),
        },
        'folders': {
            'watch': os.getenv('FRAMEIO_WATCH_FOLDER', 'data/watch_folder'),
            'processed': os.getenv('FRAMEIO_PROCESSED_FOLDER', 'data/processed_folder'),
            'failed': os.getenv('FRAMEIO_FAILED_FOLDER', 'data/failed_folder'),
            'qr_codes': os.getenv('FRAMEIO_QR_CODES_FOLDER', 'data/qr_codes'),
        },
        'oauth': {
            'redirect_uri': os.getenv('FRAMEIO_REDIRECT_URI', 'https://localhost:8000/callback'),
            'auth_url': os.getenv('FRAMEIO_AUTH_URL', 'https://ims-na1.adobelogin.com/ims/authorize/v2'),
            'token_url': os.getenv('FRAMEIO_TOKEN_URL', 'https://ims-na1.adobelogin.com/ims/token/v3'),
            'scope': os.getenv('FRAMEIO_OAUTH_SCOPE', 'offline_access,openid,email,profile,additional_info.roles'),
        },
    }


# Load .env in dev mode (non-frozen) for convenience
_load_dotenv_if_available()

CONFIG_PATH = find_config_path()

# ---------------------------------------------------------------------------
# First-run bootstrap: if running as EXE and no persistent config exists yet,
# copy the bundled config_build.json to data/config.json next to the EXE.
# ---------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    exe_dir = Path(sys.executable).parent
    persistent_path = (exe_dir / 'data' / 'config.json').resolve()
    bundled_default = resource_path('config_build.json')
    # If no persistent config exists, seed it from bundled default
    if not persistent_path.exists() and bundled_default.exists():
        try:
            persistent_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled_default, persistent_path)
            print(f'[CONFIG] First run: seeded persistent config from bundled default to {persistent_path}')
            CONFIG_PATH = persistent_path
        except Exception as exc:
            print(f'[CONFIG] Warning: Could not seed persistent config: {exc}')

# ---------------------------------------------------------------------------
# Setup mode flag (must be declared before config loading to avoid redefinition)
# ---------------------------------------------------------------------------
setup_mode: bool = False
needs_folder_setup: bool = False
TEMPLATES_PATH = resource_path('templates') if getattr(sys, 'frozen', False) else Path('./templates')
FRAMEIO_API_BASE = 'https://api.frame.io/v4'
REDIRECT_URI = 'https://localhost:8000/callback'
FRAMEIO_AUTH_URL = 'https://ims-na1.adobelogin.com/ims/authorize/v2'
FRAMEIO_TOKEN_URL = 'https://ims-na1.adobelogin.com/ims/token/v3'
OAUTH_SCOPE = 'offline_access,openid,email,profile,additional_info.roles'
SERVER_HOST = '0.0.0.0'
SERVER_PORT = 8000
SSL_CERTFILE = 'data/cert.pem'
SSL_KEYFILE = 'data/key.pem'
FRAMEIO_FOLDER_NAME = 'qr_code_uploader'

# Priority 1: Environment variables
ENV_CLIENT_ID = os.getenv('FRAMEIO_CLIENT_ID')
ENV_CLIENT_SECRET = os.getenv('FRAMEIO_CLIENT_SECRET')
ENV_FOLDER_ID = os.getenv('FRAMEIO_FOLDER_ID')

if all([ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_FOLDER_ID]):
    print('[CONFIG] Using credentials from environment variables.')
    CFG = _build_config_from_env()
    FRAMEIO_CLIENT_ID = CFG['frameio']['client_id']
    FRAMEIO_CLIENT_SECRET = CFG['frameio']['client_secret']
    FRAMEIO_FOLDER_ID = CFG['frameio']['folder_id']
    setup_mode = False

# Priority 2: config.json (or bundled config_build.json)
elif CONFIG_PATH is not None:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        CFG = json.load(f)

    FRAMEIO_CLIENT_ID = CFG['frameio']['client_id']
    FRAMEIO_CLIENT_SECRET = CFG['frameio']['client_secret']
    FRAMEIO_FOLDER_ID = CFG['frameio']['folder_id']

    CFG['server']['port'] = int(CFG['server']['port'])

    if not all([FRAMEIO_CLIENT_ID, FRAMEIO_CLIENT_SECRET, FRAMEIO_FOLDER_ID]):
        print('[CONFIG] ERROR: Missing required config')
        print('[CONFIG] Entering setup mode.')
        setup_mode = True
    else:
        setup_mode = False
        print(f'[CONFIG] Configuration loaded from {CONFIG_PATH}')
else:
    print('[CONFIG] No credentials found in environment or config file.')
    print('[CONFIG] Entering setup mode.')
    setup_mode = True
    CONFIG_PATH = None
    CFG = {
        'frameio': {'client_id': 'YOUR_CLIENT_ID', 'client_secret': 'YOUR_CLIENT_SECRET', 'folder_id': 'YOUR_FOLDER_ID', 'folder_name': 'qr_code_uploader'},
        'server': {'host': '0.0.0.0', 'port': 8000, 'ssl_certfile': 'data/cert.pem', 'ssl_keyfile': 'data/key.pem', 'log_file': 'data/automation.log'},
        'folders': {'watch': 'data/watch_folder', 'processed': 'data/processed_folder', 'failed': 'data/failed_folder', 'qr_codes': 'data/qr_codes'},
        'oauth': {'redirect_uri': 'https://localhost:8000/callback', 'auth_url': 'https://ims-na1.adobelogin.com/ims/authorize/v2', 'token_url': 'https://ims-na1.adobelogin.com/ims/token/v3', 'scope': 'offline_access,openid,email,profile,additional_info.roles'},
        'share': {'expiration_days': 7},
    }

if setup_mode:
    FRAMEIO_CLIENT_ID = CFG.get('frameio', {}).get('client_id', '')
    FRAMEIO_CLIENT_SECRET = CFG.get('frameio', {}).get('client_secret', '')
    FRAMEIO_FOLDER_ID = CFG.get('frameio', {}).get('folder_id', '')

SERVER_HOST = CFG['server']['host']
SERVER_PORT = CFG['server']['port']
SSL_CERTFILE = CFG['server']['ssl_certfile']
SSL_KEYFILE = CFG['server']['ssl_keyfile']
LOG_FILE = CFG['server'].get('log_file', 'data/automation.log')
TEMPLATES_PATH = resource_path('templates') if getattr(sys, 'frozen', False) else Path('./templates')
REDIRECT_URI = CFG['oauth']['redirect_uri']
FRAMEIO_AUTH_URL = CFG['oauth']['auth_url']
FRAMEIO_TOKEN_URL = CFG['oauth']['token_url']
OAUTH_SCOPE = CFG['oauth']['scope']

# ---------------------------------------------------------------------------
# Auto-generate SSL certificate if not present
# ---------------------------------------------------------------------------

def ensure_ssl_certificate(cert_path: str, key_path: str) -> None:
    '''Ensure SSL certificate/key files exist at the given paths.'''
    cert_file = Path(cert_path)
    key_file = Path(key_path)

    if not cert_file.is_absolute():
        if getattr(sys, 'frozen', False):
            cert_file = Path(sys.executable).parent / cert_file
        else:
            cert_file = Path(__file__).parent / cert_file

    if not key_file.is_absolute():
        if getattr(sys, 'frozen', False):
            key_file = Path(sys.executable).parent / key_file
        else:
            key_file = Path(__file__).parent / key_file

    if cert_file.exists() and key_file.exists():
        print(f'[SSL] Certificate and key already exist: {cert_file}, {key_file}')
    else:
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.backends import default_backend
        except ImportError:
            print('[SSL] WARNING: cryptography library not available.')
            return

        print('[SSL] Certificate/key missing. Auto-generating self-signed certificate...')
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, 'US'),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, 'California'),
            x509.NameAttribute(NameOID.LOCALITY_NAME, 'San Francisco'),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Frame.io Exhibition Uploader'),
            x509.NameAttribute(NameOID.COMMON_NAME, 'localhost'),
        ])

        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                critical=False,
            )
            .sign(private_key, hashes.SHA256(), backend=default_backend())
        )

        cert_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cert_file, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        with open(key_file, 'wb') as f:
            f.write(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        print(f'[SSL] Self-signed certificate auto-generated: {cert_file}')
        print(f'[SSL] Private key auto-generated: {key_file}')

# ---------------------------------------------------------------------------
# Dynamic Runtime Configuration
# ---------------------------------------------------------------------------

@dataclass
class DynamicConfig:
    '''Thread-safe dynamic configuration for runtime-adjustable parameters.'''
    watch_folder: str = 'data/watch_folder'
    processed_folder: str = 'data/processed_folder'
    failed_folder: str = 'data/failed_folder'
    qr_codes_folder: str = 'data/qr_codes'
    processing_folder: str = 'data/processing_folder'
    stabilization_delay: float = 2.0
    max_retries: int = 5
    share_expiration_days: int = 7
    auto_open_browser: bool = True
    _lock: Lock = field(default_factory=Lock, repr=False)

    def update(self, **kwargs) -> None:
        '''Atomically validate and update settings.'''
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key):
                    raise ValueError(f'Unknown setting: {key}')
                if key in ('stabilization_delay',):
                    value = float(value)
                    if value < 0:
                        raise ValueError('stabilization_delay must be non-negative')
                elif key in ('max_retries', 'share_expiration_days'):
                    value = int(value)
                    if value < 0:
                        raise ValueError(f'{key} must be non-negative')
                elif key.endswith('_folder'):
                    value = str(value).strip()
                    if not value:
                        raise ValueError(f'{key} must be a non-empty string')
                setattr(self, key, value)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                'WATCH_FOLDER': self.watch_folder,
                'PROCESSED_FOLDER': self.processed_folder,
                'FAILED_FOLDER': self.failed_folder,
                'QR_CODES_FOLDER': self.qr_codes_folder,
                'STABILIZATION_DELAY': self.stabilization_delay,
                'MAX_RETRIES': self.max_retries,
                'SHARE_EXPIRATION_DAYS': self.share_expiration_days,
            }

    @property
    def watch_path(self) -> Path:
        return Path(self.watch_folder)

    @property
    def processed_path(self) -> Path:
        return Path(self.processed_folder)

    @property
    def failed_path(self) -> Path:
        return Path(self.failed_folder)

    @property
    def qr_codes_path(self) -> Path:
        return Path(self.qr_codes_folder)

    @property
    def processing_path(self) -> Path:
        return Path(self.processing_folder)


# Initialize from config.json
config_instance = DynamicConfig(
    watch_folder=CFG['folders']['watch'],
    processed_folder=CFG['folders']['processed'],
    failed_folder=CFG['folders']['failed'],
    qr_codes_folder=CFG['folders']['qr_codes'],
    processing_folder=CFG['folders'].get('processing', 'data/processing_folder'),
    share_expiration_days=CFG.get('share', {}).get('expiration_days', 7),
)

# Backward-compatible path variables
WATCH_PATH = config_instance.watch_path
PROCESSED_PATH = config_instance.processed_path
FAILED_PATH = config_instance.failed_path
QR_CODES_PATH = config_instance.qr_codes_path
PROCESSING_PATH = config_instance.processing_path
PROCESSING_PATH.mkdir(parents=True, exist_ok=True)

# Global auto-open browser toggle
AUTO_OPEN_BROWSER: bool = config_instance.auto_open_browser
_browser_opened = False


def get_config() -> DynamicConfig:
    return config_instance


# ---------------------------------------------------------------------------
# Ensure folders exist
# ---------------------------------------------------------------------------

for folder in [WATCH_PATH, PROCESSED_PATH, FAILED_PATH, QR_CODES_PATH, PROCESSING_PATH, TEMPLATES_PATH]:
    folder.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_file_path = Path(LOG_FILE)
if not _log_file_path.is_absolute():
    if getattr(sys, 'frozen', False):
        _log_file_path = Path(sys.executable).parent / _log_file_path
    else:
        _log_file_path = Path(__file__).parent / _log_file_path
_log_file_path.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(str(_log_file_path), encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------

upload_queue: list[dict] = []
upload_queue_lock = Lock()

access_token: Optional[str] = None
refresh_token: Optional[str] = None
token_lock = Lock()
token_expires_at: Optional[datetime] = None
user_info: Optional[dict] = None
account_id: Optional[str] = None
project_id: Optional[str] = None

# Shared log feed for the frontend
log_feed: list[dict] = []
MAX_LOG_FEED = 200

session_processed_count: int = 0
session_failed_count: int = 0

config_lock = Lock()

# Sequence number tracking for QR codes
current_sequence_number: int = 0
sequence_lock = Lock()

display_state_lock = Lock()
active_display_qr: Optional[str] = None
latest_qr: Optional[str] = None
manual_override: bool = False
queued_qrs: list[str] = []

# Display message for customer panel (editable by staff)
display_message_lock = Lock()
display_message: str = "Point your camera at the QR code to access your video"
DEFAULT_DISPLAY_MESSAGE = "Point your camera at the QR code to access your video"

upload_status_lock = Lock()
upload_status: dict[str, str] = {}
current_upload_name: Optional[str] = None

def _update_queue_entry(upload_name: str, status: str, qr_path: str) -> None:
    with upload_queue_lock:
        for entry in upload_queue:
            if entry.get('name') == upload_name:
                entry['status'] = status
                entry['qr_path'] = qr_path
                break

def add_log_entry(message: str, tag: str = 'INFO'):
    '''Add a log entry to the in-memory feed for the frontend.'''
    entry = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'tag': tag,
        'message': message,
    }
    log_feed.append(entry)
    if len(log_feed) > MAX_LOG_FEED:
        log_feed[:50] = []


def get_display_message_file_path() -> Path:
    '''Get the path to the display message file.'''
    global CFG
    # Check config for custom path, otherwise use default
    if CFG and isinstance(CFG, dict):
        server_cfg = CFG.get('server', {})
        msg_path = server_cfg.get('display_message_path', 'data/message.txt')
    else:
        msg_path = 'data/message.txt'
    
    path = Path(msg_path)
    if not path.is_absolute():
        if getattr(sys, 'frozen', False):
            path = Path(sys.executable).parent / path
        else:
            path = Path(__file__).parent / path
    return path


def load_display_message() -> str:
    '''Load the display message from file, or return default if not exists.'''
    global display_message
    msg_path = get_display_message_file_path()
    msg_path.parent.mkdir(parents=True, exist_ok=True)
    
    if msg_path.exists():
        try:
            content = msg_path.read_text(encoding='utf-8').strip()
            if content:
                with display_message_lock:
                    display_message = content
                return content
        except Exception:
            pass
    
    # Return default message
    return DEFAULT_DISPLAY_MESSAGE


def save_display_message(message: str) -> bool:
    '''Save the display message to file.'''
    global display_message
    msg_path = get_display_message_file_path()
    msg_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        msg_path.write_text(message, encoding='utf-8')
        with display_message_lock:
            display_message = message
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Frame.io V4 — Adobe IMS OAuth 2.0
# ---------------------------------------------------------------------------

def get_auth_url() -> str:
    '''Build the Adobe IMS OAuth authorization URL.'''
    params = (
        f'response_type=code'
        f'&client_id={FRAMEIO_CLIENT_ID}'
        f'&redirect_uri={REDIRECT_URI}'
        f'&scope={OAUTH_SCOPE}'
    )
    return f'{FRAMEIO_AUTH_URL}?{params}'


def get_auth_headers() -> dict:
    '''Return authorization headers for Frame.io API calls.'''
    with token_lock:
        if access_token is None:
            raise RuntimeError('Not authenticated')
        return {'Authorization': f'Bearer {access_token}'}


async def exchange_code_for_token(code: str) -> dict:
    '''Exchange the authorization code for an access token using Adobe IMS.'''
    import base64
    credentials = f'{FRAMEIO_CLIENT_ID}:{FRAMEIO_CLIENT_SECRET}'
    encoded_creds = base64.b64encode(credentials.encode()).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            FRAMEIO_TOKEN_URL,
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': REDIRECT_URI,
            },
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {encoded_creds}',
            },
        )
        resp.raise_for_status()
        data = resp.json()
        with token_lock:
            global access_token, refresh_token, token_expires_at
            access_token = data['access_token']
            refresh_token = data.get('refresh_token')
            expires_in = data.get('expires_in', 86400)
            token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        print(f'[OAUTH] Access token obtained via Adobe IMS. Expires in {expires_in}s')
        add_log_entry('Access token obtained via Adobe IMS', 'OAUTH')
        return data


async def refresh_access_token() -> Optional[str]:
    '''Refresh the access token using the refresh_token (Adobe IMS Basic Auth).'''
    import base64
    global access_token, refresh_token, token_expires_at
    credentials = f'{FRAMEIO_CLIENT_ID}:{FRAMEIO_CLIENT_SECRET}'
    encoded_creds = base64.b64encode(credentials.encode()).decode()

    with token_lock:
        current_refresh = refresh_token

    if not current_refresh:
        print('[OAUTH] No refresh token available')
        return None

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            FRAMEIO_TOKEN_URL,
            data={
                'grant_type': 'refresh_token',
                'refresh_token': current_refresh,
            },
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {encoded_creds}',
            },
        )
        if resp.status_code != 200:
            print(f'[OAUTH] Token refresh failed: {resp.status_code}')
            add_log_entry(f'Token refresh failed: {resp.status_code}', 'OAUTH')
            return None

        data = resp.json()
        with token_lock:
            access_token = data['access_token']
            refresh_token = data.get('refresh_token', current_refresh)
            expires_in = data.get('expires_in', 86400)
            token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        print(f'[OAUTH] Access token refreshed. Expires in {expires_in}s')
        add_log_entry('Access token refreshed', 'OAUTH')
        return access_token


async def ensure_valid_token():
    '''Check if token is expired and refresh if needed.'''
    with token_lock:
        if access_token is None:
            return False
        if token_expires_at and datetime.now(timezone.utc) < token_expires_at - timedelta(minutes=5):
            return True
    new_token = await refresh_access_token()
    return new_token is not None


async def get_current_user() -> dict:
    '''Fetch the currently authenticated user from GET /v4/me.'''
    await ensure_valid_token()
    headers = get_auth_headers()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f'{FRAMEIO_API_BASE}/me', headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_account_id() -> str:
    '''Get the primary account ID by calling GET /v4/accounts.'''
    global account_id
    if account_id:
        return account_id

    headers = get_auth_headers()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f'{FRAMEIO_API_BASE}/accounts', headers=headers)
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get('data', [])
        if not accounts:
            raise RuntimeError('No accounts found in /v4/accounts response')
        account_id = accounts[0].get('id')
        if not account_id:
            raise RuntimeError(f'Could not extract account_id from: {data}')
        print(f'[OAUTH] Resolved account_id: {account_id}')
        add_log_entry(f'Resolved account_id: {account_id}', 'OAUTH')
        return account_id


async def get_project_id() -> str:
    '''Resolve the project_id from the folder_id using GET /v4/accounts/{aid}/folders/{fid}.'''
    global project_id
    if project_id:
        return project_id

    aid = await get_account_id()
    headers = get_auth_headers()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/folders/{FRAMEIO_FOLDER_ID}',
            headers=headers,
        )
        if resp.status_code == 404:
            raise RuntimeError(
                f'Folder {FRAMEIO_FOLDER_ID} not found in account {aid}. '
                f'Check that FRAMEIO_FOLDER_ID in config.json is valid.'
            )
        resp.raise_for_status()
        data = resp.json()
        folder_data = data.get('data', {})
        project_id = folder_data.get('project_id')
        if not project_id:
            raise RuntimeError(f'Could not resolve project_id from folder. Response: {data}')
        print(f'[OAUTH] Resolved project_id: {project_id} from folder: {FRAMEIO_FOLDER_ID}')
        add_log_entry(f'Resolved project_id: {project_id}', 'OAUTH')
        return project_id


# ---------------------------------------------------------------------------
# Frame.io V4 — Local Upload
# ---------------------------------------------------------------------------

async def _initiate_upload(file_path: Path, upload_filename: str) -> dict:
    '''Step 1: Call local_upload endpoint to get file_id and upload_urls (no S3 upload).'''
    await ensure_valid_token()
    headers = get_auth_headers()
    file_size = file_path.stat().st_size
    aid = await get_account_id()
    folder_id = FRAMEIO_FOLDER_ID

    async with httpx.AsyncClient(timeout=60) as client:
        upload_req = await client.post(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/folders/{folder_id}/files/local_upload',
            headers=headers,
            json={'data': {'name': upload_filename, 'file_size': file_size}},
        )
        upload_req.raise_for_status()
        upload_data = upload_req.json()
        file_record = upload_data.get('data', {})
        file_id = file_record.get('id')
        upload_urls = file_record.get('upload_urls', [])
        media_type = file_record.get('media_type', 'video/mp4')

        if not file_id or not upload_urls:
            raise RuntimeError(f"Missing 'id' or 'upload_urls' in local_upload response: {upload_data}")

    return {'id': file_id, 'upload_urls': upload_urls, 'media_type': media_type, 'file_size': file_size}


async def _finish_upload(file_path: Path, upload_urls: list, media_type: str) -> None:
    '''Step 2: Upload file bytes to S3 via the pre-signed URLs.'''
    with open(file_path, 'rb') as f:
        file_bytes = f.read()

    file_size = len(file_bytes)
    num_urls = len(upload_urls)

    if num_urls == 1:
        url = upload_urls[0].get('url')
        async with httpx.AsyncClient(timeout=600) as client:
            put_resp = await client.put(
                url, content=file_bytes,
                headers={'Content-Type': media_type, 'x-amz-acl': 'private'},
            )
            put_resp.raise_for_status()
    else:
        chunk_size = upload_urls[0].get('size') or math.ceil(file_size / num_urls)
        for i, url_info in enumerate(upload_urls):
            url = url_info.get('url')
            start_byte = i * chunk_size
            end_byte = min(start_byte + chunk_size, file_size)
            chunk = file_bytes[start_byte:end_byte]
            async with httpx.AsyncClient(timeout=600) as client:
                put_resp = await client.put(
                    url, content=chunk,
                    headers={'Content-Type': media_type, 'x-amz-acl': 'private'},
                )
                put_resp.raise_for_status()
            print(f'[UPLOAD] Chunk {i + 1}/{num_urls} uploaded')


# ---------------------------------------------------------------------------
# Frame.io V4 — Create Share
# ---------------------------------------------------------------------------

async def create_share_link(asset_id: str) -> Optional[str]:
    '''Create a public share link for an asset via the Create Share endpoint.'''
    await ensure_valid_token()
    headers = get_auth_headers()
    aid = await get_account_id()
    pid = await get_project_id()
    cfg = get_config()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=int(cfg.share_expiration_days))).isoformat()
    # Frame.io V4 API expects ISO 8601 format with 'Z' suffix for UTC
    expires_at = expires_at.replace('+00:00', 'Z')

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/projects/{pid}/shares',
            headers=headers,
            json={
                'data': {
                    'type': 'asset',
                    'access': 'public',
                    'name': f'QR Share - {asset_id}',
                    'asset_ids': [asset_id],
                    'downloading_enabled': True,
                    'expiration': expires_at,
                },
            },
        )
        if resp.status_code == 201:
            data = resp.json()
            share_data = data.get('data', {})
            short_url = share_data.get('short_url')
            if short_url:
                print(f'[SUCCESS] Share link created: {short_url}')
                add_log_entry(f'Share link created for asset {asset_id}', 'SUCCESS')
                return short_url
            else:
                print(f'[FAILED] No short_url in share response: {share_data}')
                add_log_entry('No short_url in share response', 'FAILED')
                return None
        else:
            error_text = resp.text[:300]
            print(f'[FAILED] Create share failed ({resp.status_code}): {error_text}')
            add_log_entry(f'Create share failed: {resp.status_code}', 'FAILED')
            resp.raise_for_status()
            return None


# ---------------------------------------------------------------------------
# QR Code Generation
# ---------------------------------------------------------------------------

def generate_qr_code(share_url: str, original_filename: str) -> Path:
    '''Generate a PNG QR code (black & white, Error Correction 'H').'''
    global current_sequence_number
    
    stem = Path(original_filename).stem
    now = datetime.now()
    ts = now.strftime('%Y%m%d%H%M%S')  # 4-digit year: YYYYMMDDhhmmss
    
    # Get next sequence number
    with sequence_lock:
        seq_num = current_sequence_number
        current_sequence_number += 1
    
    # Format: {seqnum}_{timecode}_{video}.png
    # The timecode (and video name) are taken from the source filename stem,
    # e.g. "20240101_120000_video.mp4" -> "0_20240101_120000_video.png".
    qr_filename = f'{seq_num}_{stem}.png'
    qr_path = get_config().qr_codes_path / qr_filename

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(share_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color='black', back_color='white')
    img.save(qr_path, 'PNG')
    print(f'[SUCCESS] QR code saved: {qr_path}')
    add_log_entry(f'QR code saved: {qr_path}', 'SUCCESS')

    # Update customer display state
    global active_display_qr, latest_qr
    with display_state_lock:
        latest_qr = f'/qr_codes/{qr_path.name}'
        if manual_override:
            queued_qrs.append(str(qr_path))
            add_log_entry(f'QR queued (manual override active): {qr_filename}', 'DISPLAY')
        else:
            active_display_qr = latest_qr
            add_log_entry(f'Active display QR updated: {qr_filename}', 'DISPLAY')

    return qr_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def timestamped_name(original_name: str) -> str:
    '''Prefix a file name with YYYYMMDD_HHMMSS_ so it is unique on Frame.io.'''
    now = datetime.now()
    ts = now.strftime('%Y%m%d_%H%M%S')
    return f'{ts}_{original_name}'


def _reserve_sequence_number() -> int:
    '''Reserve and return the next sequence number in a thread-safe way.'''
    global current_sequence_number
    with sequence_lock:
        seq_num = current_sequence_number
        current_sequence_number += 1
    return seq_num


def _build_upload_name(original_name: str, seq_num: int, when: Optional[datetime] = None) -> str:
    '''Build the queue/upload filename using the new sequence-prefixed format.'''
    timestamp = (when or datetime.now()).strftime('%Y%m%d%H%M%S')
    original_path = Path(original_name)
    return f'{timestamp}_{seq_num}_{original_path.stem}{original_path.suffix}'


def _append_upload_queue_entry(path: Path, seq_num: int) -> str:
    '''Add a stable file to the upload queue using the new filename format.'''
    upload_name = _build_upload_name(path.name, seq_num)
    file_size = path.stat().st_size

    with upload_queue_lock:
        if any(entry.get('original_name') == path.name for entry in upload_queue):
            return upload_name
        upload_queue.append({
            'name': upload_name,
            'original_name': path.name,
            'size_bytes': file_size,
            'status': 'queuing',
            'qr_path': '',
            'seq_num': seq_num,
        })

    return upload_name


def _format_bytes(n: int) -> str:
    '''Convert bytes to a human-readable string (e.g., 1.2 MB).'''
    if n < 1024:
        return f'{n} B'
    for unit in ('KiB', 'MiB', 'GiB'):
        n /= 1024.0
        if n < 1024:
            return f'{n:.1f} {unit}'
    return f'{n:.1f} GiB'


def extract_sequence_number(filename: str) -> Optional[int]:
    '''Extract sequence number from a QR code or upload filename.

    Two formats are supported:
      * Legacy / upload format: {YYYYMMDDhhmmss}_{seqnum}_{video}.png
        (the first segment is a 14-digit timecode, seqnum is the second segment)
      * New QR format: {seqnum}_{timecode}_{video}.png
        (the first segment is the sequence number)

    Returns None if no sequence number can be determined.
    '''
    without_ext = filename.replace('.png', '')
    parts = without_ext.split('_')
    if len(parts) >= 2:
        first = parts[0]
        # A 14-digit leading segment indicates the legacy timestamp-first format.
        if first.isdigit() and len(first) == 14:
            seq_part = parts[1]
        else:
            seq_part = first
        if seq_part.isdigit():
            return int(seq_part)
    return None


def parse_qr_filename(filename: str) -> tuple:
    '''Parse a QR code / upload filename into (seqnum, video_name, timecode_human).

    Two formats are supported:
      * Legacy / upload format: {YYYYMMDDhhmmss}_{seqnum}_{video}.png
      * New QR format: {seqnum}_{timecode}_{video}.png
        (the first segment is the sequence number)

    The video name may contain underscores.
    Returns (seqnum, video_name, timecode_human) where seqnum may be None.
    '''
    without_ext = filename.replace('.png', '')
    seq = extract_sequence_number(filename)
    parts = without_ext.split('_')

    # Detect the legacy timestamp-first format: the first segment is a 14-digit
    # timecode. Otherwise treat it as the new seqnum-first QR format.
    if parts and parts[0].isdigit() and len(parts[0]) == 14:
        # Legacy / upload format: {YYYYMMDDhhmmss}_{seqnum}_{video}.png
        timecode_raw = without_ext[:14]
        if seq is not None:
            after_timecode = without_ext[15:]  # skip "YYYYMMDDhhmmss_"
            after_seq_idx = after_timecode.find('_') + 1
            video_name = after_timecode[after_seq_idx:]
        else:
            video_name = without_ext[15:]
    else:
        # New QR format: {seqnum}_{timecode}_{video}.png
        seq_len = len(parts[0]) if parts else 0
        after_seq = without_ext[seq_len + 1:]  # skip "{seqnum}_"
        timecode_raw = after_seq[:14] if len(after_seq) >= 14 else after_seq
        if len(after_seq) > 14:
            video_name = after_seq[15:]
        else:
            video_name = after_seq
        # Strip leading underscore if present
        if video_name.startswith('_'):
            video_name = video_name[1:]

    # Strip any remaining leading underscore
    if video_name.startswith('_'):
        video_name = video_name[1:]

    # Parse timecode: YYYYMMDDhhmmss -> human readable
    timecode_human = timecode_raw
    if len(timecode_raw) == 14 and timecode_raw.isdigit():
        try:
            dt = datetime.strptime(timecode_raw, '%Y%m%d%H%M%S')
            timecode_human = dt.strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, IndexError):
            pass

    return seq, video_name, timecode_human


def get_next_sequence_number() -> int:
    '''Scan qr_codes folder to find the next sequence number.
    
    Called at startup to establish the initial counter based on existing files.
    Thread-safe.
    '''
    global current_sequence_number
    qr_codes_dir = get_config().qr_codes_path
    
    if not qr_codes_dir.exists():
        return 1
    
    max_seq = 0
    for qr_file in qr_codes_dir.glob('*.png'):
        seq = extract_sequence_number(qr_file.name)
        if seq is not None and seq > max_seq:
            max_seq = seq
    
    return max_seq + 1


def initialize_sequence_counter() -> None:
    '''Initialize the sequence counter from existing QR files. Called once at startup.'''
    global current_sequence_number
    with sequence_lock:
        current_sequence_number = get_next_sequence_number()
        print(f'[SEQ] Sequence counter initialized to {current_sequence_number}')
        add_log_entry(f'Sequence counter initialized: next will be {current_sequence_number}', 'SEQ')


# ---------------------------------------------------------------------------
# File Processor
# ---------------------------------------------------------------------------

async def process_single_file(queue_item: dict) -> None:
    '''Process a single file from the upload queue.'''
    cfg = get_config()
    original_name = queue_item['original_name']
    upload_name = queue_item['name']
    seq_num = queue_item.get('seq_num')

    # Backward-compatible fallback for older queue items or direct unit tests.
    if seq_num is None:
        seq_num = extract_sequence_number(upload_name)
    if seq_num is None:
        seq_num = _reserve_sequence_number()
        upload_name = _build_upload_name(original_name, seq_num)

    original_stem = Path(original_name).stem
    original_suffix = Path(original_name).suffix  # should be .mp4

    # Keep the queue entry name as the canonical upload name.
    upload_name_with_ts = upload_name

    # Set active-upload tracking so /api/uploading shows this file
    global current_upload_name
    current_upload_name = upload_name_with_ts
    status_key = Path(upload_name_with_ts).stem
    with upload_status_lock:
        upload_status[status_key] = 'uploading'

    # Move to processing_folder if not already there
    watch_path = cfg.watch_path / original_name
    processing_path = cfg.processing_path / original_name

    # Stability check before moving - ONLY process if file is stable (not currently being recorded)
    watch_path = cfg.watch_path / original_name
    
    try:
        if not watch_path.exists():
            print(f'[FAILED] File not found: {original_name}')
            add_log_entry(f'File not found: {original_name}', 'FAILED')
            return
        
        # Check if file is stable (not being written to)
        initial_size = watch_path.stat().st_size
        
        if cfg.stabilization_delay > 0:
            time.sleep(cfg.stabilization_delay)  # Wait for stabilization
            stable_check_size = watch_path.stat().st_size
            if stable_check_size != initial_size:
                print(f'[WATCHER] File is still being written (size changed from {_format_bytes(initial_size)} to {_format_bytes(stable_check_size)}). Skipping for now: {original_name}')
                add_log_entry(f'File still being recorded, will skip: {original_name}', 'WATCHER')
                return  # Skip this file entirely - don't queue it
        
        # File is stable, ready to move
        shutil.move(str(watch_path), str(processing_path))
        print(f'[WATCHER] Moved to processing_folder: {original_name}')
        add_log_entry(f'Moved to processing_folder: {original_name}', 'WATCHER')
        
    except PermissionError as e:
        print(f'[FAILED] File is locked (OBS still writing). Skipping: {original_name}')
        add_log_entry(f'File locked, skipping: {original_name}', 'WATCHER')
        return
    except (OSError, FileNotFoundError) as e:
        print(f'[FAILED] File error: {e}')
        add_log_entry(f'File error: {e}', 'FAILED')
        return
    except Exception as e:
        print(f'[FAILED] Could not move {original_name} to processing_folder: {e}')
        add_log_entry(f'Could not move {original_name} to processing_folder: {e}', 'FAILED')
        return

    print(f'[WATCHER] Processing: {original_name}')
    add_log_entry(f'Processing: {original_name}', 'WATCHER')

    # --- Step 1: Initiate upload on Frame.io (create placeholder, get file_id) ---
    max_retries = cfg.max_retries
    init_result = None
    last_error = ''

    for attempt in range(1, max_retries + 1):
        try:
            print(f'[UPLOAD] Initiating upload for {upload_name_with_ts} (attempt {attempt}/{max_retries})')
            add_log_entry(f'Initiating upload for {upload_name_with_ts} (attempt {attempt}/{max_retries})', 'UPLOAD')
            init_result = await _initiate_upload(processing_path, upload_name_with_ts)
            print(f'[UPLOAD] Initiation successful: {upload_name_with_ts} (id={init_result["id"]})')
            add_log_entry(f'Upload initiated: {upload_name_with_ts} (id={init_result["id"]})', 'UPLOAD')
            break
        except httpx.HTTPStatusError as e:
            last_error = f'HTTP {e.response.status_code}: {e.response.text[:200]}'
            print(f'[FAILED] Upload initiation attempt {attempt} failed: {last_error}')
            add_log_entry(f'Upload initiation attempt {attempt} failed: {last_error}', 'FAILED')
            if attempt < max_retries:
                wait = min(2 ** attempt, 30)
                await asyncio.sleep(wait)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            last_error = f'Network error: {e}'
            print(f'[FAILED] Network error (attempt {attempt}): {e}')
            add_log_entry(f'Network error (attempt {attempt}): {e}', 'FAILED')
            if attempt < max_retries:
                wait = min(2 ** attempt, 30)
                await asyncio.sleep(wait)
        except Exception as e:
            last_error = str(e)
            print(f'[FAILED] Unexpected error (attempt {attempt}): {e}')
            add_log_entry(f'Unexpected error (attempt {attempt}): {e}', 'FAILED')
            if attempt < max_retries:
                await asyncio.sleep(5)

    if init_result is None:
        failed_name = timestamped_name(original_name)
        failed_path = cfg.failed_path / failed_name
        shutil.move(str(processing_path), str(failed_path))
        print(f'[FAILED] Moved {original_name} to failed_folder after {max_retries} attempts. Error: {last_error}')
        add_log_entry(f'Moved {original_name} to failed_folder. Error: {last_error}', 'FAILED')
        global session_failed_count
        session_failed_count += 1
        return

    asset_id = init_result['id']
    file_bytes_uploaded = False

    # --- Step 2: Try to create share link BEFORE S3 upload (fast path) ---
    share_url = None
    try:
        share_url = await create_share_link(asset_id)
        if share_url:
            print(f'[SUCCESS] Share link created for {upload_name_with_ts} BEFORE S3 upload (fast path)')
            add_log_entry(f'Share link created before S3 upload: {upload_name_with_ts}', 'SUCCESS')
        else:
            print(f'[INFO] Share creation before S3 returned no URL, will retry after upload')
            add_log_entry('Share before S3 returned no URL', 'INFO')
    except Exception as e:
        print(f'[INFO] Share before S3 failed: {e}. Will retry after upload.')
        add_log_entry(f'Share before S3 failed, will retry: {e}', 'INFO')

    # --- Step 3: Generate QR code (if we have the share URL already) ---
    qr_processing_path = None
    qr_filename_for_history = None
    if share_url:
        try:
            now = datetime.now()
            ts = now.strftime('%Y%m%d%H%M%S')
            # QR filename uses the same sequence number as the video
            qr_filename = f'{ts}_{seq_num}_{original_stem}.png'
            qr_processing_path = cfg.qr_codes_path / qr_filename
            qr_filename_for_history = qr_filename

            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
            qr.add_data(share_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color='black', back_color='white')
            img.save(qr_processing_path, 'PNG')
            print(f'[SUCCESS] QR code saved BEFORE upload finishes: {qr_filename}')
            add_log_entry(f'QR code saved before upload finished: {qr_filename}', 'SUCCESS')

            # Update customer display immediately
            with display_state_lock:
                latest_qr = f'/qr_codes/{qr_filename}'
                if manual_override:
                    queued_qrs.append(str(qr_processing_path))
                else:
                    active_display_qr = latest_qr
        except Exception as e:
            print(f'[FAILED] QR code generation before upload failed: {e}')
            add_log_entry(f'QR code generation before upload failed: {e}', 'FAILED')

    # --- Step 4: Upload file bytes to S3 (the slow part) ---
    s3_upload_success = False
    for attempt in range(1, max_retries + 1):
        try:
            print(f'[UPLOAD] Uploading bytes to S3 for {upload_name_with_ts} (attempt {attempt}/{max_retries})')
            add_log_entry(f'Uploading bytes to S3 for {upload_name_with_ts} (attempt {attempt}/{max_retries})', 'UPLOAD')
            await _finish_upload(processing_path, init_result['upload_urls'], init_result['media_type'])
            file_bytes_uploaded = True
            s3_upload_success = True
            print(f'[UPLOAD] S3 upload complete: {upload_name_with_ts}')
            add_log_entry(f'S3 upload complete: {upload_name_with_ts}', 'UPLOAD')
            break
        except (httpx.RequestError, httpx.TimeoutException) as e:
            last_error = f'S3 upload error: {e}'
            print(f'[FAILED] S3 upload attempt {attempt} failed: {e}')
            add_log_entry(f'S3 upload attempt {attempt} failed: {e}', 'FAILED')
            if attempt < max_retries:
                wait = min(2 ** attempt, 30)
                await asyncio.sleep(wait)
        except Exception as e:
            last_error = str(e)
            print(f'[FAILED] S3 upload unexpected error (attempt {attempt}): {e}')
            add_log_entry(f'S3 upload unexpected error (attempt {attempt}): {e}', 'FAILED')
            if attempt < max_retries:
                await asyncio.sleep(5)

    if not s3_upload_success:
        failed_name = timestamped_name(original_name)
        failed_path = cfg.failed_path / failed_name
        shutil.move(str(processing_path), str(failed_path))
        print(f'[FAILED] Moved {original_name} to failed_folder. S3 upload failed: {last_error}')
        add_log_entry(f'Moved {original_name} to failed_folder. S3 upload failed: {last_error}', 'FAILED')
        session_failed_count += 1
        return

    # --- Step 5: If share wasn't created before S3, create it now ---
    share_url = None
    try:
        share_url = await create_share_link(asset_id)
        if share_url:
            print(f'[SUCCESS] Share link created for {upload_name_with_ts}')
            add_log_entry(f'Share link created for {upload_name_with_ts}', 'SUCCESS')
        else:
            print(f'[FAILED] Could not create share link for {upload_name_with_ts}')
            add_log_entry(f'Could not create share link for {upload_name_with_ts}', 'FAILED')
    except Exception as e:
        print(f'[FAILED] Error creating share link: {e}')
        add_log_entry(f'Error creating share link: {e}', 'FAILED')

    # --- Generate QR Code in qr_codes_folder (fallback: only if not already done in Step 3) ---
    if share_url and qr_filename_for_history is None:
        try:
            qr_filename = f"{Path(upload_name_with_ts).stem}.png"
            qr_processing_path = cfg.qr_codes_path / qr_filename
            qr_filename_for_history = qr_filename

            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
            qr.add_data(share_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color='black', back_color='white')
            img.save(qr_processing_path, 'PNG')
            print(f'[SUCCESS] QR code saved: {qr_filename}')
            add_log_entry(f'QR code saved: {qr_filename}', 'SUCCESS')

            with display_state_lock:
                latest_qr = f'/qr_codes/{qr_filename}'
                if manual_override:
                    queued_qrs.append(str(qr_processing_path))
                else:
                    active_display_qr = latest_qr
        except Exception as e:
            print(f'[FAILED] QR code generation failed: {e}')
            add_log_entry(f'QR code generation failed: {e}', 'FAILED')

    # Mark as ready
    status_key = Path(upload_name_with_ts).stem
    with upload_status_lock:
        upload_status[status_key] = 'ready'
    qr_path_for_queue = f'/qr_codes/{qr_filename_for_history}' if qr_filename_for_history else ''
    _update_queue_entry(upload_name_with_ts, 'ready', qr_path_for_queue)
    if current_upload_name == upload_name_with_ts:
        current_upload_name = None

    # --- Move video to processed_folder ---
    processed_path = cfg.processed_path / upload_name_with_ts
    try:
        shutil.move(str(processing_path), str(processed_path))
        processed_name = processed_path.name
        print(f'[SUCCESS] Moved {original_name} -> processed_folder as {processed_name}')
        add_log_entry(f'Moved to processed_folder: {processed_name}', 'SUCCESS')
    except Exception as e:
        print(f'[FAILED] Error moving video after upload: {e}')
        add_log_entry(f'Error moving video after upload: {e}', 'FAILED')

    global session_processed_count
    session_processed_count += 1


# ---------------------------------------------------------------------------
# Watcher (catch-up + live monitoring)
# ---------------------------------------------------------------------------

# Track files currently being monitored for stability to prevent duplicate threads
_pending_stability_checks: set = set()
_pending_stability_checks_lock = Lock()

class MP4Handler(PatternMatchingEventHandler):
    '''Watchdog handler for new .mp4 files.'''

    def __init__(self):
        super().__init__(patterns=['*.mp4'], ignore_directories=True)

    def on_created(self, event):
        if event.is_directory:
            return
        with token_lock:
            if access_token is None:
                print('[WARNING] File detected but user is not authenticated. Skipping.')
                add_log_entry('File detected but user is not authenticated. Skipping.', 'WARNING')
                return
        self._queue_file(event.src_path)

    def on_deleted(self, event):
        '''Handle file deletion - remove from queue only if file was truly deleted.'''
        if event.is_directory:
            return
        if not event.src_path.lower().endswith('.mp4'):
            return
        
        filepath = event.src_path
        path = Path(filepath)
        filename = path.name
        
        # Check if file was moved to processing_folder (legitimate processing, not deletion)
        cfg = get_config()
        processing_path = cfg.processing_path / filename
        if processing_path.exists():
            # File was legitimately moved to processing - let the upload continue
            return
        
        with upload_queue_lock:
            # Find and remove the entry from queue
            for i, entry in enumerate(upload_queue):
                if entry.get('original_name') == filename:
                    removed_name = entry.get('name', 'unknown')
                    upload_queue.pop(i)
                    print(f'[WATCHER] Removed from queue (deleted): {filename}')
                    add_log_entry(f'Removed from queue (file deleted): {filename}', 'WATCHER')
                    break
        
        # Also clear from upload_status if present
        with upload_status_lock:
            status_key = path.stem
            if status_key in upload_status:
                del upload_status[status_key]
                print(f'[WATCHER] Cleared upload status for deleted file: {filename}')

    def on_modified(self, event):
        '''Handle file modification - only process if not already being tracked.
        
        OBS fires on_modified events while writing. We only process the file
        if there isn't already a thread waiting for it to stabilize.
        '''
        if event.is_directory:
            return
        if not event.src_path.lower().endswith('.mp4'):
            return
        with token_lock:
            if access_token is None:
                return
        # Skip if this file is already being monitored by a _queue_file thread
        filepath = str(Path(event.src_path).resolve())
        if filepath in _pending_stability_checks:
            return
        self._queue_file(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        if not event.dest_path.lower().endswith('.mp4'):
            return
        with token_lock:
            if access_token is None:
                print('[WARNING] File detected but user is not authenticated. Skipping.')
                add_log_entry('File detected but user is not authenticated. Skipping.', 'WARNING')
                return
        self._queue_file(event.dest_path)

    def _queue_file(self, filepath: str):
        '''Wait indefinitely for file to stabilize, then add to upload queue.
        
        Since this runs in a background thread, it will patiently wait for OBS
        to finish recording (which may take minutes). Only queues the file when
        its size remains unchanged for 3 consecutive checks (3 seconds of stability).
        Only ONE thread monitors each file at a time (deduplicated via _pending_stability_checks).
        '''
        def delayed_queue():
            resolved_path = str(Path(filepath).resolve())
            
            # Register this file as being monitored
            with _pending_stability_checks_lock:
                if resolved_path in _pending_stability_checks:
                    return  # Another thread is already monitoring this file
                _pending_stability_checks.add(resolved_path)
            
            try:
                delay = get_config().stabilization_delay
                time.sleep(delay)
                path = Path(filepath)
                if not path.exists():
                    return
                
                print(f'[WATCHER] Waiting for file to finish recording: {path.name}')
                add_log_entry(f'Waiting for file to stabilize: {path.name}', 'WATCHER')
                
                # Loop indefinitely until file stabilizes - OBS may take minutes to finish recording
                stability_delay = 1.0  # Wait 1 second between size checks
                required_stable_checks = 3  # File must be stable for 3 consecutive checks (3 seconds)
                
                previous_size = -1
                stable_count = 0
                check_count = 0
                
                while True:
                    try:
                        current_size = path.stat().st_size
                    except (OSError, FileNotFoundError):
                        return
                    
                    check_count += 1
                    
                    if current_size == previous_size and current_size > 0:
                        stable_count += 1
                        if stable_count >= required_stable_checks:
                            print(f'[STABILITY] File stabilized after {check_count} checks: {path.name} ({_format_bytes(current_size)})')
                            add_log_entry(f'File stabilized: {path.name} ({_format_bytes(current_size)})', 'WATCHER')
                            break
                    else:
                        stable_count = 0
                        if check_count % 5 == 0:
                            print(f'[STABILITY] Still recording: {path.name} (size={_format_bytes(current_size)}, check {check_count})')
                    
                    previous_size = current_size
                    time.sleep(stability_delay)
                
                with upload_queue_lock:
                    if any(entry.get('original_name') == path.name for entry in upload_queue):
                        return
                seq_num = _reserve_sequence_number()
                upload_name = _append_upload_queue_entry(path, seq_num)
                print(f'[QUEUE] Added to upload queue: {path.name} as {upload_name} ({_format_bytes(path.stat().st_size)})')
                add_log_entry(f'Added to upload queue: {upload_name}', 'QUEUE')
            finally:
                # Remove from pending set when done (or on error)
                with _pending_stability_checks_lock:
                    _pending_stability_checks.discard(resolved_path)

        Thread(target=delayed_queue, daemon=True).start()


def _scan_watch_folder_for_new_files() -> int:
    '''Queue stable MP4 files already present in the watch folder.'''
    watch_path = get_config().watch_path
    mp4_files = sorted(watch_path.glob('*.mp4'))

    if not mp4_files:
        print('[WATCHER] No existing .mp4 files to scan.')
        add_log_entry('No existing .mp4 files to scan.', 'WATCHER')
        return 0

    print(f'[WATCHER] Scan mode: Found {len(mp4_files)} .mp4 file(s)')
    add_log_entry(f'Scan mode: Found {len(mp4_files)} .mp4 file(s)', 'WATCHER')

    queued_count = 0
    for mp4 in mp4_files:
        resolved_path = str(mp4.resolve())

        with _pending_stability_checks_lock:
            if resolved_path in _pending_stability_checks:
                continue
            _pending_stability_checks.add(resolved_path)

        try:
            size_before = mp4.stat().st_size
            time.sleep(0.5)
            size_after = mp4.stat().st_size

            if size_before == size_after and size_after > 0:
                with upload_queue_lock:
                    if any(entry.get('original_name') == mp4.name for entry in upload_queue):
                        print(f'[QUEUE] Skipping already-queued file: {mp4.name}')
                        add_log_entry(f'Skipping already-queued file: {mp4.name}', 'QUEUE')
                        continue

                seq_num = _reserve_sequence_number()
                upload_name = _append_upload_queue_entry(mp4, seq_num)
                queued_count += 1
                print(f'[QUEUE] Added to queue: {mp4.name} as {upload_name} ({_format_bytes(size_after)})')
                add_log_entry(f'Added to queue: {upload_name}', 'QUEUE')
            else:
                print(
                    f'[WARNING] Skipping unstable file during scan: {mp4.name} '
                    f'(size changed from {_format_bytes(size_before)} to {_format_bytes(size_after)})'
                )
                add_log_entry(f'Skipping unstable file: {mp4.name}', 'WARNING')
        except (OSError, FileNotFoundError):
            print(f'[WARNING] File disappeared during scan: {mp4.name}')
            add_log_entry(f'File disappeared during scan: {mp4.name}', 'WARNING')
        finally:
            with _pending_stability_checks_lock:
                _pending_stability_checks.discard(resolved_path)

    return queued_count


def upload_worker():
    '''Background worker that processes the upload queue continuously.'''
    while True:
        next_item = None
        with upload_queue_lock:
            for entry in upload_queue:
                if entry.get('status') != 'uploading':
                    next_item = entry
                    break

        if next_item is None:
            time.sleep(1)
            continue

        with upload_queue_lock:
            next_item['status'] = 'uploading'

        try:
            asyncio.run(process_single_file(next_item))
        except Exception as e:
            print(f'[FAILED] Error processing {next_item["original_name"]}: {e}')
            add_log_entry(f'Error processing {next_item["original_name"]}: {e}', 'FAILED')

        with upload_queue_lock:
            try:
                upload_queue.remove(next_item)
            except ValueError:
                pass


# Track the upload worker thread so we can ensure it is running after settings changes
upload_worker_thread: Optional[Thread] = None
upload_worker_thread_lock = Lock()


def ensure_upload_worker_running() -> None:
    '''Start the upload_worker thread if it is not already alive.'''
    global upload_worker_thread
    with upload_worker_thread_lock:
        if upload_worker_thread is None or not upload_worker_thread.is_alive():
            upload_worker_thread = Thread(target=upload_worker, daemon=True)
            upload_worker_thread.start()
            print('[UPLOAD] Upload worker thread started (ensured)')
            add_log_entry('Upload worker thread started', 'UPLOAD')


async def run_catch_up(processing_lock: Lock):
    '''Process any existing .mp4 files in watch_folder synchronously for tests.'''
    processing_path = get_config().processing_path

    # Handle leftover files in processing_folder from previous run
    leftover_processing = sorted(processing_path.glob('*.mp4'))
    if leftover_processing:
        print(f'[WATCHER] Found {len(leftover_processing)} leftover file(s) in processing_folder')
        add_log_entry(f'Found {len(leftover_processing)} leftover file(s) in processing_folder', 'WATCHER')
        for mp4 in leftover_processing:
            matching_qr = list(processing_path.glob(f'{mp4.stem}.png'))
            if matching_qr:
                try:
                    processed_name = timestamped_name(mp4.name)
                    processed_path = get_config().processed_path / processed_name
                    shutil.move(str(mp4), str(processed_path))
                    qr_dest = get_config().qr_codes_path / matching_qr[0].name
                    shutil.move(str(matching_qr[0]), str(qr_dest))
                    print(f'[WATCHER] Recovered completed upload: {mp4.name}')
                    add_log_entry(f'Recovered completed upload: {mp4.name}', 'WATCHER')
                    global session_processed_count
                    session_processed_count += 1
                except Exception as e:
                    print(f'[WATCHER] Error recovering file {mp4.name}: {e}')
                    add_log_entry(f'Error recovering file {mp4.name}: {e}', 'WATCHER')
            else:
                try:
                    watch_path = get_config().watch_path
                    dest = watch_path / mp4.name
                    if dest.exists():
                        stem = mp4.stem
                        suffix = mp4.suffix
                        i = 1
                        while dest.exists():
                            dest = watch_path / f'{stem}_{i}{suffix}'
                            i += 1
                    shutil.move(str(mp4), str(dest))
                    print(f'[WATCHER] Moved unprocessed file back to watch_folder: {mp4.name}')
                    add_log_entry(f'Moved unprocessed file back to watch_folder: {mp4.name}', 'WATCHER')
                except Exception as e:
                    print(f'[WATCHER] Error moving file back to watch_folder {mp4.name}: {e}')
                    add_log_entry(f'Error moving file back to watch_folder {mp4.name}: {e}', 'WATCHER')

    # Add existing files from watch_folder to queue with stability check
    queued_count = _scan_watch_folder_for_new_files()
    print(f'[WATCHER] Catch-up complete. {queued_count} file(s) queued for upload.')
    add_log_entry(f'Catch-up complete. {queued_count} file(s) queued.', 'WATCHER')
    # Note: upload_worker thread will process these items asynchronously


def start_watcher():
    '''Start the watchdog observer and upload worker in background threads (called once at login).'''
    processing_lock = Lock()

    # Initialize sequence counter from existing QR files
    initialize_sequence_counter()

    try:
        asyncio.run(run_catch_up(processing_lock))
    except Exception as e:
        print(f'[WATCHER] Catch-up error: {e}')
        add_log_entry(f'Catch-up error: {e}', 'WATCHER')

    try:
        _start_watcher_observer()
    except Exception as e:
        print(f'[WATCHER] Failed to start observer: {e}')
        add_log_entry(f'Failed to start watcher observer: {e}', 'WATCHER')
        return

    # Start the upload worker thread
    try:
        Thread(target=upload_worker, daemon=True).start()
        print('[UPLOAD] Upload worker thread started')
        add_log_entry('Upload worker thread started', 'UPLOAD')
    except Exception as e:
        print(f'[FAILED] Failed to start upload worker: {e}')
        add_log_entry(f'Failed to start upload worker: {e}', 'FAILED')

    try:
        while True:
            time.sleep(30)
            _scan_watch_folder_for_new_files()
    except KeyboardInterrupt:
        pass


def _run_catch_up_sync(processing_lock: Lock) -> None:
    '''Synchronous wrapper for run_catch_up, intended for background threads.'''
    try:
        asyncio.run(run_catch_up(processing_lock))
    except Exception as e:
        print(f'[WATCHER] Catch-up error: {e}')
        add_log_entry(f'Catch-up error: {e}', 'WATCHER')


_watcher_observer: Optional[BaseObserver] = None
_watcher_lock = Lock()


def _start_watcher_observer() -> None:
    '''Create and start a new observer on the current WATCH_PATH.'''
    global _watcher_observer
    processing_lock = Lock()
    event_handler = MP4Handler()
    observer = Observer()
    watch_path_str = str(get_config().watch_path)
    print(f'[WATCHER] Starting observer on path: {watch_path_str}')
    add_log_entry(f'Starting file watcher on: {watch_path_str}', 'WATCHER')
    observer.schedule(event_handler, watch_path_str, recursive=False)
    observer.start()
    _watcher_observer = observer
    print(f'[WATCHER] Live monitoring started on {watch_path_str}')
    add_log_entry(f'Live monitoring started on {watch_path_str}', 'WATCHER')
    
    # Verify the observer is actually running
    import time
    time.sleep(0.5)
    if _watcher_observer.is_alive():
        print(f'[WATCHER] Observer is running and monitoring for new .mp4 files')
        add_log_entry('Watcher is active and monitoring for new files', 'WATCHER')
    else:
        print(f'[WATCHER] WARNING: Observer failed to start properly!')
        add_log_entry('WARNING: Watcher observer is not running!', 'WATCHER')


def _restart_watcher_for_settings() -> None:
    '''Stop existing observer and start a new one with updated settings.'''
    global _watcher_observer
    with _watcher_lock:
        if _watcher_observer is not None:
            _watcher_observer.stop()
            try:
                _watcher_observer.join(timeout=5)
            except Exception:
                pass
            _watcher_observer = None

    cfg = get_config()
    cfg.watch_path.mkdir(parents=True, exist_ok=True)

    _start_watcher_observer()
    print(f'[SETTINGS] Watcher restarted with updated settings. Watch={cfg.watch_path}')
    add_log_entry(f'Watcher restarted. Watch folder: {cfg.watch_path}', 'SETTINGS')

    # Trigger catch-up scan on the new folder (deduped inside run_catch_up).
    # This ensures pre-existing files in a newly-selected watch folder are processed.
    try:
        ensure_upload_worker_running()
        Thread(target=_run_catch_up_sync, args=(Lock(),), daemon=True).start()
        print('[SETTINGS] Catch-up scan triggered for new watch folder')
        add_log_entry('Catch-up scan triggered for new watch folder', 'SETTINGS')
    except Exception as e:
        print(f'[SETTINGS] Catch-up trigger error: {e}')
        add_log_entry(f'Catch-up trigger error: {e}', 'SETTINGS')


def _open_browser_delayed() -> None:
    '''Background worker: wait for server to bind, then open browser.'''
    global _browser_opened
    if _browser_opened:
        return
    _browser_opened = True
    time.sleep(1.5)
    if setup_mode:
        configure_url = f'https://localhost:{SERVER_PORT}/configure'
        print(f'[INFO] Setup mode detected. Opening configuration page: {configure_url}')
        add_log_entry('Opening configuration page for initial setup.', 'INFO')
        webbrowser.open(configure_url)
    else:
        control_panel_url = f'https://localhost:{SERVER_PORT}/'
        customer_display_url = f'https://localhost:{SERVER_PORT}/customer'
        print(f'[INFO] Launching browser windows...')
        add_log_entry(f'Launching control panel: {control_panel_url}', 'INFO')
        add_log_entry(f'Launching customer display: {customer_display_url}', 'INFO')
        webbrowser.open(control_panel_url)
        time.sleep(0.5)
        webbrowser.open_new_tab(customer_display_url)


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(title='Frame.io V4 QR Code Automation')
templates = Jinja2Templates(directory=str(TEMPLATES_PATH))

_qr_static_path = get_config().qr_codes_path.resolve()
app.mount('/qr_codes', StaticFiles(directory=str(_qr_static_path)), name='qr_codes')


@app.get('/', response_class=HTMLResponse)
async def home(request: Request):
    '''Render the main page.'''
    global user_info
    is_authenticated = False
    display_name = ''
    auth_url = get_auth_url()

    with token_lock:
        if access_token is not None:
            is_authenticated = True
            if user_info is None:
                try:
                    user_info = await get_current_user()
                except Exception:
                    user_info = {}
            display_name = user_info.get('name') or user_info.get('email') or 'Staff'

    return templates.TemplateResponse(
        'index.html',
        {
            'request': request,
            'is_authenticated': is_authenticated,
            'display_name': display_name,
            'auth_url': auth_url,
            'log_feed': list(log_feed[-50:]),
        },
    )


@app.get('/customer', response_class=HTMLResponse)
async def customer_page(request: Request):
    '''Render the customer-facing QR display page.'''
    return templates.TemplateResponse(
        'customer.html',
        {
            'request': request,
        },
    )


@app.get('/login')
async def login():
    '''Redirect to Adobe IMS OAuth authorization page.'''
    return RedirectResponse(get_auth_url())


@app.get('/callback')
async def callback(code: str, error: Optional[str] = None):
    '''Handle OAuth callback from Adobe IMS.'''
    if error:
        print(f'[OAUTH] Authorization error: {error}')
        add_log_entry(f'Authorization error: {error}', 'OAUTH')
        raise HTTPException(status_code=400, detail=f'OAuth error: {error}')

    if not code:
        raise HTTPException(status_code=400, detail='Missing authorization code')

    try:
        await exchange_code_for_token(code)
        global user_info, needs_folder_setup
        user_data = await get_current_user()
        user_info = user_data.get('data', {})
        name = user_info.get('name') or user_info.get('email') or 'Staff'
        print(f'[OAUTH] Successfully authenticated as: {name}')
        add_log_entry(f'Authenticated as: {name}', 'OAUTH')

        try:
            await get_account_id()
        except Exception as e:
            print(f'[OAUTH] Warning: Could not resolve account ID: {e}')

        if not FRAMEIO_FOLDER_ID or FRAMEIO_FOLDER_ID in ('', 'YOUR_FOLDER_ID'):
            needs_folder_setup = True
            print(f'[OAUTH] No folder_id configured. Redirecting to folder setup.')
            add_log_entry('No folder configured. Redirecting to folder setup.', 'OAUTH')
            return RedirectResponse(url='/setup-folder', status_code=303)

        try:
            await get_project_id()
        except Exception as e:
            print(f'[OAUTH] Warning: Could not resolve project ID: {e}')

        Thread(target=start_watcher, daemon=True).start()

        return RedirectResponse(url='/', status_code=303)
    except httpx.HTTPStatusError as e:
        print(f'[OAUTH] Token exchange failed: {e}')
        add_log_entry(f'Token exchange failed: {e}', 'OAUTH')
        raise HTTPException(status_code=400, detail=f'Token exchange failed: {e.response.text}')
    except Exception as e:
        print(f'[OAUTH] Error during callback: {e}')
        add_log_entry(f'Error during callback: {e}', 'OAUTH')
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/logs')
async def get_logs():
    '''Return the log feed as JSON for the frontend to poll.'''
    return {'logs': list(log_feed[-50:])}


@app.get('/api/stats')
async def get_stats():
    '''Return statistics with session-based processed/failed counts.'''
    cfg = get_config()
    qr_count = len(list(cfg.qr_codes_path.glob('*.png'))) if cfg.qr_codes_path.exists() else 0
    return {
        'processed': session_processed_count,
        'qr_codes': qr_count,
        'failed': session_failed_count
    }


@app.get('/api/queue')
async def get_queue():
    '''Return the current upload queue for the frontend.'''
    with upload_queue_lock:
        items = []
        for entry in upload_queue:
            items.append({
                'name': entry.get('name', ''),
                'original_name': entry.get('original_name', ''),
                'size_bytes': entry.get('size_bytes', 0),
                'size_human': _format_bytes(entry.get('size_bytes', 0)),
                'status': entry.get('status', 'queuing'),
                'qr_path': entry.get('qr_path', ''),
            })
        return {'queue': items}


@app.get('/api/uploading')
async def get_uploading():
    '''Return the currently uploading file info for the frontend.'''
    name = current_upload_name
    if not name:
        return {'name': None}
    # Find the entry in the queue to get size info
    with upload_queue_lock:
        for entry in upload_queue:
            if entry.get('name') == name:
                return {
                    'name': name,
                    'original_name': entry.get('original_name', ''),
                    'size_bytes': entry.get('size_bytes', 0),
                    'size_human': _format_bytes(entry.get('size_bytes', 0)),
                    'qr_path': entry.get('qr_path', ''),
                }
    return {'name': name, 'size_human': '', 'qr_path': ''}


@app.get('/status')
async def status():
    '''Return current authentication status.'''
    with token_lock:
        return {
            'authenticated': access_token is not None,
            'name': user_info.get('name', '') if user_info else '',
            'email': user_info.get('email', '') if user_info else '',
            'account_id': account_id or '',
            'project_id': project_id or '',
        }


@app.get('/api/qr-history')
async def get_qr_history(timecode: Optional[str] = None, video_name: Optional[str] = None, seqnum: Optional[str] = None):
    '''Return list of all generated QR codes with parsed metadata and upload status.'''
    qr_codes_dir = get_config().qr_codes_path
    qr_history = []

    if not qr_codes_dir.exists():
        return {'qr_codes': []}

    normalized_timecode_search = None
    if timecode:
        normalized_timecode_search = timecode.replace('_', '').replace('-', '').replace('/', '').replace(':', '').replace(' ', '')

    # Parse sequence number filter
    seqnum_filter = None
    if seqnum:
        seqnum_filter = seqnum.strip()
        # Support formats: "5", "5-10", ">5", "<10"
        if '-' in seqnum_filter and not seqnum_filter.startswith('-') and seqnum_filter.count('-') == 1:
            parts = seqnum_filter.split('-')
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                seqnum_filter = {'type': 'range', 'min': int(parts[0]), 'max': int(parts[1])}
            else:
                seqnum_filter = {'type': 'exact', 'value': int(seqnum_filter) if seqnum_filter.isdigit() else None}
        elif seqnum_filter.startswith('>='):
            seqnum_filter = {'type': 'gte', 'value': int(seqnum_filter[2:]) if seqnum_filter[2:].isdigit() else None}
        elif seqnum_filter.startswith('<='):
            seqnum_filter = {'type': 'lte', 'value': int(seqnum_filter[2:]) if seqnum_filter[2:].isdigit() else None}
        elif seqnum_filter.startswith('>'):
            seqnum_filter = {'type': 'gt', 'value': int(seqnum_filter[1:]) if seqnum_filter[1:].isdigit() else None}
        elif seqnum_filter.startswith('<'):
            seqnum_filter = {'type': 'lt', 'value': int(seqnum_filter[1:]) if seqnum_filter[1:].isdigit() else None}
        elif seqnum_filter.isdigit():
            seqnum_filter = {'type': 'exact', 'value': int(seqnum_filter)}
        else:
            seqnum_filter = None

    for qr_file in sorted(qr_codes_dir.glob('*.png'), reverse=True):
        filename = qr_file.name
        without_ext = filename.replace('.png', '')
        
        # Extract sequence number first (before the first underscore)
        seq_num = extract_sequence_number(filename)
        
        # Apply sequence number filter
        if seqnum_filter:
            if seq_num is None:
                continue  # Skip files without sequence numbers if filter is active
            if seqnum_filter.get('type') == 'exact':
                if seq_num != seqnum_filter.get('value'):
                    continue
            elif seqnum_filter.get('type') == 'range':
                if seq_num < seqnum_filter.get('min', 0) or seq_num > seqnum_filter.get('max', 999999):
                    continue
            elif seqnum_filter.get('type') == 'gt':
                if seq_num <= seqnum_filter.get('value', 0):
                    continue
            elif seqnum_filter.get('type') == 'gte':
                if seq_num < seqnum_filter.get('value', 0):
                    continue
            elif seqnum_filter.get('type') == 'lt':
                if seq_num >= seqnum_filter.get('value', 999999):
                    continue
            elif seqnum_filter.get('type') == 'lte':
                if seq_num > seqnum_filter.get('value', 999999):
                    continue
        
        # Skip if too short (backward compatibility for old format)
        if len(without_ext) < 14:
            continue

        # Use the shared parse_qr_filename function
        parsed_seq, parsed_video_name, timecode_human = parse_qr_filename(filename)
        timecode_raw = without_ext  # fallback raw

        if normalized_timecode_search:
            normalized_raw = timecode_raw.replace('_', '').replace('-', '')
            normalized_human = timecode_human.replace('/', '').replace(':', '').replace(' ', '')
            if normalized_timecode_search not in normalized_raw and normalized_timecode_search not in normalized_human:
                continue

        if video_name:
            if video_name.lower() not in parsed_video_name.lower():
                continue

        # Determine upload status for this QR code
        status = None
        # The filename stem (without .png) matches the upload_name
        upload_name_stem = without_ext
        with upload_status_lock:
            if upload_name_stem in upload_status:
                status = upload_status[upload_name_stem]
            elif current_upload_name and upload_name_stem == current_upload_name:
                status = 'uploading'

        qr_entry = {
            'filename': filename,
            'qr_path': f'/qr_codes/{filename}',
            'seqnum': seq_num,
            'timecode_raw': timecode_raw,
            'timecode_human': timecode_human,
            'video_name': parsed_video_name,
            'datetime_obj': None,
            'status': status,
        }
        qr_history.append(qr_entry)

    return {'qr_codes': qr_history}


@app.get('/api/settings')
async def get_settings():
    '''Return the current dynamic configuration as JSON.'''
    return get_config().to_dict()


@app.get('/configure', response_class=HTMLResponse)
async def configure_page(request: Request):
    '''Render the standalone configuration page.'''
    return templates.TemplateResponse(
        'configure.html',
        {
            'request': request,
            'config_path': str(CONFIG_PATH) if CONFIG_PATH else None,
            'current_config': CFG,
            'setup_mode': setup_mode,
        },
    )


@app.get('/api/config/raw')
async def get_raw_config():
    '''Return the raw config.json contents for the configure page.'''
    if CONFIG_PATH and CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    return CFG


@app.post('/api/config/save')
async def save_config(payload: dict):
    '''Write the provided payload to config.json and reload globals.'''
    global CFG, CONFIG_PATH, FRAMEIO_CLIENT_ID, FRAMEIO_CLIENT_SECRET, FRAMEIO_FOLDER_ID
    global SERVER_HOST, SERVER_PORT, SSL_CERTFILE, SSL_KEYFILE
    global TEMPLATES_PATH, REDIRECT_URI, FRAMEIO_AUTH_URL, FRAMEIO_TOKEN_URL, OAUTH_SCOPE
    global setup_mode
    try:
        config_path = CONFIG_PATH or Path('data/config.json')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Ensure share section exists in payload
        if 'share' not in payload:
            payload['share'] = {'expiration_days': 7}
        
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)

        CONFIG_PATH = config_path.resolve()

        CFG = payload
        FRAMEIO_CLIENT_ID = payload['frameio']['client_id']
        FRAMEIO_CLIENT_SECRET = payload['frameio']['client_secret']
        FRAMEIO_FOLDER_ID = payload['frameio']['folder_id']
        SERVER_HOST = payload['server']['host']
        SERVER_PORT = payload['server']['port']
        SSL_CERTFILE = payload['server']['ssl_certfile']
        SSL_KEYFILE = payload['server']['ssl_keyfile']
        LOG_FILE = payload['server'].get('log_file', 'data/automation.log')
        REDIRECT_URI = payload['oauth']['redirect_uri']
        FRAMEIO_AUTH_URL = payload['oauth']['auth_url']
        FRAMEIO_TOKEN_URL = payload['oauth']['token_url']
        OAUTH_SCOPE = payload['oauth']['scope']
        
        # Ensure share section exists with default if not provided
        if 'share' not in CFG:
            CFG['share'] = {'expiration_days': 7}

        TEMPLATES_PATH = resource_path('templates') if getattr(sys, 'frozen', False) else Path('./templates')

        setup_mode = False

        ensure_ssl_certificate(SSL_CERTFILE, SSL_KEYFILE)

        global config_instance, WATCH_PATH, PROCESSED_PATH, FAILED_PATH, QR_CODES_PATH
        config_instance = DynamicConfig(
            watch_folder=payload['folders']['watch'],
            processed_folder=payload['folders']['processed'],
            failed_folder=payload['folders']['failed'],
            qr_codes_folder=payload['folders']['qr_codes'],
            share_expiration_days=payload.get('share', {}).get('expiration_days', 7),
        )
        WATCH_PATH = config_instance.watch_path
        PROCESSED_PATH = config_instance.processed_path
        FAILED_PATH = config_instance.failed_path
        QR_CODES_PATH = config_instance.qr_codes_path

        for folder in [WATCH_PATH, PROCESSED_PATH, FAILED_PATH, QR_CODES_PATH, TEMPLATES_PATH]:
            folder.mkdir(parents=True, exist_ok=True)

        _restart_watcher_for_settings()

        return {'status': 'ok', 'message': 'Configuration saved'}
    except Exception as exc:
        logging.exception('Setup failed')
        return JSONResponse(
            status_code=500,
            content={'status': 'error', 'message': f'Configuration save failed: {exc}'},
        )


@app.post('/api/shutdown')
async def shutdown():
    '''Shut down the server cleanly: stop watcher, then exit the process.'''
    def _do_shutdown():
        global _watcher_observer
        with _watcher_lock:
            if _watcher_observer is not None:
                try:
                    _watcher_observer.stop()
                    _watcher_observer.join(timeout=3)
                except Exception:
                    pass
                _watcher_observer = None
        logging.shutdown()
        time.sleep(0.5)
        os._exit(0)

    Thread(target=_do_shutdown, daemon=True).start()
    return {'status': 'shutting_down', 'message': 'Server is shutting down cleanly...'}


@app.post('/api/settings')
async def update_settings(payload: dict):
    '''Validate and apply new settings. Restart watcher if folder paths changed.'''
    key_mapping = {
        'WATCH_FOLDER': 'watch_folder',
        'PROCESSED_FOLDER': 'processed_folder',
        'FAILED_FOLDER': 'failed_folder',
        'QR_CODES_FOLDER': 'qr_codes_folder',
        'STABILIZATION_DELAY': 'stabilization_delay',
        'MAX_RETRIES': 'max_retries',
        'SHARE_EXPIRATION_DAYS': 'share_expiration_days',
    }
    mapped_payload = {}
    for key, value in payload.items():
        mapped_key = key_mapping.get(key, key)
        mapped_payload[mapped_key] = value

    try:
        config_instance.update(**mapped_payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cfg = get_config()
    for path in [cfg.watch_path, cfg.processed_path, cfg.failed_path, cfg.qr_codes_path]:
        path.mkdir(parents=True, exist_ok=True)

    _restart_watcher_for_settings()

    return {'status': 'ok', 'settings': get_config().to_dict()}


# ---------------------------------------------------------------------------
# First-Time Setup API Endpoints
# ---------------------------------------------------------------------------

@app.get('/api/setup/status')
async def setup_status():
    '''Return whether the application is in first-time setup mode.'''
    global setup_mode
    return {'setup_mode': setup_mode}


@app.post('/api/setup/initialize')
async def setup_initialize(payload: dict):
    '''Initialize configuration from the setup wizard.'''
    global setup_mode, CFG, CONFIG_PATH, FRAMEIO_CLIENT_ID, FRAMEIO_CLIENT_SECRET, FRAMEIO_FOLDER_ID
    global SERVER_HOST, SERVER_PORT, SSL_CERTFILE, SSL_KEYFILE
    global TEMPLATES_PATH, REDIRECT_URI, FRAMEIO_AUTH_URL, FRAMEIO_TOKEN_URL, OAUTH_SCOPE

    client_id = payload.get('client_id', '').strip()
    client_secret = payload.get('client_secret', '').strip()
    folder_id = payload.get('folder_id', '').strip()

    if not all([client_id, client_secret]):
        raise HTTPException(status_code=400, detail='client_id and client_secret are required')

    new_config = {
        'frameio': {'client_id': client_id, 'client_secret': client_secret, 'folder_id': folder_id},
            'server': {
                'host': '0.0.0.0', 'port': 8000, 'ssl_certfile': 'data/cert.pem',
                'ssl_keyfile': 'data/key.pem', 'log_file': 'data/automation.log'
            },
            'folders': {
                'watch': payload.get('watch_folder', 'data/watch_folder'),
                'processed': payload.get('processed_folder', 'data/processed_folder'),
                'failed': payload.get('failed_folder', 'data/failed_folder'),
                'qr_codes': payload.get('qr_codes_folder', 'data/qr_codes'),
            },
            'oauth': {
                'redirect_uri': payload.get('redirect_uri', f'https://localhost:{payload.get("server_port", 8000)}/callback'),
                'auth_url': payload.get('auth_url', 'https://ims-na1.adobelogin.com/ims/authorize/v2'),
                'token_url': payload.get('token_url', 'https://ims-na1.adobelogin.com/ims/token/v3'),
                'scope': payload.get('scope', 'offline_access,openid,email,profile,additional_info.roles'),
            },
            'share': {'expiration_days': 7},
        }

    try:
        config_path = CONFIG_PATH or Path('data/config.json')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=2)

        CONFIG_PATH = config_path.resolve()

        for folder_name in ['watch', 'processed', 'failed', 'qr_codes']:
            folder_path = Path(new_config['folders'][folder_name])
            os.makedirs(folder_path, exist_ok=True)

        CFG = new_config
        FRAMEIO_CLIENT_ID = new_config['frameio']['client_id']
        FRAMEIO_CLIENT_SECRET = new_config['frameio']['client_secret']
        FRAMEIO_FOLDER_ID = new_config['frameio']['folder_id']
        SERVER_HOST = new_config['server']['host']
        SERVER_PORT = new_config['server']['port']
        SSL_CERTFILE = new_config['server']['ssl_certfile']
        SSL_KEYFILE = new_config['server']['ssl_keyfile']
        REDIRECT_URI = new_config['oauth']['redirect_uri']
        FRAMEIO_AUTH_URL = new_config['oauth']['auth_url']
        FRAMEIO_TOKEN_URL = new_config['oauth']['token_url']
        OAUTH_SCOPE = new_config['oauth']['scope']

        global config_instance, WATCH_PATH, PROCESSED_PATH, FAILED_PATH, QR_CODES_PATH, AUTO_OPEN_BROWSER
        config_instance = DynamicConfig(
            watch_folder=new_config['folders']['watch'],
            processed_folder=new_config['folders']['processed'],
            failed_folder=new_config['folders']['failed'],
            qr_codes_folder=new_config['folders']['qr_codes'],
            share_expiration_days=new_config.get('share', {}).get('expiration_days', 7),
        )
        WATCH_PATH = config_instance.watch_path
        PROCESSED_PATH = config_instance.processed_path
        FAILED_PATH = config_instance.failed_path
        QR_CODES_PATH = config_instance.qr_codes_path
        AUTO_OPEN_BROWSER = config_instance.auto_open_browser

        for folder in [WATCH_PATH, PROCESSED_PATH, FAILED_PATH, QR_CODES_PATH, TEMPLATES_PATH]:
            os.makedirs(folder, exist_ok=True)

        with config_lock:
            setup_mode = False

        print('[SETUP] Configuration initialized. Exiting setup mode.')
        add_log_entry('Setup complete. Configuration saved.', 'SETUP')

        return {'status': 'ok', 'message': 'Configuration saved successfully'}
    except Exception as exc:
        logging.exception('Setup failed')
        return JSONResponse(
            status_code=500,
            content={'status': 'error', 'message': f'Setup initialization failed: {exc}'},
        )


# ---------------------------------------------------------------------------
# Folder Setup Wizard
# ---------------------------------------------------------------------------

@app.get('/api/setup/folders')
async def setup_list_folders():
    '''(Legacy) Return a list of folders for the authenticated user to pick from.'''
    if access_token is None:
        raise HTTPException(status_code=401, detail='Not authenticated')
    try:
        aid = await get_account_id()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'Could not resolve account: {e}')
    headers = get_auth_headers()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/folders',
            headers=headers,
            params={'page[size]': 100},
        )
        resp.raise_for_status()
        data = resp.json()
    folders = []
    for item in data.get('data', []):
        folders.append({'id': item.get('id'), 'name': item.get('name', 'Untitled'), 'type': item.get('type', 'folder')})
    return {'folders': folders}


@app.get('/api/setup/workspaces')
async def setup_list_workspaces():
    '''List workspaces in the user's account for folder setup.'''
    if access_token is None:
        raise HTTPException(status_code=401, detail='Not authenticated')
    headers = get_auth_headers()
    aid = await get_account_id()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f'{FRAMEIO_API_BASE}/accounts/{aid}/workspaces', headers=headers)
        resp.raise_for_status()
        data = resp.json()
    workspaces = []
    for item in data.get('data', []):
        workspaces.append({'id': item.get('id'), 'name': item.get('name', 'Untitled')})
    return {'workspaces': workspaces}


@app.get('/api/setup/projects')
async def setup_list_projects(workspace_id: str):
    '''List projects in a given workspace.'''
    if access_token is None:
        raise HTTPException(status_code=401, detail='Not authenticated')
    headers = get_auth_headers()
    aid = await get_account_id()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/workspaces/{workspace_id}/projects',
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
    projects = []
    for item in data.get('data', []):
        projects.append({'id': item.get('id'), 'name': item.get('name', 'Untitled'), 'root_folder_id': item.get('root_folder_id')})
    return {'projects': projects}


@app.get('/api/setup/folders-list')
async def setup_list_folders_in_folder(parent_folder_id: str):
    '''List folders inside a given parent folder (scoped listing).'''
    if access_token is None:
        raise HTTPException(status_code=401, detail='Not authenticated')
    headers = get_auth_headers()
    aid = await get_account_id()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/folders/{parent_folder_id}/folders',
            headers=headers,
            params={'page_size': 100, 'include_total_count': True},
        )
        resp.raise_for_status()
        data = resp.json()
    folders = []
    for item in data.get('data', []):
        folders.append({'id': item.get('id'), 'name': item.get('name', 'Untitled'), 'type': item.get('type', 'folder')})
    return {'folders': folders, 'total_count': data.get('total_count', 0)}


@app.get('/setup-folder', response_class=HTMLResponse)
async def setup_folder_page(request: Request):
    '''Render the folder setup page where users choose workspace/project/folder.'''
    global needs_folder_setup, setup_mode
    return templates.TemplateResponse(
        'setup_folder.html',
        {
            'request': request,
            'setup_mode': setup_mode,
            'needs_folder_setup': needs_folder_setup,
            'folder_id': FRAMEIO_FOLDER_ID or '',
            'folder_name': FRAMEIO_FOLDER_NAME,
        },
    )


@app.post('/api/setup/auto-folder')
async def setup_auto_create_folder(payload: dict):
    '''Automatically find or create a folder with the configured name.'''
    global FRAMEIO_FOLDER_ID, needs_folder_setup, setup_mode, CFG, CONFIG_PATH, FRAMEIO_FOLDER_NAME

    workspace_id = payload.get('workspace_id', '').strip()
    project_id = payload.get('project_id', '').strip()
    root_folder_id = payload.get('root_folder_id', '').strip()
    folder_name = payload.get('folder_name', FRAMEIO_FOLDER_NAME).strip()

    if not all([workspace_id, project_id, root_folder_id]):
        raise HTTPException(status_code=400, detail='workspace_id, project_id, and root_folder_id are required')
    if not folder_name:
        folder_name = FRAMEIO_FOLDER_NAME

    if access_token is None:
        raise HTTPException(status_code=401, detail='Not authenticated')

    headers = get_auth_headers()
    aid = await get_account_id()

    existing_folders = []
    url = f'{FRAMEIO_API_BASE}/accounts/{aid}/folders/{root_folder_id}/folders'
    params = {'page_size': 100, 'include_total_count': True}

    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers=headers, params=params if params else {})
            resp.raise_for_status()
            data = resp.json()
            existing_folders.extend(data.get('data', []))
            next_link = data.get('links', {}).get('next')
            url = f'{FRAMEIO_API_BASE}{next_link}' if next_link else None
            params = {}

        for folder in existing_folders:
            if folder.get('name') == folder_name:
                folder_id = folder.get('id')
                FRAMEIO_FOLDER_ID = folder_id
                FRAMEIO_FOLDER_NAME = folder_name
                print(f'[SETUP] Found existing folder: {folder_name} (id={folder_id})')
                add_log_entry(f'Using existing folder: {folder_name} (id={folder_id})', 'SETUP')
                _update_config_with_folder(folder_id, folder_name)
                return {'status': 'found', 'folder_id': folder_id, 'folder_name': folder_name, 'message': f'Using existing folder: {folder_name}'}

        create_resp = await client.post(
            f'{FRAMEIO_API_BASE}/accounts/{aid}/folders/{root_folder_id}/folders',
            headers=headers,
            json={'data': {'name': folder_name}},
        )
        create_resp.raise_for_status()
        create_data = create_resp.json()
        folder_data = create_data.get('data', {})
        new_folder_id = folder_data.get('id')

        if not new_folder_id:
            raise RuntimeError(f'Failed to create folder. Response: {create_data}')

        FRAMEIO_FOLDER_ID = new_folder_id
        FRAMEIO_FOLDER_NAME = folder_name
        print(f'[SETUP] Created folder: {folder_name} (id={new_folder_id})')
        add_log_entry(f'Created folder: {folder_name} (id={new_folder_id})', 'SETUP')
        _update_config_with_folder(new_folder_id, folder_name)

        return {'status': 'created', 'folder_id': new_folder_id, 'folder_name': folder_name, 'message': f'Folder \'{folder_name}\' created'}


def _update_config_with_folder(folder_id: str, folder_name: str) -> None:
    '''Helper to persist folder_id and folder_name to config.json and update globals.'''
    global CFG, CONFIG_PATH, FRAMEIO_FOLDER_ID, FRAMEIO_FOLDER_NAME, needs_folder_setup, setup_mode

    if 'frameio' not in CFG:
        CFG['frameio'] = {}
    CFG['frameio']['folder_id'] = folder_id
    CFG['frameio']['folder_name'] = folder_name

    config_path = CONFIG_PATH or Path('data/config.json')
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(CFG, f, indent=2)
        CONFIG_PATH = config_path.resolve()
        print(f'[CONFIG] Folder configuration saved to {config_path}')
    except Exception as exc:
        print(f'[CONFIG] Warning: Could not write config.json: {exc}')

    needs_folder_setup = False
    setup_mode = False
    FRAMEIO_FOLDER_ID = folder_id
    FRAMEIO_FOLDER_NAME = folder_name

    if access_token is not None:
        Thread(target=start_watcher, daemon=True).start()


# ---------------------------------------------------------------------------
# Customer Display API Endpoints
# ---------------------------------------------------------------------------

@app.get('/api/customer/qr')
async def get_customer_qr():
    '''Return the current QR code URL to display on the customer page.'''
    with display_state_lock:
        qr_url = None
        timecode_raw = ''
        timecode_human = ''
        video_name = ''
        seqnum = None
        is_uploading = False

        # If manual override is active, show the selected QR
        if manual_override and active_display_qr:
            qr_path = Path(active_display_qr)
            qr_url = f'/qr_codes/{qr_path.name}'
            seqnum, video_name, timecode_human = parse_qr_filename(qr_path.name)
        else:
            # Auto mode: find and return the latest QR code by timecode
            qr_codes_dir = get_config().qr_codes_path
            if qr_codes_dir.exists():
                qr_files = sorted(qr_codes_dir.glob('*.png'), reverse=True)
                if qr_files:
                    latest_qr = qr_files[0]
                    qr_url = f'/qr_codes/{latest_qr.name}'
                    seqnum, video_name, timecode_human = parse_qr_filename(latest_qr.name)

        # Check if currently uploading
        with upload_status_lock:
            is_uploading = current_upload_name is not None

        return {
            'qr_path': qr_url,
            'manual_override': manual_override,
            'queue_length': len(queued_qrs),
            'timecode_human': timecode_human,
            'video_name': video_name,
            'seqnum': seqnum,
            'is_uploading': is_uploading,
        }


# ---------------------------------------------------------------------------
# Staff Control Panel API Endpoints
# ---------------------------------------------------------------------------

@app.post('/api/staff/select-qr')
async def staff_select_qr(payload: dict):
    '''Staff manually selects a QR code to display on the customer screen.'''
    global active_display_qr, manual_override

    qr_path = payload.get('qr_path', '').strip()
    if not qr_path:
        raise HTTPException(status_code=400, detail='qr_path is required')

    with display_state_lock:
        active_display_qr = qr_path
        manual_override = True
        queued_qrs.clear()

    add_log_entry(f'Manual override activated: {Path(qr_path).name}', 'STAFF')
    return {'status': 'ok', 'qr_path': qr_path, 'manual_override': True}


@app.post('/api/staff/clear-override')
async def staff_clear_override():
    '''Staff clears the manual override, returning to automatic display mode.'''
    global active_display_qr, manual_override, queued_qrs, latest_qr

    with display_state_lock:
        if queued_qrs:
            active_display_qr = queued_qrs.pop(0)
            manual_override = len(queued_qrs) > 0
            add_log_entry(f'Manual override cleared. Showing queued QR: {Path(active_display_qr).name}', 'STAFF')
        else:
            manual_override = False
            active_display_qr = latest_qr
            add_log_entry('Manual override cleared. Showing latest QR.', 'STAFF')

    return {
        'status': 'ok',
        'qr_path': active_display_qr,
        'manual_override': manual_override,
        'queue_length': len(queued_qrs),
    }


# ---------------------------------------------------------------------------
# Display Message API Endpoints
# ---------------------------------------------------------------------------

@app.get('/api/display-message')
async def get_display_message_api():
    '''Return the current display message for the customer page.'''
    msg = load_display_message()
    return {'display_message': msg}


@app.post('/api/display-message')
async def save_display_message_api(payload: dict):
    '''Save a new display message.'''
    message = payload.get('message', '').strip()
    
    # Save to file
    success = save_display_message(message)
    
    if success:
        add_log_entry(f'Display message updated: "{message[:50]}..."', 'STAFF')
        return {'status': 'ok', 'display_message': message}
    else:
        return JSONResponse(
            status_code=500,
            content={'status': 'error', 'message': 'Failed to save display message'},
        )


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import uvicorn

    if setup_mode and not getattr(sys, 'frozen', False):
        # Interactive CLI setup could go here
        pass

    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print('=' * 60)
    print('  Frame.io V4 QR Code Automation System')
    print('  (Adobe IMS OAuth 2.0 / Frame.io V4 API)')
    print('=' * 60)
    if CONFIG_PATH:
        print(f'[CONFIG] Configuration loaded from: {CONFIG_PATH}')
    else:
        print('[CONFIG] No config.json found.')
    print(f'[OAUTH] Starting server on https://localhost:{SERVER_PORT}')
    print(f'[OAUTH] Redirect URI: {REDIRECT_URI}')
    if setup_mode:
        print('[OAUTH] Setup mode active — complete configuration via the web wizard.')
    else:
        print(f'[OAUTH] Auth URL: {get_auth_url()}')
    print(f'[OAUTH] Using SSL cert/key: {SSL_CERTFILE} / {SSL_KEYFILE}')
    print('=' * 60)
    ensure_ssl_certificate(SSL_CERTFILE, SSL_KEYFILE)

    _resolved_cert = Path(SSL_CERTFILE)
    _resolved_key = Path(SSL_KEYFILE)
    if not _resolved_cert.is_absolute():
        if getattr(sys, 'frozen', False):
            _resolved_cert = Path(sys.executable).parent / _resolved_cert
        else:
            _resolved_cert = Path(__file__).parent / _resolved_cert
    if not _resolved_key.is_absolute():
        if getattr(sys, 'frozen', False):
            _resolved_key = Path(sys.executable).parent / _resolved_key
        else:
            _resolved_key = Path(__file__).parent / _resolved_key

    if AUTO_OPEN_BROWSER:
        Thread(target=_open_browser_delayed, daemon=True).start()

    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level='info',
        ssl_certfile=str(_resolved_cert),
        ssl_keyfile=str(_resolved_key),
    )