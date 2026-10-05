"""
Subscriber and List Management API endpoints.
Provides CRUD, search, list memberships, bulk CSV import with column mapping, and CSV export.
"""

import csv
import io
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.db import get_db, utc_now_iso
from app.models import (
    SubscriberBulkImportRequest,
    SubscriberBulkImportResponse,
    SubscriberCreate,
    SubscriberListCreate,
    SubscriberListDetail,
    SubscriberListResponse,
    SubscriberListUpdate,
    SubscriberResponse,
    SubscriberStatus,
    SubscriberUpdate,
)

router = APIRouter(tags=["Subscribers & Lists"])


# ======================================================================
# Subscriber Endpoints (/api/subscribers)
# ======================================================================

@router.get("/api/subscribers", response_model=List[SubscriberResponse])
async def list_subscribers(
    search: Optional[str] = Query(default=None, description="Search keyword in email or name"),
    list_id: Optional[str] = Query(default=None, description="Filter by list ID"),
    status: Optional[str] = Query(default=None, description="Filter by status (active, unsubscribed, bounced)"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    page: Optional[int] = Query(default=None, ge=1),
    per_page: Optional[int] = Query(default=None, ge=1, le=1000)
):
    """
    List subscribers with optional search, list filtering, status filtering, and pagination.
    """
    if page and per_page:
        limit = per_page
        offset = (page - 1) * per_page

    async with get_db() as db:
        query = """
            SELECT DISTINCT s.*
            FROM subscribers s
            LEFT JOIN subscriber_list_memberships m ON s.id = m.subscriber_id
            WHERE 1=1
        """
        params: List[Any] = []

        if search:
            query += " AND (s.email LIKE ? OR s.first_name LIKE ? OR s.last_name LIKE ?)"
            term = f"%{search}%"
            params.extend([term, term, term])

        if list_id:
            query += " AND (m.list_id = ? OR s.id IN (SELECT subscriber_id FROM list_subscribers WHERE list_id = ?))"
            params.extend([list_id, list_id])

        if status:
            query += " AND s.status = ?"
            params.append(status.lower())

        query += " ORDER BY s.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            results: List[SubscriberResponse] = []

            for r in rows:
                sub_dict = dict(r)
                custom_fields = {}
                try:
                    if sub_dict.get("custom_fields"):
                        custom_fields = json.loads(sub_dict["custom_fields"])
                except Exception:
                    pass

                sub_id = sub_dict["id"]
                list_ids: List[str] = []
                async with db.execute(
                    "SELECT list_id FROM subscriber_list_memberships WHERE subscriber_id = ? UNION SELECT list_id FROM list_subscribers WHERE subscriber_id = ?",
                    (sub_id, sub_id)
                ) as list_cur:
                    l_rows = await list_cur.fetchall()
                    list_ids = [lr[0] for lr in l_rows]

                results.append(
                    SubscriberResponse(
                        id=sub_dict["id"],
                        email=sub_dict["email"],
                        first_name=sub_dict.get("first_name"),
                        last_name=sub_dict.get("last_name"),
                        custom_fields=custom_fields,
                        status=SubscriberStatus(sub_dict.get("status", "active")),
                        created_at=sub_dict["created_at"],
                        updated_at=sub_dict["updated_at"],
                        lists=list_ids
                    )
                )

            return results


@router.post("/api/subscribers", response_model=SubscriberResponse, status_code=status.HTTP_201_CREATED)
async def create_subscriber(payload: SubscriberCreate):
    """
    Create a new subscriber and optionally associate them with lists.
    """
    email_clean = payload.email.strip().lower()
    now = utc_now_iso()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"

    async with get_db() as db:
        async with db.execute("SELECT id FROM subscribers WHERE email = ?", (email_clean,)) as cursor:
            existing = await cursor.fetchone()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Subscriber with email '{email_clean}' already exists."
                )

        custom_fields_json = json.dumps(payload.custom_fields)

        await db.execute("""
            INSERT INTO subscribers (id, email, first_name, last_name, custom_fields, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sub_id,
            email_clean,
            payload.first_name,
            payload.last_name,
            custom_fields_json,
            payload.status.value,
            now,
            now
        ))

        if payload.status == SubscriberStatus.ACTIVE:
            await db.execute("DELETE FROM suppressions WHERE email = ?", (email_clean,))
            await db.execute("DELETE FROM suppression_list WHERE email = ?", (email_clean,))

        lists_added: List[str] = []
        if payload.list_ids:
            for l_id in payload.list_ids:
                await db.execute("""
                    INSERT OR IGNORE INTO subscriber_list_memberships (subscriber_id, list_id, added_at)
                    VALUES (?, ?, ?)
                """, (sub_id, l_id, now))
                await db.execute("""
                    INSERT OR IGNORE INTO list_subscribers (list_id, subscriber_id, status, subscribed_at)
                    VALUES (?, ?, 'active', ?)
                """, (l_id, sub_id, now))
                lists_added.append(l_id)

        await db.commit()

        return SubscriberResponse(
            id=sub_id,
            email=email_clean,
            first_name=payload.first_name,
            last_name=payload.last_name,
            custom_fields=payload.custom_fields,
            status=payload.status,
            created_at=now,
            updated_at=now,
            lists=lists_added
        )


# Static subpaths (export, import-csv) must come BEFORE /{subscriber_id}
@router.get("/api/subscribers/export")
async def export_subscribers_csv(
    list_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None)
):
    """
    Export subscribers to CSV format.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "email", "first_name", "last_name", "status", "custom_fields", "created_at"])

    async with get_db() as db:
        query = """
            SELECT DISTINCT s.*
            FROM subscribers s
            LEFT JOIN subscriber_list_memberships m ON s.id = m.subscriber_id
            WHERE 1=1
        """
        params: List[Any] = []

        if list_id:
            query += " AND (m.list_id = ? OR s.id IN (SELECT subscriber_id FROM list_subscribers WHERE list_id = ?))"
            params.extend([list_id, list_id])

        if status:
            query += " AND s.status = ?"
            params.append(status.lower())

        query += " ORDER BY s.created_at DESC"

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                writer.writerow([
                    r["id"],
                    r["email"],
                    r["first_name"] or "",
                    r["last_name"] or "",
                    r["status"],
                    r["custom_fields"] or "{}",
                    r["created_at"]
                ])

    output.seek(0)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscribers_export.csv"}
    )


@router.post("/api/subscribers/import-csv", response_model=SubscriberBulkImportResponse)
async def import_subscribers_csv(
    file: Optional[UploadFile] = File(default=None),
    list_id: Optional[str] = Form(default=None),
    new_list_name: Optional[str] = Form(default=None),
    column_mapping: Optional[str] = Form(default=None),
    update_duplicates: bool = Form(default=True)
):
    """
    Upload and parse CSV file, map columns to subscriber fields,
    insert or update records, and attach to list or create a new table on the fly.
    """
    if not file:
        raise HTTPException(status_code=400, detail="No CSV file uploaded.")

    content = await file.read()
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text_content))
    fieldnames = reader.fieldnames or []

    mapping: Dict[str, str] = {}
    if column_mapping:
        try:
            mapping = json.loads(column_mapping)
        except Exception:
            pass

    email_col = mapping.get("email")
    first_name_col = mapping.get("first_name")
    last_name_col = mapping.get("last_name")

    if not email_col:
        for f in fieldnames:
            if "email" in f.lower() or "mail" in f.lower():
                email_col = f
                break

    if not first_name_col:
        for f in fieldnames:
            f_low = f.lower()
            if "first" in f_low or "fname" in f_low or f_low == "name":
                first_name_col = f
                break

    if not last_name_col:
        for f in fieldnames:
            f_low = f.lower()
            if "last" in f_low or "lname" in f_low or "surname" in f_low:
                last_name_col = f
                break

    if not email_col and fieldnames:
        email_col = fieldnames[0]

    target_list_id = list_id
    created_list_name = None
    if new_list_name and new_list_name.strip():
        created_list_name = new_list_name.strip()
        target_list_id = f"list_{uuid.uuid4().hex[:10]}"

    added_count = 0
    updated_count = 0
    failed_count = 0
    errors: List[Dict[str, Any]] = []
    detected_custom_fields: set = set()
    now = utc_now_iso()

    async with get_db() as db:
        if created_list_name:
            filename_str = file.filename or 'CSV' if file else 'CSV'
            await db.execute("""
                INSERT INTO subscriber_lists (id, name, description, schema_fields, created_at, updated_at)
                VALUES (?, ?, ?, '[]', ?, ?)
            """, (
                target_list_id,
                created_list_name,
                f"Imported from {filename_str}",
                now,
                now
            ))

        for row_idx, row in enumerate(reader, start=2):
            raw_email = row.get(email_col, "").strip().lower() if email_col else ""
            if not raw_email or "@" not in raw_email:
                failed_count += 1
                errors.append({"row": row_idx, "error": f"Invalid email value: '{raw_email}'"})
                continue

            first_name = row.get(first_name_col, "").strip() if first_name_col else None
            last_name = row.get(last_name_col, "").strip() if last_name_col else None

            custom_fields = {}
            for col_k, col_v in row.items():
                if col_k not in [email_col, first_name_col, last_name_col] and col_v:
                    clean_k = re.sub(r'[^a-z0-9]+', '_', col_k.strip().lower()).strip('_')
                    if clean_k:
                        custom_fields[clean_k] = col_v.strip()
                        detected_custom_fields.add(clean_k)

            async with db.execute("SELECT id, custom_fields FROM subscribers WHERE email = ?", (raw_email,)) as cursor:
                existing = await cursor.fetchone()

            if existing:
                sub_id = existing["id"]
                if update_duplicates:
                    old_custom = {}
                    try:
                        if existing["custom_fields"]:
                            old_custom = json.loads(existing["custom_fields"])
                    except Exception:
                        pass
                    old_custom.update(custom_fields)

                    await db.execute("""
                        UPDATE subscribers
                        SET first_name = COALESCE(?, first_name),
                            last_name = COALESCE(?, last_name),
                            custom_fields = ?,
                            updated_at = ?
                        WHERE id = ?
                    """, (first_name, last_name, json.dumps(old_custom), now, sub_id))
                    updated_count += 1
            else:
                sub_id = f"sub_{uuid.uuid4().hex[:10]}"
                await db.execute("""
                    INSERT INTO subscribers (id, email, first_name, last_name, custom_fields, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """, (sub_id, raw_email, first_name, last_name, json.dumps(custom_fields), now, now))
                added_count += 1

            if target_list_id:
                await db.execute("""
                    INSERT OR IGNORE INTO subscriber_list_memberships (subscriber_id, list_id, added_at)
                    VALUES (?, ?, ?)
                """, (sub_id, target_list_id, now))
                await db.execute("""
                    INSERT OR IGNORE INTO list_subscribers (list_id, subscriber_id, status, subscribed_at)
                    VALUES (?, ?, 'active', ?)
                """, (target_list_id, sub_id, now))

        if created_list_name:
            # Update the new list with detected_custom_fields as schema_fields
            await db.execute("""
                UPDATE subscriber_lists
                SET schema_fields = ?, updated_at = ?
                WHERE id = ?
            """, (
                json.dumps(sorted(list(detected_custom_fields))),
                now,
                target_list_id
            ))
        elif target_list_id and detected_custom_fields:
            # If importing into an existing list, merge any newly detected custom fields into schema_fields
            async with db.execute("SELECT schema_fields FROM subscriber_lists WHERE id = ?", (target_list_id,)) as cur:
                row_list = await cur.fetchone()
                if row_list:
                    existing_schema = []
                    try:
                        if row_list["schema_fields"]:
                            existing_schema = json.loads(row_list["schema_fields"])
                    except Exception:
                        pass
                    merged_schema = sorted(list(set(existing_schema).union(detected_custom_fields)))
                    if merged_schema != existing_schema:
                        await db.execute(
                            "UPDATE subscriber_lists SET schema_fields = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(merged_schema), now, target_list_id)
                        )

        await db.commit()

    total_received = added_count + updated_count + failed_count
    return SubscriberBulkImportResponse(
        total_received=total_received,
        added_count=added_count,
        updated_count=updated_count,
        failed_count=failed_count,
        errors=errors[:50],
        custom_fields_detected=sorted(list(detected_custom_fields)),
        list_id=target_list_id,
        list_name=created_list_name
    )


@router.get("/api/subscribers/placeholders")
async def get_available_placeholders(list_id: Optional[str] = Query(default=None, description="Optional customer table/list ID")):
    """
    Return all discovered merge tag placeholders across subscribers, custom attributes,
    and lists to support multi-table dynamic placeholder discovery.
    If list_id is provided, includes specific schema columns and subscriber attributes for that table.
    """
    standard_tags = ["first_name", "last_name", "email", "name", "company", "unsubscribe_url", "year", "date"]
    custom_tags = set()
    table_fields = []
    selected_list_name = None

    async with get_db() as db:
        if list_id and list_id.lower() != "all":
            # Load list schema
            async with db.execute("SELECT id, name, schema_fields FROM subscriber_lists WHERE id = ?", (list_id,)) as cur:
                l_row = await cur.fetchone()
                if l_row:
                    selected_list_name = l_row["name"]
                    try:
                        sf = json.loads(l_row["schema_fields"] or "[]")
                        if isinstance(sf, list):
                            for col in sf:
                                clean_c = re.sub(r'[^a-z0-9_]+', '_', str(col).strip().lower()).strip('_')
                                if clean_c:
                                    table_fields.append(clean_c)
                                    custom_tags.add(clean_c)
                    except Exception:
                        pass

            # Inspect subscribers belonging to this specific list
            sql = """
                SELECT DISTINCT s.custom_fields 
                FROM subscribers s
                LEFT JOIN subscriber_list_memberships m ON s.id = m.subscriber_id
                LEFT JOIN list_subscribers ls ON s.id = ls.subscriber_id
                WHERE (m.list_id = ? OR ls.list_id = ?)
                  AND s.custom_fields IS NOT NULL AND s.custom_fields != '' AND s.custom_fields != '{}'
            """
            params = [list_id, list_id]
        else:
            sql = "SELECT custom_fields FROM subscribers WHERE custom_fields IS NOT NULL AND custom_fields != '' AND custom_fields != '{}'"
            params = []

        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            for r in rows:
                try:
                    cf = json.loads(r["custom_fields"])
                    if isinstance(cf, dict):
                        for k in cf.keys():
                            if k:
                                clean_k = re.sub(r'[^a-z0-9_]+', '_', k.strip().lower()).strip('_')
                                if clean_k:
                                    custom_tags.add(clean_k)
                except Exception:
                    pass

        # Inspect all list names
        async with db.execute("SELECT id, name FROM subscriber_lists") as cur:
            list_rows = await cur.fetchall()
            list_names = [lr["name"] for lr in list_rows if lr["name"]]

    return {
        "success": True,
        "list_id": list_id,
        "list_name": selected_list_name,
        "standard_tags": standard_tags,
        "standard_placeholders": standard_tags,
        "table_placeholders": sorted(list(set(table_fields))),
        "custom_tags": sorted(list(custom_tags)),
        "custom_fields": sorted(list(custom_tags)),
        "list_names": list_names
    }



@router.get("/api/subscribers/{subscriber_id}", response_model=SubscriberResponse)
async def get_subscriber(subscriber_id: str):
    """
    Get a single subscriber by ID.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscriber not found")

            sub_dict = dict(row)
            custom_fields = {}
            try:
                if sub_dict.get("custom_fields"):
                    custom_fields = json.loads(sub_dict["custom_fields"])
            except Exception:
                pass

            async with db.execute(
                "SELECT list_id FROM subscriber_list_memberships WHERE subscriber_id = ? UNION SELECT list_id FROM list_subscribers WHERE subscriber_id = ?",
                (subscriber_id, subscriber_id)
            ) as list_cur:
                l_rows = await list_cur.fetchall()
                list_ids = [lr[0] for lr in l_rows]

            return SubscriberResponse(
                id=sub_dict["id"],
                email=sub_dict["email"],
                first_name=sub_dict.get("first_name"),
                last_name=sub_dict.get("last_name"),
                custom_fields=custom_fields,
                status=SubscriberStatus(sub_dict.get("status", "active")),
                created_at=sub_dict["created_at"],
                updated_at=sub_dict["updated_at"],
                lists=list_ids
            )


@router.put("/api/subscribers/{subscriber_id}", response_model=SubscriberResponse)
async def update_subscriber(subscriber_id: str, payload: SubscriberUpdate):
    """
    Update subscriber attributes.
    """
    now = utc_now_iso()

    async with get_db() as db:
        async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscriber not found")
            sub_dict = dict(row)

        new_email = payload.email.strip().lower() if payload.email is not None else sub_dict["email"]
        new_first_name = payload.first_name if payload.first_name is not None else sub_dict.get("first_name")
        new_last_name = payload.last_name if payload.last_name is not None else sub_dict.get("last_name")
        new_status = payload.status.value if payload.status is not None else sub_dict.get("status", "active")

        existing_custom = {}
        try:
            if sub_dict.get("custom_fields"):
                existing_custom = json.loads(sub_dict["custom_fields"])
        except Exception:
            pass

        if payload.custom_fields is not None:
            existing_custom.update(payload.custom_fields)

        await db.execute("""
            UPDATE subscribers
            SET email = ?, first_name = ?, last_name = ?, custom_fields = ?, status = ?, updated_at = ?
            WHERE id = ?
        """, (
            new_email,
            new_first_name,
            new_last_name,
            json.dumps(existing_custom),
            new_status,
            now,
            subscriber_id
        ))

        if new_status == "active":
            await db.execute("DELETE FROM suppressions WHERE email = ?", (new_email,))
            await db.execute("DELETE FROM suppression_list WHERE email = ?", (new_email,))

        if payload.list_ids is not None:
            await db.execute("DELETE FROM subscriber_list_memberships WHERE subscriber_id = ?", (subscriber_id,))
            await db.execute("DELETE FROM list_subscribers WHERE subscriber_id = ?", (subscriber_id,))
            for lid in payload.list_ids:
                await db.execute("""
                    INSERT OR IGNORE INTO subscriber_list_memberships (subscriber_id, list_id, added_at)
                    VALUES (?, ?, ?)
                """, (subscriber_id, lid, now))
                await db.execute("""
                    INSERT OR IGNORE INTO list_subscribers (list_id, subscriber_id, status, subscribed_at)
                    VALUES (?, ?, 'active', ?)
                """, (lid, subscriber_id, now))

        await db.commit()

        async with db.execute(
            "SELECT list_id FROM subscriber_list_memberships WHERE subscriber_id = ? UNION SELECT list_id FROM list_subscribers WHERE subscriber_id = ?",
            (subscriber_id, subscriber_id)
        ) as list_cur:
            l_rows = await list_cur.fetchall()
            list_ids = [lr[0] for lr in l_rows]


        return SubscriberResponse(
            id=subscriber_id,
            email=new_email,
            first_name=new_first_name,
            last_name=new_last_name,
            custom_fields=existing_custom,
            status=SubscriberStatus(new_status),
            created_at=sub_dict["created_at"],
            updated_at=now,
            lists=list_ids
        )


@router.delete("/api/subscribers/{subscriber_id}")
async def delete_subscriber(subscriber_id: str):
    """
    Delete a subscriber from the system and all list memberships.
    """
    async with get_db() as db:
        async with db.execute("SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscriber not found")

        await db.execute("DELETE FROM subscriber_list_memberships WHERE subscriber_id = ?", (subscriber_id,))
        await db.execute("DELETE FROM list_subscribers WHERE subscriber_id = ?", (subscriber_id,))
        await db.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
        await db.commit()

    return {"success": True, "message": f"Subscriber {subscriber_id} deleted successfully."}


# ======================================================================
# Subscriber List Endpoints (/api/lists)
# ======================================================================

@router.get("/api/lists", response_model=List[SubscriberListResponse])
@router.get("/api/subscribers/lists", response_model=List[SubscriberListResponse], include_in_schema=False)
async def list_subscriber_lists():
    """
    List all subscriber lists with live member counts and schema fields.
    """
    async with get_db() as db:
        query = """
            SELECT 
                l.id,
                l.name,
                l.description,
                l.schema_fields,
                l.created_at,
                l.updated_at,
                (SELECT COUNT(DISTINCT subscriber_id) FROM (
                    SELECT subscriber_id FROM subscriber_list_memberships WHERE list_id = l.id
                    UNION
                    SELECT subscriber_id FROM list_subscribers WHERE list_id = l.id
                )) as subscriber_count
            FROM subscriber_lists l
            ORDER BY l.created_at DESC
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            results = []
            for r in rows:
                sf = []
                try:
                    if r["schema_fields"]:
                        sf = json.loads(r["schema_fields"])
                        if not isinstance(sf, list):
                            sf = []
                except Exception:
                    sf = []
                results.append(
                    SubscriberListResponse(
                        id=r["id"],
                        name=r["name"],
                        description=r["description"],
                        schema_fields=sf,
                        subscriber_count=r["subscriber_count"] or 0,
                        created_at=r["created_at"],
                        updated_at=r["updated_at"]
                    )
                )
            return results


@router.post("/api/lists", response_model=SubscriberListResponse, status_code=status.HTTP_201_CREATED)
@router.post("/api/subscribers/lists", response_model=SubscriberListResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_subscriber_list(payload: SubscriberListCreate):
    """
    Create a new subscriber list / customer data table with custom schema columns.
    """
    list_id = f"list_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()

    clean_schema: List[str] = []
    if payload.schema_fields:
        for f in payload.schema_fields:
            cf = re.sub(r'[^a-z0-9_]+', '_', str(f).strip().lower()).strip('_')
            if cf and cf not in clean_schema:
                clean_schema.append(cf)

    async with get_db() as db:
        await db.execute("""
            INSERT INTO subscriber_lists (id, name, description, schema_fields, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (list_id, payload.name.strip(), payload.description, json.dumps(clean_schema), now, now))
        await db.commit()

    return SubscriberListResponse(
        id=list_id,
        name=payload.name.strip(),
        description=payload.description,
        schema_fields=clean_schema,
        subscriber_count=0,
        created_at=now,
        updated_at=now
    )


@router.get("/api/lists/{list_id}", response_model=SubscriberListDetail)
async def get_subscriber_list(list_id: str):
    """
    Get list details along with its member subscribers.
    """
    async with get_db() as db:
        async with db.execute("SELECT * FROM subscriber_lists WHERE id = ?", (list_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscriber list not found")
            list_dict = dict(row)

        schema_fields = []
        try:
            if list_dict.get("schema_fields"):
                schema_fields = json.loads(list_dict["schema_fields"])
                if not isinstance(schema_fields, list):
                    schema_fields = []
        except Exception:
            schema_fields = []

        query = """
            SELECT DISTINCT s.*
            FROM subscribers s
            WHERE s.id IN (
                SELECT subscriber_id FROM subscriber_list_memberships WHERE list_id = ?
                UNION
                SELECT subscriber_id FROM list_subscribers WHERE list_id = ?
            )
            ORDER BY s.created_at DESC
        """
        subs: List[SubscriberResponse] = []
        async with db.execute(query, (list_id, list_id)) as cursor:
            s_rows = await cursor.fetchall()
            for sr in s_rows:
                custom_fields = {}
                try:
                    if sr["custom_fields"]:
                        custom_fields = json.loads(sr["custom_fields"])
                except Exception:
                    pass
                subs.append(
                    SubscriberResponse(
                        id=sr["id"],
                        email=sr["email"],
                        first_name=sr["first_name"],
                        last_name=sr["last_name"],
                        custom_fields=custom_fields,
                        status=SubscriberStatus(sr["status"]),
                        created_at=sr["created_at"],
                        updated_at=sr["updated_at"],
                        lists=[list_id]
                    )
                )

        return SubscriberListDetail(
            id=list_dict["id"],
            name=list_dict["name"],
            description=list_dict["description"],
            schema_fields=schema_fields,
            subscriber_count=len(subs),
            created_at=list_dict["created_at"],
            updated_at=list_dict["updated_at"],
            subscribers=subs
        )


@router.put("/api/lists/{list_id}", response_model=SubscriberListResponse)
@router.put("/api/subscribers/lists/{list_id}", response_model=SubscriberListResponse, include_in_schema=False)
async def update_subscriber_list(list_id: str, payload: SubscriberListUpdate):
    """
    Update subscriber list name, description, or schema fields.
    """
    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT * FROM subscriber_lists WHERE id = ?", (list_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscriber list not found")
            list_dict = dict(row)

        new_name = payload.name.strip() if payload.name is not None else list_dict["name"]
        new_desc = payload.description if payload.description is not None else list_dict["description"]
        
        current_schema = []
        try:
            if list_dict.get("schema_fields"):
                current_schema = json.loads(list_dict["schema_fields"])
        except Exception:
            pass

        if payload.schema_fields is not None:
            clean_schema = []
            for f in payload.schema_fields:
                cf = re.sub(r'[^a-z0-9_]+', '_', str(f).strip().lower()).strip('_')
                if cf and cf not in clean_schema:
                    clean_schema.append(cf)
            new_schema = clean_schema
        else:
            new_schema = current_schema

        await db.execute("""
            UPDATE subscriber_lists
            SET name = ?, description = ?, schema_fields = ?, updated_at = ?
            WHERE id = ?
        """, (new_name, new_desc, json.dumps(new_schema), now, list_id))
        await db.commit()

        async with db.execute(
            "SELECT COUNT(DISTINCT subscriber_id) FROM (SELECT subscriber_id FROM subscriber_list_memberships WHERE list_id = ? UNION SELECT subscriber_id FROM list_subscribers WHERE list_id = ?)",
            (list_id, list_id)
        ) as count_cur:
            c_row = await count_cur.fetchone()
            count = c_row[0] if c_row else 0

        return SubscriberListResponse(
            id=list_id,
            name=new_name,
            description=new_desc,
            schema_fields=new_schema,
            subscriber_count=count,
            created_at=list_dict["created_at"],
            updated_at=now
        )


@router.delete("/api/lists/{list_id}")
@router.delete("/api/subscribers/lists/{list_id}", include_in_schema=False)
async def delete_subscriber_list(list_id: str):
    """
    Delete a subscriber list and clear its memberships.
    """
    async with get_db() as db:
        async with db.execute("SELECT id FROM subscriber_lists WHERE id = ?", (list_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Subscriber list not found")

        await db.execute("DELETE FROM subscriber_list_memberships WHERE list_id = ?", (list_id,))
        await db.execute("DELETE FROM list_subscribers WHERE list_id = ?", (list_id,))
        await db.execute("DELETE FROM subscriber_lists WHERE id = ?", (list_id,))
        await db.commit()

    return {"success": True, "message": f"Subscriber list {list_id} deleted successfully."}


# ======================================================================
# Bulk Customer Multi-Email Add Endpoint
# ======================================================================

import re
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
NAME_EMAIL_REGEX = re.compile(r'^(?:"?([^"<]+)"?\s*)?<([^>]+)>$')

def parse_raw_emails(raw_text: str) -> List[Dict[str, str]]:
    """Parse comma, semicolon, newline, or tab separated emails."""
    if not raw_text:
        return []
    normalized = re.sub(r'[\r\n;]+', ',', raw_text)
    raw_tokens = [t.strip() for t in normalized.split(',') if t.strip()]
    results = []
    seen = set()
    for token in raw_tokens:
        m = NAME_EMAIL_REGEX.match(token)
        if m:
            name_part = (m.group(1) or "").strip()
            email_part = m.group(2).strip().lower()
        else:
            if "<" in token and ">" in token:
                parts = token.split("<")
                name_part = parts[0].strip().strip('"')
                email_part = parts[1].split(">")[0].strip().lower()
            else:
                name_part = ""
                email_part = token.strip().strip('"').lower()
        
        if email_part and EMAIL_REGEX.match(email_part):
            if email_part not in seen:
                seen.add(email_part)
                first_name = ""
                last_name = ""
                if name_part:
                    words = name_part.split()
                    first_name = words[0]
                    last_name = " ".join(words[1:]) if len(words) > 1 else ""
                else:
                    first_name = email_part.split("@")[0].capitalize()
                
                results.append({
                    "email": email_part,
                    "first_name": first_name,
                    "last_name": last_name,
                    "name": name_part or first_name
                })
    return results


class BulkTextImportPayload(BaseModel):
    raw_text: str = Field(..., description="Customer emails string (newlines, commas, semicolons)")
    list_id: Optional[str] = Field(default=None, description="Optional List ID to associate with")
    tags: List[str] = Field(default_factory=list, description="Tags to assign")


@router.post("/api/subscribers/bulk-text")
async def bulk_add_subscribers_text(payload: BulkTextImportPayload):
    """
    Directly parse and import multiple customer emails from pasted raw text into SQLite.
    """
    parsed = parse_raw_emails(payload.raw_text)
    if not parsed:
        return {
            "success": False,
            "message": "No valid email addresses found in the provided text.",
            "imported_count": 0,
            "subscribers": []
        }

    now = utc_now_iso()
    imported = 0
    updated = 0
    created_subscribers = []

    async with get_db() as db:
        for item in parsed:
            email_str = item["email"]
            first_name = item["first_name"]
            last_name = item["last_name"]

            async with db.execute("SELECT id FROM subscribers WHERE email = ?", (email_str,)) as cur:
                existing = await cur.fetchone()

            if existing:
                sub_id = existing["id"]
                await db.execute("""
                    UPDATE subscribers
                    SET first_name = COALESCE(NULLIF(?, ''), first_name),
                        last_name = COALESCE(NULLIF(?, ''), last_name),
                        status = 'active',
                        updated_at = ?
                    WHERE id = ?
                """, (first_name, last_name, now, sub_id))
                updated += 1
            else:
                sub_id = f"sub_{uuid.uuid4().hex[:10]}"
                await db.execute("""
                    INSERT INTO subscribers (
                        id, email, first_name, last_name, status,
                        tags, custom_fields, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """, (
                    sub_id,
                    email_str,
                    first_name,
                    last_name,
                    json.dumps(payload.tags or ["bulk-import"]),
                    json.dumps({}),
                    now,
                    now
                ))
                imported += 1

            if payload.list_id:
                await db.execute("""
                    INSERT OR IGNORE INTO subscriber_list_memberships (subscriber_id, list_id, added_at)
                    VALUES (?, ?, ?)
                """, (sub_id, payload.list_id, now))

            created_subscribers.append({
                "id": sub_id,
                "email": email_str,
                "first_name": first_name,
                "last_name": last_name,
                "status": "active"
            })

        await db.commit()

    return {
        "success": True,
        "message": f"Successfully processed {len(parsed)} customer emails ({imported} new, {updated} updated).",
        "total_parsed": len(parsed),
        "imported_count": imported,
        "updated_count": updated,
        "subscribers": created_subscribers
    }

