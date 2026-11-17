import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from database import get_connection
from paxiva import PaxivaClient, PaxivaError


logger = logging.getLogger(__name__)

# ============================================================
# تنظیمات Lifecycle
# ============================================================

SYNC_INTERVAL_SECONDS = 0.5

# چند روز بعد از انقضا، اشتراک از فهرست کاربر حذف شود.
REMOVE_FROM_LIST_AFTER_DAYS = 7

# چند روز بعد از حذف از فهرست، رکورد دیتابیس پاک شود.
HARD_DELETE_AFTER_DAYS = 30

# وضعیت‌هایی که از پنل معتبر محسوب می‌شوند.
VALID_STATES = {
    "active",
    "expired",
    "depleted",
    "disabled",
    "deleted",
}


# ============================================================
# ابزارهای داخلی
# ============================================================

def _utc_now() -> int:
    """زمان فعلی به صورت timestamp عددی (ثانیه)."""
    return int(time.time())


def _to_ts(value: Any) -> Optional[int]:
    """
    تبدیل هر ورودی (ISO string، datetime، int) به timestamp عددی.
    برای استفاده در دیتابیس که از INTEGER استفاده می‌کند.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    return None


def _row_to_dict(row: Any) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _is_not_found_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    return status_code == 404


def _extract_client_data(client_data: Any) -> dict:
    if isinstance(client_data, dict):
        return client_data
    result = {}
    for field in ("used_bytes", "expires_at", "state"):
        if hasattr(client_data, field):
            result[field] = getattr(client_data, field)
    return result


# ============================================================
# همگام‌سازی یک اشتراک
# ============================================================

async def sync_single_subscription(
    paxiva: PaxivaClient,
    subscription: Any,
) -> bool:
    """
    اطلاعات یک اشتراک را از پنل دریافت و در دیتابیس به‌روزرسانی می‌کند.

    خروجی:
        True  = همگام‌سازی موفق
        False = اشتراک پیدا نشد یا همگام‌سازی ناموفق بود
    """

    sub = _row_to_dict(subscription)
    sub_id = sub.get("id")
    client_id = sub.get("paxiva_client_id")

    if not sub_id or not client_id:
        logger.warning("Lifecycle skipped subscription with missing identifiers.")
        return False

    try:
        client_data = await paxiva.get_client(client_id)
        data = _extract_client_data(client_data)

        used_bytes = data.get("used_bytes")
        expires_at_raw = data.get("expires_at")
        panel_state = data.get("state")

        # حجم مصرف‌شده
        if used_bytes is not None:
            try:
                used_bytes = max(0, int(used_bytes))
            except (TypeError, ValueError):
                logger.warning("Invalid used_bytes for subscription_id=%s", sub_id)
                used_bytes = None

        # وضعیت
        if panel_state is not None:
            panel_state = str(panel_state).lower().strip()
            if panel_state not in VALID_STATES:
                logger.warning("Unknown panel state for subscription_id=%s", sub_id)
                panel_state = None

        # انقضا: تبدیل به timestamp عددی
        expires_at = _to_ts(expires_at_raw)

        updates = []
        values = []

        if used_bytes is not None:
            updates.append("used_bytes = ?")
            values.append(used_bytes)

        if expires_at is not None:
            updates.append("expires_at = ?")
            values.append(expires_at)

        if panel_state is not None:
            updates.append("state = ?")
            values.append(panel_state)

        if not updates:
            logger.info("No lifecycle data to update for subscription_id=%s", sub_id)
            return True

        updates.append("updated_at = ?")
        values.append(_utc_now())

        values.append(sub_id)

        with get_connection() as conn:
            conn.execute(
                f"""
                UPDATE subscriptions
                SET {", ".join(updates)}
                WHERE id = ?
                """,
                values,
            )
            conn.commit()

        logger.info("Subscription synchronized. subscription_id=%s", sub_id)
        return True

    except PaxivaError as exc:
        if _is_not_found_error(exc):
            with get_connection() as conn:
                conn.execute(
                    """
                    UPDATE subscriptions
                    SET state = 'deleted', updated_at = ?
                    WHERE id = ?
                    """,
                    (_utc_now(), sub_id),
                )
                conn.commit()

            logger.info("Panel client not found. subscription_id=%s", sub_id)
            return False

        logger.exception("Paxiva error while syncing subscription_id=%s", sub_id)
        return False

    except Exception:
        logger.exception("Unexpected lifecycle sync error. subscription_id=%s", sub_id)
        return False


# ============================================================
# همگام‌سازی تمام اشتراک‌ها
# ============================================================

async def sync_all_active_subscriptions(paxiva: PaxivaClient) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE deleted_from_list_at IS NULL
              AND COALESCE(state, 'active') != 'deleted'
              AND paxiva_client_id IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()

    stats = {"total": len(rows), "synced": 0, "failed": 0}

    for row in rows:
        result = await sync_single_subscription(paxiva, row)
        if result:
            stats["synced"] += 1
        else:
            stats["failed"] += 1
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)

    logger.info(
        "Lifecycle sync completed. total=%s synced=%s failed=%s",
        stats["total"], stats["synced"], stats["failed"],
    )
    return stats


# ============================================================
# حذف اشتراک‌های منقضی‌شده از فهرست کاربر
# ============================================================

async def remove_old_subscriptions_from_list() -> int:
    """
    اشتراک‌هایی که بیش از ۷ روز از انقضای آن‌ها گذشته،
    از فهرست کاربر حذف می‌شوند.
    """

    now = _utc_now()
    cutoff = now - (REMOVE_FROM_LIST_AFTER_DAYS * 86400)

    removed_count = 0

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, expires_at
            FROM subscriptions
            WHERE deleted_from_list_at IS NULL
              AND state IN ('expired', 'depleted')
            """
        ).fetchall()

        for row in rows:
            sub = _row_to_dict(row)
            expires_at = _to_ts(sub.get("expires_at"))

            if expires_at is None or expires_at > cutoff:
                continue

            conn.execute(
                """
                UPDATE subscriptions
                SET deleted_from_list_at = ?, updated_at = ?
                WHERE id = ?
                  AND deleted_from_list_at IS NULL
                """,
                (now, now, sub["id"]),
            )
            removed_count += 1

        conn.commit()

    if removed_count:
        logger.info("Old subscriptions removed from user lists. count=%s", removed_count)

    return removed_count


# ============================================================
# حذف نهایی رکوردهای قدیمی
# ============================================================

async def hard_delete_old_subscriptions() -> int:
    """
    رکوردهایی که بیش از ۳۰ روز از حذف آن‌ها از فهرست گذشته،
    از دیتابیس پاک می‌شوند.
    """

    now = _utc_now()
    cutoff = now - (HARD_DELETE_AFTER_DAYS * 86400)

    deleted_count = 0

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, deleted_from_list_at
            FROM subscriptions
            WHERE deleted_from_list_at IS NOT NULL
            """
        ).fetchall()

        for row in rows:
            sub = _row_to_dict(row)
            deleted_at = _to_ts(sub.get("deleted_from_list_at"))

            if deleted_at is None or deleted_at > cutoff:
                continue

            conn.execute(
                """
                DELETE FROM subscriptions
                WHERE id = ?
                  AND deleted_from_list_at IS NOT NULL
                """,
                (sub["id"],),
            )
            deleted_count += 1

        conn.commit()

    if deleted_count:
        logger.info("Old subscription records permanently deleted. count=%s", deleted_count)

    return deleted_count


# ============================================================
# پردازش چرخه عمر اشتراک‌ها
# ============================================================

async def process_expired_subscriptions() -> dict:
    removed = await remove_old_subscriptions_from_list()
    deleted = await hard_delete_old_subscriptions()
    return {
        "removed_from_list": removed,
        "hard_deleted": deleted,
    }


# ============================================================
# اجرای یک چرخه کامل
# ============================================================

async def run_lifecycle_cycle(paxiva: PaxivaClient) -> dict:
    sync_stats = await sync_all_active_subscriptions(paxiva)
    cleanup_stats = await process_expired_subscriptions()

    result = {"sync": sync_stats, "cleanup": cleanup_stats}
    logger.info("Lifecycle cycle completed: %s", result)
    return result