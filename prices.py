import logging
import re
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import (
    PREMIUM_EMOJIS,
    PREMIUM_EMOJI_ENABLED,
)

from database import (
    get_setting,
    set_setting,
    log_event,
)
from rich import premium_emoji
from .admin_ui import protect  # ← SEC-02: گارد یکدست


logger = logging.getLogger(__name__)
router = Router(name="admin_prices")
protect(router)  # فقط ادمین اصلی؛ چک تکی از هر هندلر حذف شد


# ============================================================
# STATES
# ============================================================

class PriceStates(StatesGroup):
    waiting_value = State()


# ============================================================
# PRICE DEFINITIONS
# ============================================================

PRICE_ITEMS = {
    "per_gb": {
        "title": "هر گیگ سرویس حجمی",
        "description": "قیمت پایه هر گیگ سرویس حجمی",
        "toman_key": "price_per_gb",
        "stars_key": "stars_per_gb",
        "default_toman": 5000,
        "default_stars": 2,
        "emoji": "blue_dot",
    },

    "vol_device": {
        "title": "هر یوزر اضافه حجمی",
        "description": "هزینه هر دستگاه اضافه در سرویس حجمی",
        "toman_key": "per_device_vol_price",
        "stars_key": "per_device_vol_stars",
        "default_toman": 15000,
        "default_stars": 5,
        "emoji": "telegram",
    },

    "unl_device": {
        "title": "هر یوزر اضافه نامحدود بی‌نهایت",
        "description": "هزینه هر یوزر اضافه در سرویس نامحدود بی‌نهایت",
        "toman_key": "per_device_unl_price",
        "stars_key": "per_device_unl_stars",
        "default_toman": 15000,
        "default_stars": 5,
        "emoji": "world",
    },

    "unl_fair_device": {
        "title": "هر یوزر اضافه نامحدود منصفانه",
        "description": "هزینه هر یوزر اضافه در سرویس نامحدود منصفانه",
        "toman_key": "per_device_unl_fair_price",
        "stars_key": "per_device_unl_fair_stars",
        "default_toman": 15000,
        "default_stars": 5,
        "emoji": "diamond",
    },

    "vol_month": {
        "title": "هر ماه اضافه حجمی",
        "description": "هزینه هر ماه اضافه در سرویس حجمی",
        "toman_key": "per_month_vol_price",
        "stars_key": "per_month_vol_stars",
        "default_toman": 30000,
        "default_stars": 10,
        "emoji": "light",
    },

    "unl_month": {
        "title": "هر ماه اضافه نامحدود",
        "description": "هزینه هر ماه اضافه در سرویس نامحدود",
        "toman_key": "per_month_unl_price",
        "stars_key": "per_month_unl_stars",
        "default_toman": 30000,
        "default_stars": 10,
        "emoji": "diamond",
    },

    "no_expiry_gb": {
        "title": "هر گیگ بدون محدودیت زمانی",
        "description": "قیمت هر گیگ سرویس بدون انقضای زمانی",
        "toman_key": "price_per_gb_no_expiry",
        "stars_key": "stars_per_gb_no_expiry",
        "default_toman": 7000,
        "default_stars": 3,
        "emoji": "safe",
    },
}


CURRENCIES = {
    "toman": {
        "title": "تومان",
        "symbol": "تومان",
        "key_type": "toman_key",
        "emoji": "wallet",
    },
    "stars": {
        "title": "استارز",
        "symbol": "Stars",
        "key_type": "stars_key",
        "emoji": "stars",
    },
}


MAX_TOMAN_PRICE = 500_000_000
MAX_STARS_PRICE = 1_000_000


# ============================================================
# HELPERS
# ============================================================

def get_emoji_id(key: str) -> Optional[str]:
    if not PREMIUM_EMOJI_ENABLED:
        return None

    emoji_id = PREMIUM_EMOJIS.get(key)

    if not emoji_id:
        return None

    return str(emoji_id)


def make_button(
    text: str,
    callback_data: str,
    style: str = "primary",
    emoji_key: Optional[str] = None,
) -> InlineKeyboardButton:
    if style not in ("primary", "success", "danger"):
        raise ValueError("Invalid button style")

    kwargs = {
        "text": text,
        "callback_data": callback_data,
        "style": style,
    }

    if emoji_key:
        emoji_id = get_emoji_id(emoji_key)

        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id

    return InlineKeyboardButton(**kwargs)


def keyboard(
    rows: list[list[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def read_price(key: str, default: int) -> int:
    try:
        value = get_setting(key, str(default))
        result = int(value)

        if result < 0:
            return default

        return result

    except (TypeError, ValueError):
        logger.warning("Invalid price setting: %s", key)
        return default

    except Exception:
        logger.exception("Could not read price setting: %s", key)
        return default


def format_number(value: int) -> str:
    return f"{value:,}"


def normalize_digits(value: str) -> str:
    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return value.translate(translation)


def parse_price(value: str, currency: str) -> Optional[int]:
    value = normalize_digits(value.strip())
    value = value.replace(",", "").replace("٬", "").replace(" ", "")

    if not re.fullmatch(r"[0-9]+", value):
        return None

    try:
        amount = int(value)
    except ValueError:
        return None

    if amount <= 0:
        return None

    if currency == "toman" and amount > MAX_TOMAN_PRICE:
        return None

    if currency == "stars" and amount > MAX_STARS_PRICE:
        return None

    return amount


def get_item_prices(item_key: str) -> tuple[int, int]:
    item = PRICE_ITEMS[item_key]

    toman = read_price(
        item["toman_key"],
        item["default_toman"],
    )

    stars = read_price(
        item["stars_key"],
        item["default_stars"],
    )

    return toman, stars


def log_price_change(
    user_id: int,
    item_key: str,
    currency: str,
    old_value: int,
    new_value: int,
) -> None:
    try:
        log_event(
            "INFO",
            "price_updated",
            user_id,
            (
                f"item={item_key}; "
                f"currency={currency}; "
                f"old={old_value}; "
                f"new={new_value}"
            ),
        )

    except Exception:
        logger.exception("Could not log price update")


# ============================================================
# MAIN PRICE MENU
# ============================================================

def prices_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            make_button(
                "هر گیگ حجمی",
                "admin:prices:edit:per_gb",
                "primary",
                "blue_dot",
            ),
            make_button(
                "هر گیگ بدون زمان",
                "admin:prices:edit:no_expiry_gb",
                "success",
                "safe",
            ),
        ],
        [
            make_button(
                "یوزر اضافه حجمی",
                "admin:prices:edit:vol_device",
                "danger",
                "telegram",
            ),
            make_button(
                "یوزر اضافه نامحدود بی‌نهایت",
                "admin:prices:edit:unl_device",
                "primary",
                "world",
            ),
        ],
        [
            make_button(
                "یوزر اضافه نامحدود منصفانه",
                "admin:prices:edit:unl_fair_device",
                "success",
                "diamond",
            ),
        ],
        [
            make_button(
                "ماه اضافه حجمی",
                "admin:prices:edit:vol_month",
                "success",
                "light",
            ),
            make_button(
                "ماه اضافه نامحدود",
                "admin:prices:edit:unl_month",
                "danger",
                "diamond",
            ),
        ],
        [
            make_button(
                "بازگشت به پنل",
                "admin:main",
                "danger",
                "back",
            ),
        ],
    ]

    return keyboard(rows)


def get_prices_menu_text() -> str:
    lines = [
        f"{premium_emoji('wallet', '💰')} <b>مدیریت قیمت‌گذاری</b>",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "قیمت‌های فعلی ربات:",
        "",
    ]

    for item_key, item in PRICE_ITEMS.items():
        toman, stars = get_item_prices(item_key)

        lines.extend([
            f"{premium_emoji('blue_dot', '🔹')} <b>{item['title']}</b>",
            f"   {premium_emoji('wallet', '💵')} {format_number(toman)} تومان",
            f"   {premium_emoji('stars', '⭐')} {format_number(stars)} Stars",
            "",
        ])

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "برای تغییر قیمت، مؤلفه موردنظر را انتخاب کن.",
        "",
        "قیمت تومان و Stars کاملاً مستقل هستند.",
    ])

    return "\n".join(lines)


async def show_prices_menu(
    callback: CallbackQuery,
    state: Optional[FSMContext] = None,
) -> None:
    if state:
        await state.clear()

    try:
        if callback.message:
            await callback.message.edit_text(
                text=get_prices_menu_text(),
                reply_markup=prices_menu_keyboard(),
                parse_mode="HTML"
            )
        else:
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=get_prices_menu_text(),
                reply_markup=prices_menu_keyboard(),
                parse_mode="HTML"
            )

    except Exception:
        logger.exception("Could not display prices menu")

        try:
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=get_prices_menu_text(),
                reply_markup=prices_menu_keyboard(),
                parse_mode="HTML"
            )
        except Exception:
            logger.exception("Could not send prices menu fallback")

    await callback.answer()


# ============================================================
# CURRENCY SELECTION
# ============================================================

def currency_selection_keyboard(item_key: str) -> InlineKeyboardMarkup:
    return keyboard([
        [
            make_button(
                "تغییر قیمت تومانی",
                f"admin:prices:currency:{item_key}:toman",
                "success",
                "wallet",
            ),
            make_button(
                "تغییر قیمت Stars",
                f"admin:prices:currency:{item_key}:stars",
                "primary",
                "stars",
            ),
        ],
        [
            make_button(
                "بازگشت به قیمت‌ها",
                "admin:prices:menu",
                "danger",
                "back",
            ),
        ],
    ])


@router.callback_query(F.data == "admin:prices:menu")
async def prices_menu_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    await show_prices_menu(callback, state)


# ============================================================
# SELECT PRICE ITEM
# ============================================================

@router.callback_query(F.data.startswith("admin:prices:edit:"))
async def select_price_item(
    callback: CallbackQuery,
    state: FSMContext,
):
    item_key = callback.data.split(":")[-1]

    item = PRICE_ITEMS.get(item_key)

    if not item:
        await callback.answer(
            "آیتم قیمت‌گذاری نامعتبر است.",
            show_alert=True,
        )
        return

    await state.clear()
    await state.update_data(price_item=item_key)

    toman, stars = get_item_prices(item_key)

    text = (
        f"{premium_emoji('wallet', '💰')} <b>انتخاب نوع قیمت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>مؤلفه:</b> {item['title']}\n\n"
        f"<b>قیمت فعلی تومانی:</b> {format_number(toman)} تومان\n"
        f"<b>قیمت فعلی Stars:</b> {format_number(stars)} Stars\n\n"
        "مشخص کن کدام قیمت را می‌خواهی تغییر بدهی."
    )

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=currency_selection_keyboard(item_key),
            parse_mode="HTML"
        )

    except Exception:
        logger.exception("Could not display currency selection")

    await callback.answer()


# ============================================================
# ASK NEW PRICE
# ============================================================

async def ask_new_price(
    callback: CallbackQuery,
    state: FSMContext,
    item_key: str,
    currency: str,
):
    item = PRICE_ITEMS[item_key]
    currency_info = CURRENCIES[currency]

    toman, stars = get_item_prices(item_key)

    current_value = toman if currency == "toman" else stars
    current_label = (
        f"{format_number(current_value)} {currency_info['symbol']}"
    )

    await state.update_data(
        price_item=item_key,
        price_currency=currency,
        panel_chat=callback.message.chat.id if callback.message else None,
        panel_msg=callback.message.message_id if callback.message else None,
    )

    await state.set_state(PriceStates.waiting_value)

    text = (
        f"{premium_emoji('light', '✏️')} <b>تغییر قیمت</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>مؤلفه:</b> {item['title']}\n"
        f"<b>نوع قیمت:</b> {currency_info['title']}\n"
        f"<b>قیمت فعلی:</b> {current_label}\n\n"
        "مقدار جدید را فقط به‌صورت عدد ارسال کن.\n\n"
        "مثال: <code>50000</code>\n\n"
        "برای لغو، از دکمه زیر استفاده کن."
    )

    cancel_keyboard = keyboard([
        [
            make_button(
                "لغو عملیات",
                "admin:prices:cancel",
                "danger",
                "false",
            ),
        ],
    ])

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=cancel_keyboard,
            parse_mode="HTML"
        )

    except Exception:
        logger.exception("Could not display price input prompt")

    await callback.answer()


@router.callback_query(F.data.startswith("admin:prices:currency:"))
async def select_currency(
    callback: CallbackQuery,
    state: FSMContext,
):
    parts = callback.data.split(":")

    if len(parts) != 5:
        await callback.answer(
            "درخواست نامعتبر است.",
            show_alert=True,
        )
        return

    item_key = parts[3]
    currency = parts[4]

    if item_key not in PRICE_ITEMS or currency not in CURRENCIES:
        await callback.answer(
            "انتخاب نامعتبر است.",
            show_alert=True,
        )
        return

    await ask_new_price(
        callback,
        state,
        item_key,
        currency,
    )


# ============================================================
# RECEIVE NEW PRICE
# ============================================================

@router.message(PriceStates.waiting_value)
async def receive_new_price(
    message: Message,
    state: FSMContext,
):
    user = message.from_user

    data = await state.get_data()

    item_key = data.get("price_item")
    currency = data.get("price_currency")

    if item_key not in PRICE_ITEMS or currency not in CURRENCIES:
        await state.clear()

        await message.answer(
            f"{premium_emoji('false', '❌')} اطلاعات عملیات قیمت‌گذاری معتبر نیست. دوباره از پنل وارد شو.",
            parse_mode="HTML"
        )
        return

    raw_value = message.text or ""

    amount = parse_price(raw_value, currency)

    if amount is None:
        if currency == "toman":
            limit_text = f"حداکثر {MAX_TOMAN_PRICE:,} تومان"
        else:
            limit_text = f"حداکثر {MAX_STARS_PRICE:,} Stars"

        await message.answer(
            f"{premium_emoji('false', '❌')} <b>مبلغ واردشده معتبر نیست.</b>\n\n"
            "لطفاً فقط عدد صحیح مثبت ارسال کن.\n"
            f"محدوده مجاز: {limit_text}",
            parse_mode="HTML"
        )
        return

    item = PRICE_ITEMS[item_key]
    currency_info = CURRENCIES[currency]

    setting_key = item[currency_info["key_type"]]

    old_value = read_price(
        setting_key,
        (
            item["default_toman"]
            if currency == "toman"
            else item["default_stars"]
        ),
    )

    try:
        set_setting(setting_key, str(amount))

    except Exception:
        logger.exception(
            "Could not save price setting: %s",
            setting_key,
        )

        await message.answer(
            f"{premium_emoji('warning', '❌')} ذخیره قیمت ناموفق بود. دیتابیس را بررسی کن.",
            parse_mode="HTML"
        )
        return

    log_price_change(
        user.id,
        item_key,
        currency,
        old_value,
        amount,
    )

    await state.clear()

    toman, stars = get_item_prices(item_key)

    success_text = (
        f"{premium_emoji('true', '✅')} <b>قیمت با موفقیت تغییر کرد.</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>مؤلفه:</b> {item['title']}\n"
        f"<b>قیمت قبلی:</b> {format_number(old_value)} "
        f"{currency_info['symbol']}\n"
        f"<b>قیمت جدید:</b> {format_number(amount)} "
        f"{currency_info['symbol']}\n\n"
        "قیمت‌های فعلی این مؤلفه:\n"
        f"{premium_emoji('wallet', '💵')} تومان: {format_number(toman)}\n"
        f"{premium_emoji('stars', '⭐')} Stars: {format_number(stars)}"
    )

    success_markup = keyboard([
        [
            make_button(
                "مدیریت سایر قیمت‌ها",
                "admin:prices:menu",
                "success",
                "shop",
            ),
            make_button(
                "بازگشت به پنل",
                "admin:main",
                "danger",
                "back",
            ),
        ],
    ])

    # همان پیام پنل ویرایش می‌شود (پیام عددِ ادمین پاک می‌شود)؛
    # اگر نشد، پیام جدید ارسال می‌شود.
    panel_chat = data.get("panel_chat")
    panel_msg = data.get("panel_msg")
    edited = False

    if panel_chat and panel_msg:
        try:
            await message.bot.edit_message_text(
                chat_id=panel_chat,
                message_id=panel_msg,
                text=success_text,
                reply_markup=success_markup,
                parse_mode="HTML"
            )
            edited = True
        except Exception:
            logger.warning("Could not edit price panel message", exc_info=True)

    if edited:
        try:
            await message.delete()
        except Exception:
            pass
    else:
        await message.answer(success_text, reply_markup=success_markup, parse_mode="HTML")

    logger.info(
        "Admin %s updated %s (%s): %s -> %s",
        user.id,
        item_key,
        currency,
        old_value,
        amount,
    )


# ============================================================
# CANCEL PRICE EDIT
# ============================================================

@router.callback_query(F.data == "admin:prices:cancel")
async def cancel_price_edit(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    try:
        await callback.message.edit_text(
            text=f"{premium_emoji('false', '❌')} <b>عملیات تغییر قیمت لغو شد.</b>",
            reply_markup=keyboard([
                [
                    make_button(
                        "بازگشت به قیمت‌ها",
                        "admin:prices:menu",
                        "primary",
                        "shop",
                    ),
                    make_button(
                        "بازگشت به پنل",
                        "admin:main",
                        "danger",
                        "back",
                    ),
                ],
            ]),
            parse_mode="HTML"
        )

    except Exception:
        logger.exception("Could not cancel price edit")

    await callback.answer("عملیات لغو شد.")
