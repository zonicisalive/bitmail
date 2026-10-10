"""
Contacts and Lists API.

Data model (the same one commercial senders use):
- A contact (``subscribers`` row) exists once per email address. It carries the
  standard fields, free-form ``tags`` and account-wide ``custom_fields`` that are
  created automatically from CSV column headings.
- A list (``subscriber_lists`` row) is only a membership group. A contact can be
  in any number of lists via ``subscriber_list_memberships``.
- ``status`` is the contact's mailability. Anything other than ``active`` is
  mirrored into ``suppressions`` so every send path blocks it.
- Imports never change the status of an existing contact: re-uploading a file
  must not re-subscribe someone who opted out or bounced.

The contacts listing is paginated server-side; the total match count is returned
in the ``X-Total-Count`` header so the body stays a plain array for API clients.
"""

import csv
import io
import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator

from app.db import get_db, utc_now_iso
from app.models import (
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

# SQLite's default host-parameter limit is generous on modern builds, but chunking
# keeps very large "select all matching" actions safe on older ones too.
ID_CHUNK = 500

SORT_COLUMNS = {
    "email": "s.email",
    "name": "COALESCE(NULLIF(s.first_name, ''), s.email)",
    "status": "s.status",
    "created_at": "s.created_at",
    "updated_at": "s.updated_at",
}

# Malformed JSON would make json_each() abort the whole query, so guard it.
SAFE_TAGS = "CASE WHEN json_valid(s.tags) THEN s.tags ELSE '[]' END"
SAFE_FIELDS = "CASE WHEN json_valid(s.custom_fields) THEN s.custom_fields ELSE '{}' END"


# ======================================================================
# Helpers
# ======================================================================

def clean_field_key(raw: str) -> str:
    """Turn a CSV heading / field label into a merge-tag-safe key."""
    return re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")


def _json(value: Optional[str], default: Any) -> Any:
    try:
        parsed = json.loads(value) if value else default
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def _chunks(items: List[str], size: int = ID_CHUNK) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filter_sql(
    search: Optional[str],
    list_id: Optional[str],
    status_filter: Optional[str],
    tag: Optional[str],
) -> Tuple[str, List[Any]]:
    """WHERE clause shared by listing, export and select-all-matching bulk actions."""
    where = ["1=1"]
    params: List[Any] = []

    if search and search.strip():
        term = _like(search.strip().lower())
        where.append(f"""(
            s.email LIKE ? ESCAPE '\\'
            OR LOWER(COALESCE(s.first_name, '')) LIKE ? ESCAPE '\\'
            OR LOWER(COALESCE(s.last_name, '')) LIKE ? ESCAPE '\\'
            OR EXISTS (SELECT 1 FROM json_each({SAFE_FIELDS}) WHERE LOWER(CAST(value AS TEXT)) LIKE ? ESCAPE '\\')
            OR EXISTS (SELECT 1 FROM json_each({SAFE_TAGS}) WHERE value LIKE ? ESCAPE '\\')
        )""")
        params.extend([term] * 5)

    if list_id and list_id != "all":
        if list_id == "none":
            where.append("NOT EXISTS (SELECT 1 FROM subscriber_list_memberships m WHERE m.subscriber_id = s.id)")
        else:
            where.append("EXISTS (SELECT 1 FROM subscriber_list_memberships m WHERE m.subscriber_id = s.id AND m.list_id = ?)")
            params.append(list_id)

    if status_filter and status_filter != "all":
        where.append("s.status = ?")
        params.append(status_filter.lower())

    if tag and tag.strip():
        where.append(f"EXISTS (SELECT 1 FROM json_each({SAFE_TAGS}) WHERE value = ?)")
        params.append(tag.strip().lower())

    return " AND ".join(where), params


async def _lists_for(db, subscriber_ids: List[str]) -> Dict[str, List[str]]:
    """Membership for a page of contacts in one query instead of one per row."""
    out: Dict[str, List[str]] = {sid: [] for sid in subscriber_ids}
    for chunk in _chunks(subscriber_ids):
        async with db.execute(
            f"SELECT subscriber_id, list_id FROM subscriber_list_memberships WHERE subscriber_id IN ({_placeholders(len(chunk))})",
            chunk,
        ) as cur:
            for r in await cur.fetchall():
                out[r["subscriber_id"]].append(r["list_id"])
    return out


def _to_response(row, list_ids: List[str]) -> SubscriberResponse:
    try:
        sub_status = SubscriberStatus(row["status"] or "active")
    except ValueError:
        # An unknown legacy status must not 500 the whole contacts page.
        sub_status = SubscriberStatus.UNSUBSCRIBED
    return SubscriberResponse(
        id=row["id"],
        email=row["email"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        custom_fields=_json(row["custom_fields"], {}),
        tags=_json(row["tags"], []),
        status=sub_status,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        lists=list_ids,
    )


async def _sync_suppressions(db, emails: List[str], new_status: str, now: str) -> None:
    """
    Keep the global suppression table in step with contact status. Sends check
    suppressions, so a contact marked unsubscribed/bounced here is blocked on every
    path (lists, pasted recipients, transactional), and reactivating removes the block.
    """
    for chunk in _chunks(emails):
        if new_status == SubscriberStatus.ACTIVE.value:
            await db.execute(
                f"DELETE FROM suppressions WHERE email IN ({_placeholders(len(chunk))})", chunk
            )
        else:
            await db.executemany(
                "INSERT OR IGNORE INTO suppressions (id, email, campaign_id, reason, created_at) VALUES (?, ?, NULL, ?, ?)",
                [(f"sup_{uuid.uuid4().hex[:10]}", e, f"manual_{new_status}", now) for e in chunk],
            )


async def _require_list(db, list_id: str) -> None:
    async with db.execute("SELECT 1 FROM subscriber_lists WHERE id = ?", (list_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="List not found")


async def _add_memberships(db, subscriber_ids: List[str], list_id: str, now: str) -> None:
    await db.executemany(
        "INSERT OR IGNORE INTO subscriber_list_memberships (subscriber_id, list_id, added_at) VALUES (?, ?, ?)",
        [(sid, list_id, now) for sid in subscriber_ids],
    )


# ======================================================================
# Contacts
# ======================================================================

@router.get("/api/subscribers", response_model=List[SubscriberResponse])
async def list_subscribers(
    response: Response,
    search: Optional[str] = Query(default=None, description="Matches email, name, tags and custom field values"),
    list_id: Optional[str] = Query(default=None, description="List ID, 'all', or 'none' for contacts in no list"),
    status: Optional[str] = Query(default=None, description="active, unsubscribed, bounced, complained"),
    tag: Optional[str] = Query(default=None),
    sort: str = Query(default="created_at", description="email, name, status, created_at, updated_at"),
    order: Literal["asc", "desc"] = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    page: Optional[int] = Query(default=None, ge=1),
    per_page: Optional[int] = Query(default=None, ge=1, le=1000),
):
    """
    Paginated contact listing. The number of contacts matching the filters (ignoring
    pagination) is returned in the ``X-Total-Count`` response header.
    """
    if page and per_page:
        limit, offset = per_page, (page - 1) * per_page

    sort_sql = SORT_COLUMNS.get(sort, SORT_COLUMNS["created_at"])
    where, params = _filter_sql(search, list_id, status, tag)

    async with get_db() as db:
        async with db.execute(f"SELECT COUNT(*) FROM subscribers s WHERE {where}", params) as cur:
            total = (await cur.fetchone())[0]

        async with db.execute(
            f"SELECT s.* FROM subscribers s WHERE {where} ORDER BY {sort_sql} {order.upper()}, s.id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ) as cur:
            rows = await cur.fetchall()

        memberships = await _lists_for(db, [r["id"] for r in rows])

    response.headers["X-Total-Count"] = str(total)
    return [_to_response(r, memberships[r["id"]]) for r in rows]


@router.post("/api/subscribers", response_model=SubscriberResponse, status_code=status.HTTP_201_CREATED)
async def create_subscriber(payload: SubscriberCreate):
    """Create a contact and optionally add it to lists."""
    email_clean = payload.email.strip().lower()
    now = utc_now_iso()
    sub_id = f"sub_{uuid.uuid4().hex[:10]}"
    custom_fields = {clean_field_key(k): v for k, v in payload.custom_fields.items() if clean_field_key(k)}

    async with get_db() as db:
        async with db.execute("SELECT id FROM subscribers WHERE email = ?", (email_clean,)) as cursor:
            if await cursor.fetchone():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Subscriber with email '{email_clean}' already exists."
                )

        for l_id in payload.list_ids or []:
            await _require_list(db, l_id)

        await db.execute("""
            INSERT INTO subscribers (id, email, first_name, last_name, tags, custom_fields, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sub_id, email_clean, payload.first_name, payload.last_name,
            json.dumps(payload.tags), json.dumps(custom_fields), payload.status.value, now, now,
        ))
        await _sync_suppressions(db, [email_clean], payload.status.value, now)

        list_ids = list(dict.fromkeys(payload.list_ids or []))
        for l_id in list_ids:
            await _add_memberships(db, [sub_id], l_id, now)

        await db.commit()

    return SubscriberResponse(
        id=sub_id,
        email=email_clean,
        first_name=payload.first_name,
        last_name=payload.last_name,
        custom_fields=custom_fields,
        tags=payload.tags,
        status=payload.status,
        created_at=now,
        updated_at=now,
        lists=list_ids,
    )


# Static subpaths must be declared BEFORE /api/subscribers/{subscriber_id}.

@router.get("/api/subscribers/summary")
async def subscribers_summary():
    """Contact counts per status, for headers and audience pickers."""
    counts = {s.value: 0 for s in SubscriberStatus}
    async with get_db() as db:
        async with db.execute("SELECT status, COUNT(*) AS n FROM subscribers GROUP BY status") as cur:
            for r in await cur.fetchall():
                counts[r["status"] or "active"] = counts.get(r["status"] or "active", 0) + r["n"]
        async with db.execute("""
            SELECT COUNT(*) FROM subscribers s
            WHERE NOT EXISTS (SELECT 1 FROM subscriber_list_memberships m WHERE m.subscriber_id = s.id)
        """) as cur:
            no_list = (await cur.fetchone())[0]
        async with db.execute(f"""
            SELECT DISTINCT value FROM subscribers s, json_each({SAFE_TAGS}) ORDER BY value
        """) as cur:
            tags = [r[0] for r in await cur.fetchall()]

    return {
        "total": sum(counts.values()),
        "by_status": counts,
        "active": counts.get("active", 0),
        "not_in_any_list": no_list,
        "tags": tags,
    }


@router.get("/api/subscribers/export")
async def export_subscribers_csv(
    search: Optional[str] = Query(default=None),
    list_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    ids: Optional[str] = Query(default=None, description="Comma-separated contact IDs; overrides the filters"),
):
    """
    Export contacts as CSV with one column per custom field, so the file can be
    edited in a spreadsheet and re-imported without losing data.
    """
    if ids:
        id_list = [i for i in ids.split(",") if i.strip()]
        where = f"s.id IN ({_placeholders(len(id_list))})" if id_list else "0"
        params: List[Any] = id_list
    else:
        where, params = _filter_sql(search, list_id, status, tag)

    async with get_db() as db:
        async with db.execute(f"SELECT s.* FROM subscribers s WHERE {where} ORDER BY s.created_at DESC", params) as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT id, name FROM subscriber_lists") as cur:
            list_names = {r["id"]: r["name"] for r in await cur.fetchall()}
        memberships = await _lists_for(db, [r["id"] for r in rows])

    field_keys: List[str] = []
    parsed_fields = []
    for r in rows:
        cf = _json(r["custom_fields"], {})
        parsed_fields.append(cf)
        for k in cf:
            if k not in field_keys:
                field_keys.append(k)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "first_name", "last_name", "status", "tags", "lists", *field_keys, "created_at", "updated_at"])
    for r, cf in zip(rows, parsed_fields):
        writer.writerow([
            r["email"],
            r["first_name"] or "",
            r["last_name"] or "",
            r["status"],
            "; ".join(_json(r["tags"], [])),
            "; ".join(list_names.get(l, l) for l in memberships[r["id"]]),
            *[cf.get(k, "") for k in field_keys],
            r["created_at"],
            r["updated_at"],
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contacts_export.csv"},
    )


class ContactFilter(BaseModel):
    search: Optional[str] = None
    list_id: Optional[str] = None
    status: Optional[str] = None
    tag: Optional[str] = None


class SubscriberBulkAction(BaseModel):
    action: Literal["delete", "add_to_list", "remove_from_list", "set_status", "add_tag", "remove_tag"]
    ids: Optional[List[str]] = Field(default=None, description="Explicit contact IDs")
    filter: Optional[ContactFilter] = Field(
        default=None, description="Act on every contact matching these filters (select all matching)"
    )
    list_id: Optional[str] = None
    status: Optional[SubscriberStatus] = None
    tag: Optional[str] = None

    @model_validator(mode="after")
    def check_target_and_args(self):
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Provide exactly one of 'ids' or 'filter'.")
        if self.action in ("add_to_list", "remove_from_list") and not self.list_id:
            raise ValueError(f"'{self.action}' requires list_id.")
        if self.action == "set_status" and self.status is None:
            raise ValueError("'set_status' requires status.")
        if self.action in ("add_tag", "remove_tag"):
            self.tag = (self.tag or "").strip().lower()
            if not self.tag:
                raise ValueError(f"'{self.action}' requires tag.")
        return self


@router.post("/api/subscribers/bulk")
async def bulk_subscriber_action(payload: SubscriberBulkAction):
    """
    Apply one action to many contacts: explicit IDs, or every contact matching a
    filter (the "select all N matching" option on the contacts page).
    """
    now = utc_now_iso()
    async with get_db() as db:
        if payload.filter is not None:
            f = payload.filter
            where, params = _filter_sql(f.search, f.list_id, f.status, f.tag)
            async with db.execute(f"SELECT s.id FROM subscribers s WHERE {where}", params) as cur:
                ids = [r[0] for r in await cur.fetchall()]
        else:
            ids = list(dict.fromkeys(payload.ids or []))

        if payload.list_id and payload.action in ("add_to_list", "remove_from_list"):
            await _require_list(db, payload.list_id)

        affected = 0
        for chunk in _chunks(ids):
            ph = _placeholders(len(chunk))

            if payload.action == "delete":
                await db.execute(f"DELETE FROM subscriber_list_memberships WHERE subscriber_id IN ({ph})", chunk)
                cur = await db.execute(f"DELETE FROM subscribers WHERE id IN ({ph})", chunk)

            elif payload.action == "add_to_list":
                async with db.execute(f"SELECT id FROM subscribers WHERE id IN ({ph})", chunk) as c:
                    existing = [r[0] for r in await c.fetchall()]
                before = db.total_changes
                await _add_memberships(db, existing, payload.list_id, now)
                affected += db.total_changes - before
                continue

            elif payload.action == "remove_from_list":
                cur = await db.execute(
                    f"DELETE FROM subscriber_list_memberships WHERE list_id = ? AND subscriber_id IN ({ph})",
                    [payload.list_id, *chunk],
                )

            elif payload.action == "set_status":
                new_status = payload.status.value
                async with db.execute(f"SELECT email FROM subscribers WHERE id IN ({ph})", chunk) as c:
                    emails = [r[0] for r in await c.fetchall()]
                cur = await db.execute(
                    f"UPDATE subscribers SET status = ?, updated_at = ? WHERE id IN ({ph})",
                    [new_status, now, *chunk],
                )
                await _sync_suppressions(db, emails, new_status, now)

            elif payload.action == "add_tag":
                cur = await db.execute(f"""
                    UPDATE subscribers AS s
                    SET tags = json_insert({SAFE_TAGS}, '$[#]', ?), updated_at = ?
                    WHERE s.id IN ({ph})
                      AND NOT EXISTS (SELECT 1 FROM json_each({SAFE_TAGS}) WHERE value = ?)
                """, [payload.tag, now, *chunk, payload.tag])

            else:  # remove_tag
                cur = await db.execute(f"""
                    UPDATE subscribers AS s
                    SET tags = (SELECT COALESCE(json_group_array(value), '[]') FROM json_each({SAFE_TAGS}) WHERE value != ?),
                        updated_at = ?
                    WHERE s.id IN ({ph})
                      AND EXISTS (SELECT 1 FROM json_each({SAFE_TAGS}) WHERE value = ?)
                """, [payload.tag, now, *chunk, payload.tag])

            affected += cur.rowcount

        await db.commit()

    verbs = {
        "delete": "Deleted",
        "add_to_list": "Added to list",
        "remove_from_list": "Removed from list",
        "set_status": f"Set to {payload.status.value if payload.status else ''}",
        "add_tag": f"Tagged '{payload.tag}'",
        "remove_tag": f"Removed tag '{payload.tag}' from",
    }
    return {
        "success": True,
        "action": payload.action,
        "matched": len(ids),
        "affected": affected,
        "message": f"{verbs[payload.action]}: {affected} of {len(ids)} contact{'s' if len(ids) != 1 else ''}.",
    }


@router.post("/api/subscribers/import-csv", response_model=SubscriberBulkImportResponse)
async def import_subscribers_csv(
    file: Optional[UploadFile] = File(default=None),
    list_id: Optional[str] = Form(default=None),
    new_list_name: Optional[str] = Form(default=None),
    column_mapping: Optional[str] = Form(default=None),
    update_duplicates: bool = Form(default=True),
    tags: Optional[str] = Form(default=None, description="Comma-separated tags applied to every imported row"),
):
    """
    Import contacts from CSV. Email / first name / last name columns are detected
    from the headings (or given via ``column_mapping``); every other column becomes a
    custom field named after its heading. Existing contacts are matched by email:
    their fields are updated when ``update_duplicates`` is set, their status never is.
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

    # {"email": "<heading>", "first_name": ..., "last_name": ..., "skip": ["<heading>", ...]}
    mapping: Dict[str, Any] = _json(column_mapping, {}) if column_mapping else {}
    skip_cols = {c for c in (mapping.get("skip") or []) if isinstance(c, str)}
    email_col = mapping.get("email")
    first_name_col = mapping.get("first_name")
    last_name_col = mapping.get("last_name")

    if not email_col:
        email_col = next((f for f in fieldnames if "email" in f.lower() or "mail" in f.lower()), None)
    if not first_name_col:
        first_name_col = next(
            (f for f in fieldnames if any(k in f.lower() for k in ("first", "fname", "given")) or f.lower().strip() == "name"),
            None,
        )
    if not last_name_col:
        last_name_col = next(
            (f for f in fieldnames if any(k in f.lower() for k in ("last", "lname", "surname", "family"))),
            None,
        )
    if not email_col and fieldnames:
        email_col = fieldnames[0]

    reserved_cols = {c for c in (email_col, first_name_col, last_name_col) if c} | skip_cols
    import_tags = [t.strip().lower() for t in (tags or "").split(",") if t.strip()]

    target_list_id = list_id or None
    created_list_name = None

    added_count = updated_count = failed_count = 0
    errors: List[Dict[str, Any]] = []
    detected_custom_fields: List[str] = []
    now = utc_now_iso()

    async with get_db() as db:
        if new_list_name and new_list_name.strip():
            created_list_name = new_list_name.strip()
            target_list_id = f"list_{uuid.uuid4().hex[:10]}"
            await db.execute("""
                INSERT INTO subscriber_lists (id, name, description, schema_fields, created_at, updated_at)
                VALUES (?, ?, ?, '[]', ?, ?)
            """, (target_list_id, created_list_name, f"Imported from {file.filename or 'CSV'}", now, now))
        elif target_list_id:
            await _require_list(db, target_list_id)

        seen_in_file = set()
        for row_idx, row in enumerate(reader, start=2):
            raw_email = (row.get(email_col) or "").strip().lower() if email_col else ""
            if not raw_email or "@" not in raw_email or "." not in raw_email.split("@")[-1]:
                failed_count += 1
                errors.append({"row": row_idx, "error": f"Invalid email value: '{raw_email}'"})
                continue
            if raw_email in seen_in_file:
                failed_count += 1
                errors.append({"row": row_idx, "error": f"Duplicate of an earlier row: '{raw_email}'"})
                continue
            seen_in_file.add(raw_email)

            first_name = (row.get(first_name_col) or "").strip() or None if first_name_col else None
            last_name = (row.get(last_name_col) or "").strip() or None if last_name_col else None

            custom_fields: Dict[str, str] = {}
            for col_k, col_v in row.items():
                if col_k in reserved_cols or col_k is None or not col_v or not str(col_v).strip():
                    continue
                key = clean_field_key(col_k)
                if key:
                    custom_fields[key] = str(col_v).strip()
                    if key not in detected_custom_fields:
                        detected_custom_fields.append(key)

            async with db.execute("SELECT id, custom_fields, tags FROM subscribers WHERE email = ?", (raw_email,)) as cursor:
                existing = await cursor.fetchone()

            if existing:
                sub_id = existing["id"]
                if update_duplicates:
                    merged_fields = {**_json(existing["custom_fields"], {}), **custom_fields}
                    merged_tags = list(dict.fromkeys([*_json(existing["tags"], []), *import_tags]))
                    await db.execute("""
                        UPDATE subscribers
                        SET first_name = COALESCE(?, first_name),
                            last_name = COALESCE(?, last_name),
                            custom_fields = ?,
                            tags = ?,
                            updated_at = ?
                        WHERE id = ?
                    """, (first_name, last_name, json.dumps(merged_fields), json.dumps(merged_tags), now, sub_id))
                    updated_count += 1
            else:
                sub_id = f"sub_{uuid.uuid4().hex[:10]}"
                await db.execute("""
                    INSERT INTO subscribers (id, email, first_name, last_name, tags, custom_fields, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """, (sub_id, raw_email, first_name, last_name, json.dumps(import_tags), json.dumps(custom_fields), now, now))
                added_count += 1

            if target_list_id:
                await _add_memberships(db, [sub_id], target_list_id, now)

        if target_list_id and detected_custom_fields:
            async with db.execute("SELECT schema_fields FROM subscriber_lists WHERE id = ?", (target_list_id,)) as cur:
                list_row = await cur.fetchone()
            if list_row:
                existing_schema = _json(list_row["schema_fields"], [])
                merged = list(dict.fromkeys([*existing_schema, *detected_custom_fields]))
                if merged != existing_schema:
                    await db.execute(
                        "UPDATE subscriber_lists SET schema_fields = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(merged), now, target_list_id),
                    )

        await db.commit()

    return SubscriberBulkImportResponse(
        total_received=added_count + updated_count + failed_count,
        added_count=added_count,
        updated_count=updated_count,
        failed_count=failed_count,
        errors=errors[:50],
        custom_fields_detected=detected_custom_fields,
        list_id=target_list_id,
        list_name=created_list_name,
    )


@router.get("/api/subscribers/placeholders")
async def get_available_placeholders(list_id: Optional[str] = Query(default=None, description="Optional list ID")):
    """
    Merge tags available for composing: the standard ones plus every custom field
    key in use. With ``list_id``, also the fields declared on / used by that list.
    """
    standard_tags = ["first_name", "last_name", "email", "name", "company", "unsubscribe_url", "year", "date"]
    custom_tags = set()
    table_fields: List[str] = []
    selected_list_name = None

    async with get_db() as db:
        if list_id and list_id.lower() != "all":
            async with db.execute("SELECT name, schema_fields FROM subscriber_lists WHERE id = ?", (list_id,)) as cur:
                l_row = await cur.fetchone()
            if l_row:
                selected_list_name = l_row["name"]
                for col in _json(l_row["schema_fields"], []):
                    key = clean_field_key(col)
                    if key:
                        table_fields.append(key)
                        custom_tags.add(key)
            sql = f"""
                SELECT DISTINCT j.key FROM subscribers s
                JOIN subscriber_list_memberships m ON m.subscriber_id = s.id,
                json_each({SAFE_FIELDS}) j
                WHERE m.list_id = ?
            """
            params: List[Any] = [list_id]
        else:
            sql = f"SELECT DISTINCT j.key FROM subscribers s, json_each({SAFE_FIELDS}) j"
            params = []

        async with db.execute(sql, params) as cur:
            for r in await cur.fetchall():
                key = clean_field_key(r[0] or "")
                if key:
                    custom_tags.add(key)

        async with db.execute("SELECT name FROM subscriber_lists") as cur:
            list_names = [r["name"] for r in await cur.fetchall() if r["name"]]

    return {
        "success": True,
        "list_id": list_id,
        "list_name": selected_list_name,
        "standard_tags": standard_tags,
        "standard_placeholders": standard_tags,
        "table_placeholders": sorted(set(table_fields)),
        "custom_tags": sorted(custom_tags),
        "custom_fields": sorted(custom_tags),
        "list_names": list_names,
    }


# ======================================================================
# Bulk paste
# ======================================================================

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
        elif "<" in token and ">" in token:
            parts = token.split("<")
            name_part = parts[0].strip().strip('"')
            email_part = parts[1].split(">")[0].strip().lower()
        else:
            name_part = ""
            email_part = token.strip().strip('"').lower()

        if email_part and EMAIL_REGEX.match(email_part) and email_part not in seen:
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
                "name": name_part or first_name,
            })

    return results


class BulkTextImportPayload(BaseModel):
    raw_text: str = Field(..., description="Customer emails string (newlines, commas, semicolons)")
    list_id: Optional[str] = Field(default=None, description="Optional List ID to associate with")
    tags: List[str] = Field(default_factory=list, description="Tags to assign")


@router.post("/api/subscribers/bulk-text")
async def bulk_add_subscribers_text(payload: BulkTextImportPayload):
    """
    Add pasted addresses as contacts. Existing contacts get missing names filled in
    and the tags/list added, but their status is left alone - pasting a list must
    never re-subscribe someone who unsubscribed, bounced or complained.
    """
    parsed = parse_raw_emails(payload.raw_text)
    if not parsed:
        return {
            "success": False,
            "message": "No valid email addresses found in the provided text.",
            "imported_count": 0,
            "subscribers": [],
        }

    now = utc_now_iso()
    tags = list(dict.fromkeys(t.strip().lower() for t in payload.tags if t.strip()))
    imported = updated = 0
    skipped_inactive: List[str] = []
    created_subscribers = []

    async with get_db() as db:
        if payload.list_id:
            await _require_list(db, payload.list_id)

        for item in parsed:
            email_str = item["email"]

            async with db.execute("SELECT id, status, tags FROM subscribers WHERE email = ?", (email_str,)) as cur:
                existing = await cur.fetchone()

            if existing:
                sub_id = existing["id"]
                sub_status = existing["status"] or "active"
                if sub_status != "active":
                    skipped_inactive.append(email_str)
                merged_tags = list(dict.fromkeys([*_json(existing["tags"], []), *tags]))
                await db.execute("""
                    UPDATE subscribers
                    SET first_name = COALESCE(NULLIF(first_name, ''), NULLIF(?, '')),
                        last_name = COALESCE(NULLIF(last_name, ''), NULLIF(?, '')),
                        tags = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (item["first_name"], item["last_name"], json.dumps(merged_tags), now, sub_id))
                updated += 1
            else:
                sub_id = f"sub_{uuid.uuid4().hex[:10]}"
                sub_status = "active"
                await db.execute("""
                    INSERT INTO subscribers (id, email, first_name, last_name, status, tags, custom_fields, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'active', ?, '{}', ?, ?)
                """, (sub_id, email_str, item["first_name"], item["last_name"], json.dumps(tags), now, now))
                imported += 1

            if payload.list_id:
                await _add_memberships(db, [sub_id], payload.list_id, now)

            created_subscribers.append({
                "id": sub_id,
                "email": email_str,
                "first_name": item["first_name"],
                "last_name": item["last_name"],
                "status": sub_status,
            })

        await db.commit()

    message = f"Processed {len(parsed)} emails ({imported} new, {updated} already existed)."
    if skipped_inactive:
        message += f" {len(skipped_inactive)} are unsubscribed/bounced and stay that way."

    return {
        "success": True,
        "message": message,
        "total_parsed": len(parsed),
        "imported_count": imported,
        "updated_count": updated,
        "inactive_count": len(skipped_inactive),
        "subscribers": created_subscribers,
    }


# ======================================================================
# Lists (membership groups)
# ======================================================================

LIST_COUNTS_SQL = """
    SELECT l.*,
        (SELECT COUNT(*) FROM subscriber_list_memberships m WHERE m.list_id = l.id) AS subscriber_count,
        (SELECT COUNT(*) FROM subscriber_list_memberships m
            JOIN subscribers s ON s.id = m.subscriber_id
            WHERE m.list_id = l.id AND s.status = 'active') AS active_count
    FROM subscriber_lists l
"""


def _list_response(r) -> SubscriberListResponse:
    return SubscriberListResponse(
        id=r["id"],
        name=r["name"],
        description=r["description"],
        schema_fields=_json(r["schema_fields"], []),
        subscriber_count=r["subscriber_count"] or 0,
        active_count=r["active_count"] or 0,
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


async def _fetch_list(db, list_id: str) -> SubscriberListResponse:
    async with db.execute(f"{LIST_COUNTS_SQL} WHERE l.id = ?", (list_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="List not found")
    return _list_response(row)


def _clean_schema(fields: Optional[List[str]]) -> List[str]:
    return list(dict.fromkeys(k for k in (clean_field_key(f) for f in fields or []) if k))


@router.get("/api/lists", response_model=List[SubscriberListResponse])
@router.get("/api/subscribers/lists", response_model=List[SubscriberListResponse], include_in_schema=False)
async def list_subscriber_lists():
    """All lists with total and mailable (active) member counts."""
    async with get_db() as db:
        async with db.execute(f"{LIST_COUNTS_SQL} ORDER BY l.name COLLATE NOCASE") as cur:
            return [_list_response(r) for r in await cur.fetchall()]


@router.post("/api/lists", response_model=SubscriberListResponse, status_code=status.HTTP_201_CREATED)
@router.post("/api/subscribers/lists", response_model=SubscriberListResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_subscriber_list(payload: SubscriberListCreate):
    """Create a list. ``schema_fields`` are optional suggested custom fields for it."""
    list_id = f"list_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()
    name = payload.name.strip()

    async with get_db() as db:
        async with db.execute("SELECT 1 FROM subscriber_lists WHERE name = ? COLLATE NOCASE", (name,)) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail=f"A list named '{name}' already exists.")
        await db.execute("""
            INSERT INTO subscriber_lists (id, name, description, schema_fields, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (list_id, name, payload.description, json.dumps(_clean_schema(payload.schema_fields)), now, now))
        await db.commit()
        return await _fetch_list(db, list_id)


@router.get("/api/lists/{list_id}", response_model=SubscriberListDetail)
async def get_subscriber_list(list_id: str):
    """
    List details with its members. Prefer ``GET /api/subscribers?list_id=`` for
    paging through large lists; this returns every member.
    """
    async with get_db() as db:
        summary = await _fetch_list(db, list_id)
        async with db.execute("""
            SELECT s.* FROM subscribers s
            JOIN subscriber_list_memberships m ON m.subscriber_id = s.id
            WHERE m.list_id = ?
            ORDER BY s.created_at DESC
        """, (list_id,)) as cur:
            rows = await cur.fetchall()
        memberships = await _lists_for(db, [r["id"] for r in rows])

    return SubscriberListDetail(
        **summary.model_dump(),
        subscribers=[_to_response(r, memberships[r["id"]]) for r in rows],
    )


@router.put("/api/lists/{list_id}", response_model=SubscriberListResponse)
@router.put("/api/subscribers/lists/{list_id}", response_model=SubscriberListResponse, include_in_schema=False)
async def update_subscriber_list(list_id: str, payload: SubscriberListUpdate):
    """Rename a list or change its description / suggested fields."""
    now = utc_now_iso()
    async with get_db() as db:
        current = await _fetch_list(db, list_id)
        new_name = payload.name.strip() if payload.name is not None else current.name
        if new_name.lower() != current.name.lower():
            async with db.execute(
                "SELECT 1 FROM subscriber_lists WHERE name = ? COLLATE NOCASE AND id != ?", (new_name, list_id)
            ) as cur:
                if await cur.fetchone():
                    raise HTTPException(status_code=409, detail=f"A list named '{new_name}' already exists.")

        await db.execute("""
            UPDATE subscriber_lists SET name = ?, description = ?, schema_fields = ?, updated_at = ? WHERE id = ?
        """, (
            new_name,
            payload.description if payload.description is not None else current.description,
            json.dumps(_clean_schema(payload.schema_fields) if payload.schema_fields is not None else current.schema_fields),
            now,
            list_id,
        ))
        await db.commit()
        return await _fetch_list(db, list_id)


@router.delete("/api/lists/{list_id}")
@router.delete("/api/subscribers/lists/{list_id}", include_in_schema=False)
async def delete_subscriber_list(list_id: str):
    """Delete a list. Its contacts are kept; they just leave the list."""
    async with get_db() as db:
        await _require_list(db, list_id)
        await db.execute("DELETE FROM subscriber_list_memberships WHERE list_id = ?", (list_id,))
        await db.execute("DELETE FROM subscriber_lists WHERE id = ?", (list_id,))
        await db.commit()
    return {"success": True, "message": f"Subscriber list {list_id} deleted successfully."}


# ======================================================================
# Single contact (path-parameter routes last so static paths win)
# ======================================================================

@router.get("/api/subscribers/{subscriber_id}", response_model=SubscriberResponse)
async def get_subscriber(subscriber_id: str):
    """Get a single contact by ID."""
    async with get_db() as db:
        async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subscriber not found")
        memberships = await _lists_for(db, [subscriber_id])
    return _to_response(row, memberships[subscriber_id])


@router.put("/api/subscribers/{subscriber_id}", response_model=SubscriberResponse)
async def update_subscriber(subscriber_id: str, payload: SubscriberUpdate):
    """
    Update a contact. ``custom_fields`` is merged into the existing fields; send a
    key with an empty string to clear it. ``list_ids`` and ``tags`` replace the
    current values when given.
    """
    now = utc_now_iso()
    async with get_db() as db:
        async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subscriber not found")

        new_email = payload.email.strip().lower() if payload.email is not None else row["email"]
        if new_email != row["email"]:
            async with db.execute("SELECT 1 FROM subscribers WHERE email = ? AND id != ?", (new_email, subscriber_id)) as cur:
                if await cur.fetchone():
                    raise HTTPException(status_code=409, detail=f"Another contact already uses '{new_email}'.")

        custom_fields = _json(row["custom_fields"], {})
        if payload.custom_fields is not None:
            for k, v in payload.custom_fields.items():
                key = clean_field_key(k)
                if not key:
                    continue
                if v is None or str(v).strip() == "":
                    custom_fields.pop(key, None)
                else:
                    custom_fields[key] = v

        new_status = payload.status.value if payload.status is not None else (row["status"] or "active")
        tags = payload.tags if payload.tags is not None else _json(row["tags"], [])

        await db.execute("""
            UPDATE subscribers
            SET email = ?, first_name = ?, last_name = ?, custom_fields = ?, tags = ?, status = ?, updated_at = ?
            WHERE id = ?
        """, (
            new_email,
            payload.first_name if payload.first_name is not None else row["first_name"],
            payload.last_name if payload.last_name is not None else row["last_name"],
            json.dumps(custom_fields),
            json.dumps(tags),
            new_status,
            now,
            subscriber_id,
        ))

        # Only an explicit status change touches suppressions; editing a typo in an
        # address must not quietly lift a block on it.
        if payload.status is not None:
            await _sync_suppressions(db, [new_email], new_status, now)

        if payload.list_ids is not None:
            for lid in payload.list_ids:
                await _require_list(db, lid)
            await db.execute("DELETE FROM subscriber_list_memberships WHERE subscriber_id = ?", (subscriber_id,))
            for lid in dict.fromkeys(payload.list_ids):
                await _add_memberships(db, [subscriber_id], lid, now)

        await db.commit()

        async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            updated = await cursor.fetchone()
        memberships = await _lists_for(db, [subscriber_id])

    return _to_response(updated, memberships[subscriber_id])


@router.delete("/api/subscribers/{subscriber_id}")
async def delete_subscriber(subscriber_id: str):
    """Delete a contact and its list memberships. Suppressions are kept."""
    async with get_db() as db:
        async with db.execute("SELECT id FROM subscribers WHERE id = ?", (subscriber_id,)) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Subscriber not found")
        await db.execute("DELETE FROM subscriber_list_memberships WHERE subscriber_id = ?", (subscriber_id,))
        await db.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
        await db.commit()
    return {"success": True, "message": f"Subscriber {subscriber_id} deleted successfully."}
