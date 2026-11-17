"""
dev/alerts.py
-------------
سیستم هشدار دولوپر (سازگار با کد قبلی: send_alert(level, text) هنوز کار می‌کند).

قابلیت‌های جدید:
* هر هشدار خودکار شامل: شناسهٔ یکتا، زمان (تهران)، محل فراخوانی (فایل:تابع:خط)،
  Traceback کامل (اگر داخل except صدا زده شود یا exc= بدهید)، و فیلدهای context.
* پارامترهای اختیاری: send_alert("ERROR", "متن", exc=e, user_id=123, order="x", ...)
* متن‌های طولانی بریده نمی‌شوند: Traceback کامل به‌صورت فایل .txt ارسال می‌شود.
* پنهان‌سازی خودکار توکن/رمز/کلید در متن.
* جلوگیری از اسپم: هشدار تکراری در ۶۰ ثانیه فقط یک‌بار ارسال و تعداد تکرار گزارش می‌شود.
* مدیریت Flood Control تلگرام (RetryAfter) و صف محدود (بدون رشد بی‌نهایت حافظه).
* install_log_handler(): هر logger.error / logger.exception در کل پروژه هم هشدار می‌شود.
* describe_update(update): جزئیات کاربر/چت/دکمه/متن برای هندلر خطای سراسری.
"""

import asyncio
import html
import io
import logging
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile

from config import DEV_ID

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    _TZ = timezone.utc

logger = logging.getLogger(__name__)

_QUEUE_MAX = 1000
_alert_queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
_alert_bot: Optional[Bot] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

_DEDUP_WINDOW = 60.0
_dedup: dict[str, list] = {}          # key -> [last_sent_ts, suppressed_count]
_dropped = 0                          # هشدارهای ازدست‌رفته به‌دلیل پر بودن صف

_LEVEL_EMOJI = {"SECURITY": "🚨", "CRITICAL": "🔥", "ERROR": "⚠️", "WARNING": "🟡", "INFO": "📢"}
_MAX_MESSAGE = 3900                   # حد امن پیام تلگرام (۴۰۹۶)

# ------------------------------------------------------------
# پنهان‌سازی اطلاعات حساس
# ------------------------------------------------------------
_SECRET_PATTERNS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<BOT_TOKEN>"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|authorization|bearer)\b(\s*[=:]\s*|\s+)([^\s,;'\"]+)"),
     r"\1\2<hidden>"),
]


def _mask(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _esc(value: Any) -> str:
    return html.escape(_mask(str(value)), quote=False)


# ------------------------------------------------------------
# راه‌اندازی
# ------------------------------------------------------------

def setup_alerts(bot: Bot) -> None:
    """ذخیره‌ی bot و event loop برای ارسال هشدارها (در main صدا زده شود)."""
    global _alert_bot, _loop
    _alert_bot = bot
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None


# ------------------------------------------------------------
# ساخت هشدار
# ------------------------------------------------------------

def _caller_location(depth: int = 2) -> str:
    """محل واقعی فراخوانی (فایل:تابع:خط) خارج از همین ماژول."""
    try:
        frame = sys._getframe(depth)
        here = __file__
        while frame and frame.f_code.co_filename == here:
            frame = frame.f_back
        if frame:
            name = frame.f_code.co_filename.replace("\\", "/").split("/")[-1]
            return f"{name}:{frame.f_code.co_name}:{frame.f_lineno}"
    except Exception:
        pass
    return "unknown"


def _format_traceback(exc: Optional[BaseException]) -> str:
    if exc is None:
        info = sys.exc_info()
        exc = info[1]
        if exc is None:
            return ""
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        return repr(exc)


def send_alert(level: str, text: str, *, exc: Optional[BaseException] = None,
               user_id: Optional[int] = None, **context: Any) -> None:
    """
    افزودن هشدار به صف (غیرهمزمان و بدون بلاک).
    سطح‌ها: SECURITY, CRITICAL, ERROR, WARNING, INFO
    نمونه:  send_alert("ERROR", "کسر کیف پول ناموفق", exc=e, user_id=uid, order=order_key)
    داخل بلاک except بدون exc= هم Traceback خودکار گرفته می‌شود.
    """
    try:
        item = {
            "id": uuid.uuid4().hex[:6].upper(),
            "level": str(level).upper(),
            "text": str(text),
            "where": _caller_location(),
            "time": datetime.now(_TZ),
            "tb": _format_traceback(exc),
            "user_id": user_id,
            "context": {k: v for k, v in context.items() if v is not None},
        }
        _enqueue(item)
    except Exception:
        logger.exception("Could not build alert")


def _enqueue(item: dict) -> None:
    global _dropped

    def _put():
        global _dropped
        try:
            _alert_queue.put_nowait(item)
        except asyncio.QueueFull:
            _dropped += 1

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if _loop is not None and running is not _loop and _loop.is_running():
        _loop.call_soon_threadsafe(_put)   # فراخوانی از thread دیگر
    else:
        _put()


# ------------------------------------------------------------
# ارسال
# ------------------------------------------------------------

def _dedup_note(item: dict) -> str:
    """اگر همین هشدار اخیراً ارسال شده، None برمی‌گرداند (سرکوب)؛ وگرنه یادداشت تکرارهای قبلی."""
    key = f"{item['level']}|{item['where']}|{item['text'][:200]}"
    now = time.monotonic()
    entry = _dedup.get(key)
    if entry and now - entry[0] < _DEDUP_WINDOW:
        entry[1] += 1
        return None  # type: ignore[return-value]
    suppressed = entry[1] if entry else 0
    _dedup[key] = [now, 0]
    if len(_dedup) > 500:  # جلوگیری از رشد حافظه
        for k in [k for k, v in _dedup.items() if now - v[0] > _DEDUP_WINDOW * 5]:
            _dedup.pop(k, None)
    return f"\n🔁 <i>{suppressed} بار دیگر در دقیقه‌های قبل تکرار شده بود.</i>" if suppressed else ""


def _render(item: dict, repeat_note: str) -> tuple[str, str]:
    """(پیام اصلی HTML، Traceback کامل)"""
    emoji = _LEVEL_EMOJI.get(item["level"], "📢")
    lines = [
        f"{emoji} <b>{_esc(item['level'])}</b> · <code>#{item['id']}</code>",
        f"🕐 {item['time'].strftime('%Y-%m-%d %H:%M:%S')} (تهران)",
        f"📍 <code>{_esc(item['where'])}</code>",
    ]
    if item.get("user_id"):
        lines.append(f"👤 کاربر: <code>{_esc(item['user_id'])}</code>")
    lines.append("")
    lines.append(f"<pre>{_esc(item['text'][:2000])}</pre>")

    if item["context"]:
        lines.append("🧩 <b>جزئیات:</b>")
        for key, value in item["context"].items():
            lines.append(f"• <b>{_esc(key)}</b>: <code>{_esc(str(value)[:300])}</code>")

    tb = item["tb"]
    if tb:
        short = "\n".join(tb.strip().splitlines()[-6:])
        lines.append("")
        lines.append("🐞 <b>Traceback (خلاصه):</b>")
        lines.append(f"<pre>{_esc(short[-900:])}</pre>")
    message = "\n".join(lines) + repeat_note
    return message[:_MAX_MESSAGE], tb


async def _deliver(item: dict, note: str) -> None:
    message, tb = _render(item, note)
    while True:
        try:
            await _alert_bot.send_message(DEV_ID, message, parse_mode="HTML")
            if tb and len(tb) > 900:  # Traceback کامل فقط وقتی خلاصه کافی نبود
                doc = BufferedInputFile(
                    _mask(tb).encode("utf-8", "replace"), filename=f"traceback_{item['id']}.txt")
                await _alert_bot.send_document(DEV_ID, doc, caption=f"Traceback کامل #{item['id']}")
            return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)


async def alert_worker() -> None:
    """تسک پس‌زمینه برای ارسال هشدارها به DEV_ID."""
    global _dropped
    while True:
        item = await _alert_queue.get()
        try:
            if _alert_bot is None:
                continue
            if _dropped:
                lost, _dropped = _dropped, 0
                item["context"]["alerts_dropped"] = lost
            note = _dedup_note(item)
            if note is None:
                continue
            await _deliver(item, note)
            await asyncio.sleep(0.5)  # فاصله بین پیام‌ها
        except Exception:
            logger.exception("Alert worker error")
        finally:
            try:
                _alert_queue.task_done()
            except Exception:
                pass


# ------------------------------------------------------------
# اتصال به logging: هر logger.error/exception هم هشدار می‌شود
# ------------------------------------------------------------

class AlertLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("dev.alerts") or record.name == __name__:
                return  # جلوگیری از حلقهٔ بی‌نهایت
            exc = record.exc_info[1] if record.exc_info else None
            level = "CRITICAL" if record.levelno >= logging.CRITICAL else "ERROR"
            item = {
                "id": uuid.uuid4().hex[:6].upper(),
                "level": level,
                "text": record.getMessage(),
                "where": f"{record.filename}:{record.funcName}:{record.lineno}",
                "time": datetime.now(_TZ),
                "tb": _format_traceback(exc) if exc else "",
                "user_id": None,
                "context": {"logger": record.name},
            }
            _enqueue(item)
        except Exception:
            pass


def install_log_handler(level: int = logging.ERROR) -> None:
    """یک‌بار در main صدا بزنید. همهٔ لاگ‌های ERROR به بالا برای دولوپر ارسال می‌شوند."""
    root = logging.getLogger()
    if not any(isinstance(h, AlertLogHandler) for h in root.handlers):
        handler = AlertLogHandler(level)
        root.addHandler(handler)


# ------------------------------------------------------------
# جزئیات آپدیت (برای هندلر خطای سراسری aiogram)
# ------------------------------------------------------------

def describe_update(update: Any) -> dict:
    """از یک Update، فیلدهای قابل‌خواندن برای context می‌سازد."""
    out: dict[str, Any] = {}
    try:
        out["update_id"] = getattr(update, "update_id", None)
        for kind in ("message", "edited_message", "callback_query", "my_chat_member", "inline_query"):
            obj = getattr(update, kind, None)
            if not obj:
                continue
            out["type"] = kind
            user = getattr(obj, "from_user", None)
            if user:
                out["user_id"] = user.id
                out["username"] = f"@{user.username}" if user.username else None
            msg = obj if kind in ("message", "edited_message") else getattr(obj, "message", None)
            chat = getattr(msg, "chat", None) or getattr(obj, "chat", None)
            if chat:
                out["chat"] = f"{chat.type}:{chat.id}"
            if kind == "callback_query":
                out["callback_data"] = obj.data
            elif msg is not None and getattr(msg, "text", None):
                out["text"] = msg.text[:200]
            break
    except Exception:
        pass
    return {k: v for k, v in out.items() if v is not None}
