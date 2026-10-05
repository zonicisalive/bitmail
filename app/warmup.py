"""
Automated IP & Domain Warmup Engine with Multi-Relay Rotation.
Provides ramp-up curve generators, provider-balanced recipient slicing,
multi-relay rotation pool management with failover & cooldown,
and safety circuit breakers (bounce rate and failure threshold auto-brakes).
"""

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.auth import decrypt_credential
from app.db import get_db, utc_now_iso
from app.models import WarmupCurveStrategy

logger = logging.getLogger("bitmail.warmup")


# ==============================================================================
# 1. Warmup Curve Generators
# ==============================================================================
class WarmupScheduleCurves:
    """
    Ramp-up schedule curves designed to build positive sender reputation
    with Gmail, Outlook, Yahoo, and corporate spam filtering systems.
    """

    CONSERVATIVE_30_CAPS = [
        50, 75, 110, 160, 240, 350, 500, 700, 950, 1300,
        1800, 2500, 3400, 4600, 6200, 8200, 10800, 14000, 18000, 23000,
        29000, 36000, 44000, 52000, 60000, 70000, 80000, 90000, 100000, 120000
    ]

    STANDARD_14_CAPS = [
        100, 200, 400, 750, 1200, 1800, 2700, 3800, 5200, 7000,
        9500, 12500, 16000, 20000
    ]

    AGGRESSIVE_7_CAPS = [
        250, 500, 1000, 2500, 5000, 10000, 20000
    ]

    @classmethod
    def get_curve_caps(
        cls,
        strategy: str,
        total_recipients: int,
        custom_days: int = 14,
        custom_start_cap: int = 50
    ) -> List[int]:
        """
        Compute daily recipient caps for each day of the warmup schedule.
        """
        strat = strategy.lower().strip()
        if strat in ["conservative_30", "conservative"]:
            base_caps = cls.CONSERVATIVE_30_CAPS.copy()
        elif strat in ["standard_14", "standard"]:
            base_caps = cls.STANDARD_14_CAPS.copy()
        elif strat in ["aggressive_7", "aggressive"]:
            base_caps = cls.AGGRESSIVE_7_CAPS.copy()
        elif strat == "custom":
            days = max(2, min(90, custom_days))
            start_cap = max(5, custom_start_cap)
            if total_recipients <= start_cap:
                return [total_recipients]
            if total_recipients <= start_cap * days:
                base = total_recipients // days
                rem = total_recipients % days
                return [base + (1 if i < rem else 0) for i in range(days)]

            low = 1.0001
            high = 10.0
            for _ in range(35):
                mid = (low + high) / 2.0
                val = start_cap * (math.pow(mid, days) - 1.0) / (mid - 1.0)
                if val < total_recipients:
                    low = mid
                else:
                    high = mid
            r = (low + high) / 2.0

            raw = [round(start_cap * math.pow(r, i)) for i in range(days)]
            raw[0] = start_cap
            for i in range(1, days):
                if raw[i] < raw[i - 1]:
                    raw[i] = raw[i - 1]
            diff = total_recipients - sum(raw)
            raw[-1] += diff
            return raw
        else:
            base_caps = cls.STANDARD_14_CAPS.copy()

        # Slice or scale to fit total_recipients
        daily_allocations: List[int] = []
        remaining = total_recipients

        for cap in base_caps:
            if remaining <= 0:
                break
            allocation = min(cap, remaining)
            daily_allocations.append(allocation)
            remaining -= allocation

        # If there are still remaining recipients after base curve, append remainder to last day
        if remaining > 0:
            if daily_allocations:
                daily_allocations[-1] += remaining
            else:
                daily_allocations.append(remaining)

        return daily_allocations


# ==============================================================================
# 2. Warmup Slicer & Provider Balancer
# ==============================================================================
class WarmupSlicer:
    """
    Partitions recipient contacts into day-by-day scheduled slices,
    balancing mailbox provider categories (Gmail, Microsoft, Yahoo, Corporate)
    so that traffic to any individual ISP is throttled evenly across days.
    """

    @staticmethod
    def identify_provider(email_str: str) -> str:
        """Categorize email domain into primary provider bucket."""
        domain = email_str.split("@")[-1].strip().lower() if "@" in email_str else ""
        if any(d in domain for d in ["gmail.com", "googlemail.com"]):
            return "gmail"
        if any(d in domain for d in ["outlook.com", "hotmail.com", "live.com", "msn.com", "office365.com"]):
            return "microsoft"
        if any(d in domain for d in ["yahoo.com", "ymail.com", "aol.com"]):
            return "yahoo"
        if any(d in domain for d in ["icloud.com", "me.com", "mac.com"]):
            return "apple"
        return "corporate"

    @classmethod
    def balance_and_slice(
        cls,
        recipients: List[Any],
        daily_caps: List[int],
        start_datetime: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Partition recipients across days using provider round-robin balancing.
        """
        if not start_datetime:
            start_datetime = datetime.now(timezone.utc)
        elif start_datetime.tzinfo is None:
            start_datetime = start_datetime.replace(tzinfo=timezone.utc)

        # Bucket recipients by provider
        buckets: Dict[str, List[Any]] = {
            "gmail": [],
            "microsoft": [],
            "yahoo": [],
            "apple": [],
            "corporate": []
        }
        for item in recipients:
            email_val = item.get("email") if isinstance(item, dict) else (getattr(item, "email", str(item)))
            provider = cls.identify_provider(str(email_val))
            buckets[provider].append(item)

        # Interleave buckets into a balanced flat list
        interleaved: List[Any] = []
        bucket_keys = ["gmail", "microsoft", "yahoo", "apple", "corporate"]
        idx = 0
        total_items = len(recipients)

        while len(interleaved) < total_items:
            for key in bucket_keys:
                if idx < len(buckets[key]):
                    interleaved.append(buckets[key][idx])
            idx += 1

        # Slice according to daily caps
        slices: List[Dict[str, Any]] = []
        cursor = 0

        for day_idx, cap in enumerate(daily_caps, start=1):
            slice_items = interleaved[cursor : cursor + cap]
            cursor += cap
            if not slice_items:
                break

            scheduled_for = start_datetime + timedelta(days=day_idx - 1)
            
            # Count provider distribution in this slice
            dist: Dict[str, int] = {}
            for si in slice_items:
                em = si.get("email") if isinstance(si, dict) else (getattr(si, "email", str(si)))
                prov = cls.identify_provider(str(em))
                dist[prov] = dist.get(prov, 0) + 1

            slices.append({
                "day_number": day_idx,
                "scheduled_for": scheduled_for.strftime("%Y-%m-%d %H:%M:%S"),
                "target_count": len(slice_items),
                "recipients": slice_items,
                "provider_distribution": dist,
            })

        return slices


# ==============================================================================
# 3. Multi-Relay Rotation & Failover Pool Manager
# ==============================================================================
class RelayPoolManager:
    """
    Manages an active pool of SMTP relays with round-robin rotation,
    automatic failover, and cooldown tracking when temporary errors (421/451) occur.
    """

    _round_robin_counter: int = 0

    @classmethod
    async def get_pool_relays(cls) -> List[Dict[str, Any]]:
        """Fetch all relays designated as in the warmup pool."""
        async with get_db() as db:
            query = """
                SELECT s.*, 
                       COALESCE(r.in_pool, s.in_relay_pool, 0) as is_in_pool,
                       COALESCE(r.current_day, s.warmup_day, 1) as pool_warmup_day,
                       COALESCE(r.daily_sends, 0) as pool_daily_sends,
                       COALESCE(r.daily_failures, 0) as pool_daily_failures,
                       r.cooldown_until,
                       r.last_error
                FROM smtp_configs s
                LEFT JOIN relay_pool_stats r ON s.id = r.smtp_config_id
                WHERE s.is_active = 1
                ORDER BY s.is_default DESC, s.created_at ASC
            """
            async with db.execute(query) as cur:
                rows = await cur.fetchall()

        now_iso = utc_now_iso()
        relays = []
        for r in rows:
            is_cooling_down = False
            if r["cooldown_until"] and r["cooldown_until"] > now_iso:
                is_cooling_down = True

            relays.append({
                "id": r["id"],
                "name": r["name"],
                "host": r["host"],
                "port": r["port"],
                "username": r["username"],
                "password": decrypt_credential(r["password"]) if r["password"] else "",
                "use_tls": bool(r["use_tls"]),
                "use_ssl": bool(r["use_ssl"]),
                "rate_limit_per_second": r["rate_limit_per_second"] or 25,
                "daily_quota": r["daily_quota"] or 50000,
                "in_pool": bool(r["is_in_pool"]),
                "current_day": r["pool_warmup_day"],
                "daily_sends": r["pool_daily_sends"],
                "daily_failures": r["pool_daily_failures"],
                "is_cooling_down": is_cooling_down,
                "cooldown_until": r["cooldown_until"],
                "last_error": r["last_error"],
            })
        return relays

    @classmethod
    async def select_relay(
        cls,
        rotation_mode: str = "round_robin",
        pool_relay_ids: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Select an available healthy SMTP relay from the active rotation pool.
        Filters out relays currently in cooldown.
        """
        all_relays = await cls.get_pool_relays()
        pool = [
            r for r in all_relays
            if (r["in_pool"] or (pool_relay_ids and r["id"] in pool_relay_ids))
            and not r["is_cooling_down"]
        ]

        # If all pool relays are in cooldown, fall back to any available active relay
        if not pool:
            pool = [r for r in all_relays if not r["is_cooling_down"]]
        if not pool:
            pool = all_relays  # Emergency fallback

        if not pool:
            return None

        if rotation_mode == "round_robin" or rotation_mode == "failover":
            cls._round_robin_counter += 1
            idx = (cls._round_robin_counter - 1) % len(pool)
            return pool[idx]

        # Default fallback
        return pool[0]

    @classmethod
    async def mark_success(cls, smtp_config_id: str) -> None:
        """Record successful transmission on this relay."""
        now = utc_now_iso()
        async with get_db() as db:
            await db.execute("""
                INSERT INTO relay_pool_stats (
                    id, smtp_config_id, in_pool, daily_sends, daily_failures, last_used_at, created_at, updated_at
                ) VALUES (?, ?, 1, 1, 0, ?, ?, ?)
                ON CONFLICT(smtp_config_id) DO UPDATE SET
                    daily_sends = daily_sends + 1,
                    last_used_at = excluded.last_used_at,
                    updated_at = excluded.updated_at
            """, (f"stat_{uuid.uuid4().hex[:8]}", smtp_config_id, now, now, now))
            await db.commit()

    @classmethod
    async def mark_failure(
        cls,
        smtp_config_id: str,
        error_msg: str,
        cooldown_seconds: int = 900
    ) -> None:
        """
        Record delivery failure or rate-limiting on this relay.
        Places relay into cooldown if greylisting or rate-limiting is detected.
        """
        err_lower = error_msg.lower()
        is_temp_block = any(code in err_lower for code in ["421", "451", "rate limit", "too many connections", "try again later", "timed out"])
        
        cooldown_until = None
        if is_temp_block:
            cooldown_until = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        now = utc_now_iso()
        async with get_db() as db:
            await db.execute("""
                INSERT INTO relay_pool_stats (
                    id, smtp_config_id, in_pool, daily_sends, daily_failures, cooldown_until, last_error, last_used_at, created_at, updated_at
                ) VALUES (?, ?, 1, 0, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(smtp_config_id) DO UPDATE SET
                    daily_failures = daily_failures + 1,
                    cooldown_until = COALESCE(excluded.cooldown_until, relay_pool_stats.cooldown_until),
                    last_error = excluded.last_error,
                    last_used_at = excluded.last_used_at,
                    updated_at = excluded.updated_at
            """, (f"stat_{uuid.uuid4().hex[:8]}", smtp_config_id, cooldown_until, error_msg[:255], now, now, now))
            await db.commit()

    @classmethod
    async def toggle_relay_pool(cls, smtp_config_id: str, in_pool: bool) -> bool:
        """Add or remove an SMTP relay profile from the rotation pool."""
        now = utc_now_iso()
        val = 1 if in_pool else 0
        async with get_db() as db:
            await db.execute("UPDATE smtp_configs SET in_relay_pool = ?, updated_at = ? WHERE id = ?", (val, now, smtp_config_id))
            await db.execute("""
                INSERT INTO relay_pool_stats (
                    id, smtp_config_id, in_pool, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(smtp_config_id) DO UPDATE SET
                    in_pool = excluded.in_pool,
                    updated_at = excluded.updated_at
            """, (f"stat_{uuid.uuid4().hex[:8]}", smtp_config_id, val, now, now))
            await db.commit()
        return True


# ==============================================================================
# 4. Safety Circuit Breaker
# ==============================================================================
class WarmupCircuitBreaker:
    """
    Evaluates deliverability telemetry after daily slices.
    Halts ramp-up if bounce rates exceed 2% or relay failure rates exceed 5%.
    """

    MAX_BOUNCE_RATE = 0.02   # 2% standard
    MAX_FAILURE_RATE = 0.05  # 5% relay hard failure

    @classmethod
    async def check_and_apply(cls, schedule_id: str, slice_id: str) -> Tuple[bool, str]:
        """
        Evaluate slice performance and auto-pause schedule if thresholds breached.
        Returns (is_healthy, status_message).
        """
        async with get_db() as db:
            async with db.execute(
                "SELECT * FROM warmup_slices WHERE id = ?", (slice_id,)
            ) as cur:
                slice_row = await cur.fetchone()

            if not slice_row:
                return True, "Slice not found"

            dispatched = slice_row["dispatched_count"]
            bounces = slice_row["bounce_count"]
            failures = slice_row["failure_count"]

            if dispatched < 10:
                return True, "Low volume slice; within safe limits"

            bounce_rate = bounces / dispatched
            failure_rate = failures / dispatched

            if bounce_rate > cls.MAX_BOUNCE_RATE:
                msg = f"Safety Circuit Breaker: Bounce rate {bounce_rate * 100:.1f}% exceeded 2.0% threshold. Pausing schedule."
                await db.execute(
                    "UPDATE warmup_schedules SET status = 'paused', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), schedule_id)
                )
                await db.commit()
                logger.warning("[WarmupCircuitBreaker] %s (Schedule: %s)", msg, schedule_id)
                return False, msg

            if failure_rate > cls.MAX_FAILURE_RATE:
                msg = f"Safety Circuit Breaker: Relay failure rate {failure_rate * 100:.1f}% exceeded 5.0% threshold. Pausing schedule."
                await db.execute(
                    "UPDATE warmup_schedules SET status = 'paused', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), schedule_id)
                )
                await db.commit()
                logger.warning("[WarmupCircuitBreaker] %s (Schedule: %s)", msg, schedule_id)
                return False, msg

            return True, "Slice deliverability metrics healthy"
