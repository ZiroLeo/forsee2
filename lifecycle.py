import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database import get_connection
from pasar_guard import PasarGuardError, get_live, is_not_found
from dev.alerts import send_alert

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

# اشتراک فقط وقتی «حذف‌شده در پنل» ثبت می‌شود که در این تعداد چرخهٔ پیاپی
# پنل صریحاً «کاربر پیدا نشد» (404) برگردانده باشد. (جلوگیری از علامت‌گذاری
# اشتباه در اثر خطای لحظه‌ای یا پیکربندی اشتباه پنل)
NOT_FOUND_CONFIRMATIONS = 2

# حداقل فاصلهٔ هشدار دولوپر برای خطای نامطمئن هر پنل (ثانیه)
ALERT_THROTTLE_SECONDS = 3600

_not_found_strikes: Dict[int, int] = {}
_last_alert_at: Dict[str, float] = {}


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


def _is_user_not_found(error: BaseException) -> bool:
    """
    فقط وقتی True است که پنل صریحاً بگوید «این کاربر وجود ندارد».
    404 ناشی از لاگین/آدرس اشتباه پنل («Login failed (404)») حذف‌شده حساب نمی‌شود.
    """
    if not isinstance(error, PasarGuardError) or not is_not_found(error):
        return False
    return "user not found" in str(error).lower()


def _alert_throttled(key: str, level: str, message: str, **context: Any) -> None:
    """هشدار دولوپر با throttle تا در قطعی پنل، پیام انبوه ارسال نشود."""
    now = time.monotonic()
    last = _last_alert_at.get(key, 0.0)
    if last and now - last < ALERT_THROTTLE_SECONDS:
        return
    _last_alert_at[key] = now
    try:
        send_alert(level, message, **context)
    except Exception:
        logger.exception("Could not send lifecycle alert.")


# ============================================================
# همگام‌سازی یک اشتراک
# ============================================================

async def sync_single_subscription(
    panel_registry: Any,
    subscription: Any,
) -> bool:
    """
    اطلاعات یک اشتراک را از پنل دریافت و در دیتابیس به‌روزرسانی می‌کند.

    panel_registry برای سازگاری امضا نگه داشته شده؛ دسترسی به پنل از طریق
    لایهٔ سازگاری pasar_guard (get_live) و panel_id خود اشتراک انجام می‌شود.

    خروجی:
        True  = همگام‌سازی موفق
        False = اشتراک پیدا نشد یا همگام‌سازی ناموفق بود
    """
    sub = _row_to_dict(subscription)

    sub_id = sub.get("id")
    panel_id = sub.get("panel_id")
    config_name = sub.get("config_name")

    if not sub_id or not panel_id or not config_name:
        logger.warning(
            "Lifecycle skipped subscription with missing identifiers. subscription_id=%s",
            sub_id,
        )
        return False

    try:
        live = await get_live(sub)

        used_bytes = live.get("used_bytes")
        panel_state = live.get("state")

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

        # انقضا: None یعنی بدون انقضا یا نامشخص → تاریخ قبلی دیتابیس را پاک نمی‌کنیم.
        expires_at = _to_ts(live.get("expires_at"))

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

        # پاسخ سالم از پنل گرفته شد → شمارندهٔ «پیدا نشد» صفر می‌شود.
        _not_found_strikes.pop(int(sub_id), None)

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
                  AND COALESCE(state, 'active') != 'deleted'
                """,
                values,
            )
            conn.commit()

        logger.info("Subscription synchronized. subscription_id=%s", sub_id)
        return True

    except PasarGuardError as exc:
        if _is_user_not_found(exc):
            strikes = _not_found_strikes.get(int(sub_id), 0) + 1
            _not_found_strikes[int(sub_id)] = strikes

            if strikes < NOT_FOUND_CONFIRMATIONS:
                logger.warning(
                    "Panel user not found (%s/%s), awaiting confirmation. subscription_id=%s",
                    strikes, NOT_FOUND_CONFIRMATIONS, sub_id,
                )
                return False

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

            _not_found_strikes.pop(int(sub_id), None)
            logger.info("Panel client not found. subscription_id=%s", sub_id)
            return False

        # خطای نامطمئن (timeout / 5xx / مشکل لاگین / circuit-open / ...):
        # هیچ تغییری در دیتابیس نمی‌دهیم.
        logger.warning(
            "PasarGuard error while syncing subscription_id=%s panel_id=%s status=%s",
            sub_id, panel_id, getattr(exc, "status", None),
        )
        _alert_throttled(
            f"sync:{panel_id}",
            "WARNING",
            "همگام‌سازی اشتراک‌ها با پنل ناموفق بود (وضعیت دیتابیس تغییری نکرد)",
            exc=exc,
            panel_id=panel_id,
            subscription_id=sub_id,
        )
        return False

    except Exception as exc:
        logger.exception("Unexpected lifecycle sync error. subscription_id=%s", sub_id)
        _alert_throttled(
            "sync:unexpected",
            "ERROR",
            "خطای غیرمنتظره در همگام‌سازی چرخهٔ عمر اشتراک",
            exc=exc,
            subscription_id=sub_id,
        )
        return False


# ============================================================
# همگام‌سازی تمام اشتراک‌ها
# ============================================================

async def sync_all_active_subscriptions(panel_registry: Any = None) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE deleted_from_list_at IS NULL
              AND COALESCE(state, 'active') != 'deleted'
              AND panel_id IS NOT NULL
              AND config_name IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()

        orphan_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM subscriptions
            WHERE deleted_from_list_at IS NULL
              AND COALESCE(state, 'active') != 'deleted'
              AND (panel_id IS NULL OR config_name IS NULL)
            """
        ).fetchone()

    orphans = 0
    if orphan_row is not None:
        try:
            orphans = int(orphan_row[0])
        except (TypeError, ValueError, IndexError, KeyError):
            orphans = 0

    if orphans:
        logger.warning(
            "Lifecycle: %s subscription(s) without panel_id/config_name were skipped.",
            orphans,
        )
        _alert_throttled(
            "sync:orphans",
            "WARNING",
            "اشتراک‌هایی بدون panel_id در دیتابیس هستند و همگام‌سازی نمی‌شوند "
            "(نیاز به backfill)",
            count=orphans,
        )

    stats = {"total": len(rows), "synced": 0, "failed": 0, "skipped": orphans}

    for row in rows:
        result = await sync_single_subscription(panel_registry, row)
        if result:
            stats["synced"] += 1
        else:
            stats["failed"] += 1

        await asyncio.sleep(SYNC_INTERVAL_SECONDS)

    logger.info(
        "Lifecycle sync completed. total=%s synced=%s failed=%s skipped=%s",
        stats["total"], stats["synced"], stats["failed"], stats["skipped"],
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
    از دیتابیس پاک می‌شوند. (فقط رکورد دیتابیس؛ به پنل دست نمی‌زند.)
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

async def run_lifecycle_cycle(panel_registry: Any = None) -> dict:
    sync_stats = await sync_all_active_subscriptions(panel_registry)
    cleanup_stats = await process_expired_subscriptions()

    result = {"sync": sync_stats, "cleanup": cleanup_stats}
    logger.info("Lifecycle cycle completed: %s", result)
    return result
