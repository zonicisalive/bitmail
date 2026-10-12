"""
User Authentication and Credential Management Routes for Bitmail.
Provides endpoints for login, logout, current user session status,
password updates, and administrative user account management.
"""

import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth import (
    create_user_session,
    delete_user_session,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from app.db import TENANT_TABLES, get_db, utc_now_iso

router = APIRouter(prefix="/api/auth", tags=["User Authentication"])


# ======================================================================
# Pydantic Schemas
# ======================================================================

class LoginRequest(BaseModel):
    login: str = Field(..., description="Email address or username")
    password: str = Field(..., description="Account password")
    remember_me: Optional[bool] = Field(default=True, description="Persist 30-day session")


class UserProfileResponse(BaseModel):
    id: str
    email: str
    username: Optional[str] = None
    name: Optional[str] = None
    role: str
    status: str


class LoginResponse(BaseModel):
    success: bool
    token: str
    user: UserProfileResponse
    message: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., description="Existing account password")
    new_password: str = Field(..., min_length=6, description="New account password (min 6 chars)")


class CreateUserRequest(BaseModel):
    email: str = Field(..., description="User email address")
    username: Optional[str] = Field(default=None, description="Unique username")
    password: str = Field(..., min_length=6, description="User password")
    name: Optional[str] = Field(default=None, description="Full name")
    role: Literal["admin", "user"] = Field(default="user", description="Role: admin or user")


class UpdateUserRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[Literal["admin", "user"]] = None
    status: Optional[Literal["active", "disabled"]] = None
    password: Optional[str] = Field(default=None, min_length=6, description="Set a new password")


class AdminUserResponse(UserProfileResponse):
    created_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    contacts: int = 0
    campaigns: int = 0
    emails_sent: int = 0
    relays: int = 0


# ======================================================================
# API Endpoints
# ======================================================================

@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response):
    """
    Authenticate user via email or username and password.
    Returns Bearer auth token and sets HTTP cookie.
    """
    clean_login = payload.login.strip().lower()
    clean_pass = payload.password

    if not clean_login or not clean_pass:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide both login and password."
        )

    async with get_db() as db:
        async with db.execute("""
            SELECT id, email, username, password_hash, name, role, status
            FROM users
            WHERE (LOWER(email) = ? OR LOWER(username) = ?)
        """, (clean_login, clean_login)) as cur:
            row = await cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email/username or password."
        )

    user = dict(row)

    if user["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This user account has been disabled."
        )

    if not verify_password(clean_pass, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email/username or password."
        )

    client_ip = request.client.host if request.client else "127.0.0.1"
    user_agent = request.headers.get("user-agent", "Browser")
    ttl_days = 30 if payload.remember_me else 1

    token = await create_user_session(
        user_id=user["id"],
        ip_address=client_ip,
        user_agent=user_agent,
        days_valid=ttl_days
    )

    # Set cookie for browser sessions / asset downloads
    max_age_sec = ttl_days * 86400
    response.set_cookie(
        key="bitmail_token",
        value=token,
        max_age=max_age_sec,
        path="/",
        samesite="lax",
        secure=(request.url.scheme == "https"),
        httponly=False  # Accessible to client JS for unified token synchronization
    )

    user_profile = UserProfileResponse(
        id=user["id"],
        email=user["email"],
        username=user.get("username"),
        name=user.get("name") or user.get("username") or user["email"],
        role=user.get("role", "user"),
        status=user.get("status", "active")
    )

    return LoginResponse(
        success=True,
        token=token,
        user=user_profile,
        message=f"Welcome back, {user_profile.name}!"
    )


@router.post("/logout")
async def logout(request: Request, response: Response):
    """
    Log out active user, revoke session token, and clear auth cookie.
    """
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    if not token:
        token = request.cookies.get("bitmail_token")

    if token:
        await delete_user_session(token)

    response.delete_cookie("bitmail_token", path="/")
    return {"success": True, "message": "Successfully logged out."}


@router.get("/me", response_model=UserProfileResponse)
async def get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Get profile details for the currently logged-in user.
    """
    return UserProfileResponse(
        id=current_user["id"],
        email=current_user["email"],
        username=current_user.get("username"),
        name=current_user.get("name") or current_user.get("username") or current_user["email"],
        role=current_user.get("role", "user"),
        status=current_user.get("status", "active")
    )


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Change account password for current user.
    """
    async with get_db() as db:
        async with db.execute("SELECT password_hash FROM users WHERE id = ?", (current_user["id"],)) as cur:
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="User account not found.")

        stored_hash = row["password_hash"]
        if not verify_password(payload.current_password, stored_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect.")

        new_hash = hash_password(payload.new_password)
        now = utc_now_iso()
        await db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (new_hash, now, current_user["id"]))
        await db.commit()

    return {"success": True, "message": "Password changed successfully."}


# ======================================================================
# Admin: user management
# ======================================================================

_USER_STATS_SQL = """
    SELECT u.id, u.email, u.username, u.name, u.role, u.status, u.created_at,
        (SELECT MAX(last_seen_at) FROM user_sessions WHERE user_id = u.id) AS last_seen_at,
        (SELECT COUNT(*) FROM subscribers WHERE user_id = u.id) AS contacts,
        (SELECT COUNT(*) FROM campaigns WHERE user_id = u.id AND COALESCE(is_warmup, 0) = 0) AS campaigns,
        (SELECT COUNT(*) FROM sent_emails WHERE user_id = u.id AND status IN ('sent', 'delivered')) AS emails_sent,
        (SELECT COUNT(*) FROM smtp_configs WHERE user_id = u.id) AS relays
    FROM users u
"""


async def _get_admin_user(db, user_id: str) -> AdminUserResponse:
    async with db.execute(_USER_STATS_SQL + " WHERE u.id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return AdminUserResponse(**dict(row))


async def _active_admin_count(db, excluding: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active' AND id != ?", (excluding,)
    ) as cur:
        return (await cur.fetchone())[0]


@router.get("/users", response_model=List[AdminUserResponse])
async def list_users(_: Dict[str, Any] = Depends(require_admin)):
    """List every account with a summary of what it owns (admin only)."""
    async with get_db() as db:
        async with db.execute(_USER_STATS_SQL + " ORDER BY u.created_at ASC") as cur:
            rows = await cur.fetchall()
    return [AdminUserResponse(**dict(r)) for r in rows]


@router.post("/users", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(payload: CreateUserRequest, _: Dict[str, Any] = Depends(require_admin)):
    """Create an account (admin only). New accounts start empty: no contacts, relays or campaigns."""
    clean_email = payload.email.strip().lower()
    if "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    clean_username = (payload.username.strip().lower() if payload.username else clean_email.split("@")[0])
    new_id = f"usr_{uuid.uuid4().hex[:12]}"
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute(
            "SELECT id FROM users WHERE LOWER(email) = ? OR LOWER(username) = ?", (clean_email, clean_username)
        ) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="A user with this email or username already exists.")
        await db.execute("""
            INSERT INTO users (id, email, username, password_hash, name, role, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (new_id, clean_email, clean_username, hash_password(payload.password),
              (payload.name or "").strip() or clean_username.capitalize(), payload.role, now, now))
        await db.commit()
        return await _get_admin_user(db, new_id)


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
async def update_user(user_id: str, payload: UpdateUserRequest, admin: Dict[str, Any] = Depends(require_admin)):
    """Rename, change role, enable/disable, or reset the password of an account (admin only)."""
    changes = payload.model_dump(exclude_unset=True)
    demoting = changes.get("role") == "user" or changes.get("status") == "disabled"

    async with get_db() as db:
        target = await _get_admin_user(db, user_id)
        if demoting and target.role == "admin" and await _active_admin_count(db, user_id) == 0:
            raise HTTPException(status_code=400, detail="At least one active administrator must remain.")
        if user_id == admin["id"] and changes.get("status") == "disabled":
            raise HTTPException(status_code=400, detail="You cannot disable your own account.")

        sets, params = [], []
        if changes.get("name") is not None:
            sets.append("name = ?"); params.append(changes["name"].strip() or target.name)
        if changes.get("email") is not None:
            email = changes["email"].strip().lower()
            if "@" not in email:
                raise HTTPException(status_code=400, detail="Enter a valid email address.")
            async with db.execute("SELECT 1 FROM users WHERE LOWER(email) = ? AND id != ?", (email, user_id)) as cur:
                if await cur.fetchone():
                    raise HTTPException(status_code=400, detail="Another user already has this email.")
            sets.append("email = ?"); params.append(email)
        for field in ("role", "status"):
            if changes.get(field):
                sets.append(f"{field} = ?"); params.append(changes[field])
        if changes.get("password"):
            sets.append("password_hash = ?"); params.append(hash_password(changes["password"]))

        if sets:
            await db.execute(
                f"UPDATE users SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*params, utc_now_iso(), user_id)
            )
            # Disabling or a password reset signs the account out everywhere.
            if changes.get("status") == "disabled" or changes.get("password"):
                await db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            await db.commit()
        return await _get_admin_user(db, user_id)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: Dict[str, Any] = Depends(require_admin)):
    """Delete an account and everything it owns (admin only)."""
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    async with get_db() as db:
        target = await _get_admin_user(db, user_id)
        if target.role == "admin" and await _active_admin_count(db, user_id) == 0:
            raise HTTPException(status_code=400, detail="At least one active administrator must remain.")
        await db.execute(
            "DELETE FROM relay_pool_stats WHERE smtp_config_id IN (SELECT id FROM smtp_configs WHERE user_id = ?)",
            (user_id,),
        )
        # Child rows (memberships, events, recipients, slices, deliveries) cascade.
        # ponytail: archived .eml files stay on disk; sweep them if storage matters.
        for table in TENANT_TABLES:
            await db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
    return {"success": True, "message": f"Deleted {target.email} and all of their data."}
