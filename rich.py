
import logging
from typing import Optional, List, Any

from aiogram import Bot
from aiogram.types import InputRichMessage
from aiogram.types.rich_message_button import RichMessageButton
from aiogram.types.input_rich_block_buttons import InputRichBlockButtons
from aiogram.types.input_rich_block_paragraph import InputRichBlockParagraph
from aiogram.types.rich_text_custom_emoji import RichTextCustomEmoji
from aiogram.types.rich_text_bold import RichTextBold

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED


logger = logging.getLogger(__name__)

# فقط ایموجی‌هایی که تلگرام معتبر بودنشان را تأیید کرده
# key: (custom_emoji_id, alternative_text)
_PREMIUM_EMOJI_CACHE: dict[str, tuple[str, str]] = {}


# ============================================================
# HTML
# ============================================================

def _escape_html(text: Any) -> str:
    """Escape کردن کاراکترهای HTML."""
    if text is None:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# PREMIUM EMOJI INITIALIZATION
# ============================================================

async def initialize_premium_emojis(bot: Bot) -> int:
    """
    اعتبارسنجی شناسه‌های ایموجی پریمیوم با Telegram.

    خروجی:
        تعداد ایموجی‌های معتبر و ذخیره‌شده در کش.

    در صورت خطای شبکه، کش قبلی حفظ می‌شود.
    """

    if not PREMIUM_EMOJI_ENABLED:
        _PREMIUM_EMOJI_CACHE.clear()
        logger.info("Premium emojis are disabled.")
        return 0

    if not isinstance(PREMIUM_EMOJIS, dict):
        logger.warning("PREMIUM_EMOJIS must be a dictionary.")
        return 0

    emoji_ids = {
        str(key): str(value).strip()
        for key, value in PREMIUM_EMOJIS.items()
        if value is not None and str(value).strip()
    }

    if not emoji_ids:
        _PREMIUM_EMOJI_CACHE.clear()
        logger.warning("No premium emoji IDs configured.")
        return 0

    unique_ids = list(set(emoji_ids.values()))

    try:
        stickers = await bot.get_custom_emoji_stickers(
            custom_emoji_ids=unique_ids
        )

        valid_ids: dict[str, str] = {}

        for sticker in stickers:
            sticker_id = str(sticker.custom_emoji_id)

            valid_ids[sticker_id] = (
                sticker.emoji or "✨"
            )

        new_cache: dict[str, tuple[str, str]] = {}

        for key, emoji_id in emoji_ids.items():
            alternative_text = valid_ids.get(emoji_id)

            if alternative_text:
                new_cache[key] = (
                    emoji_id,
                    alternative_text,
                )
            else:
                logger.warning(
                    "Premium emoji '%s' is invalid or unavailable.",
                    key,
                )

        # فقط پس از پاسخ موفق تلگرام، کش جایگزین می‌شود.
        _PREMIUM_EMOJI_CACHE.clear()
        _PREMIUM_EMOJI_CACHE.update(new_cache)

        logger.info(
            "Premium emoji cache initialized: %s/%s",
            len(new_cache),
            len(emoji_ids),
        )

        return len(new_cache)

    except Exception:
        logger.exception(
            "Could not initialize premium emoji cache. "
            "Existing cache was preserved."
        )

        return len(_PREMIUM_EMOJI_CACHE)


# ============================================================
# SAFE EMOJI TEXT
# ============================================================

SAFE_EMOJI_DEFAULT = "⭐"


def is_valid_emoji_text(value: str) -> bool:
    """
    متن داخل <tg-emoji> باید ایموجی واقعی باشد.
    کاراکترهایی مثل ● ◉ ◈ • باعث خطای ENTITY_TEXT_INVALID می‌شوند.
    """

    if not value or not value.strip():
        return False

    for char in value:
        code = ord(char)

        if code < 0x2100 or 0x25A0 <= code <= 0x25FF:
            return False

    return True


def safe_emoji_text(key: str = "", fallback: str = "") -> str:
    """
    بهترین متن معتبر برای داخل تگ ایموجی پریمیوم:
    ۱) ایموجی خود استیکر (از کش تلگرام)
    ۲) fallback اگر ایموجی واقعی باشد
    ۳) ⭐
    """

    cached = _PREMIUM_EMOJI_CACHE.get(key) if key else None

    if cached and is_valid_emoji_text(cached[1]):
        return cached[1]

    if is_valid_emoji_text(fallback):
        return fallback

    return SAFE_EMOJI_DEFAULT


# ============================================================
# PREMIUM EMOJI FOR MESSAGE TEXT
# ============================================================

def premium_emoji(
    key: str,
    fallback: str = "●",
) -> str:
    """
    ساخت تگ HTML برای ایموجی پریمیوم در متن پیام.

    عمداً به کش اعتبارسنجی وابسته نیست؛
    بنابراین خطای شبکه هنگام راه‌اندازی بات باعث حذف
    ایموجی‌های متن پیام نمی‌شود.
    """

    if not PREMIUM_EMOJI_ENABLED:
        return _escape_html(fallback)

    if not isinstance(PREMIUM_EMOJIS, dict):
        return _escape_html(fallback)

    emoji_id = PREMIUM_EMOJIS.get(key)

    if not emoji_id:
        return _escape_html(fallback)

    emoji_id = str(emoji_id).strip()

    if not emoji_id:
        return _escape_html(fallback)

    safe_fallback = _escape_html(safe_emoji_text(key, fallback))

    return (
        f'<tg-emoji emoji-id="{emoji_id}">'
        f'{safe_fallback}'
        f'</tg-emoji>'
    )


# ============================================================
# RICH BUTTON
# ============================================================

def build_button(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    style: str = "primary",
    premium_emoji_key: Optional[str] = None,
) -> RichMessageButton:

    if callback_data and url:
        raise ValueError(
            "Cannot set both callback_data and url."
        )

    if not callback_data and not url:
        raise ValueError(
            "Either callback_data or url must be provided."
        )

    if callback_data:
        if len(callback_data.encode("utf-8")) > 64:
            raise ValueError(
                "callback_data exceeds Telegram's 64-byte limit."
            )

    if url:
        if not url.startswith(("https://", "http://")):
            raise ValueError(
                "Button URL must start with http:// or https://."
            )

    display_text: Any = str(text)

    # ایموجی پریمیوم دکمه فقط از کش معتبر استفاده می‌کند.
    if (
        premium_emoji_key
        and PREMIUM_EMOJI_ENABLED
    ):
        cached_emoji = _PREMIUM_EMOJI_CACHE.get(
            premium_emoji_key
        )

        if cached_emoji:
            emoji_id, alternative_text = cached_emoji

            try:
                custom_emoji = RichTextCustomEmoji(
                    custom_emoji_id=emoji_id,
                    alternative_text=alternative_text,
                )

                display_text = [
                    custom_emoji,
                    " ",
                    str(text),
                ]

            except Exception:
                logger.exception(
                    "Could not build premium emoji '%s'.",
                    premium_emoji_key,
                )

                display_text = str(text)

    if callback_data:
        return RichMessageButton(
            text=display_text,
            callback_data=callback_data,
            style=style,
        )

    return RichMessageButton(
        text=display_text,
        url=url,
        style=style,
    )


# ============================================================
# RICH BUTTON ROW
# ============================================================

def build_button_row(
    buttons: List[RichMessageButton],
    align: str = "center",
) -> InputRichBlockButtons:

    if not isinstance(buttons, list):
        raise TypeError(
            "buttons must be a list."
        )

    if not 1 <= len(buttons) <= 8:
        raise ValueError(
            "Button row must contain 1-8 buttons."
        )

    if align not in ("left", "center", "right"):
        raise ValueError(
            "align must be left, center or right."
        )

    return InputRichBlockButtons(
        buttons=buttons,
        align=align,
    )


# ============================================================
# RICH MESSAGE
# ============================================================

def rich_emoji(
    key: str,
    fallback: str = "●",
) -> Any:
    """
    ایموجی پریمیوم برای متن Rich Message (به‌صورت RichText).

    اگر ایموجی در دسترس نباشد، ایموجی معمولی (fallback) برگردانده می‌شود.
    """

    if not PREMIUM_EMOJI_ENABLED or not isinstance(PREMIUM_EMOJIS, dict):
        return fallback

    emoji_id = str(PREMIUM_EMOJIS.get(key) or "").strip()

    if not emoji_id:
        return fallback

    alternative_text = safe_emoji_text(key, fallback)

    return RichTextCustomEmoji(
        custom_emoji_id=emoji_id,
        alternative_text=alternative_text,
    )


def rich_bold(*parts: Any) -> RichTextBold:
    """متن بولد برای Rich Message."""
    return RichTextBold(text=list(parts))


def build_rich_message(
    paragraphs: List[Any],
    button_rows: Optional[List[InputRichBlockButtons]] = None,
    rtl: bool = True,
) -> InputRichMessage:
    """
    ساخت پیام Rich فقط با blocks.

    تلگرام اجازه می‌دهد دقیقاً یکی از html / markdown / blocks
    استفاده شود. ارسال هم‌زمان html و blocks باعث می‌شد متن اول
    نمایش داده نشود؛ بنابراین متن هم به‌صورت بلوک پاراگراف ساخته
    می‌شود و دکمه‌ها بعد از آن می‌آیند.

    paragraphs: لیستی از متن‌ها. هر عضو یک پاراگراف است و می‌تواند
                str یا لیستی از str / rich_emoji() / rich_bold() باشد.
    """

    if not isinstance(paragraphs, list) or not paragraphs:
        raise TypeError(
            "paragraphs must be a non-empty list."
        )

    blocks: list = [
        InputRichBlockParagraph(text=paragraph)
        for paragraph in paragraphs
    ]

    if button_rows:
        blocks.extend(button_rows)

    return InputRichMessage(
        blocks=blocks,
        is_rtl=rtl,
    )
