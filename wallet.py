
import logging
import re
import html

from dev.alerts import send_alert
from aiogram import Router, F, Bot
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import CARD_NUMBER, CARD_HOLDER, ADMIN_ID, DEFAULT_RECEIPT_ADMIN_ID

# Official Premium Emoji IDs supplied for this project.
WALLET_EMOJI_IDS = {
    "wallet": "5445353829304387411",
    "shop": "5211047317309767043",
    "gift": "5264853679751600327",
    "back": "5325598171018051937",
    "light": "5827691921073512783",
    "false": "5440681540541502133",
    "true": "5440547189669516347",
    "warning": "5440739793182937488",
    "telegram": "5440389890787281213",
    "blue_pixel": "5244609046459228264",
    "safe": "5197288647275071607",
}
from database import (
    get_balance,
    create_payment_request,
    get_payment_request,
    approve_payment,
    reject_payment,
    get_wallet_transactions,
    record_user_activity,
    log_event,
)
from utils import rate_limiter
import ui


logger = logging.getLogger(__name__)
router = Router(name="wallet")


# ============================================================
# States
# ============================================================

class WalletStates(StatesGroup):
    waiting_amount = State()
    waiting_receipt = State()


# ============================================================
# Constants
# ============================================================

MIN_AMOUNT = 10_000
MAX_AMOUNT = 50_000_000
MAX_RECEIPT_SIZE = 5 * 1024 * 1024


# ============================================================
# Keyboards - Inline فقط
# ============================================================

def wallet_button(
    text: str,
    callback_data: str,
    emoji_key: str,
    style: str = "primary",
) -> InlineKeyboardButton:
    """ساخت دکمه یکپارچه با Premium Emoji و رنگ مشخص."""
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=WALLET_EMOJI_IDS[emoji_key],
        style=style,
    )


def wallet_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                wallet_button("شارژ کیف پول", "wallet:charge", "shop", "success"),
                wallet_button("تاریخچه تراکنش‌ها", "wallet:history", "light", "primary"),
            ],
            [
                wallet_button("بازگشت به منوی اصلی", "menu:main", "back", "danger")
            ],
        ]
    )


def wallet_history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [wallet_button("بازگشت به کیف پول", "menu:wallet", "back", "danger")]
        ]
    )


def wallet_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [wallet_button("لغو عملیات", "wallet:cancel", "false", "danger")]
        ]
    )


# ============================================================
# Helpers
# ============================================================

def normalize_digits(value: str) -> str:
    """تبدیل اعداد فارسی و عربی به انگلیسی."""

    persian = "۰۱۲۳۴۵۶۷۸۹"
    arabic = "٠١٢٣٤٥٦٧٨٩"

    for index, digit in enumerate(persian):
        value = value.replace(digit, str(index))

    for index, digit in enumerate(arabic):
        value = value.replace(digit, str(index))

    return value


def parse_amount(value: str) -> int | None:
    """اعتبارسنجی مبلغ واردشده توسط کاربر."""

    if not value:
        return None

    value = normalize_digits(value.strip())
    value = value.replace(",", "").replace("٬", "").replace(" ", "")

    if not re.fullmatch(r"\d+", value):
        return None

    try:
        amount = int(value)
    except ValueError:
        return None

    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        return None

    return amount


async def safe_edit_message(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """
    نمایش صفحه‌ی کیف پول.

    اگر کلیک از روی پیام اصلی (Rich) باشد، پیام جدید ارسال می‌شود
    و پیام اصلی ویرایش نمی‌شود؛ در غیر این صورت پیام Inline فعلی
    ویرایش می‌شود.
    """

    await ui.show(callback, text, reply_markup)


async def answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
):
    """پاسخ سریع به Callback برای جلوگیری از منقضی شدن Query."""

    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception:
        logger.warning("Could not answer callback query.")


def premium_emoji(key: str) -> str:
    """نمایش Premium Emoji در متن پیام با HTML تلگرام."""
    emoji_id = WALLET_EMOJI_IDS[key]
    return f"<tg-emoji emoji-id='{emoji_id}'>⭐</tg-emoji>"


def get_wallet_text(user_id: int) -> str:
    balance = get_balance(user_id)

    return (
        f"{premium_emoji('wallet')} <b>کیف پول من</b>\n"
        "<i>مدیریت موجودی و تراکنش‌های حساب شما</i>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{premium_emoji('safe')} <b>موجودی قابل استفاده</b>\n"
        f"<code>{balance:,}</code> <b>تومان</b>\n\n"
        "از گزینه‌های زیر برای شارژ حساب یا مشاهده سوابق مالی استفاده کنید."
    )

# ============================================================
# Wallet Main Page
# ============================================================

@router.callback_query(F.data.in_({"menu:wallet", "wallet:main"}))
async def show_wallet(
    callback: CallbackQuery,
    state: FSMContext,
):
    await answer_callback(callback)

    user = callback.from_user
    if not user:
        return

    await state.clear()

    try:
        record_user_activity(user.id)

        text = get_wallet_text(user.id)

        await safe_edit_message(
            callback,
            text,
            wallet_main_keyboard(),
        )

    except Exception:
        logger.exception("Failed to display wallet.")
        send_alert("ERROR", f"خطا در نمایش کیف پول\nکاربر: {user.id}")

        if isinstance(callback.message, Message):
            await callback.message.answer(
                "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> در حال حاضر امکان نمایش کیف پول وجود ندارد. لطفاً کمی بعد دوباره تلاش کنید."
            )


# ============================================================
# Wallet History
# ============================================================

@router.callback_query(F.data == "wallet:history")
async def show_wallet_history(
    callback: CallbackQuery,
    state: FSMContext,
):
    await answer_callback(callback)

    user = callback.from_user
    if not user:
        return

    await state.clear()

    try:
        transactions = get_wallet_transactions(user.id, limit=10)

        if not transactions:
            text = (
                "<tg-emoji emoji-id='5827691921073512783'>⭐</tg-emoji> <b>تاریخچه تراکنش‌ها</b>\n\n"
                "هنوز هیچ تراکنشی برای حساب شما ثبت نشده است."
            )

        else:
            lines = [
                "<tg-emoji emoji-id='5827691921073512783'>⭐</tg-emoji> <b>۱۰ تراکنش آخر کیف پول</b>\n"
            ]

            for tx in transactions:
                amount = int(tx["amount"])
                sign = "+" if amount > 0 else ""

                tx_type = html.escape(str(tx.get("type", "نامشخص")))
                description = html.escape(
                    str(tx.get("description") or "")
                )

                lines.append(
                    f"• {sign}{amount:,} تومان\n"
                    f"نوع: {tx_type}\n"
                    f"توضیحات: {description}\n"
                )

            text = "\n".join(lines)

        await safe_edit_message(
            callback,
            text,
            wallet_history_keyboard(),
        )

    except Exception:
        logger.exception("Failed to load wallet history.")
        send_alert("ERROR", f"خطا در دریافت تاریخچه کیف پول\nکاربر: {user.id}")

        if isinstance(callback.message, Message):
            await callback.message.answer(
                "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> دریافت تاریخچه تراکنش‌ها با مشکل مواجه شد."
            )


# ============================================================
# Start Wallet Charge
# ============================================================

@router.callback_query(F.data == "wallet:charge")
async def start_wallet_charge(
    callback: CallbackQuery,
    state: FSMContext,
):
    await answer_callback(callback)

    user = callback.from_user
    if not user:
        return

    if not rate_limiter.is_allowed(user.id):
        await callback.answer(
            "<tg-emoji emoji-id='5827691921073512783'>⭐</tg-emoji> لطفاً کمی صبر کنید و دوباره تلاش کنید.",
            show_alert=True,
        )
        return

    await state.clear()
    await state.set_state(WalletStates.waiting_amount)

    text = (
        "<tg-emoji emoji-id='5211047317309767043'>⭐</tg-emoji> <b>شارژ کیف پول</b>\n\n"
        "مرحله ۱ از ۳: <b>انتخاب مبلغ</b>\n\n"
        "مبلغ موردنظر خود را به تومان وارد کنید.\n\n"
        f"<tg-emoji emoji-id='5244609046459228264'>⭐</tg-emoji> حداقل مبلغ: <b>{MIN_AMOUNT:,} تومان</b>\n"
        f"<tg-emoji emoji-id='5244609046459228264'>⭐</tg-emoji> حداکثر مبلغ: <b>{MAX_AMOUNT:,} تومان</b>\n\n"
        "مثال: <code>50000</code>\n\n"
        "پس از ثبت مبلغ، اطلاعات پرداخت برای شما نمایش داده می‌شود."
    )

    await safe_edit_message(
        callback,
        text,
        wallet_cancel_keyboard(),
    )


# ============================================================
# Receive Amount
# ============================================================

@router.message(WalletStates.waiting_amount)
async def receive_wallet_amount(
    message: Message,
    state: FSMContext,
):
    user = message.from_user
    if not user:
        return

    amount = parse_amount(message.text or "")

    if amount is None:
        await message.answer(
            "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> <b>مبلغ واردشده معتبر نیست.</b>\n\n"
            f"لطفاً مبلغی بین {MIN_AMOUNT:,} تا "
            f"{MAX_AMOUNT:,} تومان وارد کنید.\n\n"
            "مثال: <code>50000</code>",
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(WalletStates.waiting_receipt)

    text = (
        "<tg-emoji emoji-id='5211047317309767043'>⭐</tg-emoji> <b>اطلاعات پرداخت</b>\n\n"
        "مرحله ۲ از ۳: <b>واریز مبلغ</b>\n\n"
        f"<tg-emoji emoji-id='5445353829304387411'>⭐</tg-emoji> مبلغ قابل پرداخت: <b>{amount:,} تومان</b>\n\n"
        f"<tg-emoji emoji-id='5197288647275071607'>⭐</tg-emoji> شماره کارت:\n<code>{html.escape(CARD_NUMBER)}</code>\n\n"
        f"<tg-emoji emoji-id='5440389890787281213'>⭐</tg-emoji> نام صاحب حساب: <b>{html.escape(CARD_HOLDER)}</b>\n\n"
        "لطفاً مبلغ دقیق بالا را به شماره کارت اعلام‌شده واریز کنید.\n\n"
        "بعد از پرداخت، <b>عکس رسید بانکی</b> را همین‌جا ارسال کنید.\n\n"
        "<tg-emoji emoji-id='5827691921073512783'>⭐</tg-emoji> فقط عکس رسید پذیرفته می‌شود؛ فایل یا متن قابل قبول نیست."
    )

    await message.answer(
        text,
        reply_markup=wallet_cancel_keyboard(),
    )


# ============================================================
# Receive Receipt Photo
# ============================================================

@router.message(WalletStates.waiting_receipt, F.photo)
async def receive_wallet_receipt(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    user = message.from_user
    if not user:
        return

    photo = message.photo[-1]

    if photo.file_size and photo.file_size > MAX_RECEIPT_SIZE:
        await message.answer(
            "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> حجم عکس رسید بیشتر از ۵ مگابایت است.\n"
            "لطفاً تصویر کم‌حجم‌تری ارسال کنید."
        )
        return

    data = await state.get_data()
    amount = data.get("amount")

    if not amount:
        await state.clear()

        await message.answer(
            "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> اطلاعات درخواست شارژ پیدا نشد.\n"
            "لطفاً دوباره از بخش کیف پول شروع کنید."
        )
        return

    try:
        req_id = create_payment_request(
            telegram_id=user.id,
            amount=amount,
            photo_file_id=photo.file_id,
        )

    except Exception:
        logger.exception("Could not create wallet payment request.")
        send_alert("ERROR", f"خطا در ثبت درخواست شارژ\nکاربر: {user.id}\nمبلغ: {amount}")

        await message.answer(
            "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> هنگام ثبت رسید مشکلی رخ داد.\n"
            "لطفاً چند دقیقه دیگر دوباره تلاش کنید."
        )
        return

    if not req_id:
        await state.clear()

        await message.answer(
            "<tg-emoji emoji-id='5440739793182937488'>⭐</tg-emoji>️ <b>درخواست قبلی شما هنوز در حال بررسی است.</b>\n\n"
            "تا زمان تعیین وضعیت درخواست قبلی، امکان ثبت درخواست جدید وجود ندارد."
        )
        return

    await state.clear()

    await message.answer(
        "<tg-emoji emoji-id='5440547189669516347'>⭐</tg-emoji> <b>رسید شما با موفقیت دریافت شد.</b>\n\n"
        f"<tg-emoji emoji-id='5445353829304387411'>⭐</tg-emoji> مبلغ درخواست: <b>{amount:,} تومان</b>\n"
        f"<tg-emoji emoji-id='5244609046459228264'>⭐</tg-emoji> شماره درخواست: <code>{req_id}</code>\n\n"
        "<tg-emoji emoji-id='5440389890787281213'>⭐</tg-emoji> درخواست شما برای ادمین ارسال شد.\n\n"
        "<tg-emoji emoji-id='5827691921073512783'>⭐</tg-emoji> وضعیت درخواست: <b>در انتظار بررسی</b>\n\n"
        "پس از تأیید پرداخت، مبلغ به موجودی کیف پول شما اضافه خواهد شد."
    )

    await notify_admin_receipt(
        bot=bot,
        req_id=req_id,
        user_id=user.id,
        amount=amount,
        file_id=photo.file_id,
    )

    try:
        log_event(
            "INFO",
            "payment_receipt_submitted",
            user.id,
            f"req_id={req_id}, amount={amount}",
        )
    except Exception:
        logger.exception("Could not log payment receipt submission.")


# ============================================================
# Invalid Receipt
# ============================================================

@router.message(WalletStates.waiting_receipt)
async def invalid_wallet_receipt(message: Message):
    await message.answer(
        "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> <b>نوع فایل ارسالی صحیح نیست.</b>\n\n"
        "لطفاً فقط عکس رسید بانکی را ارسال کنید.\n"
        "از ارسال متن، ویدیو یا فایل خودداری کنید."
    )


# ============================================================
# Cancel Wallet Operation
# ============================================================

@router.callback_query(F.data == "wallet:cancel")
async def cancel_wallet_operation(
    callback: CallbackQuery,
    state: FSMContext,
):
    await answer_callback(callback, "عملیات لغو شد.")

    await state.clear()

    user = callback.from_user
    if not user:
        return

    try:
        text = get_wallet_text(user.id)

        await safe_edit_message(
            callback,
            "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> <b>عملیات شارژ لغو شد.</b>\n\n" + text,
            wallet_main_keyboard(),
        )

    except Exception:
        logger.exception("Failed to cancel wallet operation.")
        send_alert("WARNING", f"خطا در لغو عملیات شارژ\nکاربر: {user.id}")


# ============================================================
# Notify Admin
# ============================================================

async def notify_admin_receipt(
    bot: Bot,
    req_id: int,
    user_id: int,
    amount: int,
    file_id: str,
):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                wallet_button(
                    "تأیید پرداخت",
                    f"admin:pay:approve:{req_id}",
                    "true",
                    "success",
                ),
                wallet_button(
                    "رد پرداخت",
                    f"admin:pay:reject:{req_id}",
                    "false",
                    "danger",
                ),
            ]
        ]
    )

    caption = (
        "<tg-emoji emoji-id='5264853679751600327'>⭐</tg-emoji> <b>درخواست شارژ جدید</b>\n\n"
        f"<tg-emoji emoji-id='5440389890787281213'>⭐</tg-emoji> شناسه کاربر: <code>{user_id}</code>\n"
        f"<tg-emoji emoji-id='5445353829304387411'>⭐</tg-emoji> مبلغ: <b>{amount:,} تومان</b>\n"
        f"<tg-emoji emoji-id='5244609046459228264'>⭐</tg-emoji> شماره درخواست: <code>{req_id}</code>\n\n"
        "وضعیت: <b>در انتظار بررسی</b>"
    )

    from database import get_receipt_admin_id

    target = get_receipt_admin_id(DEFAULT_RECEIPT_ADMIN_ID)

    try:
        await bot.send_photo(
            chat_id=target,
            photo=file_id,
            caption=caption,
            reply_markup=keyboard,
        )
        return

    except Exception:
        logger.exception(
            "Failed to send payment receipt to receipt admin %s. Request ID: %s",
            target,
            req_id,
        )
        send_alert("ERROR", f"ارسال رسید به ادمین رسید ناموفق\nreq_id: {req_id}\nuser: {user_id}\nadmin: {target}")

    # اگر ادمین رسید در دسترس نبود (مثلاً ربات را استارت نکرده)، به ادمین اصلی می‌رود.
    if target != ADMIN_ID:
        try:
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=file_id,
                caption=caption + "\n\n⚠️ ارسال برای ادمین رسید ناموفق بود.",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Fallback receipt delivery failed. Request ID: %s", req_id)
            send_alert("ERROR", f"ارسال رسید به ادمین اصلی هم ناموفق بود\nreq_id: {req_id}\nuser: {user_id}")


# ============================================================
# Admin - Approve Payment
# ============================================================

def _is_receipt_reviewer(user_id: int) -> bool:
    from database import get_receipt_admin_id

    return user_id == ADMIN_ID or user_id == get_receipt_admin_id(DEFAULT_RECEIPT_ADMIN_ID)


@router.callback_query(F.data.startswith("admin:pay:approve:"))
async def admin_approve_payment(
    callback: CallbackQuery,
    bot: Bot,
):
    if not _is_receipt_reviewer(callback.from_user.id):
        await answer_callback(
            callback,
            "دسترسی غیرمجاز.",
            show_alert=True,
        )

        try:
            log_event(
                "SECURITY",
                "unauthorized_admin_payment",
                callback.from_user.id,
            )
        except Exception:
            logger.exception("Could not log unauthorized approval.")

        return

    await answer_callback(callback, "در حال بررسی درخواست...")

    try:
        req_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError, AttributeError):
        await answer_callback(
            callback,
            "شناسه درخواست نامعتبر است.",
            show_alert=True,
        )
        return

    try:
        req = get_payment_request(req_id)

        if not req or req["status"] != "pending":
            await answer_callback(
                callback,
                "این درخواست پیدا نشد یا قبلاً بررسی شده است.",
                show_alert=True,
            )
            return

        if not approve_payment(req_id, callback.from_user.id):
            send_alert("WARNING", f"تأیید پرداخت ناموفق (منطقی)\nreq_id: {req_id}\nadmin: {callback.from_user.id}")
            await answer_callback(
                callback,
                "تأیید پرداخت انجام نشد.",
                show_alert=True,
            )
            return

        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_caption(
                    caption=(
                        "<tg-emoji emoji-id='5440547189669516347'>⭐</tg-emoji> <b>پرداخت تأیید شد.</b>\n\n"
                        f"شناسه درخواست: <code>{req_id}</code>\n"
                        f"مبلغ: <b>{req['amount']:,} تومان</b>"
                    ),
                    reply_markup=None,
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    logger.exception("Could not update approved receipt message.")

        try:
            await bot.send_message(
                chat_id=req["telegram_id"],
                text=(
                    "<tg-emoji emoji-id='5440547189669516347'>⭐</tg-emoji> <b>پرداخت شما تأیید شد.</b>\n\n"
                    f"<tg-emoji emoji-id='5445353829304387411'>⭐</tg-emoji> مبلغ افزوده‌شده: <b>{req['amount']:,} تومان</b>\n\n"
                    "مبلغ با موفقیت به کیف پول شما اضافه شد."
                ),
            )
        except Exception:
            logger.exception("Could not notify user about approved payment.")

        try:
            log_event(
                "INFO",
                "payment_approved",
                req["telegram_id"],
                f"req_id={req_id}",
            )
        except Exception:
            logger.exception("Could not log payment approval.")

    except Exception:
        logger.exception("Payment approval failed.")
        send_alert("ERROR", f"خطا در تأیید پرداخت\nreq_id: {req_id}\nadmin: {callback.from_user.id}")

        await answer_callback(
            callback,
            "خطای داخلی هنگام تأیید پرداخت.",
            show_alert=True,
        )


# ============================================================
# Admin - Reject Payment
# ============================================================

@router.callback_query(F.data.startswith("admin:pay:reject:"))
async def admin_reject_payment(
    callback: CallbackQuery,
    bot: Bot,
):
    if not _is_receipt_reviewer(callback.from_user.id):
        await answer_callback(
            callback,
            "دسترسی غیرمجاز.",
            show_alert=True,
        )

        try:
            log_event(
                "SECURITY",
                "unauthorized_admin_payment",
                callback.from_user.id,
            )
        except Exception:
            logger.exception("Could not log unauthorized rejection.")

        return

    await answer_callback(callback, "در حال بررسی درخواست...")

    try:
        req_id = int(callback.data.split(":")[3])
    except (ValueError, IndexError, AttributeError):
        await answer_callback(
            callback,
            "شناسه درخواست نامعتبر است.",
            show_alert=True,
        )
        return

    try:
        req = get_payment_request(req_id)

        if not req or req["status"] != "pending":
            await answer_callback(
                callback,
                "این درخواست پیدا نشد یا قبلاً بررسی شده است.",
                show_alert=True,
            )
            return

        if not reject_payment(req_id, callback.from_user.id):
            send_alert("WARNING", f"رد پرداخت ناموفق (منطقی)\nreq_id: {req_id}\nadmin: {callback.from_user.id}")
            await answer_callback(
                callback,
                "رد درخواست انجام نشد.",
                show_alert=True,
            )
            return

        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_caption(
                    caption=(
                        "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> <b>درخواست پرداخت رد شد.</b>\n\n"
                        f"شناسه درخواست: <code>{req_id}</code>"
                    ),
                    reply_markup=None,
                )
            except TelegramBadRequest as error:
                if "message is not modified" not in str(error).lower():
                    logger.exception("Could not update rejected receipt message.")

        try:
            await bot.send_message(
                chat_id=req["telegram_id"],
                text=(
                    "<tg-emoji emoji-id='5440681540541502133'>⭐</tg-emoji> <b>درخواست شارژ کیف پول شما رد شد.</b>\n\n"
                    f"<tg-emoji emoji-id='5445353829304387411'>⭐</tg-emoji> مبلغ درخواست: <b>{req['amount']:,} تومان</b>\n\n"
                    "اگر فکر می‌کنید این تصمیم اشتباه است، "
                    "لطفاً با پشتیبانی تماس بگیرید."
                ),
            )
        except Exception:
            logger.exception("Could not notify user about rejected payment.")

        try:
            log_event(
                "INFO",
                "payment_rejected",
                req["telegram_id"],
                f"req_id={req_id}",
            )
        except Exception:
            logger.exception("Could not log payment rejection.")

    except Exception:
        logger.exception("Payment rejection failed.")
        send_alert("ERROR", f"خطا در رد پرداخت\nreq_id: {req_id}\nadmin: {callback.from_user.id}")

        await answer_callback(
            callback,
            "خطای داخلی هنگام رد درخواست.",
            show_alert=True,
        )
