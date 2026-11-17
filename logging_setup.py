import logging
import logging.handlers
import queue
import re
import sys
from pathlib import Path
from typing import Optional

from config import DATA_DIR, DEV_ID

# ============================================================
# ثابت‌ها
# ============================================================

LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bot.log"

# سطح لاگ سفارشی برای رویدادهای امنیتی (بالاتر از CRITICAL)
SECURITY_LEVEL = 60
logging.addLevelName(SECURITY_LEVEL, "SECURITY")

# ============================================================
# فیلتر حذف اطلاعات حساس (اولین سد دفاعی)
# ============================================================

class SensitiveDataFilter(logging.Filter):
    """
    فیلتر حذف/ماسک اطلاعات حساس از لاگ‌ها.
    - توکن‌های Bearer
    - شماره کارت ۱۶ رقمی
    - کلمات کلیدی حساس (token, password, secret, api_key)
    """
    # الگوی توکن Bearer (rk_... یا هر توکن طولانی)
    _bearer_pattern = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+", re.IGNORECASE)
    # الگوی شماره کارت ۱۶ رقمی (با یا بدون فاصله)
    _card_pattern = re.compile(r"\b(?:\d[ -]*?){16}\b")
    # الگوی کلمات کلیدی حساس
    _sensitive_keywords = re.compile(
        r"(token|password|secret|api_key|apikey|authorization)\s*[:=]\s*\S+",
        re.IGNORECASE
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.msg:
            return True
        try:
            msg = str(record.msg)
            # ماسک کردن توکن Bearer
            msg = self._bearer_pattern.sub("Bearer [REDACTED]", msg)
            # ماسک کردن شماره کارت
            msg = self._card_pattern.sub("[CARD REDACTED]", msg)
            # ماسک کردن کلمات کلیدی حساس
            msg = self._sensitive_keywords.sub(r"\1: [REDACTED]", msg)
            record.msg = msg
        except Exception:
            # در صورت خطا در فیلتر، پیام اصلی را حفظ می‌کنیم (fail-safe)
            pass
        return True


# ============================================================
# فرمت‌کننده لاگ (شامل اطلاعات بافت)
# ============================================================

class ContextFormatter(logging.Formatter):
    """
    فرمت‌کننده لاگ با اطلاعات بافت (user_id, event_type).
    """
    def format(self, record: logging.LogRecord) -> str:
        # اضافه کردن فیلدهای پیش‌فرض اگر وجود ندارند
        record.user_id = getattr(record, "user_id", "-")
        record.event_type = getattr(record, "event_type", "-")
        return super().format(record)


# ============================================================
# پیکربندی اصلی لاگ
# ============================================================

# صف برای جدا کردن عملیات لاگ از حلقه اصلی
_log_queue: queue.Queue = queue.Queue(-1)

def setup_logging(log_level: int = logging.INFO) -> None:
    """
    پیکربندی لاگ‌ها با QueueHandler و QueueListener.
    - تمام لاگ‌ها در صف قرار می‌گیرند.
    - یک ترد جداگانه آن‌ها را در فایل و کنسول می‌نویسد.
    - فیلتر اطلاعات حساس روی همه اعمال می‌شود.
    """
    # فرمت لاگ
    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)s | "
        "user=%(user_id)s | event=%(event_type)s | %(message)s"
    )
    formatter = ContextFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # فیلتر حساس
    sensitive_filter = SensitiveDataFilter()

    # --- Handler فایل (با چرخش روزانه و محدودیت حجم) ---
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # ۱۰ مگابایت
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(sensitive_filter)

    # --- Handler کنسول (برای دیباگ) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)

    # --- QueueListener: اجرای Handlerها در ترد جداگانه ---
    listener = logging.handlers.QueueListener(
        _log_queue, file_handler, console_handler, respect_handler_level=True
    )
    listener.start()

    # --- QueueHandler: قرار دادن لاگ‌ها در صف (در حلقه اصلی) ---
    queue_handler = logging.handlers.QueueHandler(_log_queue)
    queue_handler.setFormatter(formatter)
    queue_handler.addFilter(sensitive_filter)

    # --- پیکربندی root logger ---
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # حذف Handlerهای قبلی (برای جلوگیری از تکرار در ری‌استارت)
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    root_logger.addHandler(queue_handler)

    # کاهش سطح لاگ برای کتابخانه‌های خارجی (کاهش نویز)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("sqlite3").setLevel(logging.WARNING)

    # ثبت اینکه لاگ‌گیری راه‌اندازی شد
    logging.getLogger(__name__).info("Logging system initialized.")


# ============================================================
# توابع کمکی برای لاگ رویدادهای امنیتی و کاربر
# ============================================================

def log_security_event(
    event_type: str,
    message: str,
    user_id: Optional[int] = None,
    extra: Optional[dict] = None
) -> None:
    """
    ثبت یک رویداد امنیتی (سطح SECURITY).
    این رویدادها در Dev Panel قابل مشاهده و فیلتر هستند.
    """
    logger = logging.getLogger("security")
    logger.log(
        SECURITY_LEVEL,
        message,
        extra={"user_id": user_id or "-", "event_type": event_type, **(extra or {})}
    )


def log_user_action(
    user_id: int,
    action: str,
    detail: Optional[str] = None
) -> None:
    """
    ثبت یک اقدام کاربر (سطح INFO) برای ردیابی.
    """
    logger = logging.getLogger("user_action")
    msg = f"{action}"
    if detail:
        msg += f" | {detail}"
    logger.info(msg, extra={"user_id": user_id, "event_type": action})


def log_system_event(
    level: int,
    event_type: str,
    message: str,
    extra: Optional[dict] = None
) -> None:
    """
    ثبت یک رویداد سیستمی (خطا، هشدار، اتصال به پنل).
    """
    logger = logging.getLogger("system")
    logger.log(level, message, extra={"event_type": event_type, **(extra or {})})