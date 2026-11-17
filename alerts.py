import asyncio
import html
import logging
from typing import Optional

from aiogram import Bot

from config import DEV_ID

logger = logging.getLogger(__name__)

_alert_queue: asyncio.Queue = asyncio.Queue()
_alert_bot: Optional[Bot] = None


def setup_alerts(bot: Bot) -> None:
    """ذخیره‌ی bot برای ارسال هشدارها."""
    global _alert_bot
    _alert_bot = bot


def send_alert(level: str, text: str) -> None:
    """
    افزودن هشدار به صف (غیرهمزمان).
    سطح‌ها: SECURITY, ERROR, WARNING, INFO
    """
    try:
        _alert_queue.put_nowait((level.upper(), text))
    except Exception:
        logger.exception("Could not queue alert")


async def alert_worker() -> None:
    """تسک پس‌زمینه برای ارسال هشدارها به DEV_ID."""
    while True:
        try:
            level, text = await _alert_queue.get()

            if _alert_bot is None:
                continue

            emoji = {
                "SECURITY": "🚨",
                "ERROR": "⚠️",
                "WARNING": "⚠️",
                "INFO": "📢",
            }.get(level, "📢")

            safe_text = html.escape(str(text)[:3500])

            await _alert_bot.send_message(
                DEV_ID,
                f"{emoji} <b>{level}</b>\n\n<pre>{safe_text}</pre>",
                parse_mode="HTML",
            )

        except Exception:
            logger.exception("Alert worker error")
        finally:
            try:
                _alert_queue.task_done()
            except Exception:
                pass