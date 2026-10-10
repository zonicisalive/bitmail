"""
Database module for the Enterprise Mass Email System.
Provides asynchronous SQLite connection management, schema initialization,
WAL configuration, performance pragmas, and database helper methods.
"""

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiosqlite

from app.config import settings


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Asynchronous context manager for SQLite database connection.
    Configures WAL mode, busy timeout, memory map, and row factory.
    """
    settings.ensure_directories()
    conn = await aiosqlite.connect(str(settings.DATABASE_PATH))
    conn.row_factory = aiosqlite.Row

    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute(f"PRAGMA busy_timeout={settings.SQLITE_BUSY_TIMEOUT_MS};")
    await conn.execute(f"PRAGMA cache_size={settings.SQLITE_CACHE_SIZE_KB};")
    await conn.execute(f"PRAGMA mmap_size={settings.SQLITE_MMAP_SIZE_BYTES};")
    await conn.execute("PRAGMA foreign_keys=ON;")

    try:
        yield conn
    finally:
        await conn.close()


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) as cur:
        return await cur.fetchone() is not None


async def _consolidate_duplicate_tables(db: aiosqlite.Connection) -> None:
    """
    Earlier versions wrote list membership to both subscriber_list_memberships and
    list_subscribers, and suppressions to both suppressions and suppression_list.
    Readers had to UNION both and some (warmup) only read one, so lists showed
    different members depending on the screen. Fold the duplicates into the
    canonical table once and drop them so there is a single source of truth.
    """
    if await _table_exists(db, "list_subscribers"):
        await db.execute("""
            INSERT OR IGNORE INTO subscriber_list_memberships (subscriber_id, list_id, added_at)
            SELECT ls.subscriber_id, ls.list_id, ls.subscribed_at
            FROM list_subscribers ls
            WHERE ls.subscriber_id IN (SELECT id FROM subscribers)
              AND ls.list_id IN (SELECT id FROM subscriber_lists)
        """)
        await db.execute("DROP TABLE list_subscribers")

    if await _table_exists(db, "suppression_list"):
        await db.execute("""
            INSERT OR IGNORE INTO suppressions (id, email, campaign_id, reason, created_at)
            SELECT id, email, campaign_id, reason, created_at FROM suppression_list
        """)
        await db.execute("DROP TABLE suppression_list")

    await db.commit()


async def init_db() -> None:
    """
    Initialize SQLite database schema, create tables, views, and indexes.
    Seeds default configurations if the database is newly initialized.
    """
    settings.ensure_directories()

    async with get_db() as db:
        # 1. Subscribers Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                first_name TEXT,
                last_name TEXT,
                tags TEXT DEFAULT '[]',
                custom_fields TEXT DEFAULT '{}',
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # Migration: Add tags column if table existed from previous version
        try:
            await db.execute("ALTER TABLE subscribers ADD COLUMN tags TEXT DEFAULT '[]'")
        except Exception:
            pass

        # 2. Subscriber Lists Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                schema_fields TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # Migration: Add schema_fields column if table existed
        try:
            await db.execute("ALTER TABLE subscriber_lists ADD COLUMN schema_fields TEXT DEFAULT '[]'")
        except Exception:
            pass

        # 3. Subscriber List Memberships Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriber_list_memberships (
                subscriber_id TEXT NOT NULL,
                list_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (subscriber_id, list_id),
                FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE,
                FOREIGN KEY (list_id) REFERENCES subscriber_lists(id) ON DELETE CASCADE
            );
        """)


        # 8b. Storage Audit Events Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_storage_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_timestamp TEXT,
                event_time TEXT,
                event_data_json TEXT DEFAULT '{}',
                details_json TEXT DEFAULT '{}',
                ip_address TEXT,
                user_agent TEXT,
                FOREIGN KEY (email_storage_id) REFERENCES sent_emails(id) ON DELETE CASCADE
            );
        """)

        # 4. Templates Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                subject TEXT NOT NULL,
                body_html TEXT NOT NULL,
                body_text TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 5. SMTP Configurations Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS smtp_configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 587,
                username TEXT,
                password TEXT,
                use_tls INTEGER NOT NULL DEFAULT 1,
                use_ssl INTEGER NOT NULL DEFAULT 0,
                rate_limit_per_second INTEGER NOT NULL DEFAULT 25,
                daily_quota INTEGER NOT NULL DEFAULT 50000,
                is_default INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 6. Campaigns Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                template_id TEXT REFERENCES templates(id) ON DELETE SET NULL,
                list_id TEXT REFERENCES subscriber_lists(id) ON DELETE SET NULL,
                smtp_config_id TEXT REFERENCES smtp_configs(id) ON DELETE SET NULL,
                smtp_config_json TEXT,
                template_html TEXT,
                template_text TEXT,
                sender_name TEXT NOT NULL,
                sender_email TEXT NOT NULL,
                reply_to TEXT,
                headers TEXT DEFAULT '{}',
                track_opens INTEGER NOT NULL DEFAULT 1,
                track_clicks INTEGER NOT NULL DEFAULT 1,
                custom_html TEXT,
                custom_text TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                scheduled_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                total_recipients INTEGER NOT NULL DEFAULT 0,
                sent_count INTEGER NOT NULL DEFAULT 0,
                delivered_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                open_count INTEGER NOT NULL DEFAULT 0,
                click_count INTEGER NOT NULL DEFAULT 0,
                unsubscribe_count INTEGER NOT NULL DEFAULT 0,
                unsubscribed_count INTEGER NOT NULL DEFAULT 0,
                bounce_count INTEGER NOT NULL DEFAULT 0,
                rate_limit_per_sec INTEGER NOT NULL DEFAULT 25,
                concurrency_limit INTEGER NOT NULL DEFAULT 10,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 7. Sent Emails Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_emails (
                id TEXT PRIMARY KEY,
                campaign_id TEXT REFERENCES campaigns(id) ON DELETE SET NULL,
                subscriber_id TEXT,
                recipient_email TEXT NOT NULL COLLATE NOCASE,
                recipient_name TEXT,
                sender_email TEXT NOT NULL,
                sender_name TEXT,
                subject TEXT NOT NULL,
                body_html TEXT,
                body_text TEXT,
                rendered_html TEXT,
                headers TEXT DEFAULT '{}',
                headers_json TEXT DEFAULT '{}',
                raw_headers_json TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                error_message TEXT,
                message_id TEXT,
                smtp_host TEXT,
                smtp_port INTEGER,
                is_sandbox INTEGER DEFAULT 0,
                delivery_latency_ms REAL,
                open_count INTEGER NOT NULL DEFAULT 0,
                click_count INTEGER NOT NULL DEFAULT 0,
                first_opened_at TEXT,
                last_opened_at TEXT,
                opened_at TEXT,
                clicked_at TEXT,
                raw_eml_path TEXT,
                eml_file_path TEXT,
                eml_size_bytes INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                sent_at TEXT
            );
        """)

        # 8. Email Events Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS email_events (
                id TEXT PRIMARY KEY,
                sent_email_id TEXT NOT NULL REFERENCES sent_emails(id) ON DELETE CASCADE,
                campaign_id TEXT REFERENCES campaigns(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                event_payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
        """)

        # 8b. Storage Audit Events Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_storage_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_timestamp TEXT NOT NULL DEFAULT '',
                event_time TEXT DEFAULT '',
                event_data_json TEXT NOT NULL DEFAULT '{}',
                details_json TEXT DEFAULT '{}',
                ip_address TEXT,
                user_agent TEXT,
                FOREIGN KEY (email_storage_id) REFERENCES sent_emails(id) ON DELETE CASCADE
            );
        """)

        # 8c. Campaign Recipients Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS campaign_recipients (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                email TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                custom_attributes_json TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                sent_email_id TEXT,
                error_message TEXT,
                dispatched_at TEXT,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );
        """)

        # 8d. Tracking Opens Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracking_opens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_storage_id TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(email_storage_id) REFERENCES sent_emails(id) ON DELETE CASCADE
            );
        """)

        # 8e. Tracking Clicks Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracking_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_storage_id TEXT NOT NULL,
                original_url TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(email_storage_id) REFERENCES sent_emails(id) ON DELETE CASCADE
            );
        """)

        # 8f. Unsubscribes Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS unsubscribes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                campaign_id TEXT,
                email_storage_id TEXT,
                reason TEXT,
                created_at TEXT NOT NULL
            );
        """)

        # 9. Suppressions Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS suppressions (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                campaign_id TEXT REFERENCES campaigns(id) ON DELETE SET NULL,
                reason TEXT NOT NULL DEFAULT 'user_unsubscribed',
                created_at TEXT NOT NULL
            );
        """)


        # 10. Direct QR Scan Authentication Sessions Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scan_sessions (
                id TEXT PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                user_email TEXT,
                user_name TEXT,
                device_info TEXT,
                ip_address TEXT,
                auth_token TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 11. Users Table (Authentication & Access Control)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL COLLATE NOCASE,
                username TEXT UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                name TEXT,
                role TEXT NOT NULL DEFAULT 'admin',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 12. Persistent User Sessions Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                user_agent TEXT,
                ip_address TEXT
            );
        """)

        # 13. Warmup Schedules Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warmup_schedules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                campaign_id TEXT,
                strategy TEXT NOT NULL DEFAULT 'conservative_30',
                total_recipients INTEGER NOT NULL DEFAULT 0,
                current_day INTEGER NOT NULL DEFAULT 1,
                total_days INTEGER NOT NULL DEFAULT 14,
                daily_cap INTEGER NOT NULL DEFAULT 50,
                sent_today INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                relay_pool_json TEXT DEFAULT '[]',
                rotation_mode TEXT NOT NULL DEFAULT 'round_robin',
                max_bounce_rate REAL NOT NULL DEFAULT 0.02,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 14. Warmup Slices Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warmup_slices (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL REFERENCES warmup_schedules(id) ON DELETE CASCADE,
                day_number INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                dispatched_count INTEGER NOT NULL DEFAULT 0,
                bounce_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                campaign_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                recipients_json TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 15. Relay Pool Stats Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS relay_pool_stats (
                id TEXT PRIMARY KEY,
                smtp_config_id TEXT NOT NULL UNIQUE,
                in_pool INTEGER NOT NULL DEFAULT 0,
                current_day INTEGER NOT NULL DEFAULT 1,
                daily_sends INTEGER NOT NULL DEFAULT 0,
                daily_failures INTEGER NOT NULL DEFAULT 0,
                cooldown_until TEXT,
                last_error TEXT,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 16. Webhooks Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                secret TEXT NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 17. Webhook Deliveries Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id TEXT PRIMARY KEY,
                webhook_id TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status_code INTEGER,
                response_body TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
        """)

        # Migration helper to ensure columns exist in existing SQLite databases
        async def add_column_if_missing(table_name: str, col_name: str, col_type: str):
            try:
                async with db.execute(f"PRAGMA table_info({table_name})") as cur:
                    cols = [row["name"] for row in await cur.fetchall()]
                    if col_name not in cols:
                        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

        await add_column_if_missing("sent_emails", "subscriber_id", "TEXT")
        await add_column_if_missing("sent_emails", "rendered_html", "TEXT")
        await add_column_if_missing("sent_emails", "raw_headers_json", "TEXT DEFAULT '{}'")
        await add_column_if_missing("sent_emails", "headers_json", "TEXT DEFAULT '{}'")
        await add_column_if_missing("sent_emails", "headers", "TEXT DEFAULT '{}'")
        await add_column_if_missing("sent_emails", "smtp_host", "TEXT")
        await add_column_if_missing("sent_emails", "smtp_port", "INTEGER")
        await add_column_if_missing("sent_emails", "is_sandbox", "INTEGER DEFAULT 0")
        await add_column_if_missing("sent_emails", "delivery_latency_ms", "REAL")
        await add_column_if_missing("sent_emails", "opened_at", "TEXT")
        await add_column_if_missing("sent_emails", "clicked_at", "TEXT")
        await add_column_if_missing("sent_emails", "eml_file_path", "TEXT")
        await add_column_if_missing("sent_emails", "eml_size_bytes", "INTEGER DEFAULT 0")
        await add_column_if_missing("sent_emails", "metadata_json", "TEXT DEFAULT '{}'")

        await add_column_if_missing("campaigns", "template_html", "TEXT")
        await add_column_if_missing("campaigns", "template_text", "TEXT")
        await add_column_if_missing("campaigns", "smtp_config_json", "TEXT")
        await add_column_if_missing("campaigns", "unsubscribed_count", "INTEGER DEFAULT 0")
        await add_column_if_missing("campaigns", "rate_limit_per_sec", "INTEGER DEFAULT 25")
        await add_column_if_missing("campaigns", "concurrency_limit", "INTEGER DEFAULT 10")
        await add_column_if_missing("campaigns", "is_warmup", "INTEGER DEFAULT 0")
        await add_column_if_missing("campaigns", "warmup_schedule_id", "TEXT")

        await add_column_if_missing("smtp_configs", "in_relay_pool", "INTEGER DEFAULT 0")
        await add_column_if_missing("smtp_configs", "warmup_day", "INTEGER DEFAULT 1")


        await _consolidate_duplicate_tables(db)

        # Indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers(email);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_created ON subscribers(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memberships_list ON subscriber_list_memberships(list_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_memberships_sub ON subscriber_list_memberships(subscriber_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sent_emails_campaign ON sent_emails(campaign_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sent_emails_recipient ON sent_emails(recipient_email);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sent_emails_status ON sent_emails(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sent_emails_created ON sent_emails(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_email_events_email ON email_events(sent_email_id);")

        await db.execute("CREATE INDEX IF NOT EXISTS idx_email_events_campaign ON email_events(campaign_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_email_events_type ON email_events(event_type);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_email_events_created ON email_events(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_suppressions_email ON suppressions(email);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_is_active ON webhooks(is_active);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook_id ON webhook_deliveries(webhook_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_created ON webhook_deliveries(created_at);")

        # Synchronize suppressions with active subscribers (active subscribers must not be suppressed)
        await db.execute("""
            DELETE FROM suppressions
            WHERE email IN (SELECT email FROM subscribers WHERE status = 'active')
        """)
        await db.commit()

        # Seed initial default administrator account if no users exist
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            user_count_row = await cur.fetchone()
            user_count = user_count_row[0] if user_count_row else 0

        if user_count == 0:
            from app.auth import hash_password
            now = utc_now_iso()
            admin_id = f"usr_{uuid.uuid4().hex[:12]}"
            pwd_hash = hash_password(settings.DEFAULT_ADMIN_PASSWORD)
            await db.execute("""
                INSERT INTO users (
                    id, email, username, password_hash, name, role, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'admin', 'active', ?, ?)
            """, (
                admin_id,
                settings.DEFAULT_ADMIN_EMAIL.strip().lower(),
                settings.DEFAULT_ADMIN_USERNAME.strip().lower(),
                pwd_hash,
                "Administrator",
                now,
                now
            ))
        elif settings.DEFAULT_ADMIN_PASSWORD and settings.DEFAULT_ADMIN_PASSWORD != "admin123":
            from app.auth import hash_password
            now = utc_now_iso()
            pwd_hash = hash_password(settings.DEFAULT_ADMIN_PASSWORD)
            await db.execute("""
                UPDATE users
                SET password_hash = ?, updated_at = ?
                WHERE LOWER(email) = ? OR LOWER(username) = ?
            """, (
                pwd_hash,
                now,
                settings.DEFAULT_ADMIN_EMAIL.strip().lower(),
                settings.DEFAULT_ADMIN_USERNAME.strip().lower()
            ))

        await db.commit()
