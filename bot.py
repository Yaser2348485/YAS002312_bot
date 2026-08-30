# -*- coding: utf-8 -*-
"""
ربات چت و آشنایی ناشناس (کاملاً رایگان)
------------------------------------------
شبیه ربات چتوگرام: ساخت پروفایل، جستجوی کاربران هم‌استانی،
لایک، درخواست چت، و چت ناشناس. هیچ بخشی از این ربات پولی نیست.

نصب پیش‌نیازها:
    pip install python-telegram-bot --upgrade

قبل از اجرا، توکن زیر رو ست کن:
    BOT_TOKEN -> از @BotFather می‌گیری

اجرا:
    python bot.py
"""

import logging
import os
import random
import sqlite3
from contextlib import closing

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "توکن-ربات-تلگرام-اینجا")
DB_PATH = os.environ.get("DB_PATH", "chatogeram_clone.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PROVINCES = [
    "آذربایجان شرقی", "آذربایجان غربی", "اردبیل", "اصفهان", "البرز",
    "ایلام", "بوشهر", "تهران", "چهارمحال و بختیاری", "خراسان جنوبی",
    "خراسان رضوی", "خراسان شمالی", "خوزستان", "زنجان", "سمنان",
    "سیستان و بلوچستان", "فارس", "قزوین", "قم", "کردستان",
    "کرمان", "کرمانشاه", "کهگیلویه و بویراحمد", "گلستان", "گیلان",
    "لرستان", "مازندران", "مرکزی", "هرمزگان", "همدان", "یزد",
]

GENDER_LABEL = {"male": "پسر 👦", "female": "دختر 👧"}

# حافظه‌ی موقتِ مراحل ثبت‌نام (فقط برای مدت کوتاهِ پر کردن پروفایل)
PENDING_REG: dict[int, dict] = {}
# نتایج آخرین جستجوی هر کاربر (برای نمایش «نفر بعدی»)
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
                partner_id INTEGER,
                chat_status TEXT DEFAULT 'idle'
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


def get_user(user_id: int):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def upsert_user(user_id: int, **fields):
    existing = get_user(user_id)
    with closing(db()) as conn, conn:
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE users SET {sets} WHERE user_id=?",
                (*fields.values(), user_id),
            )
        else:
            cols = ["user_id"] + list(fields.keys())
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO users ({', '.join(cols)}) VALUES ({placeholders})",
                (user_id, *fields.values()),
            )


def set_status(user_id: int, status: str, partner_id=None):
    upsert_user(user_id, chat_status=status, partner_id=partner_id)


def like_user(liker_id: int, liked_id: int) -> bool:
    """لایک ثبت می‌کنه. اگه قبلاً لایک کرده بود False برمی‌گردونه."""
    with closing(db()) as conn, conn:
        try:
            conn.execute(
                "INSERT INTO likes (liker_id, liked_id) VALUES (?, ?)",
                (liker_id, liked_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def count_likes(user_id: int) -> int:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM likes WHERE liked_id=?", (user_id,)
        ).fetchone()
        return row["c"] if row else 0


def search_users(province: str, gender: str | None, exclude_id: int, limit: int = 5):
    query = "SELECT * FROM users WHERE province=? AND user_id != ?"
    params = [province, exclude_id]
    if gender in ("male", "female"):
        query += " AND gender=?"
        params.append(gender)
    query += " ORDER BY RANDOM() LIMIT ?"
    params.append(limit)
    with closing(db()) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def find_waiting_partner(exclude_id: int):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE chat_status='waiting' AND user_id != ? LIMIT 1",
            (exclude_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# کیبوردها
# ---------------------------------------------------------------------------

def kb_gender(prefix: str):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(GENDER_LABEL["male"], callback_data=f"{prefix}_male")],
            [InlineKeyboardButton(GENDER_LABEL["female"], callback_data=f"{prefix}_female")],
        ]
    )


def kb_provinces():
    rows = []
    row = []
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
            [InlineKeyboardButton("🔗 وصل به یه ناشناس", callback_data="menu_connect")],
            [InlineKeyboardButton("🔍 جستجوی کاربران", callback_data="menu_search")],
            [InlineKeyboardButton("👤 پروفایل من", callback_data="menu_myprofile")],
            [InlineKeyboardButton("✏️ ویرایش پروفایل", callback_data="menu_edit")],
        ]
    )


def kb_search_gender():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("پسر باشه 👦", callback_data="searchgender_male")],
            [InlineKeyboardButton("دختر باشه 👧", callback_data="searchgender_female")],
            [InlineKeyboardButton("همه رو نشون بده 👫", callback_data="searchgender_any")],
        ]
    )


def kb_profile_actions(target_id: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❤️ لایک", callback_data=f"like_{target_id}"),
                InlineKeyboardButton("💬 درخواست چت", callback_data=f"chatreq_{target_id}"),
            ],
        ]
    )


def kb_chat_controls():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 پایان چت", callback_data="chat_stop")]]
    )


def kb_accept_reject(requester_id: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ قبول", callback_data=f"chatacc_{requester_id}"),
                InlineKeyboardButton("❌ رد", callback_data=f"chatrej_{requester_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# نمایش پروفایل
# ---------------------------------------------------------------------------

def profile_text(u: dict) -> str:
    likes = count_likes(u["user_id"])
    return (
        f"👤 نام: {u['name']}\n"
        f"{'👦' if u['gender'] == 'male' else '👧'} جنسیت: {GENDER_LABEL.get(u['gender'], '—')}\n"
        f"🎂 سن: {u['age']}\n"
        f"📍 استان: {u['province']}\n"
        f"🏙 شهر: {u['city']}\n"
        f"❤️ تعداد لایک‌ها: {likes}"
    )


# ---------------------------------------------------------------------------
# شروع / ثبت‌نام
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if user and user.get("name"):
        await update.message.reply_text("خوش برگشتی! 🏠", reply_markup=kb_main_menu())
        return

    PENDING_REG[user_id] = {"step": "name"}
    await update.message.reply_text(
        "سلام! 👋 بریم پروفایلتو بسازیم.\n\nاسمت چیه؟ (فقط اسم، بدون فامیل)"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # --- مراحل ثبت‌نام ---
    if user_id in PENDING_REG:
        step = PENDING_REG[user_id]["step"]

        if step == "name":
            PENDING_REG[user_id]["name"] = text[:30]
            PENDING_REG[user_id]["step"] = "age"
            await update.message.reply_text("چند سالته؟ (فقط عدد بنویس)")
            return

        if step == "age":
            if not text.isdigit() or not (10 <= int(text) <= 90):
                await update.message.reply_text("لطفاً سنت رو به‌صورت عدد و منطقی وارد کن (مثلاً 22).")
                return
            PENDING_REG[user_id]["age"] = int(text)
            PENDING_REG[user_id]["step"] = "gender"
            await update.message.reply_text("جنسیتت چیه؟", reply_markup=kb_gender("reggender"))
            return

        if step == "city":
            PENDING_REG[user_id]["city"] = text[:30]
            data = PENDING_REG.pop(user_id)
            upsert_user(
                user_id,
                name=data["name"],
                age=data["age"],
                gender=data["gender"],
                province=data["province"],
                city=data["city"],
                chat_status="idle",
                partner_id=None,
            )
            await update.message.reply_text("پروفایلت ساخته شد! ✅", reply_markup=kb_main_menu())
            return

        # اگه توی مرحله gender یا province هست، منتظر دکمه است نه متن
        await update.message.reply_text("لطفاً از دکمه‌های بالا انتخاب کن.")
        return

    # --- رله‌ی پیام در چت فعال ---
    user = get_user(user_id)
    if user and user["chat_status"] == "chatting" and user["partner_id"]:
        await context.bot.copy_message(
            chat_id=user["partner_id"],
            from_chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
        )
        return

    if user and user.get("name"):
        await update.message.reply_text("از منو یکی رو انتخاب کن 👇", reply_markup=kb_main_menu())
    else:
        await update.message.reply_text("برای شروع دستور /start رو بزن.")


# ---------------------------------------------------------------------------
# دکمه‌ها
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    data = query.data

    # --- ادامه‌ی ثبت‌نام: جنسیت ---
    if data.startswith("reggender_") and user_id in PENDING_REG:
        PENDING_REG[user_id]["gender"] = data.split("_")[-1]
        PENDING_REG[user_id]["step"] = "province"
        await query.edit_message_text("استانت کدومه؟")
        await context.bot.send_message(chat_id, "یکی رو انتخاب کن 👇", reply_markup=kb_provinces())
        return

    # --- ادامه‌ی ثبت‌نام: استان ---
    if data.startswith("regprov_") and user_id in PENDING_REG:
        idx = int(data.split("_")[-1])
        PENDING_REG[user_id]["province"] = PROVINCES[idx]
        PENDING_REG[user_id]["step"] = "city"
        await query.edit_message_text(f"استان انتخابی: {PROVINCES[idx]} ✅")
        await context.bot.send_message(chat_id, "اسم شهرت رو بنویس:")
        return

    # --- منوی اصلی ---
    if data == "menu_myprofile":
        user = get_user(user_id)
        if not user:
            await context.bot.send_message(chat_id, "اول باید پروفایل بسازی. /start رو بزن.")
            return
        await context.bot.send_message(chat_id, profile_text(user))
        return

    if data == "menu_edit":
        PENDING_REG[user_id] = {"step": "name"}
        await context.bot.send_message(chat_id, "بریم پروفایلتو دوباره بسازیم.\nاسمت چیه؟")
        return

    if data == "menu_search":
        user = get_user(user_id)
        if not user:
            await context.bot.send_message(chat_id, "اول باید پروفایل بسازی. /start رو بزن.")
            return
        await context.bot.send_message(
            chat_id, "چه کسایی رو از بین هم‌استانی‌هات نشونت بدم؟", reply_markup=kb_search_gender()
        )
        return

    if data.startswith("searchgender_"):
        user = get_user(user_id)
        gender_filter = data.split("_")[-1]
        gender_filter = None if gender_filter == "any" else gender_filter
        results = search_users(user["province"], gender_filter, exclude_id=user_id)
        SEARCH_RESULTS[user_id] = results

        if not results:
            await context.bot.send_message(chat_id, "فعلاً کسی با این فیلتر پیدا نشد. بعداً دوباره امتحان کن.")
            return

        await context.bot.send_message(chat_id, f"🔎 {len(results)} نفر پیدا شد:")
        for other in results:
            await context.bot.send_message(
                chat_id, profile_text(other), reply_markup=kb_profile_actions(other["user_id"])
            )
        return

    # --- لایک ---
    if data.startswith("like_"):
        target_id = int(data.split("_")[-1])
        added = like_user(user_id, target_id)
        if added:
            await query.answer("لایک ثبت شد ❤️", show_alert=False)
        else:
            await query.answer("قبلاً لایک کرده بودی!", show_alert=False)
        return

    # --- درخواست چت ---
    if data.startswith("chatreq_"):
        target_id = int(data.split("_")[-1])
        requester = get_user(user_id)
        target = get_user(target_id)

        if not target:
            await query.answer("این کاربر دیگه در دسترس نیست.", show_alert=True)
            return
        if requester["chat_status"] == "chatting":
            await query.answer("الان خودت داخل یه چت فعالی!", show_alert=True)
            return

        await context.bot.send_message(
            target_id,
            f"💌 یه نفر ({requester['name']}, {requester['age']} ساله) ازت درخواست چت داره.",
            reply_markup=kb_accept_reject(user_id),
        )
        await query.answer("درخواست چت ارسال شد ✅", show_alert=False)
        return

    if data.startswith("chatacc_"):
        requester_id = int(data.split("_")[-1])
        set_status(user_id, "chatting", partner_id=requester_id)
        set_status(requester_id, "chatting", partner_id=user_id)
        await context.bot.send_message(user_id, "چت شروع شد! هر چی بفرستی ناشناس ارسال میشه.", reply_markup=kb_chat_controls())
        await context.bot.send_message(requester_id, "درخواستت قبول شد! چت شروع شد 🎉", reply_markup=kb_chat_controls())
        return

    if data.startswith("chatrej_"):
        requester_id = int(data.split("_")[-1])
        await context.bot.send_message(requester_id, "متاسفانه درخواست چتت رد شد. 😔")
        await query.answer("رد شد.", show_alert=False)
        return

    # --- اتصال تصادفی ---
    if data == "menu_connect":
        user = get_user(user_id)
        if not user:
            await context.bot.send_message(chat_id, "اول باید پروفایل بسازی. /start رو بزن.")
            return
        if user["chat_status"] == "chatting":
            await context.bot.send_message(chat_id, "الان داخل یه چت فعالی.")
            return

        partner = find_waiting_partner(user_id)
        if partner:
            set_status(user_id, "chatting", partner_id=partner["user_id"])
            set_status(partner["user_id"], "chatting", partner_id=user_id)
            await context.bot.send_message(user_id, "یه همراه پیدا شد! چت شروع شد 🎉", reply_markup=kb_chat_controls())
            await context.bot.send_message(partner["user_id"], "یه همراه پیدا شد! چت شروع شد 🎉", reply_markup=kb_chat_controls())
        else:
            set_status(user_id, "waiting")
            await context.bot.send_message(chat_id, "🔎 در حال جستجوی یه همراه چت تصادفی...")
        return

    if data == "chat_stop":
        user = get_user(user_id)
        partner_id = user["partner_id"] if user else None
        set_status(user_id, "idle")
        await context.bot.send_message(chat_id, "چت پایان یافت.", reply_markup=kb_main_menu())
        if partner_id:
            set_status(partner_id, "idle")
            await context.bot.send_message(partner_id, "طرف مقابل چت رو تموم کرد. 🛑", reply_markup=kb_main_menu())
        return


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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("ربات روشن شد و منتظر پیام‌هاست...")
    app.run_polling()


if __name__ == "__main__":
    main()
