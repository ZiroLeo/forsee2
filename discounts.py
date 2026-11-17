import logging
import re
import html
import sqlite3

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from rich import premium_emoji
from config import ADMIN_ID, PREMIUM_EMOJIS
from database import create_discount_code, get_connection, log_event


logger = logging.getLogger(__name__)
router = Router(name="admin_discounts")


# ============================================================
# تنظیمات
# ============================================================

MIN_CODE_LENGTH = 3
MAX_CODE_LENGTH = 32

MIN_PERCENT = 1
MAX_PERCENT = 100

MIN_USES = 1
MAX_USES = 1_000_000


# ============================================================
# وضعیت‌های ساخت کد تخفیف
# ============================================================

class DiscountStates(StatesGroup):
    waiting_code = State()
    waiting_percent = State()
    waiting_max_uses = State()


# ============================================================
# کیبوردها
# ============================================================

def _button(text: str, callback_data: str, emoji_key: str) -> InlineKeyboardButton:
    """دکمه آبی با ایموجی پریمیوم."""
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=PREMIUM_EMOJIS.get(emoji_key),
        style="primary",
    )


def discount_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("ساخت کد تخفیف", "admin:discounts:create", "gift")
            ],
            [
                _button("بازگشت به پنل ادمین", "admin:main", "back")
            ]
        ]
    )


def discount_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("لغو عملیات", "admin:discounts:cancel", "false")
            ]
        ]
    )


# ============================================================
# ابزارهای کمکی
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def validate_code(code: str) -> bool:
    if not code:
        return False

    if not MIN_CODE_LENGTH <= len(code) <= MAX_CODE_LENGTH:
        return False

    return bool(re.fullmatch(r"[A-Z0-9_-]+", code))


async def safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup = None
) -> bool:
    """
    ویرایش امن پیام فعلی.
    """

    if not callback.message:
        return False

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        return True

    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return True

        logger.warning("Discount message edit failed: %s", error)
        return False

    except Exception:
        logger.exception("Unexpected error while editing discount message")
        return False


async def show_prompt(
    message: Message,
    text: str
):
    await message.answer(
        text=text,
        reply_markup=discount_cancel_keyboard(),
        parse_mode="HTML"
    )


def get_discount_codes():
    """
    دریافت آخرین کدهای تخفیف.
    """

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                code,
                percent,
                max_uses,
                used_count,
                is_active,
                created_at
            FROM discount_codes
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()

        return [dict(row) for row in rows]


def build_discount_menu_text(codes: list) -> str:
    lines = [
        f"{premium_emoji('gift', '🎁')} <b>مدیریت کدهای تخفیف</b>",
        "",
        "آخرین کدهای ثبت‌شده:",
        ""
    ]

    if not codes:
        lines.append("هنوز هیچ کد تخفیفی ساخته نشده است.")
    else:
        for item in codes:
            code = html.escape(str(item["code"]))
            percent = int(item["percent"])
            used = int(item["used_count"])
            maximum = int(item["max_uses"])

            active = bool(item["is_active"]) and used < maximum

            status = f"{premium_emoji('true', '🟢')} فعال" if active else f"{premium_emoji('false', '🔴')} غیرفعال"

            lines.extend([
                f"{premium_emoji('gift', '🎟️')} <code>{code}</code>",
                f"{premium_emoji('light', '💯')} تخفیف: <b>{percent}%</b>",
                f"{premium_emoji('telegram', '👥')} استفاده: <b>{used}/{maximum}</b>",
                f"{premium_emoji('light', '📌')} وضعیت: {status}",
                "━━━━━━━━━━━━━━"
            ])

    lines.extend([
        "",
        "<i>برای ساخت کد جدید، دکمه زیر را انتخاب کن.</i>"
    ])

    return "\n".join(lines)


def safe_log(level: str, event: str, user_id: int, detail: str = ""):
    try:
        log_event(level, event, user_id, detail)
    except Exception:
        logger.exception("Failed to write discount log")


# ============================================================
# منوی مدیریت کدهای تخفیف
# ============================================================

@router.callback_query(F.data == "admin:discounts:menu")
async def discounts_menu(callback: CallbackQuery, state: FSMContext):

    user = callback.from_user

    if not user or not is_admin(user.id):
        if user:
            safe_log(
                "SECURITY",
                "unauthorized_discounts_access",
                user.id,
                f"data={callback.data}"
            )

        await callback.answer(
            f"{premium_emoji('false', '⛔')} دسترسی غیرمجاز.",
            show_alert=True
        )
        return

    await callback.answer()

    # اگر ادمین از وسط ساخت کد به منو برگشت
    await state.clear()

    try:
        codes = get_discount_codes()
        text = build_discount_menu_text(codes)

        success = await safe_edit(
            callback,
            text,
            discount_menu_keyboard()
        )

        if not success:
            await callback.message.answer(
                text=text,
                reply_markup=discount_menu_keyboard(),
                parse_mode="HTML"
            )

    except sqlite3.Error:
        logger.exception("Failed to load discount codes")

        await callback.message.answer(
            f"{premium_emoji('warning', '❌')} خطا در دریافت اطلاعات کدهای تخفیف."
        )


# ============================================================
# شروع ساخت کد تخفیف
# ============================================================

@router.callback_query(F.data == "admin:discounts:create")
async def create_discount_start(
    callback: CallbackQuery,
    state: FSMContext
):

    user = callback.from_user

    if not user or not is_admin(user.id):
        await callback.answer(
            f"{premium_emoji('false', '⛔')} دسترسی غیرمجاز.",
            show_alert=True
        )
        return

    await callback.answer()

    await state.clear()
    await state.set_state(DiscountStates.waiting_code)

    text = (
        f"{premium_emoji('gift', '🎁')} <b>ساخت کد تخفیف</b>\n\n"
        "مرحله ۱ از ۳\n\n"
        "نام کد تخفیف را وارد کن.\n\n"
        "مثال: <code>HDKHDL</code>\n\n"
        "قوانین:\n"
        "• فقط حروف انگلیسی\n"
        "• اعداد\n"
        "• علامت‌های <code>_</code> و <code>-</code>\n"
        "• حداقل ۳ و حداکثر ۳۲ کاراکتر"
    )

    success = await safe_edit(
        callback,
        text,
        discount_cancel_keyboard()
    )

    if not success:
        await callback.message.answer(
            text=text,
            reply_markup=discount_cancel_keyboard(),
            parse_mode="HTML"
        )


# ============================================================
# دریافت نام کد
# ============================================================

@router.message(DiscountStates.waiting_code)
async def discount_code_received(
    message: Message,
    state: FSMContext
):

    user = message.from_user

    if not user or not is_admin(user.id):
        await state.clear()
        return

    raw_code = (message.text or "").strip()
    code = raw_code.upper()

    if not validate_code(code):
        await message.answer(
            f"{premium_emoji('false', '❌')} <b>فرمت کد نامعتبر است.</b>\n\n"
            "فقط حروف انگلیسی، اعداد و علامت‌های "
            "<code>_</code> و <code>-</code> مجاز هستند.\n"
            "طول کد باید بین ۳ تا ۳۲ کاراکتر باشد.",
            parse_mode="HTML"
        )
        return

    # بررسی کد تکراری قبل از ادامه
    try:
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM discount_codes
                WHERE code = ? COLLATE NOCASE
                LIMIT 1
                """,
                (code,)
            ).fetchone()

        if existing:
            await message.answer(
                f"{premium_emoji('false', '❌')} این کد قبلاً ثبت شده است.\n"
                "لطفاً یک نام دیگر انتخاب کن."
            )
            return

    except sqlite3.Error:
        logger.exception("Failed to check duplicate discount code")

        await message.answer(
            f"{premium_emoji('warning', '❌')} خطا در بررسی کد. دوباره تلاش کن."
        )
        return

    await state.update_data(code=code)
    await state.set_state(DiscountStates.waiting_percent)

    await show_prompt(
        message,
        (
            f"{premium_emoji('light', '💯')} <b>مرحله ۲ از ۳</b>\n\n"
            f"کد انتخابی: <code>{html.escape(code)}</code>\n\n"
            "درصد تخفیف را وارد کن.\n\n"
            "مثال: <code>20</code>\n"
            "مقدار مجاز: ۱ تا ۱۰۰"
        )
    )


# ============================================================
# دریافت درصد تخفیف
# ============================================================

@router.message(DiscountStates.waiting_percent)
async def discount_percent_received(
    message: Message,
    state: FSMContext
):

    user = message.from_user

    if not user or not is_admin(user.id):
        await state.clear()
        return

    raw_value = (message.text or "").strip()

    try:
        percent = int(raw_value)

        if not MIN_PERCENT <= percent <= MAX_PERCENT:
            raise ValueError

    except (ValueError, TypeError):
        await message.answer(
            f"{premium_emoji('false', '❌')} درصد تخفیف نامعتبر است.\n\n"
            "لطفاً عددی بین ۱ تا ۱۰۰ وارد کن.\n"
            "مثال: <code>20</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(percent=percent)
    await state.set_state(DiscountStates.waiting_max_uses)

    data = await state.get_data()

    await show_prompt(
        message,
        (
            f"{premium_emoji('telegram', '👥')} <b>مرحله ۳ از ۳</b>\n\n"
            f"کد: <code>{html.escape(data['code'])}</code>\n"
            f"درصد تخفیف: <b>{percent}%</b>\n\n"
            "حداکثر چند نفر بتوانند از این کد استفاده کنند؟\n\n"
            "مثال: <code>10</code>\n"
            "هر کاربر فقط یک بار می‌تواند از هر کد استفاده کند."
        )
    )


# ============================================================
# دریافت محدودیت استفاده و ثبت کد
# ============================================================

@router.message(DiscountStates.waiting_max_uses)
async def discount_max_uses_received(
    message: Message,
    state: FSMContext
):

    user = message.from_user

    if not user or not is_admin(user.id):
        await state.clear()
        return

    raw_value = (message.text or "").strip()

    try:
        max_uses = int(raw_value)

        if not MIN_USES <= max_uses <= MAX_USES:
            raise ValueError

    except (ValueError, TypeError):
        await message.answer(
            f"{premium_emoji('false', '❌')} تعداد استفاده نامعتبر است.\n\n"
            "یک عدد بین ۱ تا ۱٬۰۰۰٬۰۰۰ وارد کن.",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()

    code = data.get("code")
    percent = data.get("percent")

    if not code or not percent:
        await state.clear()

        await message.answer(
            f"{premium_emoji('warning', '❌')} اطلاعات ساخت کد ناقص است. لطفاً دوباره شروع کن."
        )
        return

    # نمایش خلاصه و درخواست تأیید
    await state.update_data(max_uses=max_uses)

    text = (
        f"{premium_emoji('light', '📋')} <b>بررسی اطلاعات کد تخفیف</b>\n\n"
        f"{premium_emoji('gift', '🎟️')} کد: <code>{html.escape(code)}</code>\n"
        f"{premium_emoji('light', '💯')} درصد تخفیف: <b>{percent}%</b>\n"
        f"{premium_emoji('telegram', '👥')} حداکثر استفاده: <b>{max_uses} نفر</b>\n\n"
        "آیا اطلاعات صحیح است؟"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button("تأیید و ساخت کد", "admin:discounts:confirm", "true")
            ],
            [
                _button("لغو", "admin:discounts:cancel", "false")
            ]
        ]
    )

    await state.set_state(DiscountStates.waiting_max_uses)

    await message.answer(
        text=text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# ============================================================
# تأیید نهایی و ذخیره کد
# ============================================================

@router.callback_query(
    F.data == "admin:discounts:confirm",
    DiscountStates.waiting_max_uses
)
async def confirm_discount_creation(
    callback: CallbackQuery,
    state: FSMContext
):

    user = callback.from_user

    if not user or not is_admin(user.id):
        await callback.answer(
            f"{premium_emoji('false', '⛔')} دسترسی غیرمجاز.",
            show_alert=True
        )
        return

    await callback.answer("در حال ثبت کد...")

    data = await state.get_data()

    code = data.get("code")
    percent = data.get("percent")
    max_uses = data.get("max_uses")

    if not code or not percent or not max_uses:
        await state.clear()

        await safe_edit(
            callback,
            f"{premium_emoji('warning', '❌')} اطلاعات ساخت کد ناقص است. دوباره شروع کن.",
            discount_menu_keyboard()
        )
        return

    try:
        code_id = create_discount_code(
            code=code,
            percent=percent,
            max_uses=max_uses
        )

        if not code_id:
            await safe_edit(
                callback,
                (
                    f"{premium_emoji('false', '❌')} <b>ساخت کد انجام نشد.</b>\n\n"
                    "احتمالاً این کد قبلاً ثبت شده است."
                ),
                discount_menu_keyboard()
            )

            await state.clear()
            return

        safe_log(
            "INFO",
            "discount_created",
            user.id,
            (
                f"code={code}, "
                f"percent={percent}, "
                f"max_uses={max_uses}, "
                f"code_id={code_id}"
            )
        )

        await state.clear()

        text = (
            f"{premium_emoji('true', '✅')} <b>کد تخفیف با موفقیت ساخته شد.</b>\n\n"
            f"{premium_emoji('gift', '🎟️')} کد: <code>{html.escape(code)}</code>\n"
            f"{premium_emoji('light', '💯')} تخفیف: <b>{percent}%</b>\n"
            f"{premium_emoji('telegram', '👥')} ظرفیت: <b>{max_uses} نفر</b>\n"
            f"{premium_emoji('light', '📌')} وضعیت: {premium_emoji('true', '🟢')} فعال\n\n"
            "هر کاربر فقط یک بار می‌تواند از این کد استفاده کند."
        )

        await safe_edit(
            callback,
            text,
            discount_menu_keyboard()
        )

    except Exception:
        logger.exception("Discount creation failed")

        await state.clear()

        await safe_edit(
            callback,
            f"{premium_emoji('warning', '❌')} خطای داخلی هنگام ساخت کد تخفیف رخ داد.",
            discount_menu_keyboard()
        )


# ============================================================
# لغو ساخت کد
# ============================================================

@router.callback_query(F.data == "admin:discounts:cancel")
async def cancel_discount_creation(
    callback: CallbackQuery,
    state: FSMContext
):

    user = callback.from_user

    if not user or not is_admin(user.id):
        await callback.answer(
            f"{premium_emoji('false', '⛔')} دسترسی غیرمجاز.",
            show_alert=True
        )
        return

    await callback.answer("عملیات لغو شد.")

    await state.clear()

    text = (
        f"{premium_emoji('false', '❌')} <b>عملیات ساخت کد تخفیف لغو شد.</b>\n\n"
        "برای شروع مجدد از منوی زیر استفاده کن."
    )

    success = await safe_edit(
        callback,
        text,
        discount_menu_keyboard()
    )

    if not success and callback.message:
        await callback.message.answer(
            text=text,
            reply_markup=discount_menu_keyboard(),
            parse_mode="HTML"
        )


# ============================================================
# لغو با دستور /cancel
# ============================================================

@router.message(Command("cancel"), DiscountStates.waiting_code)
@router.message(Command("cancel"), DiscountStates.waiting_percent)
@router.message(Command("cancel"), DiscountStates.waiting_max_uses)
async def cancel_discount_by_command(
    message: Message,
    state: FSMContext
):

    user = message.from_user

    if not user or not is_admin(user.id):
        await state.clear()
        return

    await state.clear()

    await message.answer(
        f"{premium_emoji('false', '❌')} <b>عملیات ساخت کد تخفیف لغو شد.</b>"
    )