import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED, LEVEL_EMOJIS
from database import (
    list_users_page, get_user_level, get_user_registration_date,
    is_banned, ban_user, unban_user, log_admin_action, log_event,
)
from rich import premium_emoji

logger = logging.getLogger(__name__)
router = Router(name="admin_users_list")

PAGE_SIZE = 5

LEVEL_FALLBACK = {
    "new": "🌱",
    "prime": "🥈",
    "royal": "👑",
    "diamond": "💎",
    "legendary": "🏆",
}


# ============================================================
# HELPERS
# ============================================================

def _emoji_id(key: str) -> str | None:
    if not PREMIUM_EMOJI_ENABLED:
        return None
    return PREMIUM_EMOJIS.get(key)


def _button(
    text: str,
    callback_data: str,
    style: str = "primary",
    emoji_key: str | None = None,
) -> InlineKeyboardButton:
    kwargs = {"text": text, "callback_data": callback_data, "style": style}
    eid = _emoji_id(emoji_key) if emoji_key else None
    if eid:
        kwargs["icon_custom_emoji_id"] = eid
    return InlineKeyboardButton(**kwargs)


async def _safe_edit(message, text, markup=None):
    """ویرایش پیام فعلی (برای صفحه‌بندی)."""
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.exception("Could not edit users list message")
    except Exception:
        logger.exception("Could not edit users list message")


def _total_pages(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def _render(page_no: int):
    users, total = list_users_page(page_no, PAGE_SIZE)
    pages = _total_pages(total)

    if not users and page_no > 1:
        return _render(pages)

    lines = [
        f"{premium_emoji('blue_heart', '📋')} <b>لیست کاربران</b> — صفحه {page_no} از {pages} ({total} کاربر)",
        "━━━━━━━━━━━━━━"
    ]
    rows = []

    if not users:
        lines.append("هنوز کاربری ثبت نشده است.")

    for u in users:
        tid = u["telegram_id"]
        banned = is_banned(tid)
        level_key, level_name = get_user_level(int(u.get("total_purchases") or 0))
        username = f"@{u['username']}" if u.get("username") else "بدون یوزرنیم"
        name = u.get("first_name") or "—"

        status = (
            f"{premium_emoji('false', '🚫')} مسدود"
            if banned
            else f"{premium_emoji('true', '✅')} فعال"
        )

        # ایموجی پریمیوم سطح
        level_emoji_id = LEVEL_EMOJIS.get(level_key)
        if level_emoji_id:
            level_display = (
                f'<tg-emoji emoji-id="{level_emoji_id}">'
                f'{LEVEL_FALLBACK.get(level_key, "⭐")}'
                f'</tg-emoji> <b>{level_name}</b>'
            )
        else:
            level_display = f"{LEVEL_FALLBACK.get(level_key, '⭐')} <b>{level_name}</b>"

        lines.append(
            f"{premium_emoji('telegram', '👤')} <code>{tid}</code> | {username} | {name}\n"
            f"     {premium_emoji('light', '📅')} عضویت: {get_user_registration_date(tid)}\n"
            f"     {premium_emoji('shop', '📦')} سرویس: <b>{u['sub_count']}</b> | "
            f"{premium_emoji('shop', '🛒')} خرید: <b>{int(u.get('total_purchases') or 0)}</b> | "
            f"{level_display}\n"
            f"     {premium_emoji('wallet', '💰')} {u.get('balance') or 0:,} تومان | {status}"
        )
        lines.append("━━━━━━━━━━━━━━")

        ban_button = (
            _button("رفع مسدودی", f"admin:users_list:unban:{tid}:{page_no}", "success", "true")
            if banned
            else _button("مسدود کردن", f"admin:users_list:ban:{tid}:{page_no}", "danger", "false")
        )

        rows.append([
            _button(f"پروفایل {tid}", f"admin:users:view:{tid}", "primary", "telegram"),
            ban_button
        ])

    nav = []
    if page_no > 1:
        nav.append(_button("قبلی", f"admin:users_list:page:{page_no - 1}", "primary", "back"))
    if page_no < pages:
        nav.append(_button("بعدی", f"admin:users_list:page:{page_no + 1}", "success", "next"))
    if nav:
        rows.append(nav)

    rows.append([_button("بازگشت", "admin:main", "danger", "back")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _page_from(data: str, index: int) -> int:
    try:
        return max(1, int(data.split(":")[index]))
    except (IndexError, ValueError):
        return 1


# ============================================================
# HANDLERS
# ============================================================

@router.callback_query(F.data == "admin:users_list:menu")
async def users_list_menu(callback: CallbackQuery):
    text, markup = _render(1)
    await _safe_edit(callback.message, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:users_list:page:"))
async def users_list_page(callback: CallbackQuery):
    text, markup = _render(_page_from(callback.data, 3))
    await _safe_edit(callback.message, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:users_list:ban:"))
async def users_list_ban(callback: CallbackQuery):
    parts = callback.data.split(":")
    target, page_no = int(parts[3]), _page_from(callback.data, 4)

    if target == ADMIN_ID:
        await callback.answer("مسدود کردن ادمین اصلی ممکن نیست.", show_alert=True)
        return

    ban_user(target, callback.from_user.id, "مسدود توسط ادمین")
    log_admin_action(callback.from_user.id, "user_ban", str(target), "from users_list")
    log_event("INFO", "admin_user_banned", callback.from_user.id, f"target={target}")

    await callback.answer("کاربر مسدود شد.")
    text, markup = _render(page_no)
    await _safe_edit(callback.message, text, markup)


@router.callback_query(F.data.startswith("admin:users_list:unban:"))
async def users_list_unban(callback: CallbackQuery):
    parts = callback.data.split(":")
    target, page_no = int(parts[3]), _page_from(callback.data, 4)

    unban_user(target, callback.from_user.id)
    log_admin_action(callback.from_user.id, "user_unban", str(target), "from users_list")
    log_event("INFO", "admin_user_unbanned", callback.from_user.id, f"target={target}")

    await callback.answer("مسدودی برداشته شد.")
    text, markup = _render(page_no)
    await _safe_edit(callback.message, text, markup)