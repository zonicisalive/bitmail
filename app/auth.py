"""
Authentication and Session Security Engine for Bitmail.
Handles secure PBKDF2-HMAC password hashing, session lifecycle,
and FastAPI route protection dependencies.
"""

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status

from app.config import settings
from app.db import get_db, utc_now_iso

logger = logging.getLogger("bitmail.auth")


def get_cipher() -> Fernet:
    """Derive 32-byte urlsafe base64 key from settings.SECRET_KEY for authenticated Fernet encryption."""
    raw_key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    b64_key = base64.urlsafe_b64encode(raw_key)
    return Fernet(b64_key)


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a secret string using authenticated Fernet symmetric encryption."""
    if not plaintext:
        return ""
    try:
        cipher = get_cipher()
        return cipher.encrypt(plaintext.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        logger.error(f"Credential encryption error: {exc}")
        return plaintext


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt an encrypted credential, or return plaintext if not encrypted or unpadded."""
    if not ciphertext:
        return ""
    if not ciphertext.startswith("gAAAAA"):
        return ciphertext
    try:
        cipher = get_cipher()
        return cipher.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except Exception:
        return ciphertext


def hash_password(password: str) -> str:
    """
    Hash a plaintext password using standard PBKDF2 HMAC SHA-256 with a 16-byte random salt.
    Format: salt_hex$hash_hex
    """
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}${key.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Constant-time password verification against stored salt_hex$hash_hex.
    """
    if not password or not stored_hash or "$" not in stored_hash:
        return False
    try:
        salt_hex, key_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return hmac.compare_digest(key, expected_key)
    except Exception as exc:
        logger.warning(f"Password verification error: {exc}")
        return False


async def create_user_session(
    user_id: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    days_valid: Optional[int] = None
) -> str:
    """
    Generate a cryptographically secure token and persist an active user session in SQLite.
    """
    token = f"bm_sess_{secrets.token_urlsafe(32)}"
    now = utc_now_iso()
    ttl_days = days_valid if days_valid is not None else settings.SESSION_EXPIRE_DAYS
    expires_dt = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    expires_at = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

    clean_ip = (ip_address or "127.0.0.1")[:45]
    clean_ua = (user_agent or "Web Client")[:200]

    async with get_db() as db:
        await db.execute("""
            INSERT INTO user_sessions (
                token, user_id, expires_at, created_at, last_seen_at, user_agent, ip_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (token, user_id, expires_at, now, now, clean_ua, clean_ip))
        await db.commit()

    return token


async def get_user_by_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Look up user by active session token.
    Validates expiration and active status, and touches last_seen_at.
    """
    if not token or not isinstance(token, str):
        return None

    clean_token = token.strip()
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("""
            SELECT 
                u.id, 
                u.email, 
                u.username, 
                u.name, 
                u.role, 
                u.status,
                s.token,
                s.expires_at
            FROM user_sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.token = ? 
              AND s.expires_at > ? 
              AND u.status = 'active'
        """, (clean_token, now)) as cur:
            row = await cur.fetchone()
            if not row:
                return None

            user_data = dict(row)

        # Touch last seen timestamp
        try:
            await db.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE token = ?",
                (now, clean_token)
            )
            await db.commit()
        except Exception:
            pass

        return user_data


async def delete_user_session(token: str) -> bool:
    """
    Revoke a user session token.
    """
    if not token:
        return False
    async with get_db() as db:
        await db.execute("DELETE FROM user_sessions WHERE token = ?", (token.strip(),))
        await db.commit()
    return True


async def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    """
    Extract token from Authorization header, Cookie, or Query param, and validate session.
    Returns None if unauthenticated.
    """
    token: Optional[str] = None

    # 1. Bearer Token in Authorization Header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    # 2. Cookie 'bitmail_token'
    if not token:
        token = request.cookies.get("bitmail_token")

    # 3. Query parameter 'token' or 'auth_token'
    if not token:
        token = request.query_params.get("token") or request.query_params.get("auth_token")

    if not token:
        return None

    return await get_user_by_token(token)


async def get_current_user(request: Request) -> Dict[str, Any]:
    """
    FastAPI Dependency: Require authenticated user.
    Raises 401 Unauthorized if missing, expired, or invalid.
    """
    user = await get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"}
        )
    return user
