
from typing import List, Optional

from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.types.input_rich_block_buttons import InputRichBlockButtons

from config import PREMIUM_EMOJIS, PREMIUM_EMOJI_ENABLED
from rich import build_button, build_button_row


# ============================================================
# BUTTON STYLES
# ============================================================

STYLE_PRIMARY = "primary"   # آبی
STYLE_SUCCESS = "success"   # سبز
STYLE_DANGER = "danger"     # قرمز


# ============================================================
# INLINE BUTTON HELPER
# ============================================================

def _get_button_style(text: str) -> str:
    """
    تعیین رنگ دکمه بر اساس عنوان آن.

    آبی: عملیات عادی و مسیرهای ناوبری
    سبز: تأیید و عملیات موفق
    قرمز: بازگشت، لغو و رد کردن
    """

    normalized = str(text).strip()

    danger_keywords = (
        "بازگشت",
        "لغو",
        "انصراف",
        "رد کردن",
        "حذف",
        "غیرفعال کردن",
    )

    success_keywords = (
        "تایید",
        "تأیید",
        "تکمیل خرید",
        "پرداخت کردم",
        "دریافت اکانت",
        "چرخوندن گردونه",
    )

    if any(word in normalized for word in danger_keywords):
        return STYLE_DANGER

    if any(word in normalized for word in success_keywords):
        return STYLE_SUCCESS

    return STYLE_PRIMARY


# آیکن پریمیوم دکمه‌ها بر اساس callback_data (به ترتیب اولویت)
_ICON_BY_CALLBACK = (
    ("menu:main", "back"),
    ("wheel:spin", "dice"),
    ("test:get", "flower_gift"),
    ("menu:wheel", "dice"),
    ("menu:wallet", "wallet"),
    ("wallet:", "wallet"),
    ("menu:referral", "referral"),
    ("referral:", "referral"),
    ("menu:account", "telegram"),
    ("account:", "telegram"),
    ("menu:support", "ask"),
    ("menu:renew", "world"),
    ("renew:", "world"),
    ("menu:buy", "shop"),
    ("buy:", "shop"),
)


def _icon_id_for(text: str, callback_data: str) -> Optional[str]:
    """شناسه ایموجی پریمیوم دکمه، یا None اگر در دسترس نباشد."""

    if not PREMIUM_EMOJI_ENABLED or not isinstance(PREMIUM_EMOJIS, dict):
        return None

    key = None

    if "بازگشت" in str(text):
        key = "back"
    else:
        for prefix, emoji_key in _ICON_BY_CALLBACK:
            if callback_data.startswith(prefix):
                key = emoji_key
                break

    if not key:
        return None

    emoji_id = str(PREMIUM_EMOJIS.get(key) or "").strip()

    return emoji_id or None


def _inline_keyboard(
    rows: List[List[InlineKeyboardButton]],
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def _button(
    text: str,
    callback_data: str,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    """
    ساخت دکمه Inline با رنگ مشخص.

    اگر style مشخص نشده باشد، رنگ بر اساس عنوان
    دکمه تعیین می‌شود.
    """

    if not isinstance(text, str) or not text.strip():
        raise ValueError("Button text cannot be empty.")

    if not isinstance(callback_data, str) or not callback_data:
        raise ValueError("callback_data cannot be empty.")

    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("callback_data exceeds Telegram's 64-byte limit.")

    selected_style = style or _get_button_style(text)

    if selected_style not in (
        STYLE_PRIMARY,
        STYLE_SUCCESS,
        STYLE_DANGER,
    ):
        raise ValueError(
            f"Invalid button style: {selected_style}"
        )

    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style=selected_style,
        icon_custom_emoji_id=_icon_id_for(text, callback_data),
    )


# ============================================================
# MAIN MENU
# تنها بخشی که از Rich Message استفاده می‌کند
# ============================================================

def main_menu_rich_message() -> List[InputRichBlockButtons]:

    row1 = build_button_row([
        build_button(
            "خرید سرویس",
            callback_data="menu:buy",
            style="primary",
            premium_emoji_key="shop",
        ),
        build_button(
            "تمدید",
            callback_data="menu:renew",
            style="primary",
            premium_emoji_key="world",
        ),
    ])

    row2 = build_button_row([
        build_button(
            "حساب کاربری",
            callback_data="menu:account",
            style="primary",
            premium_emoji_key="telegram",
        ),
        build_button(
            "کیف پول",
            callback_data="menu:wallet",
            style="primary",
            premium_emoji_key="wallet",
        ),
    ])

    row3 = build_button_row([
        build_button(
            "گردونه شانس",
            callback_data="menu:wheel",
            style="danger",
            premium_emoji_key="dice",
        ),
        build_button(
            "آموزش دریافت اپ",
            callback_data="menu:guide",
            style="primary",
            premium_emoji_key="quest",
        ),
    ])

    row4 = build_button_row([
        build_button(
            "اکانت تست",
            callback_data="menu:test",
            style="success",
            premium_emoji_key="flower_gift",
        ),
        build_button(
            "رفرال",
            callback_data="menu:referral",
            style="success",
            premium_emoji_key="referral",
        ),
    ])

    row5 = build_button_row([
        build_button(
            "پشتیبانی",
            callback_data="menu:support",
            style="success",
            premium_emoji_key="ask",
        ),
    ])

    return [
        row1,
        row2,
        row3,
        row4,
        row5,
    ]


# ============================================================
# BUY FLOW
# ============================================================

def buy_type_selection_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("سرویس حجمی", "buy:type:vol"),
            _button("سرویس نامحدود", "buy:type:unl"),
        ],
        [
            _button("بازگشت", "menu:main"),
        ],
    ])


def buy_volume_selection_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("۱۰ گیگ", "buy:vol:10"),
            _button("۲۰ گیگ", "buy:vol:20"),
        ],
        [
            _button("۳۰ گیگ", "buy:vol:30"),
            _button("۵۰ گیگ", "buy:vol:50"),
        ],
        [
            _button("۱۰۰ گیگ", "buy:vol:100"),
            _button("۲۰۰ گیگ", "buy:vol:200"),
        ],
        [
            _button("۵۰۰ گیگ", "buy:vol:500"),
            _button("نامحدود (۱۵۰ گیگ)", "buy:vol:unl150"),
        ],
        [
            _button("نامحدود (بی‌نهایت)", "buy:vol:unl_inf"),
            _button("بازگشت", "buy:main"),
        ],
    ])


def buy_time_selection_keyboard(
    current_months: int,
) -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("-", f"buy:time:dec:{current_months}"),
            _button(f"{current_months} ماه", "buy:time:noop"),
            _button("+", f"buy:time:inc:{current_months}"),
        ],
        [
            _button(
                "تایید و ادامه",
                "buy:time:confirm",
                style=STYLE_SUCCESS,
            ),
            _button("بازگشت", "buy:vol"),
        ],
    ])


def buy_device_selection_keyboard(
    current_devices: int,
) -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("-", f"buy:dev:dec:{current_devices}"),
            _button(f"{current_devices} دستگاه", "buy:dev:noop"),
            _button("+", f"buy:dev:inc:{current_devices}"),
        ],
        [
            _button(
                "تایید و ادامه",
                "buy:dev:confirm",
                style=STYLE_SUCCESS,
            ),
            _button("بازگشت", "buy:time"),
        ],
    ])


def buy_discount_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("کد تخفیف دارم", "buy:discount:enter"),
            _button("رد کردن", "buy:discount:skip"),
        ],
        [
            _button("بازگشت", "buy:dev"),
        ],
    ])


def buy_payment_keyboard(
    price_toman: int,
    price_stars: int,
) -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button(
                f"پرداخت از کیف پول ({price_toman:,} تومان)",
                "buy:pay:wallet",
                style=STYLE_SUCCESS,
            ),
        ],
        [
            _button(
                f"پرداخت با Stars ({price_stars} ⭐)",
                "buy:pay:stars",
                style=STYLE_SUCCESS,
            ),
        ],
        [
            _button("بازگشت", "buy:discount"),
        ],
    ])


# ============================================================
# RENEW FLOW
# ============================================================

def renew_list_keyboard(
    subscriptions: List[dict],
) -> InlineKeyboardMarkup:

    rows = []

    for sub in subscriptions[:7]:

        is_auto = sub.get("auto_renew") == 1

        label = sub["config_name"]

        if is_auto:
            label += " (تمدید خودکار)"

        rows.append([
            _button(
                label,
                f"renew:select:{sub['id']}",
            ),
        ])

    rows.append([
        _button("بازگشت", "menu:main"),
    ])

    return _inline_keyboard(rows)


def renew_type_keyboard(
    subscription_id: int,
    has_auto_renew: bool,
    can_normal_renew: bool,
) -> InlineKeyboardMarkup:

    rows = []

    row1 = []

    if can_normal_renew:
        row1.append(
            _button(
                "تمدید عادی",
                f"renew:normal:{subscription_id}",
            )
        )

    if has_auto_renew:
        row1.append(
            _button(
                "غیرفعال کردن خودکار",
                f"renew:auto_off:{subscription_id}",
            )
        )
    else:
        row1.append(
            _button(
                "تمدید خودکار",
                f"renew:auto:{subscription_id}",
            )
        )

    if row1:
        rows.append(row1)

    rows.append([
        _button("بازگشت", "renew:list"),
    ])

    return _inline_keyboard(rows)


def renew_confirm_keyboard(
    subscription_id: int,
) -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button(
                "تمدید",
                f"renew:do:{subscription_id}",
                style=STYLE_SUCCESS,
            ),
            _button(
                "بازگشت",
                "renew:list",
            ),
        ],
    ])


# ============================================================
# WALLET
# ============================================================

def wallet_main_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("شارژ کیف پول", "wallet:charge"),
            _button("تاریخچه", "wallet:history"),
        ],
        [
            _button("بازگشت", "menu:main"),
        ],
    ])


def wallet_history_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button(
                "بازگشت به کیف پول",
                "menu:wallet",
            ),
        ],
    ])


def wallet_charge_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button(
                "پرداخت کردم",
                "wallet:paid",
                style=STYLE_SUCCESS,
            ),
            _button("پشتیبانی", "menu:support"),
        ],
        [
            _button("بازگشت", "wallet:main"),
        ],
    ])


# ============================================================
# ACCOUNT
# ============================================================

def account_keyboard(
    subscriptions: List[dict],
) -> InlineKeyboardMarkup:

    rows = []

    for sub in subscriptions[:7]:

        rows.append([
            _button(
                sub["config_name"],
                f"account:sub:{sub['id']}",
            ),
        ])

    rows.append([
        _button("بازگشت", "menu:main"),
    ])

    return _inline_keyboard(rows)


def account_subscription_detail_keyboard(
    sub_id: int,
) -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button(
                "بازگشت به حساب",
                "menu:account",
            ),
        ],
    ])


# ============================================================
# WHEEL
# ============================================================

def wheel_keyboard(
    can_spin: bool = True,
) -> InlineKeyboardMarkup:

    spin_text = (
        "چرخوندن گردونه"
        if can_spin
        else "در انتظار"
    )

    spin_callback = (
        "wheel:spin"
        if can_spin
        else "wheel:noop"
    )

    return _inline_keyboard([
        [
            _button(
                spin_text,
                spin_callback,
                style=(
                    STYLE_SUCCESS
                    if can_spin
                    else STYLE_PRIMARY
                ),
            ),
        ],
        [
            _button("بازگشت", "menu:main"),
        ],
    ])


# ============================================================
# REFERRAL
# ============================================================

def referral_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("کپی لینک", "referral:copy"),
            _button("آمار من", "referral:stats"),
        ],
        [
            _button("بازگشت", "menu:main"),
        ],
    ])


def referral_stats_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button(
                "بازگشت",
                "menu:referral",
            ),
        ],
    ])


# ============================================================
# TEST ACCOUNT
# ============================================================

def test_account_keyboard(
    already_used: bool = False,
) -> InlineKeyboardMarkup:

    rows = []

    if not already_used:
        rows.append([
            _button(
                "دریافت اکانت تست",
                "test:get",
                style=STYLE_SUCCESS,
            ),
        ])

    rows.append([
        _button("بازگشت", "menu:main"),
    ])

    return _inline_keyboard(rows)


# ============================================================
# SUPPORT
# ============================================================

def support_keyboard() -> InlineKeyboardMarkup:

    return _inline_keyboard([
        [
            _button("بازگشت", "menu:main"),
        ],
    ])
