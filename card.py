import logging
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import CARD_NUMBER, CARD_HOLDER, ADMIN_ID
from database import (
    get_user, get_balance, create_payment_request, get_payment_request,
    approve_payment, reject_payment, get_setting, log_event
)
from rich import build_rich_message, premium_emoji, build_button_row, build_button
from utils import rate_limiter, validate_amount

logger = logging.getLogger(__name__)
router = Router(name="card_payment")


class CardPaymentStates(StatesGroup):
    waiting_amount = State()
    waiting_receipt = State()


# ============================================================
# شروع فرآیند شارژ کارت‌به‌کارت (از wallet.py صدا زده می‌شود)
# ============================================================

@router.callback_query(F.data == "wallet:charge")
async def start_card_payment(callback: CallbackQuery, state: FSMContext):
    user = callback.from_user
    if not user or not rate_limiter.is_allowed(user.id):
        await callback.answer("⏳ لطفاً کمی صبر کنید.", show_alert=True)
        return

    await state.set_state(CardPaymentStates.waiting_amount)

    text = (
        f"{premium_emoji('wallet', '💰')} **شارژ کیف پول (کارت‌به‌کارت)**\n\n"
        f"🔵 شماره کارت:\n`{CARD_NUMBER}`\n"
        f"👤 به نام: {CARD_HOLDER}\n\n"
        f"لطفاً مبلغ مورد نظر را به **تومان** وارد کنید (حداقل ۱۰,۰۰۰ تومان):"
    )
    try:
        await callback.message.edit_text(rich_message=build_rich_message(text))
    except Exception as e:
        logger.exception(f"Failed to start card payment: {e}")
    await callback.answer()


# ============================================================
# دریافت مبلغ از کاربر
# ============================================================

@router.message(CardPaymentStates.waiting_amount)
async def receive_amount(message: Message, state: FSMContext):
    user = message.from_user
    if not user:
        return

    amount = validate_amount(message.text or "")
    if amount is None:
        await message.answer(
            "❌ مبلغ نامعتبر است. لطفاً یک عدد بین ۱۰,۰۰۰ تا ۵۰,۰۰۰,۰۰۰ تومان وارد کنید."
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(CardPaymentStates.waiting_receipt)

    text = (
        f"{premium_emoji('true', '✅')} مبلغ **{amount:,} تومان** ثبت شد.\n\n"
        f"حالا لطفاً **عکس رسید پرداخت** را ارسال کنید.\n"
        f"(فقط عکس، حداکثر ۵ مگابایت)"
    )
    await message.answer_rich(rich_message=build_rich_message(text))


# ============================================================
# دریافت عکس رسید
# ============================================================

@router.message(CardPaymentStates.waiting_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext, bot):
    user = message.from_user
    if not user:
        return

    photo = message.photo[-1]
    if photo.file_size and photo.file_size > 5 * 1024 * 1024:
        await message.answer("❌ حجم عکس بیش از ۵ مگابایت است. لطفاً عکس کوچک‌تری ارسال کنید.")
        return

    data = await state.get_data()
    amount = data.get("amount")
    if not amount:
        await state.clear()
        await message.answer("خطا در پردازش. لطفاً از ابتدا شروع کنید.")
        return

    req_id = create_payment_request(
        telegram_id=user.id,
        amount=amount,
        photo_file_id=photo.file_id
    )
    if not req_id:
        await message.answer("❌ شما یک درخواست در حال بررسی دارید. لطفاً منتظر تأیید ادمین باشید.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"{premium_emoji('true', '✅')} رسید شما دریافت شد.\n\n"
        f"درخواست شما برای بررسی به ادمین ارسال شد. پس از تأیید، موجودی کیف پول شما به‌روزرسانی می‌شود."
    )

    await _notify_admin_receipt(bot, req_id, user.id, amount, photo.file_id)
    log_event("INFO", "card_receipt_submitted", user.id, f"req_id={req_id}, amount={amount}")


@router.message(CardPaymentStates.waiting_receipt)
async def invalid_receipt(message: Message):
    await message.answer("❌ لطفاً فقط **عکس رسید** را ارسال کنید (نه فایل، نه متن).")


# ============================================================
# اطلاع‌رسانی به ادمین
# ============================================================

async def _notify_admin_receipt(bot, req_id: int, user_id: int, amount: int, file_id: str):
    # دریافت ادمین رسید از settings (پیش‌فرض ADMIN_ID)
    receipt_admin_str = get_setting("receipt_admin_id", str(ADMIN_ID))
    try:
        receipt_admin_id = int(receipt_admin_str)
    except (ValueError, TypeError):
        receipt_admin_id = ADMIN_ID

    text = (
        f"📥 **درخواست شارژ جدید**\n\n"
        f"👤 کاربر: `{user_id}`\n"
        f"💰 مبلغ: **{amount:,} تومان**\n"
        f"🆔 شناسه: `{req_id}`"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تأیید", callback_data=f"admin:pay:approve:{req_id}"),
            InlineKeyboardButton(text="❌ رد", callback_data=f"admin:pay:reject:{req_id}"),
        ]
    ])
    try:
        await bot.send_photo(
            chat_id=receipt_admin_id,
            photo=file_id,
            caption=text,
            reply_markup=kb
        )
    except Exception as e:
        logger.exception(f"Failed to notify admin {receipt_admin_id}: {e}")


# ============================================================
# تأیید / رد توسط ادمین
# ============================================================

@router.callback_query(F.data.startswith("admin:pay:approve:"))
async def admin_approve_payment(callback: CallbackQuery, bot):
    user = callback.from_user
    receipt_admin_str = get_setting("receipt_admin_id", str(ADMIN_ID))
    try:
        receipt_admin_id = int(receipt_admin_str)
    except (ValueError, TypeError):
        receipt_admin_id = ADMIN_ID

    if user.id not in (ADMIN_ID, receipt_admin_id):
        log_event("SECURITY", "unauthorized_payment_approval", user.id, f"data={callback.data}")
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    req_id = int(callback.data.split(":")[3])
    req = get_payment_request(req_id)
    if not req or req["status"] != "pending":
        await callback.answer("درخواست یافت نشد یا قبلاً بررسی شده.", show_alert=True)
        return

    if approve_payment(req_id, user.id):
        await callback.message.edit_caption(caption="✅ **تأیید شد** و مبلغ به کیف پول کاربر اضافه شد.")
        try:
            await bot.send_message(
                req["telegram_id"],
                f"✅ درخواست شارژ شما به مبلغ **{req['amount']:,} تومان** تأیید شد و به کیف پول شما اضافه گردید."
            )
        except Exception:
            pass
        log_event("INFO", "card_payment_approved", req["telegram_id"], f"req_id={req_id}, admin={user.id}")
    else:
        await callback.answer("خطا در تأیید.", show_alert=True)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:pay:reject:"))
async def admin_reject_payment(callback: CallbackQuery, bot):
    user = callback.from_user
    receipt_admin_str = get_setting("receipt_admin_id", str(ADMIN_ID))
    try:
        receipt_admin_id = int(receipt_admin_str)
    except (ValueError, TypeError):
        receipt_admin_id = ADMIN_ID

    if user.id not in (ADMIN_ID, receipt_admin_id):
        log_event("SECURITY", "unauthorized_payment_rejection", user.id, f"data={callback.data}")
        await callback.answer("دسترسی غیرمجاز.", show_alert=True)
        return

    req_id = int(callback.data.split(":")[3])
    req = get_payment_request(req_id)
    if not req or req["status"] != "pending":
        await callback.answer("درخواست یافت نشد یا قبلاً بررسی شده.", show_alert=True)
        return

    if reject_payment(req_id, user.id):
        await callback.message.edit_caption(caption="❌ **رد شد**.")
        try:
            await bot.send_message(
                req["telegram_id"],
                f"❌ متأسفانه درخواست شارژ شما به مبلغ **{req['amount']:,} تومان** رد شد.\n"
                f"برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
            )
        except Exception:
            pass
        log_event("INFO", "card_payment_rejected", req["telegram_id"], f"req_id={req_id}, admin={user.id}")
    else:
        await callback.answer("خطا در رد.", show_alert=True)
    await callback.answer()