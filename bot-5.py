# -*- coding: utf-8 -*-
"""
ربات چت و آشنایی ناشناس (کاملاً رایگان - نسخه‌ی SQLite)
-----------------------------------------------------------
مدل دسترسی:
    - ۲ هفته اول کاملاً رایگان و نامحدود
    - بعد از آن: دعوت ۴ نفر → دسترسی همیشگی، یا بدون دعوت → ۱۵ دقیقه چت در روز
    - غیرفعالی ۱ ماهه → پاک‌سازی خودکار اطلاعات پروفایل
    - مسدودی به‌خاطر گزارش تایید شده → ۲ روز فرصت برای رفع مسدودی با دعوت ۳ نفر،
      وگرنه اطلاعات پروفایل پاک می‌شود (ولی امکان رفع مسدودی همیشه باقی می‌ماند)

نصب پیش‌نیاز:
    pip install python-telegram-bot --upgrade

متغیرهای محیطی لازم قبل از اجرا:
    BOT_TOKEN   -> از @BotFather می‌گیری
    ADMIN_IDS   -> آیدی عددی تلگرام ادمین‌ها، با کاما جدا شده (مثال: 111111,222222)

اجرا:
    python bot.py
"""

import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "توکن-ربات-تلگرام-اینجا")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

TRIAL_DAYS = 14
DAILY_FREE_SECONDS = 15 * 60
INACTIVITY_PURGE_DAYS = 30
BAN_GRACE_DAYS = 2
REFERRALS_FOR_LIFETIME = 4
REFERRALS_FOR_UNBAN = 3

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


PROVINCES = [
    "آذربایجان شرقی", "آذربایجان غربی", "اردبیل", "اصفهان", "البرز",
    "ایلام", "بوشهر", "تهران", "چهارمحال و بختیاری", "خراسان جنوبی",
    "خراسان رضوی", "خراسان شمالی", "خوزستان", "زنجان", "سمنان",
    "سیستان و بلوچستان", "فارس", "قزوین", "قم", "کردستان",
    "کرمان", "کرمانشاه", "کهگیلویه و بویراحمد", "گلستان", "گیلان",
    "لرستان", "مازندران", "مرکزی", "هرمزگان", "همدان", "یزد",
]
GENDER_LABEL = {"male": "پسر 👦", "female": "دختر 👧"}

# فقط لینک/آیدی/دامنه تلگرام و وب رو فیلتر می‌کنه (اشتباه تایپی A-C قبلی هم اصلاح شد)
SPAM_REGEX = re.compile(
    r"(@[a-zA-Z0-9_]{5,32})|(https?://\S+)|(www\.\S+)|(t\.me/\S+)|(telegram\.me/\S+)",
    re.IGNORECASE,
)

# --- حافظه‌ی موقتِ زمان اجرا (نیازی به ماندگاری ندارن) ---
PENDING_REG: dict[int, dict] = {}          # مراحل ثبت‌نام/انتخاب زبان
WAITING_QUEUE: list[int] = []              # صف اتصال تصادفی
ACTIVE_CHATS: dict[int, int] = {}          # {user_id: partner_id}
CHAT_START: dict[int, datetime] = {}       # زمان شروع چت فعلی هر کاربر
RATE_LIMIT: dict[int, list[float]] = {}    # ضدِ اسپم پیام
SEARCH_RESULTS: dict[int, list] = {}


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                gender TEXT,
                age INTEGER,
                province TEXT,
                city TEXT,
                language TEXT DEFAULT 'fa',
                created_at TEXT,
                last_active_at TEXT,
                referred_by INTEGER,
                referral_count INTEGER DEFAULT 0,
                is_premium_lifetime INTEGER DEFAULT 0,
                daily_chat_seconds_used INTEGER DEFAULT 0,
                last_chat_reset_date TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                banned_at TEXT,
                unban_referral_count INTEGER DEFAULT 0,
                is_data_purged INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                reported_id INTEGER,
                reason TEXT DEFAULT 'گزارش از چت',
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS likes (
                liker_id INTEGER,
                liked_id INTEGER,
                PRIMARY KEY (liker_id, liked_id)
            )
            """
        )


# ---------------------------------------------------------------------------
# توابع کاربر
# ---------------------------------------------------------------------------

def get_user(user_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_user_if_needed(user_id: int, referred_by: int | None):
    if get_user(user_id):
        return False
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, last_active_at, referred_by, last_chat_reset_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, now_utc().isoformat(), now_utc().isoformat(), referred_by, now_utc().isoformat()),
        )
    return True


def upsert_user(user_id: int, **fields):
    with closing(db()) as conn, conn:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), user_id))


def touch_last_active(user_id: int):
    upsert_user(user_id, last_active_at=now_utc().isoformat())


def is_trial_active(user: dict) -> bool:
    created = datetime.fromisoformat(user["created_at"])
    return now_utc() < created + timedelta(days=TRIAL_DAYS)


def ensure_daily_reset(user: dict) -> dict:
    last_reset = datetime.fromisoformat(user["last_chat_reset_date"])
    if now_utc().date() > last_reset.date():
        upsert_user(user["user_id"], daily_chat_seconds_used=0, last_chat_reset_date=now_utc().isoformat())
        user = get_user(user["user_id"])
    return user


def can_start_chat(user: dict) -> bool:
    if is_trial_active(user) or user["is_premium_lifetime"]:
        return True
    user = ensure_daily_reset(user)
    return user["daily_chat_seconds_used"] < DAILY_FREE_SECONDS


def remaining_daily_seconds(user: dict) -> int:
    user = ensure_daily_reset(user)
    return max(0, DAILY_FREE_SECONDS - user["daily_chat_seconds_used"])


def add_chat_seconds(user_id: int, seconds: int):
    user = get_user(user_id)
    if not user or is_trial_active(user) or user["is_premium_lifetime"]:
        return
    user = ensure_daily_reset(user)
    upsert_user(user_id, daily_chat_seconds_used=user["daily_chat_seconds_used"] + max(0, seconds))


def increment_referral(referrer_id: int):
    referrer = get_user(referrer_id)
    if not referrer:
        return
    upsert_user(referrer_id, referral_count=referrer["referral_count"] + 1)
    if referrer["referral_count"] + 1 >= REFERRALS_FOR_LIFETIME:
        upsert_user(referrer_id, is_premium_lifetime=1)


def purge_profile(user_id: int):
    upsert_user(user_id, name=None, gender=None, age=None, province=None, city=None)


# ---------------------------------------------------------------------------
# توابع مسدودی
# ---------------------------------------------------------------------------

def get_ban(user_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM bans WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def create_ban(user_id: int):
    if get_ban(user_id):
        return
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO bans (user_id, banned_at, unban_referral_count, is_data_purged) VALUES (?, ?, 0, 0)",
            (user_id, now_utc().isoformat()),
        )


def increment_unban_referral(user_id: int) -> bool:
    """برمی‌گردونه True اگه با این دعوت، مسدودی رفع بشه."""
    ban = get_ban(user_id)
    if not ban:
        return False
    new_count = ban["unban_referral_count"] + 1
    if new_count >= REFERRALS_FOR_UNBAN:
        remove_ban(user_id)
        return True
    with closing(db()) as conn, conn:
        conn.execute("UPDATE bans SET unban_referral_count=? WHERE user_id=?", (new_count, user_id))
    return False


def remove_ban(user_id: int):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM bans WHERE user_id=?", (user_id,))


def mark_ban_purged(user_id: int):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE bans SET is_data_purged=1 WHERE user_id=?", (user_id,))


# ---------------------------------------------------------------------------
# گزارش‌ها
# ---------------------------------------------------------------------------

def create_report(reporter_id: int, reported_id: int, reason: str = "گزارش از چت"):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO reports (reporter_id, reported_id, reason, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (reporter_id, reported_id, reason, now_utc().isoformat()),
        )


def get_next_pending_report():
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def count_pending_reports() -> int:
    with closing(db()) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM reports WHERE status='pending'").fetchone()
        return row["c"]


def set_report_status(report_id: int, status: str):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))


def get_report(report_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# لایک و جستجو
# ---------------------------------------------------------------------------

def like_user(liker_id: int, liked_id: int) -> bool:
    with closing(db()) as conn, conn:
        try:
            conn.execute("INSERT INTO likes (liker_id, liked_id) VALUES (?, ?)", (liker_id, liked_id))
            return True
        except sqlite3.IntegrityError:
            return False


def count_likes(user_id: int) -> int:
    with closing(db()) as conn:
        row = conn.execute("SELECT COUNT(*) c FROM likes WHERE liked_id=?", (user_id,)).fetchone()
        return row["c"]


def search_users(province: str, gender: str | None, exclude_id: int, limit: int = 5):
    query = "SELECT * FROM users WHERE province=? AND user_id != ? AND name IS NOT NULL"
    params = [province, exclude_id]
    if gender in ("male", "female"):
        query += " AND gender=?"
        params.append(gender)
    query += " ORDER BY RANDOM() LIMIT ?"
    params.append(limit)
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


# ---------------------------------------------------------------------------
# ابزار ضدِاسپم
# ---------------------------------------------------------------------------

def is_rate_limited(user_id: int) -> bool:
    t = time.time()
    hist = [x for x in RATE_LIMIT.get(user_id, []) if t - x < 5]
    if len(hist) >= 5:
        RATE_LIMIT[user_id] = hist
        return True
    hist.append(t)
    RATE_LIMIT[user_id] = hist
    return False


def contains_spam(text: str) -> bool:
    return bool(text) and bool(SPAM_REGEX.search(text))


# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------

def kb_language():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("فارسی 🇮🇷", callback_data="lang_fa"),
          InlineKeyboardButton("English 🇬🇧", callback_data="lang_en")]]
    )


def kb_gender(prefix: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(GENDER_LABEL["male"], callback_data=f"{prefix}_male")],
         [InlineKeyboardButton(GENDER_LABEL["female"], callback_data=f"{prefix}_female")]]
    )


def kb_provinces():
    rows, row = [], []
    for i, p in enumerate(PROVINCES):
        row.append(InlineKeyboardButton(p, callback_data=f"regprov_{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎲 اتصال به چت ناشناس", callback_data="menu_connect")],
            [InlineKeyboardButton("🔍 جستجوی کاربران", callback_data="menu_search")],
            [InlineKeyboardButton("👤 پروفایل من", callback_data="menu_myprofile"),
             InlineKeyboardButton("✏️ ویرایش پروفایل", callback_data="menu_edit")],
            [InlineKeyboardButton("🔗 لینک دعوت من", callback_data="get_ref_link")],
        ]
    )


def kb_search_gender():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("پسر باشه 👦", callback_data="searchgender_male")],
         [InlineKeyboardButton("دختر باشه 👧", callback_data="searchgender_female")],
         [InlineKeyboardButton("همه رو نشون بده 👫", callback_data="searchgender_any")]]
    )


def kb_profile_actions(target_id: int):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❤️ لایک", callback_data=f"like_{target_id}"),
          InlineKeyboardButton("💬 درخواست چت", callback_data=f"chatreq_{target_id}")]]
    )


def kb_chat_controls():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ پایان چت", callback_data="chat_stop"),
          InlineKeyboardButton("🚨 گزارش", callback_data="report_partner")]]
    )


def kb_accept_reject(requester_id: int):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ قبول", callback_data=f"chatacc_{requester_id}"),
          InlineKeyboardButton("❌ رد", callback_data=f"chatrej_{requester_id}")]]
    )


def kb_admin_report(report_id: int, reported_id: int):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⛔ مسدودسازی", callback_data=f"adminban_{report_id}_{reported_id}"),
          InlineKeyboardButton("🟢 رد گزارش", callback_data=f"adminignore_{report_id}")],
         [InlineKeyboardButton("➡️ گزارش بعدی", callback_data="admin_next")]]
    )


def kb_cancel_queue():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 انصراف", callback_data="cancel_queue")]])


# ---------------------------------------------------------------------------
# نمایش پروفایل
# ---------------------------------------------------------------------------

def profile_text(u: dict) -> str:
    return (
        f"👤 نام: {u['name']}\n"
        f"{'👦' if u['gender'] == 'male' else '👧'} جنسیت: {GENDER_LABEL.get(u['gender'], '—')}\n"
        f"🎂 سن: {u['age']}\n"
        f"📍 استان: {u['province']}\n"
        f"🏙 شهر: {u['city']}\n"
        f"❤️ لایک‌ها: {count_likes(u['user_id'])}"
    )


def status_text(user: dict) -> str:
    if is_trial_active(user):
        left = (datetime.fromisoformat(user["created_at"]) + timedelta(days=TRIAL_DAYS)) - now_utc()
        return f"🎁 دوره‌ی رایگان آزمایشی: {left.days} روز دیگر باقی مانده."
    if user["is_premium_lifetime"]:
        return "✅ دسترسی همیشگی فعال است."
    remaining_min = remaining_daily_seconds(user) // 60
    return f"⏱ امروز {remaining_min} دقیقه از چت رایگان روزانه‌ات باقی مانده."


# ---------------------------------------------------------------------------
# شروع / ثبت‌نام
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    is_new = create_user_if_needed(
        user_id, referred_by=int(args[0]) if (args and args[0].isdigit() and int(args[0]) != user_id) else None
    )

    if is_new:
        user = get_user(user_id)
        referrer_id = user["referred_by"]
        if referrer_id:
            unbanned = increment_unban_referral(referrer_id)
            if not unbanned:
                increment_referral(referrer_id)
            try:
                await context.bot.send_message(referrer_id, "🎉 یه نفر با لینک دعوتت وارد ربات شد!")
            except Exception:
                pass

    touch_last_active(user_id)

    # بررسی مسدودی
    ban = get_ban(user_id)
    if ban:
        ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
        await update.message.reply_text(
            "⛔ حساب شما مسدود شده است.\n\n"
            f"برای رفع مسدودی، {REFERRALS_FOR_UNBAN} نفر را با لینک زیر دعوت کن:\n"
            f"دعوت‌شده تا الان: {ban['unban_referral_count']}/{REFERRALS_FOR_UNBAN}\n\n{ref_link}"
        )
        return

    user = get_user(user_id)

    if not user.get("language") or user_id in PENDING_REG and PENDING_REG[user_id].get("step") == "lang":
        pass

    if not user.get("name"):
        PENDING_REG[user_id] = {"step": "lang"}
        await update.message.reply_text("زبان چت رو انتخاب کن / Choose your chat language:", reply_markup=kb_language())
        return

    await update.message.reply_text(f"خوش برگشتی! 🏠\n\n{status_text(user)}", reply_markup=kb_main_menu())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    ban = get_ban(user_id)
    if ban:
        return  # کاربر مسدود، هیچ پیامی پردازش نمیشه

    if user_id in PENDING_REG:
        step = PENDING_REG[user_id]["step"]

        if step == "name":
            PENDING_REG[user_id]["name"] = text[:30]
            PENDING_REG[user_id]["step"] = "age"
            await update.message.reply_text("چند سالته؟ (فقط عدد)")
            return

        if step == "age":
            if not text.isdigit() or not (10 <= int(text) <= 90):
                await update.message.reply_text("سن رو به‌صورت عدد منطقی وارد کن (مثلاً 22).")
                return
            PENDING_REG[user_id]["age"] = int(text)
            PENDING_REG[user_id]["step"] = "gender"
            await update.message.reply_text("جنسیتت چیه؟", reply_markup=kb_gender("reggender"))
            return

        if step == "city":
            PENDING_REG[user_id]["city"] = text[:30]
            data = PENDING_REG.pop(user_id)
            upsert_user(
                user_id, name=data["name"], age=data["age"], gender=data["gender"],
                province=data["province"], city=data["city"],
            )
            touch_last_active(user_id)
            user = get_user(user_id)
            await update.message.reply_text(
                f"پروفایلت ساخته شد! ✅\n\n{status_text(user)}", reply_markup=kb_main_menu()
            )
            return

        await update.message.reply_text("لطفاً از دکمه‌های بالا انتخاب کن.")
        return

    touch_last_active(user_id)

    # --- رله‌ی پیام در چت فعال ---
    if user_id in ACTIVE_CHATS:
        if is_rate_limited(user_id):
            await update.message.reply_text("🛑 سرعت ارسال پیامت زیاده، کمی صبر کن.")
            return
        if update.message.forward_date or update.message.forward_from_chat:
            await update.message.reply_text("⚠️ ارسال پیام فوروارد شده مجاز نیست.")
            return
        if contains_spam(text or (update.message.caption or "")):
            await update.message.reply_text("⚠️ ارسال لینک، آیدی یا تبلیغات ممنوعه.")
            return

        user = get_user(user_id)
        if not is_trial_active(user) and not user["is_premium_lifetime"]:
            elapsed = int((now_utc() - CHAT_START.get(user_id, now_utc())).total_seconds())
            used_today = ensure_daily_reset(user)["daily_chat_seconds_used"]
            if used_today + elapsed >= DAILY_FREE_SECONDS:
                await update.message.reply_text("⏱ زمان رایگان امروزت تموم شد. فردا دوباره امتحان کن یا با دعوت ۴ نفر دسترسی همیشگی بگیر.")
                await end_chat(user_id, context, quota_hit=True)
                return

        partner_id = ACTIVE_CHATS[user_id]
        await context.bot.copy_message(
            chat_id=partner_id, from_chat_id=update.effective_chat.id, message_id=update.effective_message.message_id
        )
        return

    user = get_user(user_id)
    if user and user.get("name"):
        await update.message.reply_text("از منو یکی رو انتخاب کن 👇", reply_markup=kb_main_menu())
    else:
        await update.message.reply_text("برای شروع دستور /start رو بزن.")


# ---------------------------------------------------------------------------
# مدیریت چت
# ---------------------------------------------------------------------------

async def end_chat(user_id: int, context: ContextTypes.DEFAULT_TYPE, quota_hit: bool = False):
    partner_id = ACTIVE_CHATS.pop(user_id, None)
    if partner_id:
        ACTIVE_CHATS.pop(partner_id, None)

    start_time = CHAT_START.pop(user_id, None)
    if start_time:
        add_chat_seconds(user_id, int((now_utc() - start_time).total_seconds()))
    if partner_id:
        p_start = CHAT_START.pop(partner_id, None)
        if p_start:
            add_chat_seconds(partner_id, int((now_utc() - p_start).total_seconds()))

    if not quota_hit:
        await context.bot.send_message(user_id, "چت پایان یافت.", reply_markup=kb_main_menu())
    if partner_id:
        await context.bot.send_message(partner_id, "طرف مقابل چت رو تموم کرد. 🛑", reply_markup=kb_main_menu())


async def try_match_queue(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    for other_id in list(WAITING_QUEUE):
        if other_id == user_id:
            continue
        WAITING_QUEUE.remove(other_id)
        if user_id in WAITING_QUEUE:
            WAITING_QUEUE.remove(user_id)
        ACTIVE_CHATS[user_id] = other_id
        ACTIVE_CHATS[other_id] = user_id
        CHAT_START[user_id] = now_utc()
        CHAT_START[other_id] = now_utc()
        await context.bot.send_message(user_id, "🎉 یه همراه پیدا شد! چت شروع شد.", reply_markup=kb_chat_controls())
        await context.bot.send_message(other_id, "🎉 یه همراه پیدا شد! چت شروع شد.", reply_markup=kb_chat_controls())
        return True
    return False


# ---------------------------------------------------------------------------
# دکمه‌ها
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    data = query.data

    if get_ban(user_id) and not data.startswith("admin"):
        return

    # --- انتخاب زبان (فعلاً فقط ذخیره میشه، ادامه فارسیه) ---
    if data.startswith("lang_"):
        upsert_user(user_id, language=data.split("_")[-1])
        PENDING_REG[user_id] = {"step": "name"}
        await query.edit_message_text("زبان ثبت شد ✅")
        await context.bot.send_message(chat_id, "بریم پروفایلتو بسازیم. اسمت چیه؟")
        return

    if data.startswith("reggender_") and user_id in PENDING_REG:
        PENDING_REG[user_id]["gender"] = data.split("_")[-1]
        PENDING_REG[user_id]["step"] = "province"
        await query.edit_message_text("استانت کدومه؟")
        await context.bot.send_message(chat_id, "یکی رو انتخاب کن 👇", reply_markup=kb_provinces())
        return

    if data.startswith("regprov_") and user_id in PENDING_REG:
        idx = int(data.split("_")[-1])
        PENDING_REG[user_id]["province"] = PROVINCES[idx]
        PENDING_REG[user_id]["step"] = "city"
        await query.edit_message_text(f"استان: {PROVINCES[idx]} ✅")
        await context.bot.send_message(chat_id, "اسم شهرت رو بنویس:")
        return

    if data == "menu_myprofile":
        user = get_user(user_id)
        if not user or not user.get("name"):
            await context.bot.send_message(chat_id, "اول باید پروفایل بسازی. /start رو بزن.")
            return
        await context.bot.send_message(chat_id, profile_text(user) + f"\n\n{status_text(user)}")
        return

    if data == "menu_edit":
        PENDING_REG[user_id] = {"step": "name"}
        await context.bot.send_message(chat_id, "بریم پروفایلتو دوباره بسازیم. اسمت چیه؟")
        return

    if data == "get_ref_link":
        user = get_user(user_id)
        ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
        await context.bot.send_message(
            chat_id,
            f"🔗 لینک دعوت اختصاصی تو:\n{ref_link}\n\n"
            f"تعداد دعوتی‌ها: {user['referral_count']}/{REFERRALS_FOR_LIFETIME}\n"
            f"با دعوت {REFERRALS_FOR_LIFETIME} نفر، دسترسی‌ت همیشگی میشه.",
        )
        return

    if data == "menu_search":
        user = get_user(user_id)
        if not user or not user.get("name"):
            await context.bot.send_message(chat_id, "اول باید پروفایل بسازی. /start رو بزن.")
            return
        await context.bot.send_message(chat_id, "چه کسایی رو از هم‌استانی‌هات نشونت بدم؟", reply_markup=kb_search_gender())
        return

    if data.startswith("searchgender_"):
        user = get_user(user_id)
        gender_filter = data.split("_")[-1]
        gender_filter = None if gender_filter == "any" else gender_filter
        results = search_users(user["province"], gender_filter, exclude_id=user_id)
        SEARCH_RESULTS[user_id] = results
        if not results:
            await context.bot.send_message(chat_id, "فعلاً کسی با این فیلتر پیدا نشد.")
            return
        await context.bot.send_message(chat_id, f"🔎 {len(results)} نفر پیدا شد:")
        for other in results:
            await context.bot.send_message(chat_id, profile_text(other), reply_markup=kb_profile_actions(other["user_id"]))
        return

    if data.startswith("like_"):
        target_id = int(data.split("_")[-1])
        added = like_user(user_id, target_id)
        await query.answer("لایک ثبت شد ❤️" if added else "قبلاً لایک کرده بودی!", show_alert=False)
        return

    if data.startswith("chatreq_"):
        target_id = int(data.split("_")[-1])
        requester = get_user(user_id)
        if not can_start_chat(requester):
            await query.answer("زمان رایگان امروزت تموم شده.", show_alert=True)
            return
        if user_id in ACTIVE_CHATS:
            await query.answer("الان خودت داخل یه چت فعالی!", show_alert=True)
            return
        target = get_user(target_id)
        if not target or not target.get("name"):
            await query.answer("این کاربر دیگه در دسترس نیست.", show_alert=True)
            return
        await context.bot.send_message(
            target_id, f"💌 {requester['name']} ({requester['age']} ساله) ازت درخواست چت داره.",
            reply_markup=kb_accept_reject(user_id),
        )
        await query.answer("درخواست چت ارسال شد ✅", show_alert=False)
        return

    if data.startswith("chatacc_"):
        requester_id = int(data.split("_")[-1])
        if user_id in ACTIVE_CHATS or requester_id in ACTIVE_CHATS:
            await context.bot.send_message(chat_id, "یکی از دو طرف الان مشغول یه چت دیگه‌ست.")
            return
        ACTIVE_CHATS[user_id] = requester_id
        ACTIVE_CHATS[requester_id] = user_id
        CHAT_START[user_id] = now_utc()
        CHAT_START[requester_id] = now_utc()
        await context.bot.send_message(user_id, "چت شروع شد! 🎉", reply_markup=kb_chat_controls())
        await context.bot.send_message(requester_id, "درخواستت قبول شد! چت شروع شد 🎉", reply_markup=kb_chat_controls())
        return

    if data.startswith("chatrej_"):
        requester_id = int(data.split("_")[-1])
        await context.bot.send_message(requester_id, "درخواست چتت رد شد. 😔")
        return

    if data == "menu_connect":
        user = get_user(user_id)
        if not user or not user.get("name"):
            await context.bot.send_message(chat_id, "اول باید پروفایل بسازی. /start رو بزن.")
            return
        if user_id in ACTIVE_CHATS:
            await context.bot.send_message(chat_id, "الان داخل یه چت فعالی.")
            return
        if not can_start_chat(user):
            await context.bot.send_message(chat_id, "⏱ زمان رایگان امروزت تموم شده. فردا دوباره بیا یا با دعوت دوستات دسترسی همیشگی بگیر.")
            return
        matched = await try_match_queue(user_id, context)
        if not matched:
            if user_id not in WAITING_QUEUE:
                WAITING_QUEUE.append(user_id)
            await context.bot.send_message(chat_id, "🔎 در حال جستجوی یه همراه...", reply_markup=kb_cancel_queue())
        return

    if data == "cancel_queue":
        if user_id in WAITING_QUEUE:
            WAITING_QUEUE.remove(user_id)
        await query.edit_message_text("❌ از صف خارج شدی.")
        return

    if data == "chat_stop":
        await end_chat(user_id, context)
        return

    if data == "report_partner":
        partner_id = ACTIVE_CHATS.get(user_id)
        if partner_id:
            create_report(user_id, partner_id)
            await context.bot.send_message(chat_id, "🚨 گزارشت ثبت شد و توسط ادمین بررسی میشه.")
            await end_chat(user_id, context)
        return

    # --- پنل ادمین ---
    if data == "admin_next" and user_id in ADMIN_IDS:
        await send_next_report(chat_id, context)
        return

    if data.startswith("adminban_") and user_id in ADMIN_IDS:
        _, report_id, reported_id = data.split("_")
        report_id, reported_id = int(report_id), int(reported_id)
        set_report_status(report_id, "approved")
        create_ban(reported_id)
        try:
            ref_link = f"https://t.me/{context.bot.username}?start={reported_id}"
            await context.bot.send_message(
                reported_id,
                f"⛔ حسابت مسدود شد.\nبرای رفع مسدودی {REFERRALS_FOR_UNBAN} نفر رو دعوت کن:\n{ref_link}",
            )
        except Exception:
            pass
        await context.bot.send_message(chat_id, f"کاربر {reported_id} مسدود شد.")
        await send_next_report(chat_id, context)
        return

    if data.startswith("adminignore_") and user_id in ADMIN_IDS:
        report_id = int(data.split("_")[-1])
        set_report_status(report_id, "rejected")
        await send_next_report(chat_id, context)
        return


async def send_next_report(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    rep = get_next_pending_report()
    if not rep:
        await context.bot.send_message(chat_id, "✅ گزارش جدیدی وجود نداره.")
        return
    reported = get_user(rep["reported_id"])
    name = reported["name"] if reported and reported.get("name") else "—"
    msg = f"🚨 گزارش #{rep['id']}\n\nکاربر گزارش‌شده: {rep['reported_id']} ({name})\nدلیل: {rep['reason']}"
    await context.bot.send_message(chat_id, msg, reply_markup=kb_admin_report(rep["id"], rep["reported_id"]))


# ---------------------------------------------------------------------------
# دستورات ادمین
# ---------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    pending = count_pending_reports()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"📥 بررسی گزارش‌ها ({pending})", callback_data="admin_next")]])
    await update.message.reply_text("⚙️ پنل مدیریت", reply_markup=kb)


# ---------------------------------------------------------------------------
# پاک‌سازی خودکار (اجرا هر ۱ ساعت با JobQueue)
# ---------------------------------------------------------------------------

async def purge_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        cutoff_ban = now_utc() - timedelta(days=BAN_GRACE_DAYS)
        cutoff_inactive = now_utc() - timedelta(days=INACTIVITY_PURGE_DAYS)

        with closing(db()) as conn:
            expired_bans = conn.execute(
                "SELECT user_id FROM bans WHERE is_data_purged=0 AND banned_at<=?",
                (cutoff_ban.isoformat(),),
            ).fetchall()
            inactive_users = conn.execute(
                "SELECT user_id FROM users WHERE last_active_at<=? AND name IS NOT NULL",
                (cutoff_inactive.isoformat(),),
            ).fetchall()

        for row in expired_bans:
            purge_profile(row["user_id"])
            mark_ban_purged(row["user_id"])

        for row in inactive_users:
            purge_profile(row["user_id"])

        logger.info(f"Purge run: {len(expired_bans)} banned, {len(inactive_users)} inactive purged.")
    except Exception:
        logger.exception("خطا در اجرای پاک‌سازی خودکار")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if "توکن" in BOT_TOKEN:
        print("⚠️ لطفاً اول BOT_TOKEN رو تنظیم کن.")
        return

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(purge_job, interval=3600, first=60)

    print("ربات روشن شد و منتظر پیام‌هاست...")
    app.run_polling()


if __name__ == "__main__":
    main()
