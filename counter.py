import logging
from typing import Optional

from database import (
    reserve_config_number,
    release_config_number,
    get_setting,
    set_setting
)

logger = logging.getLogger(__name__)


# ============================================================
# ساخت نام کانفیگ
# ============================================================

CONFIG_PREFIX = "iMan"
TEST_CONFIG_PREFIX = "iManTest"


def make_config_name(number: int, is_test: bool = False) -> str:
    """
    ساخت نام کانفیگ بر اساس شماره و نوع.
    خروجی: iMan-N یا iManTest-N
    """
    if number <= 0:
        raise ValueError("Config number must be positive")
    prefix = TEST_CONFIG_PREFIX if is_test else CONFIG_PREFIX
    return f"{prefix}-{number}"


# ============================================================
# رزرو شماره جدید (اتمیک)
# ============================================================

def reserve_new_config_number(is_test: bool = False) -> int:
    """
    رزرو یک شماره جدید به صورت اتمیک.
    از تابع دیتابیس استفاده می‌کند که BEGIN IMMEDIATE دارد.
    """
    try:
        number = reserve_config_number(is_test=is_test)
        logger.info(f"Reserved config number: {number} (is_test={is_test})")
        return number
    except Exception as e:
        logger.exception(f"Failed to reserve config number (is_test={is_test}): {e}")
        raise


def reserve_new_test_number() -> int:
    """
    رزرو شماره جدید برای اکانت تست.
    """
    return reserve_new_config_number(is_test=True)


# ============================================================
# آزادسازی شماره (در صورت خطا)
# ============================================================

def release_reserved_number(number: int, is_test: bool = False) -> bool:
    """
    آزادسازی شماره در صورت شکست در ساخت کانفیگ.
    این کار باعث می‌شود شماره‌ها هدر نروند.
    """
    if number <= 0:
        return False
    try:
        success = release_config_number(number, is_test=is_test)
        if success:
            logger.info(f"Released config number: {number} (is_test={is_test})")
        else:
            logger.warning(f"Could not release config number: {number} (is_test={is_test})")
        return success
    except Exception as e:
        logger.exception(f"Failed to release config number {number}: {e}")
        return False


# ============================================================
# همگام‌سازی شمارنده (Recovery)
# ============================================================

def sync_counter_with_database(is_test: bool = False) -> int:
    """
    همگام‌سازی شمارنده با بالاترین شماره موجود در دیتابیس.
    این تابع برای مواقعی است که شمارنده به هر دلیلی (کرش، دستکاری) عقب افتاده باشد.
    خروجی: شماره فعلی همگام‌شده.
    """
    from database import get_connection

    key = "test_counter" if is_test else "config_counter"
    with get_connection() as conn:
        if is_test:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(config_number), 0) AS mx FROM subscriptions WHERE is_trial = 1"
            ).fetchone()
        else:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(config_number), 0) AS mx FROM subscriptions WHERE is_trial = 0"
            ).fetchone()

        real_max = int(max_row["mx"] or 0)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        current = int(row["value"]) if row else 0

        if current < real_max:
            set_setting(key, str(real_max))
            logger.warning(
                f"Counter '{key}' was behind database (current={current}, real_max={real_max}). "
                f"Synced to {real_max}."
            )
            return real_max
        return current


# ============================================================
# تست سلامت شمارنده (برای Dev Panel)
# ============================================================

def get_counter_status() -> dict:
    """
    دریافت وضعیت شمارنده‌ها برای نمایش در Dev Panel.
    """
    config_counter = get_setting("config_counter", "0")
    test_counter = get_setting("test_counter", "0")
    try:
        config_val = int(config_counter)
    except (ValueError, TypeError):
        config_val = 0
    try:
        test_val = int(test_counter)
    except (ValueError, TypeError):
        test_val = 0
    return {
        "config_counter": config_val,
        "test_counter": test_val,
    }