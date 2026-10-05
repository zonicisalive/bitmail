"""
User Authentication and Credential Management Routes for Bitmail.
Provides endpoints for login, logout, current user session status,
password updates, and administrative user account management.
"""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth import (
    create_user_session,
    delete_user_session,
    get_current_user,
    hash_password,
    verify_password,
)
from app.db import get_db, utc_now_iso

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
    role: Optional[str] = Field(default="admin", description="Role: admin or member")


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
        role=user.get("role", "admin"),
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
        role=current_user.get("role", "admin"),
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


@router.get("/users", response_model=List[UserProfileResponse])
async def list_users(current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    List all registered user accounts (admin only).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    async with get_db() as db:
        async with db.execute("SELECT id, email, username, name, role, status FROM users ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()

    return [UserProfileResponse(**dict(r)) for r in rows]


@router.post("/users", response_model=UserProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: CreateUserRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Create a new user account (admin only).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")

    clean_email = payload.email.strip().lower()
    clean_username = (payload.username.strip().lower() if payload.username else clean_email.split("@")[0])
    pwd_hash = hash_password(payload.password)
    new_id = f"usr_{uuid.uuid4().hex[:12]}"
    now = utc_now_iso()

    async with get_db() as db:
        # Check existing
        async with db.execute("SELECT id FROM users WHERE LOWER(email) = ? OR LOWER(username) = ?", (clean_email, clean_username)) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=400, detail="User with this email or username already exists.")

        await db.execute("""
            INSERT INTO users (id, email, username, password_hash, name, role, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (new_id, clean_email, clean_username, pwd_hash, payload.name or clean_username.capitalize(), payload.role or "admin", now, now))
        await db.commit()

    return UserProfileResponse(
        id=new_id,
        email=clean_email,
        username=clean_username,
        name=payload.name or clean_username.capitalize(),
        role=payload.role or "admin",
        status="active"
    )
