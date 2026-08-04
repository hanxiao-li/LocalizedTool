"""Application configuration and runtime paths.

All runtime data (SQLite DB, uploads, project glossaries) lives under a
`data/` directory created on first run, so the project itself stays clean
for git.
"""

import hashlib
import os

from cryptography.fernet import Fernet

# ── Paths ────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow tests / deployments to relocate all runtime data.
DATA_DIR = os.environ.get('LOCALIZEDTOOL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
PROJECTS_DIR = os.path.join(DATA_DIR, 'projects')
DB_PATH = os.path.join(DATA_DIR, 'app.db')
SECRET_KEY_FILE = os.path.join(DATA_DIR, '.secret_key')

# Uploaded files are removed after this many hours (housekeeping helper).
UPLOAD_TTL_HOURS = 24

# Maximum upload size (MB).
MAX_UPLOAD_MB = 50


def ensure_dirs() -> None:
    """Create all runtime directories if missing."""
    for d in (DATA_DIR, UPLOAD_DIR, PROJECTS_DIR):
        os.makedirs(d, exist_ok=True)


def get_secret_key() -> bytes:
    """Return the persistent Fernet key (44-char urlsafe-base64 bytes).

    The key is generated on first run and stored in data/.secret_key so that
    encrypted tokens survive restarts. Never committed to git.
    """
    ensure_dirs()
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'rb') as f:
            key = f.read().strip()
        if len(key) == 44:  # Fernet keys are 44 url-safe base64 characters
            return key
    key = Fernet.generate_key()
    with open(SECRET_KEY_FILE, 'wb') as f:
        f.write(key)
    return key


def get_flask_secret() -> str:
    """Derive the Flask session signing secret from the Fernet key."""
    return hashlib.sha256(get_secret_key()).hexdigest()
