"""
database.py
-----------
لایهٔ دیتابیس (SQLite) ربات.

اصول امنیتی و پایداری:
* همهٔ کوئری‌ها پارامتریک هستند (بدون ساخت SQL با رشته‌های ورودی کاربر).
* هر اتصال بعد از استفاده بسته می‌شود (نشت اتصال ندارد).
* تغییرات مالی (کیف پول، پرداخت، تخفیف) داخل تراکنش BEGIN IMMEDIATE انجام می‌شود.
* ورودی‌ها قبل از ذخیره اعتبارسنجی و پاک‌سازی می‌شوند؛ جستجوها LIKE-escape دارند.
* log_event هرگز استثنا نمی‌اندازد (خطای لاگ نباید جلوی کار اصلی را بگیرد).
* فایل دیتابیس با دسترسی ۶۰۰ ساخته می‌شود و بک‌آپ با API رسمی SQLite (سازگار با WAL) گرفته می‌شود.
* مهاجرت‌ها فقط افزایشی‌اند (ستون/جدول جدید)؛ هیچ دیتایی پاک یا بازنویسی نمی‌شود.

همهٔ توابع قبلی با همان نام و امضا باقی مانده‌اند؛ توابع جدید در انتهای فایل هستند.
"""

import os
import re
import time
import sqlite3
import logging
from typing import Optional, Dict, List, Any, Tuple

from config import DB_PATH

logger = logging.getLogger(__name__)

# ============================================================
# اتصال امن
# ============================================================

class _SafeConnection(sqlite3.Connection):
    """
    رفتار `with get_connection() as conn` در sqlite3 فقط commit/rollback می‌کند و
    اتصال را باز می‌گذارد. این کلاس بعد از commit/rollback اتصال را می‌بندد.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            try:
                self.close()
            except Exception:
                pass


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0, factory=_SafeConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA secure_delete=ON;")
    conn.execute("PRAGMA trusted_schema=OFF;")
    return conn

# ============================================================
# اعتبارسنجی و Sanitize
# ============================================================

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

def sanitize_text(value, max_len: int = 500) -> Optional[str]:
    if value is None:
        return None
    text = _CONTROL_CHARS.sub("", str(value)).strip()
    if len(text) > max_len:
        text = text[:max_len]
    return text or None

def validate_telegram_id(tid) -> int:
    try:
        tid = int(tid)
    except (ValueError, TypeError):
        raise ValueError("Invalid telegram_id")
    if tid <= 0 or tid > 10_000_000_000_000:
        raise ValueError("telegram_id out of range")
    return tid

def validate_amount(amount, max_val: int = 500_000_000) -> int:
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        raise ValueError("Invalid amount")
    if amount <= 0 or amount > max_val:
        raise ValueError("Amount out of range")
    return amount

def now_ts() -> int:
    return int(time.time())

def _like_escape(text: str) -> str:
    """escape کردن % _ \\ برای LIKE ... ESCAPE '\\'."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """ستون را فقط اگر وجود ندارد اضافه می‌کند (نام‌ها ثابت و داخلی‌اند، نه ورودی کاربر)."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

# ============================================================
# ساخت جداول
# ============================================================

def init_database() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                total_purchases INTEGER NOT NULL DEFAULT 0,
                trial_used INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                ban_until INTEGER,
                joined_at INTEGER NOT NULL,
                last_seen_at INTEGER,
                CHECK (balance >= 0),
                CHECK (total_purchases >= 0)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS username_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                changed_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                config_number INTEGER NOT NULL,
                config_name TEXT NOT NULL UNIQUE,
                paxiva_client_id INTEGER,
                kind TEXT NOT NULL DEFAULT 'vol' CHECK (kind IN ('vol', 'unl')),
                volume_gb REAL NOT NULL DEFAULT 0,
                days INTEGER NOT NULL DEFAULT 30,
                devices INTEGER NOT NULL DEFAULT 2,
                price_toman INTEGER NOT NULL DEFAULT 0,
                price_stars INTEGER NOT NULL DEFAULT 0,
                is_trial INTEGER NOT NULL DEFAULT 0,
                auto_renew INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'active',
                expires_at INTEGER,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                link TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_from_list_at INTEGER,
                deleted_from_db_at INTEGER,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                balance_before INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                type TEXT NOT NULL,
                description TEXT,
                reference_id TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                photo_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
                reviewed_by INTEGER,
                reviewed_at INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                volume_gb REAL NOT NULL,
                days INTEGER NOT NULL,
                devices INTEGER NOT NULL,
                amount_stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'delivered', 'refunded')),
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS stars_charges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_id TEXT NOT NULL UNIQUE,
                telegram_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                amount_stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'charged' CHECK (status IN ('charged', 'delivered', 'refunded')),
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (referred_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                CHECK (referrer_id != referred_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                source TEXT,
                subscription_id INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (referred_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                percent INTEGER NOT NULL CHECK (percent > 0 AND percent <= 100),
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS discount_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                used_at INTEGER NOT NULL,
                UNIQUE (code_id, telegram_id),
                FOREIGN KEY (code_id) REFERENCES discount_codes(id) ON DELETE CASCADE,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                user_id INTEGER,
                detail TEXT,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                banned_by INTEGER NOT NULL,
                reason TEXT,
                until INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'vol' CHECK (kind IN ('vol', 'unl')),
                volume_gb REAL NOT NULL DEFAULT 0,
                days INTEGER NOT NULL DEFAULT 30,
                price_toman INTEGER NOT NULL,
                price_stars INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                configs_sold INTEGER NOT NULL DEFAULT 0,
                revenue_toman INTEGER NOT NULL DEFAULT 0,
                active_users INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                activity_date TEXT NOT NULL,
                UNIQUE (telegram_id, activity_date),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        # ثبت دائمی ارسال پیام خوش‌آمدگویی پین‌شده برای هر کاربر.
        # telegram_id کلید اصلی است؛ بنابراین برای هر کاربر فقط یک رکورد ممکن است.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_pinned_welcome (
                telegram_id INTEGER PRIMARY KEY,
                sent_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id)
                    REFERENCES users(telegram_id)
                    ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS lucky_wheel_spins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                prize TEXT NOT NULL,
                prize_type TEXT NOT NULL DEFAULT 'nothing',
                config_name TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                currency TEXT NOT NULL CHECK (currency IN ('toman', 'stars')),
                old_value INTEGER,
                new_value INTEGER NOT NULL,
                changed_by INTEGER,
                created_at INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT,
                created_at INTEGER NOT NULL
            )
        """)

        # مهاجرت‌های افزایشی (روی دیتابیس موجود امن است)
        _ensure_column(conn, "bans", "unbanned_by", "INTEGER")
        _ensure_column(conn, "bans", "unbanned_at", "INTEGER")
        _ensure_column(conn, "payment_requests", "review_note", "TEXT")
        # کد تخفیف اختصاصی (مثلاً جایزهٔ گردونه): فقط برای owner_id و تا expires_at معتبر است
        _ensure_column(conn, "discount_codes", "owner_id", "INTEGER")
        _ensure_column(conn, "discount_codes", "expires_at", "INTEGER")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_price_hist ON price_history(item, currency, id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_created ON payment_requests(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_created ON subscriptions(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(telegram_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_state ON subscriptions(state);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_expires ON subscriptions(expires_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_config_name ON subscriptions(config_name COLLATE NOCASE);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_user ON wallet_transactions(telegram_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_status ON payment_requests(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_user ON payment_requests(telegram_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_referrer ON referrals(referrer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_referred ON referrals(referred_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ref_earn_referrer ON referral_earnings(referrer_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_code ON discount_codes(code);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bans_user ON bans(telegram_id, is_active);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_inv_payload ON stars_invoices(payload);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_chg_charge ON stars_charges(charge_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_date ON user_activity(activity_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wheel_telegram ON lucky_wheel_spins(telegram_id);")
        # PAY-01: جلوگیری سطح دیتابیس از کسر/بازگشت/پورسانت تکراری برای یک سفارش
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_idempotent ON wallet_transactions(type, reference_id) "
                "WHERE reference_id IS NOT NULL AND (type IN ('purchase', 'referral_commission') OR reference_id LIKE 'refund:%')"
            )
        except sqlite3.Error:
            logger.warning("Could not create uq_wallet_idempotent (duplicate legacy rows?); "
                           "application-level idempotency check stays active", exc_info=True)

    # دسترسی فایل دیتابیس فقط برای صاحب برنامه
    try:
        os.chmod(DB_PATH, 0o600)
    except Exception:
        logger.debug("Could not chmod database file", exc_info=True)


# ============================================================
# تنظیمات
# ============================================================

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    key = str(key)
    if not key or len(key) > 200:
        raise ValueError("Invalid setting key")
    value = str(value)
    if len(value) > 20000:
        raise ValueError("Setting value too long")
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_ts())
        )

# ============================================================
# لاگ
# ============================================================

def log_event(level: str, event_type: str, user_id: Optional[int] = None, detail: Optional[str] = None) -> None:
    """ثبت لاگ. هرگز استثنا نمی‌اندازد."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO logs (level, event_type, user_id, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (sanitize_text(level, 20) or "INFO", sanitize_text(event_type, 100) or "event",
                 user_id, sanitize_text(detail, 1000), now_ts())
            )
    except Exception:
        logger.exception("log_event failed: %s", event_type)

# ============================================================
# کاربران
# ============================================================

def create_or_update_user(telegram_id: int, username: Optional[str] = None, first_name: Optional[str] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    username = sanitize_text(username, 64)
    first_name = sanitize_text(first_name, 128)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT username FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if existing:
                if existing["username"] != username and username:
                    conn.execute(
                        "INSERT INTO username_history (telegram_id, username, changed_at) VALUES (?, ?, ?)",
                        (telegram_id, username, now_ts())
                    )
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_seen_at = ? WHERE telegram_id = ?",
                    (username, first_name, now_ts(), telegram_id)
                )
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO users (telegram_id, username, first_name, joined_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, username, first_name, now_ts(), now_ts())
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return dict(row) if row else None

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    username = sanitize_text(username, 64)
    if not username:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username.lstrip("@"),)
        ).fetchone()
        return dict(row) if row else None

def get_user_by_identifier(query: str) -> Optional[Dict[str, Any]]:
    query = sanitize_text(query, 100)
    if not query:
        return None
    raw = query.strip().lstrip("@")
    if re.fullmatch(r"[0-9]{1,15}", raw):
        try:
            return get_user(int(raw))
        except ValueError:
            return None
    return get_user_by_username(raw)

def get_user_registration_date(telegram_id: int) -> str:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT joined_at FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        if row and row["joined_at"]:
            import datetime
            return datetime.datetime.fromtimestamp(row["joined_at"]).strftime("%Y-%m-%d")
        return "نامشخص"

def increment_total_purchases(telegram_id: int) -> None:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("UPDATE users SET total_purchases = total_purchases + 1 WHERE telegram_id = ?", (telegram_id,))

def mark_trial_used(telegram_id: int) -> None:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("UPDATE users SET trial_used = 1 WHERE telegram_id = ?", (telegram_id,))

def get_user_level(total_purchases: int):
    from config import LEVEL_NAMES
    if total_purchases <= 1:
        return "new", LEVEL_NAMES.get("new", "تازه‌وارد")
    elif total_purchases <= 5:
        return "prime", LEVEL_NAMES.get("prime", "پرایم")
    elif total_purchases <= 10:
        return "royal", LEVEL_NAMES.get("royal", "رویال")
    elif total_purchases <= 20:
        return "diamond", LEVEL_NAMES.get("diamond", "دایموند")
    else:
        return "legendary", LEVEL_NAMES.get("legendary", "لجندری")

# ============================================================
# شمارنده کانفیگ
# ============================================================

def reserve_config_number(is_test: bool = False) -> int:
    key = "test_counter" if is_test else "config_counter"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            counter = int(row["value"]) if row else 0
            if is_test:
                max_row = conn.execute("SELECT COALESCE(MAX(config_number), 0) AS mx FROM subscriptions WHERE is_trial = 1").fetchone()
            else:
                max_row = conn.execute("SELECT COALESCE(MAX(config_number), 0) AS mx FROM subscriptions WHERE is_trial = 0").fetchone()
            next_num = max(counter, int(max_row["mx"])) + 1
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, str(next_num), now_ts())
            )
            conn.commit()
            return next_num
        except Exception:
            conn.rollback()
            raise

def release_config_number(number: int, is_test: bool = False) -> bool:
    key = "test_counter" if is_test else "config_counter"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            counter = int(row["value"]) if row else 0
            used = conn.execute("SELECT 1 FROM subscriptions WHERE config_number = ? AND is_trial = ?", (number, 1 if is_test else 0)).fetchone()
            if counter == number and not used:
                conn.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = ?", (str(number - 1), now_ts(), key))
                conn.commit()
                return True
            conn.rollback()
            return False
        except Exception:
            conn.rollback()
            return False

# ============================================================
# کیف پول
# ============================================================

def get_balance(telegram_id: int) -> int:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return int(row["balance"]) if row else 0

def _is_idempotent_tx(tx_type: str, reference_id: Optional[str]) -> bool:
    """تراکنش‌هایی که برای یک reference_id فقط یک‌بار مجاز هستند (PAY-01).
    خرید، پورسانت و بازگشت وجه با پیشوند refund: . سایر انواع (مثل تمدید) بدون محدودیت‌اند."""
    if not reference_id:
        return False
    return tx_type in ("purchase", "referral_commission", "wheel_prize") or str(reference_id).startswith("refund:")


def change_balance(telegram_id: int, delta: int, tx_type: str, description: str = "", reference_id: Optional[str] = None) -> bool:
    """تغییر اتمیک موجودی.
    اگر تراکنش idempotent باشد و قبلاً ثبت شده باشد: برای واریز (delta>0) True (قبلاً انجام شده،
    دوباره اعمال نمی‌شود) و برای کسر False (جلوگیری از کسر/ساخت تکراری) برمی‌گردد."""
    telegram_id = validate_telegram_id(telegram_id)
    delta = int(delta)
    if delta == 0 or abs(delta) > 500_000_000:
        return False
    if reference_id is not None:
        reference_id = sanitize_text(str(reference_id), 200)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _is_idempotent_tx(tx_type, reference_id):
                dup = conn.execute(
                    "SELECT 1 FROM wallet_transactions WHERE type = ? AND reference_id = ? LIMIT 1",
                    (tx_type, reference_id)
                ).fetchone()
                if dup:
                    conn.rollback()
                    logger.warning("Duplicate wallet tx ignored: type=%s ref=%s user=%s", tx_type, reference_id, telegram_id)
                    return delta > 0
            row = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            if not row:
                conn.rollback()
                return False
            before = int(row["balance"])
            after = before + delta
            if after < 0:
                conn.rollback()
                return False
            conn.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (after, telegram_id))
            conn.execute(
                "INSERT INTO wallet_transactions (telegram_id, amount, balance_before, balance_after, type, description, reference_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (telegram_id, delta, before, after, tx_type, sanitize_text(description, 500), reference_id, now_ts())
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def add_balance(telegram_id: int, amount: int, tx_type: str, description: str = "", reference_id: Optional[str] = None) -> bool:
    return change_balance(telegram_id, validate_amount(amount), tx_type, description, reference_id)

def deduct_balance(telegram_id: int, amount: int, tx_type: str, description: str = "", reference_id: Optional[str] = None) -> bool:
    return change_balance(telegram_id, -validate_amount(amount), tx_type, description, reference_id)

def get_wallet_transactions(telegram_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """دریافت تاریخچه تراکنش‌های کیف پول کاربر."""
    telegram_id = validate_telegram_id(telegram_id)
    try:
        limit = max(1, min(int(limit), 100))
    except (ValueError, TypeError):
        limit = 10
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM wallet_transactions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

# ============================================================
# درخواست‌های پرداخت
# ============================================================

def create_payment_request(telegram_id: int, amount: int, photo_file_id: str) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    amount = validate_amount(amount)
    photo_file_id = sanitize_text(photo_file_id, 200)
    if not photo_file_id:
        return None
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            pending = conn.execute(
                "SELECT id FROM payment_requests WHERE telegram_id = ? AND status = 'pending'",
                (telegram_id,)
            ).fetchone()
            if pending:
                conn.rollback()
                return None
            cursor = conn.execute(
                "INSERT INTO payment_requests (telegram_id, amount, photo_file_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (telegram_id, amount, photo_file_id, now_ts())
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise

def get_payment_request(payment_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (payment_id,)).fetchone()
        return dict(row) if row else None

def approve_payment(payment_id: int, admin_id: int) -> bool:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            req = conn.execute("SELECT * FROM payment_requests WHERE id = ? AND status = 'pending'", (payment_id,)).fetchone()
            if not req:
                conn.rollback()
                return False
            user = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (req["telegram_id"],)).fetchone()
            if not user:
                conn.rollback()
                return False
            before = int(user["balance"])
            after = before + int(req["amount"])
            conn.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (after, req["telegram_id"]))
            conn.execute(
                "INSERT INTO wallet_transactions (telegram_id, amount, balance_before, balance_after, type, description, created_at) "
                "VALUES (?, ?, ?, ?, 'card_payment', ?, ?)",
                (req["telegram_id"], req["amount"], before, after, f"Payment #{payment_id}", now_ts())
            )
            conn.execute(
                "UPDATE payment_requests SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (admin_id, now_ts(), payment_id)
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def reject_payment(payment_id: int, admin_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE payment_requests SET status = 'rejected', reviewed_by = ?, reviewed_at = ? WHERE id = ? AND status = 'pending'",
            (admin_id, now_ts(), payment_id)
        )
        return cursor.rowcount > 0

# ============================================================
# Stars
# ============================================================

def create_stars_invoice(payload: str, telegram_id: int, volume_gb: float, days: int, devices: int, amount_stars: int) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    payload = sanitize_text(payload, 100)
    if not payload or amount_stars <= 0:
        return None
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO stars_invoices (payload, telegram_id, volume_gb, days, devices, amount_stars, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (payload, telegram_id, volume_gb, days, devices, amount_stars, now_ts())
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            conn.rollback()
            return None

def get_stars_invoice(payload: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM stars_invoices WHERE payload = ?", (payload,)).fetchone()
        return dict(row) if row else None

def update_stars_invoice_status(payload: str, status: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("UPDATE stars_invoices SET status = ? WHERE payload = ?", (status, payload))
        return cursor.rowcount > 0

def register_stars_charge(charge_id: str, telegram_id: int, payload: str, amount_stars: int) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    charge_id = sanitize_text(charge_id, 200)
    if not charge_id:
        return False
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO stars_charges (charge_id, telegram_id, payload, amount_stars, status, created_at) "
                "VALUES (?, ?, ?, ?, 'charged', ?)",
                (charge_id, telegram_id, payload, amount_stars, now_ts())
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise

def update_stars_charge_status(charge_id: str, status: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("UPDATE stars_charges SET status = ? WHERE charge_id = ?", (status, charge_id))
        return cursor.rowcount > 0

# ============================================================
# سرویس‌ها
# ============================================================

def create_subscription(
    telegram_id: int, config_number: int, config_name: str, kind: str,
    volume_gb: float, days: int, devices: int,
    price_toman: int, price_stars: int,
    is_trial: int = 0, paxiva_client_id: Optional[int] = None, link: Optional[str] = None
) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO subscriptions 
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, price_stars, is_trial, paxiva_client_id, link, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, price_stars, is_trial, paxiva_client_id, link, now_ts(), now_ts())
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise

def finalize_purchase(
    telegram_id: int, config_number: int, config_name: str, kind: str,
    volume_gb: float, days: int, devices: int, price_toman: int,
    paxiva_client_id: int, link: str, discount_id: Optional[int] = None,
) -> int:
    """FLOW-02: ثبت اشتراک + مصرف کد تخفیف + شمارش خرید + پورسانت رفرال در «یک» تراکنش.
    یا همه انجام می‌شود یا هیچ‌کدام. در صورت ناموجود بودن کد تخفیف ValueError می‌دهد.
    شناسهٔ اشتراک را برمی‌گرداند."""
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """INSERT INTO subscriptions
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, price_stars, is_trial, paxiva_client_id, link, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'active', ?, ?)""",
                (telegram_id, config_number, config_name, kind, volume_gb, days, devices,
                 price_toman, paxiva_client_id, link, now_ts(), now_ts())
            )
            sub_id = int(cursor.lastrowid)
            if discount_id is not None:
                code = conn.execute("SELECT * FROM discount_codes WHERE id = ? AND is_active = 1", (int(discount_id),)).fetchone()
                if not code or code["used_count"] >= code["max_uses"] or not discount_usable_by(code, telegram_id):
                    raise ValueError("discount_unavailable")
                if conn.execute("SELECT 1 FROM discount_uses WHERE code_id = ? AND telegram_id = ?",
                                (int(discount_id), telegram_id)).fetchone():
                    raise ValueError("discount_unavailable")
                conn.execute("INSERT INTO discount_uses (code_id, telegram_id, used_at) VALUES (?, ?, ?)",
                             (int(discount_id), telegram_id, now_ts()))
                new_count = code["used_count"] + 1
                conn.execute("UPDATE discount_codes SET used_count = ?, is_active = ? WHERE id = ?",
                             (new_count, 0 if new_count >= code["max_uses"] else 1, int(discount_id)))
            conn.execute("UPDATE users SET total_purchases = total_purchases + 1 WHERE telegram_id = ?", (telegram_id,))
            try:
                # شکست پورسانت نباید خرید را خراب کند
                conn.execute("SAVEPOINT ref")
                _apply_referral_commission(conn, telegram_id, price_toman, sub_id, "purchase")
                conn.execute("RELEASE ref")
            except sqlite3.Error:
                conn.execute("ROLLBACK TO ref")
                conn.execute("RELEASE ref")
                logger.exception("Referral commission failed inside finalize_purchase")
            conn.commit()
            return sub_id
        except Exception:
            conn.rollback()
            raise

def get_user_subscriptions(telegram_id: int, only_visible: bool = True) -> List[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        query = "SELECT * FROM subscriptions WHERE telegram_id = ?"
        if only_visible:
            query += " AND deleted_from_list_at IS NULL"
        query += " ORDER BY id DESC"
        rows = conn.execute(query, (telegram_id,)).fetchall()
        return [dict(r) for r in rows]

def get_subscription(subscription_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        return dict(row) if row else None

def update_subscription_state(subscription_id: int, state: str, expires_at: Optional[int] = None, used_bytes: Optional[int] = None) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET state = ?, expires_at = COALESCE(?, expires_at), used_bytes = COALESCE(?, used_bytes), updated_at = ? WHERE id = ?",
            (state, expires_at, used_bytes, now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def update_subscription_volume(subscription_id: int, new_volume: float) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET volume_gb = ?, updated_at = ? WHERE id = ?",
            (new_volume, now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def toggle_auto_renew(subscription_id: int, enabled: bool) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET auto_renew = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def delete_subscription_from_list(subscription_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET deleted_from_list_at = ? WHERE id = ?",
            (now_ts(), subscription_id)
        )
        return cursor.rowcount > 0

def hard_delete_subscription(subscription_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
        return cursor.rowcount > 0

# ============================================================
# رفرال و پورسانت
# ============================================================

def set_referral(referrer_id: int, referred_id: int) -> bool:
    referrer_id = validate_telegram_id(referrer_id)
    referred_id = validate_telegram_id(referred_id)
    if referrer_id == referred_id:
        return False
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) VALUES (?, ?, ?)",
            (referrer_id, referred_id, now_ts())
        )
        return cursor.rowcount > 0

def get_referrer(referred_id: int) -> Optional[int]:
    referred_id = validate_telegram_id(referred_id)
    with get_connection() as conn:
        row = conn.execute("SELECT referrer_id FROM referrals WHERE referred_id = ?", (referred_id,)).fetchone()
        return int(row["referrer_id"]) if row else None

def _apply_referral_commission(conn, referred_id: int, purchase_amount: int, subscription_id: int, source: str) -> bool:
    """پورسانت رفرال داخل تراکنشِ باز (conn). یک پورسانت برای هر (subscription, source)."""
    row = conn.execute("SELECT referrer_id FROM referrals WHERE referred_id = ?", (referred_id,)).fetchone()
    if not row:
        return False
    referrer_id = int(row["referrer_id"])
    commission = int(purchase_amount) * 5 // 100
    if commission <= 0:
        return False
    if conn.execute(
        "SELECT 1 FROM referral_earnings WHERE subscription_id = ? AND source = ? AND referred_id = ? LIMIT 1",
        (subscription_id, source, referred_id)
    ).fetchone():
        return False  # REF-01: پورسانت تکراری
    bal = conn.execute("SELECT balance FROM users WHERE telegram_id = ?", (referrer_id,)).fetchone()
    if not bal:
        return False
    before = int(bal["balance"])
    after = before + commission
    conn.execute(
        "INSERT INTO referral_earnings (referrer_id, referred_id, amount, source, subscription_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (referrer_id, referred_id, commission, source, subscription_id, now_ts())
    )
    conn.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (after, referrer_id))
    conn.execute(
        "INSERT INTO wallet_transactions (telegram_id, amount, balance_before, balance_after, type, description, reference_id, created_at) "
        "VALUES (?, ?, ?, ?, 'referral_commission', ?, ?, ?)",
        (referrer_id, commission, before, after, f"پورسانت ۵٪ از خرید {referred_id}", f"{source}:{subscription_id}", now_ts())
    )
    return True
def consume_pending_referral(referred_id: int) -> Optional[int]:
    """
    اگر پیش از تأیید عضویت اجباری، رفرال این کاربر به‌صورت موقت در settings
    ذخیره شده بود (توسط ReferralCaptureMiddleware)، اینجا نهایی می‌شود.
    کلید موقت در هر صورت پاک می‌شود تا دوباره استفاده نشود.
    شناسه معرفی که ثبت شد را برمی‌گرداند، یا None.
    """
    referred_id = validate_telegram_id(referred_id)
    key = f"pending_ref:{referred_id}"
    pending = get_setting(key)
    if not pending:
        return None

    try:
        referrer_id = validate_telegram_id(pending)
    except ValueError:
        set_setting(key, "")
        return None

    set_setting(key, "")  # یک‌بار مصرف؛ فوراً پاک می‌شود

    if referrer_id == referred_id:
        return None
    if get_referrer(referred_id):
        return None  # قبلاً معرف دائمی دارد؛ تغییر نمی‌کند
    if not get_user(referrer_id):
        return None

    return referrer_id if set_referral(referrer_id, referred_id) else None
def add_referral_commission(referred_id: int, purchase_amount: int, subscription_id: int, source: str = "purchase") -> bool:
    referred_id = validate_telegram_id(referred_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ok = _apply_referral_commission(conn, referred_id, purchase_amount, int(subscription_id), source)
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise

def get_referral_stats(telegram_id: int) -> Dict[str, Any]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (telegram_id,)).fetchone()["c"]
        total = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM referral_earnings WHERE referrer_id = ?", (telegram_id,)).fetchone()["s"]
        return {"count": int(count), "total_earnings": int(total)}

def get_user_referrals(referrer_id: int) -> List[Dict[str, Any]]:
    referrer_id = validate_telegram_id(referrer_id)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM referrals WHERE referrer_id = ? ORDER BY id DESC", (referrer_id,)).fetchall()
        return [dict(r) for r in rows]

def get_referral_details(referrer_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    referrer_id = validate_telegram_id(referrer_id)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM referral_earnings WHERE referrer_id = ? ORDER BY id DESC LIMIT ?", (referrer_id, limit)).fetchall()
        return [dict(r) for r in rows]

# ============================================================
# کد تخفیف
# ============================================================

def create_discount_code(code: str, percent: int, max_uses: int,
                         owner_id: Optional[int] = None, expires_days: Optional[int] = None) -> Optional[int]:
    code = sanitize_text(code, 50)
    try:
        percent, max_uses = int(percent), int(max_uses)
    except (TypeError, ValueError):
        return None
    if not code or not (1 <= percent <= 100) or not (1 <= max_uses <= 1_000_000):
        return None
    with get_connection() as conn:
        try:
            expires_at = now_ts() + int(expires_days) * 86400 if expires_days else None
            cursor = conn.execute(
                "INSERT INTO discount_codes (code, percent, max_uses, used_count, is_active, created_at, owner_id, expires_at) "
                "VALUES (?, ?, ?, 0, 1, ?, ?, ?)",
                (code, percent, max_uses, now_ts(), int(owner_id) if owner_id else None, expires_at)
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            conn.rollback()
            return None

def get_discount_code(code: str) -> Optional[Dict[str, Any]]:
    code = sanitize_text(code, 50)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM discount_codes WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
        return dict(row) if row else None

def discount_usable_by(code: Any, telegram_id: int) -> bool:
    """آیا این کد برای این کاربر قابل استفاده است؟ (مالک اختصاصی + تاریخ انقضا)"""
    try:
        owner = code["owner_id"]
        expires = code["expires_at"]
    except (KeyError, IndexError, TypeError):
        return True  # دیتابیس قدیمی بدون این ستون‌ها
    if owner and int(owner) != int(telegram_id):
        return False
    if expires and now_ts() > int(expires):
        return False
    return True

def consume_discount_code(code_id: int, telegram_id: int) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            code = conn.execute("SELECT * FROM discount_codes WHERE id = ? AND is_active = 1", (code_id,)).fetchone()
            if not code or code["used_count"] >= code["max_uses"] or not discount_usable_by(code, telegram_id):
                conn.rollback()
                return False
            used = conn.execute("SELECT 1 FROM discount_uses WHERE code_id = ? AND telegram_id = ?", (code_id, telegram_id)).fetchone()
            if used:
                conn.rollback()
                return False
            conn.execute("INSERT INTO discount_uses (code_id, telegram_id, used_at) VALUES (?, ?, ?)", (code_id, telegram_id, now_ts()))
            new_count = code["used_count"] + 1
            active = 0 if new_count >= code["max_uses"] else 1
            conn.execute("UPDATE discount_codes SET used_count = ?, is_active = ? WHERE id = ?", (new_count, active, code_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

# ============================================================
# بن
# ============================================================

def ban_user(telegram_id: int, admin_id: int, reason: str = "", until: Optional[int] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    admin_id = validate_telegram_id(admin_id)
    if until is not None:
        until = int(until)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE bans SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))
            conn.execute(
                "INSERT INTO bans (telegram_id, banned_by, reason, until, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (telegram_id, admin_id, sanitize_text(reason, 500), until, now_ts())
            )
            conn.execute("UPDATE users SET is_banned = 1, ban_until = ? WHERE telegram_id = ?", (until, telegram_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def unban_user(telegram_id: int, admin_id: Optional[int] = None) -> bool:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE bans SET is_active = 0, unbanned_by = ?, unbanned_at = ? WHERE telegram_id = ? AND is_active = 1",
                (admin_id, now_ts(), telegram_id)
            )
            conn.execute("UPDATE users SET is_banned = 0, ban_until = NULL WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

def is_banned(telegram_id: int) -> bool:
    """
    مسدود است اگر is_banned=1 باشد و (بن دائمی باشد یا مهلتش نگذشته باشد).
    (نسخهٔ قبلی بن دائمی را اشتباهاً «غیرمسدود» می‌دانست.)
    """
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT is_banned, ban_until FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if not row or not row["is_banned"]:
        return False
    until = row["ban_until"]
    if until and int(until) < now_ts():
        unban_user(telegram_id)
        return False
    return True

# ============================================================
# محصولات
# ============================================================

def create_product(kind: str, volume_gb: float, days: int, price_toman: int, price_stars: int) -> Optional[int]:
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO products (kind, volume_gb, days, price_toman, price_stars, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (kind, volume_gb, days, price_toman, price_stars, now_ts())
        )
        return cursor.lastrowid

def list_products(active_only: bool = True) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        query = "SELECT * FROM products"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY id DESC"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

# ------------------------- گردونه: چرخش اتمیک با شرط رفرال روزانه -------------------------

def tehran_day_start_ts() -> int:
    """زمان شروع «امروز» (۰۰:۰۰) به وقت تهران، به‌صورت timestamp."""
    from datetime import datetime, timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Tehran")
    except Exception:
        tz = timezone(timedelta(hours=3, minutes=30))
    now = datetime.now(tz)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

def count_referrals_since(referrer_id: int, since_ts: int) -> int:
    """تعداد دعوت‌های ثبت‌شده از since_ts به بعد (از جدول referrals)."""
    referrer_id = validate_telegram_id(referrer_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND created_at >= ?",
            (referrer_id, int(since_ts))
        ).fetchone()
        return int(row["c"])

def try_record_spin(telegram_id: int, prize: str, prize_type: str,
                    cooldown_hours: int = 24, min_referrals_today: int = 1) -> Tuple[Optional[int], str]:
    """
    در «یک» تراکنش: محدودیت زمانی + شرط رفرال امروز را چک می‌کند و چرخش را ثبت می‌کند.
    (spin_id, "ok") یا (None, "cooldown" | "no_referral_today").
    دو کلیک هم‌زمان نمی‌توانند دو چرخش بگیرند.
    """
    telegram_id = validate_telegram_id(telegram_id)
    prize = sanitize_text(prize, 100) or "?"
    prize_type = sanitize_text(prize_type, 20) or "nothing"
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute(
                "SELECT created_at FROM lucky_wheel_spins WHERE telegram_id = ? ORDER BY id DESC LIMIT 1",
                (telegram_id,)
            ).fetchone()
            if last and now_ts() - int(last["created_at"]) < int(cooldown_hours) * 3600:
                conn.rollback()
                return None, "cooldown"
            if min_referrals_today > 0:
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ? AND created_at >= ?",
                    (telegram_id, tehran_day_start_ts())
                ).fetchone()["c"]
                if int(cnt) < int(min_referrals_today):
                    conn.rollback()
                    return None, "no_referral_today"
            cursor = conn.execute(
                "INSERT INTO lucky_wheel_spins (telegram_id, prize, prize_type, config_name, created_at) VALUES (?, ?, ?, NULL, ?)",
                (telegram_id, prize, prize_type, now_ts())
            )
            conn.commit()
            return int(cursor.lastrowid), "ok"
        except Exception:
            conn.rollback()
            raise

def void_wheel_spin(spin_id: int) -> bool:
    """وقتی جایزه تحویل داده نشد، چرخش را برمی‌گرداند تا کاربر دوباره بتواند بچرخاند."""
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM lucky_wheel_spins WHERE id = ?", (int(spin_id),))
        return cursor.rowcount > 0

# ============================================================
# گزارش و DAU
# ============================================================

def record_user_activity(telegram_id: int) -> None:
    telegram_id = validate_telegram_id(telegram_id)
    today = time.strftime("%Y-%m-%d")
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_activity (telegram_id, activity_date) VALUES (?, ?)",
            (telegram_id, today)
        )

def save_daily_report(report_date: str, configs_sold: int, revenue_toman: int) -> None:
    with get_connection() as conn:
        active = conn.execute("SELECT COUNT(DISTINCT telegram_id) AS c FROM user_activity WHERE activity_date = ?", (report_date,)).fetchone()["c"]
        conn.execute(
            "INSERT OR REPLACE INTO daily_reports (report_date, configs_sold, revenue_toman, active_users, created_at) VALUES (?, ?, ?, ?, ?)",
            (report_date, configs_sold, revenue_toman, active, now_ts())
        )

def _day_start() -> int:
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))

# ============================================================
# آمار
# ============================================================

def get_admin_stats() -> Dict[str, Any]:
    with get_connection() as conn:
        total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        banned = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1").fetchone()["c"]
        total_purchases = conn.execute("SELECT COALESCE(SUM(total_purchases), 0) AS s FROM users").fetchone()["s"]
        total_balance = conn.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users").fetchone()["s"]
        total_revenue = conn.execute("SELECT COALESCE(SUM(price_toman), 0) AS s FROM subscriptions").fetchone()["s"]
        total_stars = conn.execute("SELECT COALESCE(SUM(amount_stars), 0) AS s FROM stars_charges WHERE status != 'refunded'").fetchone()["s"]
        return {
            "total_users": int(total_users),
            "banned_users": int(banned),
            "total_purchases": int(total_purchases),
            "total_balance": int(total_balance),
            "total_revenue_toman": int(total_revenue),
            "total_stars": int(total_stars),
            "pending_payments": int(conn.execute(
                "SELECT COUNT(*) AS c FROM payment_requests WHERE status = 'pending'").fetchone()["c"]),
            "new_users_today": int(conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE joined_at >= ?", (_day_start(),)).fetchone()["c"]),
            "sales_today": int(conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE is_trial = 0 AND created_at >= ?",
                (_day_start(),)).fetchone()["c"]),
            "revenue_today_toman": int(conn.execute(
                "SELECT COALESCE(SUM(price_toman), 0) AS s FROM subscriptions WHERE is_trial = 0 AND created_at >= ?",
                (_day_start(),)).fetchone()["s"]),
            "active_subscriptions": int(conn.execute(
                "SELECT COUNT(*) AS c FROM subscriptions WHERE state = 'active'").fetchone()["c"]),
        }

# ============================================================
# گردونه شانس
# ============================================================

def get_last_wheel_spin(telegram_id: int) -> Optional[Dict[str, Any]]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM lucky_wheel_spins WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (telegram_id,)).fetchone()
        return dict(row) if row else None

def can_spin_wheel(telegram_id: int, cooldown_hours: int = 24) -> bool:
    last = get_last_wheel_spin(telegram_id)
    if not last:
        return True
    import datetime
    try:
        created = datetime.datetime.fromtimestamp(last["created_at"])
        elapsed = (datetime.datetime.now() - created).total_seconds()
        return elapsed >= cooldown_hours * 3600
    except Exception:
        return True

def record_wheel_spin(telegram_id: int, prize: str, prize_type: str, config_name: Optional[str] = None) -> Optional[int]:
    telegram_id = validate_telegram_id(telegram_id)
    prize = sanitize_text(prize, 100) or "?"
    prize_type = sanitize_text(prize_type, 20) or "nothing"
    config_name = sanitize_text(config_name, 100)
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO lucky_wheel_spins (telegram_id, prize, prize_type, config_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, prize, prize_type, config_name, now_ts())
        )
        return cursor.lastrowid
# ============================================================
# ===================  توابع جدید (پنل ادمین)  ===================
# ============================================================

# ------------------------- قیمت‌ها -------------------------

# PRICE-02: توابع تکراری PRICE_ITEMS/set_price/get_price_override حذف شدند؛
# کلیدهای قیمت فقط در pricing.py (PRICE_KEYS) و prices.py تعریف می‌شوند.

def get_price_history(limit: int = 20, item: Optional[str] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        if item:
            rows = conn.execute(
                "SELECT * FROM price_history WHERE item = ? ORDER BY id DESC LIMIT ?", (item, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM price_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------- تنظیمات (ابزار) -------------------------

def delete_setting(key: str) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM settings WHERE key = ?", (str(key),)).rowcount > 0


def get_int_setting(key: str, default: int = 0) -> int:
    try:
        return int(str(get_setting(key, default)).strip())
    except (TypeError, ValueError):
        return int(default)


# ------------------------- ادمین رسید -------------------------

def get_receipt_admin_id(default: int) -> int:
    """شناسهٔ ادمین تاییدکنندهٔ رسیدها؛ اگر تنظیم نشده یا نامعتبر باشد: default."""
    raw = get_setting("receipt_admin_id")
    try:
        value = int(str(raw).strip())
        return value if 0 < value <= 10_000_000_000_000 else int(default)
    except (TypeError, ValueError):
        return int(default)


def set_receipt_admin_id(new_admin_id: int, changed_by: Optional[int] = None) -> bool:
    new_admin_id = validate_telegram_id(new_admin_id)
    set_setting("receipt_admin_id", str(new_admin_id))
    log_admin_action(changed_by or 0, "receipt_admin_set", str(new_admin_id))
    return True


# ------------------------- ثبت اقدامات ادمین -------------------------

def log_admin_action(admin_id: int, action: str, target: Optional[str] = None, detail: Optional[str] = None) -> None:
    """ردپای حسابرسی (audit). هرگز استثنا نمی‌اندازد."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO admin_audit (admin_id, action, target, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (int(admin_id or 0), sanitize_text(action, 100) or "action",
                 sanitize_text(target, 200), sanitize_text(detail, 1000), now_ts())
            )
    except Exception:
        logger.exception("log_admin_action failed: %s", action)


def get_admin_actions(limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def purge_old_logs(days: int = 90) -> int:
    """حذف لاگ‌های قدیمی‌تر از N روز (برای کوچک ماندن دیتابیس)."""
    days = max(7, int(days))
    cutoff = now_ts() - days * 86400
    with get_connection() as conn:
        return conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,)).rowcount


# ------------------------- کاربران -------------------------

def search_users(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """جستجو با آیدی عددی (دقیق) یا بخشی از یوزرنیم/نام (LIKE امن)."""
    query = sanitize_text(query, 100)
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    raw = query.lstrip("@")
    results: List[Dict[str, Any]] = []
    if re.fullmatch(r"[0-9]{1,15}", raw):
        try:
            user = get_user(int(raw))
            if user:
                results.append(user)
        except ValueError:
            pass
    pattern = f"%{_like_escape(raw)}%"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE username LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "OR first_name LIKE ? ESCAPE '\\' COLLATE NOCASE ORDER BY total_purchases DESC LIMIT ?",
            (pattern, pattern, limit)
        ).fetchall()
    seen = {u["telegram_id"] for u in results}
    for row in rows:
        if row["telegram_id"] not in seen:
            results.append(dict(row))
    return results[:limit]


def count_user_subscriptions(telegram_id: int) -> int:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()["c"])


def get_recent_subscriptions(telegram_id: int, limit: int = 7) -> List[Dict[str, Any]]:
    """آخرین سرویس‌های کاربر (شامل حذف‌شده از لیست)."""
    telegram_id = validate_telegram_id(telegram_id)
    limit = max(1, min(int(limit), 50))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_ban_info(telegram_id: int) -> Dict[str, Any]:
    telegram_id = validate_telegram_id(telegram_id)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT reason, until, banned_by, created_at FROM bans "
            "WHERE telegram_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (telegram_id,),
        ).fetchone()
        return dict(row) if row else {}


def get_user_profile(telegram_id: int) -> Optional[Dict[str, Any]]:
    """اطلاعات کامل کاربر برای پنل ادمین: کاربر + سطح + تعداد سرویس + وضعیت بن."""
    user = get_user(telegram_id)
    if not user:
        return None
    level_key, level_name = get_user_level(int(user.get("total_purchases") or 0))
    user["level_key"] = level_key
    user["level_name"] = level_name
    user["sub_count"] = count_user_subscriptions(user["telegram_id"])
    user["banned"] = is_banned(user["telegram_id"])
    user["ban_info"] = get_ban_info(user["telegram_id"]) if user["banned"] else {}
    return user


def list_users_page(page: int = 1, page_size: int = 5) -> Tuple[List[Dict[str, Any]], int]:
    """صفحه‌بندی کاربران (بیشترین خرید اول) همراه با تعداد سرویس. خروجی: (کاربران، کل)."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 50))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.telegram_id, u.username, u.first_name, u.balance, u.total_purchases, u.is_banned,
                   (SELECT COUNT(*) FROM subscriptions s WHERE s.telegram_id = u.telegram_id) AS sub_count
            FROM users u
            ORDER BY u.total_purchases DESC, u.telegram_id ASC
            LIMIT ? OFFSET ?
            """,
            (page_size, (page - 1) * page_size)
        ).fetchall()
        total = int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])
        return [dict(r) for r in rows], total


def get_all_user_ids(include_banned: bool = False) -> List[int]:
    """برای پیام همگانی."""
    with get_connection() as conn:
        if include_banned:
            rows = conn.execute("SELECT telegram_id FROM users").fetchall()
        else:
            rows = conn.execute("SELECT telegram_id FROM users WHERE is_banned = 0").fetchall()
        return [int(r["telegram_id"]) for r in rows]


# ------------------------- رسیدها / پرداخت‌ها -------------------------

def get_pending_payments(limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_payment_stats() -> Dict[str, int]:
    out = {"pending": 0, "approved": 0, "rejected": 0, "approved_amount": 0}
    with get_connection() as conn:
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c, COALESCE(SUM(amount), 0) AS a FROM payment_requests GROUP BY status"
        ).fetchall():
            out[r["status"]] = int(r["c"])
            if r["status"] == "approved":
                out["approved_amount"] = int(r["a"])
    return out


def get_reviewer_stats(admin_id: int) -> Dict[str, int]:
    """تعداد رسیدهای تأیید/رد شده توسط یک ادمین."""
    admin_id = validate_telegram_id(admin_id)
    out = {"approved": 0, "rejected": 0}
    with get_connection() as conn:
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM payment_requests WHERE reviewed_by = ? GROUP BY status", (admin_id,)
        ).fetchall():
            if r["status"] in out:
                out[r["status"]] = int(r["c"])
    return out


# ------------------------- سرویس‌ها -------------------------

def get_subscription_by_config_name(config_name: str) -> Optional[Dict[str, Any]]:
    config_name = sanitize_text(config_name, 100)
    if not config_name:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE config_name = ? COLLATE NOCASE", (config_name,)
        ).fetchone()
        return dict(row) if row else None


def update_subscription_days(subscription_id: int, days: int) -> bool:
    """days=0 یعنی بدون محدودیت زمانی."""
    days = int(days)
    if days < 0 or days > 3650:
        return False
    with get_connection() as conn:
        return conn.execute(
            "UPDATE subscriptions SET days = ?, updated_at = ? WHERE id = ?",
            (days, now_ts(), subscription_id)
        ).rowcount > 0


# ------------------------- کد تخفیف -------------------------

def get_discount_codes(limit: int = 10) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM discount_codes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def set_discount_active(code_id: int, active: bool) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE discount_codes SET is_active = ? WHERE id = ?", (1 if active else 0, code_id)
        ).rowcount > 0


def delete_discount_code(code_id: int) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM discount_codes WHERE id = ?", (code_id,)).rowcount > 0


# ------------------------- محصولات -------------------------

def get_product(product_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        return dict(row) if row else None


def update_product_prices(product_id: int, price_toman: Optional[int] = None, price_stars: Optional[int] = None) -> bool:
    sets, params = [], []
    if price_toman is not None:
        if int(price_toman) < 0:
            return False
        sets.append("price_toman = ?")
        params.append(int(price_toman))
    if price_stars is not None:
        if int(price_stars) < 0:
            return False
        sets.append("price_stars = ?")
        params.append(int(price_stars))
    if not sets:
        return False
    params.append(product_id)
    with get_connection() as conn:
        return conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", params).rowcount > 0


def set_product_active(product_id: int, active: bool) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "UPDATE products SET is_active = ? WHERE id = ?", (1 if active else 0, product_id)
        ).rowcount > 0


def delete_product(product_id: int) -> bool:
    with get_connection() as conn:
        return conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount > 0


# ------------------------- آمار -------------------------

def get_top_services(limit: int = 3) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 20))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT kind, volume_gb, COUNT(*) AS c FROM subscriptions WHERE is_trial = 0 "
            "GROUP BY kind, volume_gb ORDER BY c DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_logs(limit: int = 20, level: Optional[str] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with get_connection() as conn:
        if level:
            rows = conn.execute(
                "SELECT * FROM logs WHERE level = ? ORDER BY id DESC LIMIT ?", (level, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------- بک‌آپ و سلامت -------------------------

def backup_database(dest_path) -> str:
    """
    بک‌آپ سازگار با WAL با API رسمی SQLite
    (کپی ساده فایل در حالت WAL ممکن است ناقص باشد).
    """
    dest_path = str(dest_path)
    src = get_connection()
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        os.chmod(dest_path, 0o600)
    except Exception:
        pass
    return dest_path


def check_integrity() -> bool:
    with get_connection() as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and str(row[0]).lower() == "ok"

# ============================================================
# پیام خوش‌آمدگویی پین‌شده (یک‌بار برای هر کاربر)
# ============================================================

def has_user_pinned_welcome(telegram_id: int) -> bool:
    """بررسی می‌کند آیا پیام خوش‌آمدگویی این کاربر قبلاً ثبت شده است."""
    telegram_id = validate_telegram_id(telegram_id)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_pinned_welcome WHERE telegram_id = ? LIMIT 1",
            (telegram_id,),
        ).fetchone()

    return row is not None


def mark_user_pinned_welcome(telegram_id: int) -> bool:
    """
    ثبت اتمیک ارسال موفق پیام خوش‌آمدگویی.

    True: رکورد برای اولین بار ثبت شد.
    False: قبلاً ثبت شده بود و رکورد تکراری ساخته نشد.
    خطاهای دیتابیس عمداً به caller منتقل می‌شوند تا موفقیت جعلی ثبت نشود.
    """
    telegram_id = validate_telegram_id(telegram_id)

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO user_pinned_welcome
                    (telegram_id, sent_at)
                VALUES (?, ?)
                """,
                (telegram_id, now_ts()),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        
