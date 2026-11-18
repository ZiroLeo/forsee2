import logging, re
from aiogram import BaseMiddleware
from aiogram.types import Message
from database import get_user, get_setting, set_setting

logger = logging.getLogger(__name__)
_PAT = re.compile(r"^REF_(\d{5,15})$")


class ReferralCaptureMiddleware(BaseMiddleware):
    """payload رفرال را قبل از ForceSub ذخیره می‌کند تا با بلاک شدن /start از بین نرود."""

    async def __call__(self, handler, event: Message, data):
        try:
            user, text = event.from_user, (event.text or "")
            if user and text.startswith("/start "):
                m = _PAT.fullmatch(text.split(maxsplit=1)[1].strip())
                if m and int(m.group(1)) != user.id and not get_user(user.id) \
                        and not get_setting(f"pending_ref:{user.id}", ""):
                    set_setting(f"pending_ref:{user.id}", m.group(1))   # اولین لینک برنده است
        except Exception:
            logger.exception("referral capture failed")
        return await handler(event, data)