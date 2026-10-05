"""
SMTP Configuration Management and Live Diagnostic Probe API endpoints.
Provides profile management and comprehensive connection testing (DNS, TCP, TLS, Auth, Latency).
"""

import socket
import time
import uuid
from typing import Any, Dict, List, Optional

import aiosmtplib
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.auth import decrypt_credential, encrypt_credential
from app.db import get_db, utc_now_iso
from app.models import (
    SMTPConfigCreate,
    SMTPConfigResponse,
    SMTPConfigUpdate,
    SMTPTestRequest,
    SMTPTestResponse,
)

router = APIRouter(prefix="/api/smtp", tags=["SMTP Relay Configuration"])


@router.get("", response_model=List[SMTPConfigResponse])
async def list_smtp_configs():
    """
    List all configured SMTP server profiles (passwords masked).
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM smtp_configs ORDER BY is_default DESC, created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [
                SMTPConfigResponse(
                    id=r["id"],
                    name=r["name"],
                    host=r["host"],
                    port=r["port"],
                    username=r["username"],
                    use_tls=bool(r["use_tls"]),
                    use_ssl=bool(r["use_ssl"]),
                    rate_limit_per_second=r["rate_limit_per_second"] or 25,
                    daily_quota=r["daily_quota"] or 50000,
                    is_default=bool(r["is_default"]),
                    is_active=bool(r["is_active"]),
                    has_password=bool(r["password"]),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"]
                )
                for r in rows
            ]


@router.post("", response_model=SMTPConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_smtp_config(payload: SMTPConfigCreate):
    """
    Create a new SMTP profile.
    """
    smtp_id = f"smtp_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()

    async with get_db() as db:
        if payload.is_default:
            await db.execute("UPDATE smtp_configs SET is_default = 0")

        await db.execute("""
            INSERT INTO smtp_configs (
                id, name, host, port, username, password, use_tls, use_ssl,
                rate_limit_per_second, daily_quota, is_default, is_active,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            smtp_id,
            payload.name.strip(),
            payload.host.strip(),
            payload.port,
            payload.username,
            encrypt_credential(payload.password) if payload.password else "",
            1 if payload.use_tls else 0,
            1 if payload.use_ssl else 0,
            payload.rate_limit_per_second,
            payload.daily_quota,
            1 if payload.is_default else 0,
            1 if payload.is_active else 0,
            now,
            now
        ))
        await db.commit()

    return SMTPConfigResponse(
        id=smtp_id,
        name=payload.name.strip(),
        host=payload.host.strip(),
        port=payload.port,
        username=payload.username,
        use_tls=payload.use_tls,
        use_ssl=payload.use_ssl,
        rate_limit_per_second=payload.rate_limit_per_second,
        daily_quota=payload.daily_quota,
        is_default=payload.is_default,
        is_active=payload.is_active,
        has_password=bool(payload.password),
        created_at=now,
        updated_at=now
    )


# /test must precede /{smtp_id}
@router.post("/test", response_model=SMTPTestResponse)
async def test_smtp_connection(payload: SMTPTestRequest):
    """
    Run diagnostic connectivity probe against an SMTP server profile or ad-hoc parameters.
    Tests DNS resolution, TCP handshake, TLS/SSL negotiation, and authentication.
    """
    host = payload.host
    port = payload.port
    username = payload.username
    password = payload.password
    use_tls = payload.use_tls if payload.use_tls is not None else True
    use_ssl = payload.use_ssl if payload.use_ssl is not None else False

    if payload.smtp_config_id:
        async with get_db() as db:
            async with db.execute("SELECT * FROM smtp_configs WHERE id = ?", (payload.smtp_config_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    host = host or row["host"]
                    port = port or row["port"]
                    username = username or row["username"]
                    password = password or decrypt_credential(row["password"] or "")
                    if payload.use_tls is None:
                        use_tls = bool(row["use_tls"])
                    if payload.use_ssl is None:
                        use_ssl = bool(row["use_ssl"])

    host = host or "127.0.0.1"
    port = port or 587

    # 'sandbox' is the reserved dry-run relay: nothing to probe.
    if host.strip().lower() == "sandbox":
        return SMTPTestResponse(
            success=True,
            message="Sandbox relay selected. Messages are rendered and archived but never transmitted.",
            latency_ms=0.0,
            details={"host": host, "port": port, "sandbox": True, "steps": [
                {"step": "Sandbox Relay", "status": "passed", "latency_ms": 0.0,
                 "details": "Dry-run relay - no network transmission."}
            ]},
        )

    diagnostics: Dict[str, Any] = {
        "host": host,
        "port": port,
        "use_tls": use_tls,
        "use_ssl": use_ssl,
        "has_auth": bool(username and password),
        "steps": []
    }

    start_time = time.perf_counter()

    t0 = time.perf_counter()
    try:
        ip_addr = socket.gethostbyname(host)
        diagnostics["ip_address"] = ip_addr
        diagnostics["steps"].append({
            "step": "DNS Resolution",
            "status": "passed",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "details": f"Resolved {host} -> {ip_addr}"
        })
    except Exception as e:
        diagnostics["steps"].append({
            "step": "DNS Resolution",
            "status": "failed",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
            "error": str(e)
        })
        return SMTPTestResponse(
            success=False,
            message=f"DNS resolution failed for {host}: {e}",
            latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
            details=diagnostics
        )

    t1 = time.perf_counter()
    try:
        client = aiosmtplib.SMTP(
            hostname=host,
            port=port,
            use_tls=use_ssl,
            timeout=10
        )
        await client.connect()
        banner = str(getattr(client, "server_greeting", "220 Ready"))
        diagnostics["steps"].append({
            "step": "TCP & SMTP Handshake",
            "status": "passed",
            "latency_ms": round((time.perf_counter() - t1) * 1000, 2),
            "details": f"Connected to {host}:{port}. Banner: {banner[:80]}"
        })

        if use_tls and not use_ssl:
            t_tls = time.perf_counter()
            try:
                await client.starttls()
                diagnostics["steps"].append({
                    "step": "STARTTLS Negotiation",
                    "status": "passed",
                    "latency_ms": round((time.perf_counter() - t_tls) * 1000, 2),
                    "details": "TLS encryption established."
                })
            except Exception as tls_err:
                if "already using tls" in str(tls_err).lower():
                    diagnostics["steps"].append({
                        "step": "STARTTLS Negotiation",
                        "status": "passed",
                        "latency_ms": round((time.perf_counter() - t_tls) * 1000, 2),
                        "details": "TLS encryption already active on port."
                    })
                else:
                    diagnostics["steps"].append({
                        "step": "STARTTLS Negotiation",
                        "status": "warning",
                        "latency_ms": round((time.perf_counter() - t_tls) * 1000, 2),
                        "details": f"STARTTLS not accepted or skipped: {tls_err}"
                    })

        if username and password:
            t_auth = time.perf_counter()
            try:
                await client.login(username, password)
                diagnostics["steps"].append({
                    "step": "SMTP Authentication",
                    "status": "passed",
                    "latency_ms": round((time.perf_counter() - t_auth) * 1000, 2),
                    "details": f"Authenticated as '{username}'"
                })
            except Exception as auth_err:
                error_desc = str(auth_err)
                if "gmail" in host.lower() or "535" in error_desc:
                    error_desc += " -> Note for Gmail: You must generate and use a 16-character Google App Password at https://myaccount.google.com/apppasswords"
                diagnostics["steps"].append({
                    "step": "SMTP Authentication",
                    "status": "failed",
                    "latency_ms": round((time.perf_counter() - t_auth) * 1000, 2),
                    "error": error_desc
                })
                await client.quit()
                return SMTPTestResponse(
                    success=False,
                    message=f"Authentication failed for user '{username}': {error_desc}",
                    latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                    details=diagnostics
                )

        await client.quit()
        total_latency = round((time.perf_counter() - start_time) * 1000, 2)

        return SMTPTestResponse(
            success=True,
            message=f"SMTP relay probe connected successfully to {host}:{port} ({total_latency}ms)",
            latency_ms=total_latency,
            details=diagnostics
        )

    except Exception as e:
        total_latency = round((time.perf_counter() - start_time) * 1000, 2)
        diagnostics["steps"].append({
            "step": "TCP Connection",
            "status": "failed",
            "latency_ms": total_latency,
            "error": str(e)
        })
        return SMTPTestResponse(
            success=False,
            message=f"SMTP Connection failed to {host}:{port} - {e}",
            latency_ms=total_latency,
            details=diagnostics
        )


class GmailConnectPayload(BaseModel):
    email: str
    app_password: str
    sender_name: Optional[str] = None
    is_default: bool = True


@router.post("/gmail-connect")
async def connect_gmail_account(payload: GmailConnectPayload):
    """
    Connect a Gmail or Google Workspace account using a 16-character App Password.
    Tests live connection against smtp.gmail.com:587 (STARTTLS) and saves as default relay.
    """
    clean_email = payload.email.strip().lower()
    clean_pass = payload.app_password.replace(" ", "").strip()

    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Please enter a valid Gmail or Google Workspace email address.")

    if len(clean_pass) < 8:
        raise HTTPException(
            status_code=400,
            detail="Please enter your 16-character Google App Password (generated at https://myaccount.google.com/apppasswords)."
        )

    # Probe live Gmail SMTP server
    try:
        client = aiosmtplib.SMTP(hostname="smtp.gmail.com", port=587, use_tls=False, timeout=12)
        await client.connect()
        try:
            await client.starttls()
        except Exception as tls_e:
            if "already using tls" not in str(tls_e).lower():
                raise tls_e
        await client.login(clean_email, clean_pass)
        await client.quit()
    except Exception as e:
        err_str = str(e)
        if "535" in err_str or "Username and Password not accepted" in err_str or "badcredentials" in err_str.lower():
            raise HTTPException(
                status_code=400,
                detail="Gmail authentication failed. You must use a 16-character Google App Password, not your standard Gmail password. Generate one in 30 seconds at: https://myaccount.google.com/apppasswords (Google Account > Security > 2-Step Verification > App Passwords)."
            )
        raise HTTPException(status_code=400, detail=f"Failed to connect to smtp.gmail.com:587: {err_str}")

    # Persist as SMTP Profile
    safe_name = clean_email.split('@')[0]
    smtp_id = f"smtp_gmail_{uuid.uuid4().hex[:8]}"
    now = utc_now_iso()
    profile_name = f"Gmail ({clean_email})"

    async with get_db() as db:
        if payload.is_default:
            await db.execute("UPDATE smtp_configs SET is_default = 0")

        await db.execute("""
            INSERT INTO smtp_configs (
                id, name, host, port, username, password, use_tls, use_ssl,
                rate_limit_per_second, daily_quota, is_default, is_active,
                created_at, updated_at
            ) VALUES (?, ?, 'smtp.gmail.com', 587, ?, ?, 1, 0, 10, 500, ?, 1, ?, ?)
        """, (
            smtp_id,
            profile_name,
            clean_email,
            encrypt_credential(clean_pass),
            1 if payload.is_default else 0,
            now,
            now
        ))
        await db.commit()

    return {
        "success": True,
        "smtp_id": smtp_id,
        "name": profile_name,
        "host": "smtp.gmail.com",
        "port": 587,
        "username": clean_email,
        "message": f"Successfully connected and verified Gmail account for {clean_email}!"
    }


class BrevoConnectPayload(BaseModel):
    login_email: str
    smtp_key: str
    sender_name: Optional[str] = None
    is_default: bool = True


@router.post("/brevo-connect")
async def connect_brevo_account(payload: BrevoConnectPayload):
    """
    Connect a free Brevo (Sendinblue) account using login email and SMTP Master Key.
    Tests live connection against smtp-relay.brevo.com:587 (STARTTLS) and saves as default relay.
    """
    clean_email = payload.login_email.strip().lower()
    clean_key = payload.smtp_key.strip()

    if not clean_email or "@" not in clean_email:
        raise HTTPException(status_code=400, detail="Please enter your Brevo account login email address.")

    if len(clean_key) < 10:
        raise HTTPException(status_code=400, detail="Please enter a valid Brevo SMTP Key (found under Brevo Settings > SMTP & API Keys).")

    # Probe live Brevo SMTP server
    try:
        client = aiosmtplib.SMTP(hostname="smtp-relay.brevo.com", port=587, use_tls=False, timeout=12)
        await client.connect()
        try:
            await client.starttls()
        except Exception as tls_e:
            if "already using tls" not in str(tls_e).lower():
                raise tls_e
        await client.login(clean_email, clean_key)
        await client.quit()
    except Exception as e:
        err_str = str(e)
        if "535" in err_str or "authentication failed" in err_str.lower():
            raise HTTPException(
                status_code=400,
                detail="Brevo authentication failed. Make sure you are using your Brevo login email and the SMTP Key (generated at: https://app.brevo.com/settings/keys/smtp), not an API v3 key."
            )
        raise HTTPException(status_code=400, detail=f"Failed to connect to smtp-relay.brevo.com:587: {err_str}")

    # Persist as active SMTP profile
    smtp_id = f"smtp_brevo_{uuid.uuid4().hex[:8]}"
    now = utc_now_iso()
    profile_name = f"Brevo Relay ({clean_email})"

    async with get_db() as db:
        if payload.is_default:
            await db.execute("UPDATE smtp_configs SET is_default = 0")

        await db.execute("""
            INSERT INTO smtp_configs (
                id, name, host, port, username, password, use_tls, use_ssl,
                rate_limit_per_second, daily_quota, is_default, is_active,
                created_at, updated_at
            ) VALUES (?, ?, 'smtp-relay.brevo.com', 587, ?, ?, 1, 0, 25, 300, ?, 1, ?, ?)
        """, (
            smtp_id,
            profile_name,
            clean_email,
            clean_key,
            1 if payload.is_default else 0,
            now,
            now
        ))
        await db.commit()

    return {
        "success": True,
        "smtp_id": smtp_id,
        "name": profile_name,
        "host": "smtp-relay.brevo.com",
        "port": 587,
        "username": clean_email,
        "message": f"Successfully connected Brevo Cloud Relay for {clean_email}! (300 free emails/day active)"
    }


@router.get("/{smtp_id}", response_model=SMTPConfigResponse)
async def get_smtp_config(smtp_id: str):
    """
    Get a single SMTP profile by ID.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM smtp_configs WHERE id = ?", (smtp_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="SMTP configuration not found")

            return SMTPConfigResponse(
                id=row["id"],
                name=row["name"],
                host=row["host"],
                port=row["port"],
                username=row["username"],
                use_tls=bool(row["use_tls"]),
                use_ssl=bool(row["use_ssl"]),
                rate_limit_per_second=row["rate_limit_per_second"] or 25,
                daily_quota=row["daily_quota"] or 50000,
                is_default=bool(row["is_default"]),
                is_active=bool(row["is_active"]),
                has_password=bool(row["password"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )


@router.put("/{smtp_id}", response_model=SMTPConfigResponse)
async def update_smtp_config(smtp_id: str, payload: SMTPConfigUpdate):
    """
    Update an existing SMTP profile.
    """
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("SELECT * FROM smtp_configs WHERE id = ?", (smtp_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="SMTP configuration not found")
            cfg = dict(row)

        new_name = payload.name.strip() if payload.name is not None else cfg["name"]
        new_host = payload.host.strip() if payload.host is not None else cfg["host"]
        new_port = payload.port if payload.port is not None else cfg["port"]
        new_user = payload.username if payload.username is not None else cfg["username"]
        new_pass = encrypt_credential(payload.password) if (payload.password is not None and payload.password != "") else (payload.password if payload.password == "" else cfg["password"])
        new_tls = (1 if payload.use_tls else 0) if payload.use_tls is not None else cfg["use_tls"]
        new_ssl = (1 if payload.use_ssl else 0) if payload.use_ssl is not None else cfg["use_ssl"]
        new_rate = payload.rate_limit_per_second if payload.rate_limit_per_second is not None else cfg["rate_limit_per_second"]
        new_quota = payload.daily_quota if payload.daily_quota is not None else cfg["daily_quota"]
        new_def = (1 if payload.is_default else 0) if payload.is_default is not None else cfg["is_default"]
        new_act = (1 if payload.is_active else 0) if payload.is_active is not None else cfg["is_active"]

        if payload.is_default:
            await db.execute("UPDATE smtp_configs SET is_default = 0 WHERE id != ?", (smtp_id,))

        await db.execute("""
            UPDATE smtp_configs
            SET name = ?, host = ?, port = ?, username = ?, password = ?,
                use_tls = ?, use_ssl = ?, rate_limit_per_second = ?, daily_quota = ?,
                is_default = ?, is_active = ?, updated_at = ?
            WHERE id = ?
        """, (
            new_name, new_host, new_port, new_user, new_pass,
            new_tls, new_ssl, new_rate, new_quota,
            new_def, new_act, now, smtp_id
        ))
        await db.commit()

        return SMTPConfigResponse(
            id=smtp_id,
            name=new_name,
            host=new_host,
            port=new_port,
            username=new_user,
            use_tls=bool(new_tls),
            use_ssl=bool(new_ssl),
            rate_limit_per_second=new_rate,
            daily_quota=new_quota,
            is_default=bool(new_def),
            is_active=bool(new_act),
            has_password=bool(new_pass),
            created_at=cfg["created_at"],
            updated_at=now
        )


@router.delete("/{smtp_id}")
async def delete_smtp_config(smtp_id: str):
    """
    Delete an SMTP server profile.
    """
    async with get_db() as db:
        async with db.execute("SELECT id FROM smtp_configs WHERE id = ?", (smtp_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="SMTP configuration not found")

        await db.execute("DELETE FROM smtp_configs WHERE id = ?", (smtp_id,))
        await db.commit()

    return {"success": True, "message": f"SMTP profile {smtp_id} deleted."}
