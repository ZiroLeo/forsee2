import logging
import asyncio
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_ID, PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from database import get_connection, get_user_by_identifier, is_banned, log_event
from rich import premium_emoji, build_rich_message

logger = logging.getLogger(__name__)
router = Router(name="admin_broadcast")


class BroadcastStates(StatesGroup):
    waiting_target = State()
    waiting_text = State()
    waiting_forward_link = State()


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


async def _safe_answer(message: Message, text: str, markup=None):
    try:
        await message.answer(text, reply_markup=markup)
    except Exception:
        logger.exception("Could not send broadcast message.")


# ============================================================
# MENU
# ============================================================

@router.callback_query(F.data == "admin:broadcast:menu")
async def broadcast_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    text = (
        f"{premium_emoji('ask', '📨')} "
        "<b>ارسال پیام</b>\n\n"
        "لطفاً یک گزینه را انتخاب کنید:"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            _button("ارسال به شخص", "admin:broadcast:single", "primary", "telegram"),
            _button("ارسال به همه", "admin:broadcast:all", "primary", "world"),
        ],
        [
            _button("بازگشت", "admin:main", "danger", "back"),
        ],
    ])

    await _safe_answer(callback.message, text, markup)
    await callback.answer()


# ============================================================
# SINGLE TARGET
# ============================================================

@router.callback_query(F.data == "admin:broadcast:single")
async def broadcast_single_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    await state.clear()
    await state.set_state(BroadcastStates.waiting_target)
    await state.update_data(broadcast_mode="single")

    text = (
        f"{premium_emoji('telegram', '👤')} "
        "<b>آیدی عددی یا یوزرنیم کاربر را وارد کنید:</b>"
    )

    await _safe_answer(callback.message, text)
    await callback.answer()


@router.message(BroadcastStates.waiting_target)
async def broadcast_single_target(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ ورودی خالی است.")
        return

    target = get_user_by_identifier(raw)
    if not target:
        await message.answer("❌ کاربر یافت نشد.")
        return

    await state.update_data(target_id=target["telegram_id"])
    await state.set_state(BroadcastStates.waiting_text)

    text = (
        f"{premium_emoji('ask', '✍️')} "
        "<b>متن پیام را وارد کنید:</b>"
    )

    await _safe_answer(message, text)


# ============================================================
# SINGLE TEXT SEND
# ============================================================

@router.message(BroadcastStates.waiting_text)
async def broadcast_text_handler(message: Message, state: FSMContext, bot):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    mode = data.get("broadcast_mode")
    body = (message.text or "").strip()

    if not body:
        await message.answer("❌ متن خالی است.")
        return

    if len(body) > 4000:
        await message.answer("❌ متن بیش از حد طولانی است (حداکثر ۴۰۰۰ کاراکتر).")
        return

    if mode == "single":
        target_id = data.get("target_id")
        if not target_id:
            await message.answer("❌ کاربر هدف مشخص نیست.")
            await state.clear()
            return

        if is_banned(int(target_id)):
            await message.answer("⛔ این کاربر بن شده است و پیام ارسال نشد.")
            await state.clear()
            return

        try:
            await bot.send_message(int(target_id), body)
            await message.answer("✅ پیام ارسال شد.")
            log_event("INFO", "broadcast_single", ADMIN_ID, f"target={target_id}")
        except Exception as e:
            logger.exception("Broadcast single failed.")
            await message.answer(f"❌ خطا در ارسال: {e}")

        await state.clear()
        return

    if mode == "text":
        await _send_to_all(bot, message, text=body)
        await state.clear()
        return

    await message.answer("❌ حالت ارسال نامشخص است.")
    await state.clear()


# ============================================================
# ALL (MODE SELECT)
# ============================================================

@router.callback_query(F.data == "admin:broadcast:all")
async def broadcast_all_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    text = (
        f"{premium_emoji('ask', '📢')} "
        "<b>ارسال به همه</b>\n\n"
        "نوع ارسال را انتخاب کنید:"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            _button("ارسال متن عادی", "admin:broadcast:text", "primary", "light"),
            _button("ارسال از طریق لینک", "admin:broadcast:link", "primary", "world"),
        ],
        [
            _button("بازگشت", "admin:broadcast:menu", "danger", "back"),
        ],
    ])

    await _safe_answer(callback.message, text, markup)
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:text")
async def broadcast_text_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    await state.clear()
    await state.set_state(BroadcastStates.waiting_text)
    await state.update_data(broadcast_mode="text")

    text = (
        f"{premium_emoji('ask', '✍️')} "
        "<b>متن پیام را وارد کنید:</b>"
    )

    await _safe_answer(callback.message, text)
    await callback.answer()


@router.callback_query(F.data == "admin:broadcast:link")
async def broadcast_link_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    await state.clear()
    await state.set_state(BroadcastStates.waiting_forward_link)
    await state.update_data(broadcast_mode="link")

    text = (
        f"{premium_emoji('world', '🔗')} "
        "<b>لینک پیام کانال را وارد کنید:</b>\n\n"
        "مثال: <code>https://t.me/channel/123</code>"
    )

    await _safe_answer(callback.message, text)
    await callback.answer()


# ============================================================
# FORWARD LINK HANDLER
# ============================================================

@router.message(BroadcastStates.waiting_forward_link)
async def broadcast_link_handler(message: Message, state: FSMContext, bot):
    if message.from_user.id != ADMIN_ID:
        return

    link = (message.text or "").strip()
    if not link.startswith("https://t.me/"):
        await message.answer("❌ لینک نامعتبر است.")
        return

    try:
        parts = link.replace("https://t.me/", "").split("/")
        if parts[0] == "c":
            chat_id = int(f"-100{parts[1]}")
            msg_id = int(parts[2])
        else:
            chat_id = f"@{parts[0]}"
            msg_id = int(parts[1])
    except (IndexError, ValueError):
        await message.answer("❌ فرمت لینک نامعتبر است.")
        return

    await _send_to_all(bot, message, forward_from=(chat_id, msg_id))
    await state.clear()


# ============================================================
# SEND TO ALL USERS
# ============================================================

async def _send_to_all(bot, admin_message: Message, text=None, forward_from=None):
    """ارسال به همه کاربران غیربن‌شده با تأخیر و گزارش موفق/ناموفق."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT telegram_id FROM users WHERE is_banned = 0"
        ).fetchall()
        user_ids = [int(r["telegram_id"]) for r in rows]

    total = len(user_ids)
    ok = 0
    fail = 0

    for uid in user_ids:
        try:
            if forward_from:
                await bot.forward_message(
                    chat_id=uid,
                    from_chat_id=forward_from[0],
                    message_id=forward_from[1],
                )
            else:
                await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    log_event(
        "INFO",
        "broadcast_all",
        ADMIN_ID,
        f"ok={ok}, fail={fail}, total={total}",
    )

    report = (
        f"{premium_emoji('true', '✅')} <b>ارسال به همه انجام شد.</b>\n\n"
        f"{premium_emoji('telegram', '👥')} کل کاربران: <b>{total}</b>\n"
        f"{premium_emoji('true', '✅')} موفق: <b>{ok}</b>\n"
        f"{premium_emoji('false', '❌')} ناموفق: <b>{fail}</b>"
    )

    await _safe_answer(admin_message, report)