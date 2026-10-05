"""
Template Studio API endpoints for creating, editing, previewing, and cloning responsive email templates.
"""

import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status

from app.config import settings
from app.db import get_db, utc_now_iso
from app.models import (
    TemplateCreate,
    TemplatePreviewRequest,
    TemplatePreviewResponse,
    TemplateResponse,
    TemplateUpdate,
)
from app.sender import interpolate_template

router = APIRouter(prefix="/api/templates", tags=["Template Studio"])


@router.get("", response_model=List[TemplateResponse])
async def list_templates():
    """
    List all saved email templates.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM templates ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [
                TemplateResponse(
                    id=r["id"],
                    name=r["name"],
                    description=r["description"],
                    subject=r["subject"],
                    body_html=r["body_html"],
                    body_text=r["body_text"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"]
                )
                for r in rows
            ]


@router.post("", response_model=TemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_template(payload: TemplateCreate):
    """
    Create a new email template.
    """
    tpl_id = f"tpl_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()

    async with get_db() as db:
        await db.execute("""
            INSERT INTO templates (id, name, description, subject, body_html, body_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tpl_id,
            payload.name.strip(),
            payload.description,
            payload.subject,
            payload.body_html,
            payload.body_text,
            now,
            now
        ))
        await db.commit()

    return TemplateResponse(
        id=tpl_id,
        name=payload.name.strip(),
        description=payload.description,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        created_at=now,
        updated_at=now
    )


@router.get("/{template_id}", response_model=TemplateResponse)
async def get_template(template_id: str):
    """
    Retrieve a specific email template by ID.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")

            return TemplateResponse(
                id=row["id"],
                name=row["name"],
                description=row["description"],
                subject=row["subject"],
                body_html=row["body_html"],
                body_text=row["body_text"],
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )


@router.put("/{template_id}", response_model=TemplateResponse)
async def update_template(template_id: str, payload: TemplateUpdate):
    """
    Update an existing email template.
    """
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            tpl_dict = dict(row)

        new_name = payload.name.strip() if payload.name is not None else tpl_dict["name"]
        new_desc = payload.description if payload.description is not None else tpl_dict["description"]
        new_subject = payload.subject if payload.subject is not None else tpl_dict["subject"]
        new_html = payload.body_html if payload.body_html is not None else tpl_dict["body_html"]
        new_text = payload.body_text if payload.body_text is not None else tpl_dict["body_text"]

        await db.execute("""
            UPDATE templates
            SET name = ?, description = ?, subject = ?, body_html = ?, body_text = ?, updated_at = ?
            WHERE id = ?
        """, (new_name, new_desc, new_subject, new_html, new_text, now, template_id))
        await db.commit()

        return TemplateResponse(
            id=template_id,
            name=new_name,
            description=new_desc,
            subject=new_subject,
            body_html=new_html,
            body_text=new_text,
            created_at=tpl_dict["created_at"],
            updated_at=now
        )


@router.delete("/{template_id}")
async def delete_template(template_id: str):
    """
    Delete a template.
    """
    async with get_db() as db:
        async with db.execute("SELECT id FROM templates WHERE id = ?", (template_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")

        await db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        await db.commit()

    return {"success": True, "message": f"Template {template_id} deleted successfully."}


@router.post("/preview", response_model=TemplatePreviewResponse)
async def preview_template(payload: TemplatePreviewRequest):
    """
    Render and preview a template or raw HTML with sample merge variables.
    """
    subject_raw = payload.subject or ""
    html_raw = payload.body_html or ""
    text_raw = payload.body_text or ""

    if payload.template_id:
        async with get_db() as db:
            async with db.execute("SELECT * FROM templates WHERE id = ?", (payload.template_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    subject_raw = subject_raw or row["subject"]
                    html_raw = html_raw or row["body_html"]
                    text_raw = text_raw or row["body_text"] or ""

    # Sample default variables
    sample_vars: Dict[str, Any] = {
        "first_name": "Alexandra",
        "last_name": "Chen",
        "name": "Alexandra Chen",
        "email": "alexandra.chen@bitnade.com",
        "company": "Bitnade Technologies",
        "plan": "Enterprise Pro",
        "custom_token": "tk_live_sample99812",
        "unsubscribe_url": f"{settings.TRACKING_BASE_URL.rstrip('/')}/unsubscribe/sample_preview_token",
        "company_name": settings.COMPANY_NAME,
        "company_address": settings.COMPANY_ADDRESS,
        "year": 2026,
        "date": "2026-10-05"
    }

    # Automatically enrich with custom placeholders discovered in subscribers table
    async with get_db() as db:
        async with db.execute("SELECT custom_fields FROM subscribers WHERE custom_fields IS NOT NULL AND custom_fields != '' AND custom_fields != '{}' LIMIT 10") as cur:
            for srow in await cur.fetchall():
                try:
                    cf_data = json.loads(srow["custom_fields"])
                    if isinstance(cf_data, dict):
                        for k, v in cf_data.items():
                            sample_vars.setdefault(k, v)
                            sample_vars.setdefault(k.lower(), v)
                except Exception:
                    pass

    sample_vars.update(payload.merge_variables)

    rendered_subject = interpolate_template(subject_raw, sample_vars)
    rendered_html = interpolate_template(html_raw, sample_vars)
    rendered_text = interpolate_template(text_raw, sample_vars)

    import re
    detected = sorted(list(set(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", f"{subject_raw} {html_raw} {text_raw}"))))

    return TemplatePreviewResponse(
        rendered_subject=rendered_subject,
        rendered_body_html=rendered_html,
        rendered_body_text=rendered_text,
        rendered_body=rendered_html,
        detected_tags=detected
    )



@router.post("/{template_id}/clone", response_model=TemplateResponse)
async def clone_template(template_id: str):
    """
    Duplicate an existing template.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM templates WHERE id = ?", (template_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            source = dict(row)

        new_id = f"tpl_{uuid.uuid4().hex[:10]}"
        now = utc_now_iso()
        new_name = f"{source['name']} (Copy)"

        await db.execute("""
            INSERT INTO templates (id, name, description, subject, body_html, body_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            new_id,
            new_name,
            source["description"],
            source["subject"],
            source["body_html"],
            source["body_text"],
            now,
            now
        ))
        await db.commit()

        return TemplateResponse(
            id=new_id,
            name=new_name,
            description=source["description"],
            subject=source["subject"],
            body_html=source["body_html"],
            body_text=source["body_text"],
            created_at=now,
            updated_at=now
        )
