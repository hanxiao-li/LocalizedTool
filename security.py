"""Security helpers: password hashing and LLM API-token encryption.

Passwords use werkzeug's salted PBKDF2-SHA256 hashes.
API tokens are encrypted at rest with AES-128 (Fernet) keyed by the
persistent key in data/.secret_key. Plaintext tokens are never sent to the
browser — the API only reports whether a key is configured.
"""

from cryptography.fernet import Fernet
from werkzeug.security import check_password_hash, generate_password_hash

from config import get_secret_key

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(get_secret_key())
    return _fernet


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def encrypt_token(plain: str) -> str:
    """Encrypt a plaintext token. Empty input -> empty output."""
    if not plain:
        return ''
    return _get_fernet().encrypt(plain.encode('utf-8')).decode('utf-8')


def decrypt_token(encrypted: str) -> str:
    """Decrypt a token. Returns '' on empty input or corrupt ciphertext."""
    if not encrypted:
        return ''
    try:
        return _get_fernet().decrypt(encrypted.encode('utf-8')).decode('utf-8')
    except Exception:
        return ''
