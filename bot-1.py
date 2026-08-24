"""
ربات چت ناشناس تلگرام با فیلتر جنسیت و زبان
مشابه چتوگرام - رایگان و ساده

نحوه اجرا:
    1) pip install -r requirements.txt
    2) توکن ربات رو در فایل .env یا متغیر محیطی BOT_TOKEN قرار بده
    3) python bot.py
"""

import logging
import os
import random
from dataclasses import dataclass, field
from enum import Enum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class Status(str, Enum):
    NEW = "new"                # هنوز پروفایل نساخته
    IDLE = "idle"               # منتظر در منو
    WAITING = "waiting"         # در صف جستجوی همراه
    CHATTING = "chatting"       # درون یک چت فعال


@dataclass
class UserProfile:
    user_id: int
    gender: str = None          # "male" | "female"
    want_gender: str = None     # "male" | "female" | "any"
    language: str = None        # "fa" | "en" | "any"
    status: Status = Status.NEW
    partner_id: int = None
    pending_field: str = None   # فیلدی که در حال تنظیم آن هستیم


# حافظه در سطح برنامه (ساده و رایگان - بدون نیاز به دیتابیس خارجی)
USERS: dict[int, UserProfile] = {}
WAITING_POOL: list[int] = []  # لیست شناسه کاربرانی که منتظر پیدا کردن یک همراه هستند


def get_user(user_id: int) -> UserProfile:
    if user_id not in USERS:
        USERS[user_id] = UserProfile(user_id=user_id)
    return USERS[user_id]


GENDER_LABELS = {"male": "پسر 👦", "female": "دختر 👧"}
WANT_LABELS = {"male": "پسر 👦", "female": "دختر 👧", "any": "فرقی نمی‌کنه 🤝"}
LANG_LABELS = {"fa": "فارسی 🇮🇷", "en": "English 🇬🇧", "any": "فرقی نمی‌کنه 🌐"}


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb_gender():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(GENDER_LABELS["male"], callback_data="set_gender_male")],
        [InlineKeyboardButton(GENDER_LABELS["female"], callback_data="set_gender_female")],
    ])


def kb_want_gender():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(WANT_LABELS["male"], callback_data="set_want_male")],
        [InlineKeyboardButton(WANT_LABELS["female"], callback_data="set_want_female")],
        [InlineKeyboardButton(WANT_LABELS["any"], callback_data="set_want_any")],
    ])


def kb_language():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(LANG_LABELS["fa"], callback_data="set_lang_fa")],
        [InlineKeyboardButton(LANG_LABELS["en"], callback_data="set_lang_en")],
        [InlineKeyboardButton(LANG_LABELS["any"], callback_data="set_lang_any")],
    ])


def kb_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 پیدا کردن همراه چت", callback_data="find")],
        [InlineKeyboardButton("⚙️ تنظیمات پروفایل", callback_data="settings")],
    ])


def kb_searching():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو جستجو", callback_data="cancel_search")],
    ])


def kb_chatting():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ نفر بعدی", callback_data="next")],
        [InlineKeyboardButton("🛑 پایان چت", callback_data="stop")],
    ])


# ---------------------------------------------------------------------------
# Profile setup flow
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user.gender is None:
        user.pending_field = "gender"
        await update.message.reply_text(
            "سلام! 👋 به ربات چت ناشناس خوش اومدی.\n\n"
            "برای شروع، بگو جنسیت خودت چیه:",
            reply_markup=kb_gender(),
        )
        return

    user.status = Status.IDLE
    await send_main_menu(update.effective_chat.id, context)


async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id,
        "🏠 منوی اصلی\n\nآماده‌ای یه همراه چت پیدا کنی؟",
        reply_markup=kb_main_menu(),
    )


async def show_settings(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user: UserProfile):
    text = (
        "⚙️ تنظیمات فعلی تو:\n\n"
        f"جنسیت: {GENDER_LABELS.get(user.gender, '—')}\n"
        f"به دنبال: {WANT_LABELS.get(user.want_gender, '—')}\n"
        f"زبان: {LANG_LABELS.get(user.language, '—')}\n\n"
        "برای تغییر، یکی از گزینه‌ها رو انتخاب کن:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("تغییر جنسیت", callback_data="edit_gender")],
        [InlineKeyboardButton("تغییر ترجیح جنسیت طرف مقابل", callback_data="edit_want")],
        [InlineKeyboardButton("تغییر زبان", callback_data="edit_lang")],
        [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="back_menu")],
    ])
    await context.bot.send_message(chat_id, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Matchmaking
# ---------------------------------------------------------------------------

def compatible(a: UserProfile, b: UserProfile) -> bool:
    if a.user_id == b.user_id:
        return False
    gender_ok = (
        (a.want_gender == "any" or a.want_gender == b.gender)
        and (b.want_gender == "any" or b.want_gender == a.gender)
    )
    lang_ok = (
        a.language == "any" or b.language == "any" or a.language == b.language
    )
    return gender_ok and lang_ok


async def try_match(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    me = get_user(user_id)
    for other_id in WAITING_POOL:
        if other_id == user_id:
            continue
        other = get_user(other_id)
        if compatible(me, other):
            WAITING_POOL.remove(other_id)
            if user_id in WAITING_POOL:
                WAITING_POOL.remove(user_id)

            me.status = Status.CHATTING
            other.status = Status.CHATTING
            me.partner_id = other_id
            other.partner_id = user_id

            await context.bot.send_message(
                user_id,
                "✅ یه همراه چت پیدا شد! هر چی بفرستی به صورت ناشناس براش ارسال میشه.\n"
                "هویت هیچ‌کدومتون فاش نمیشه.",
                reply_markup=kb_chatting(),
            )
            await context.bot.send_message(
                other_id,
                "✅ یه همراه چت پیدا شد! هر چی بفرستی به صورت ناشناس براش ارسال میشه.\n"
                "هویت هیچ‌کدومتون فاش نمیشه.",
                reply_markup=kb_chatting(),
            )
            return True
    return False


async def start_search(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(user_id)
    user.status = Status.WAITING
    if user_id not in WAITING_POOL:
        WAITING_POOL.append(user_id)

    matched = await try_match(user_id, context)
    if not matched:
        await context.bot.send_message(
            chat_id,
            "🔎 در حال جستجو برای یک همراه چت مناسب...\n"
            "به محض پیدا شدن بهت خبر می‌دیم.",
            reply_markup=kb_searching(),
        )


def end_chat_session(user_id: int) -> int | None:
    """پایان چت فعلی کاربر و بازگرداندن هر دو نفر به حالت idle. آی‌دی طرف مقابل رو برمی‌گردونه."""
    user = get_user(user_id)
    partner_id = user.partner_id
    user.status = Status.IDLE
    user.partner_id = None
    if partner_id is not None:
        partner = get_user(partner_id)
        partner.status = Status.IDLE
        partner.partner_id = None
    if user_id in WAITING_POOL:
        WAITING_POOL.remove(user_id)
    return partner_id


# ---------------------------------------------------------------------------
# Callback query handler (دکمه‌های شیشه‌ای)
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user = get_user(user_id)
    data = query.data

    # --- تنظیم اولیه پروفایل ---
    if data.startswith("set_gender_"):
        user.gender = data.split("_")[-1]
        user.pending_field = "want"
        await query.edit_message_text(f"جنسیت تو ثبت شد: {GENDER_LABELS[user.gender]} ✅")
        await context.bot.send_message(
            chat_id, "به دنبال چه جنسیتی می‌گردی؟", reply_markup=kb_want_gender()
        )
        return

    if data.startswith("set_want_"):
        user.want_gender = data.split("_")[-1]
        user.pending_field = "lang"
        await query.edit_message_text(f"ترجیح تو ثبت شد: {WANT_LABELS[user.want_gender]} ✅")
        await context.bot.send_message(
            chat_id, "با چه زبانی می‌خوای چت کنی؟", reply_markup=kb_language()
        )
        return

    if data.startswith("set_lang_"):
        user.language = data.split("_")[-1]
        user.pending_field = None
        user.status = Status.IDLE
        await query.edit_message_text(f"زبان چت تو ثبت شد: {LANG_LABELS[user.language]} ✅")
        await send_main_menu(chat_id, context)
        return

    # --- ویرایش تنظیمات ---
    if data == "edit_gender":
        await query.edit_message_text("جنسیت جدیدت رو انتخاب کن:", reply_markup=kb_gender())
        return
    if data == "edit_want":
        await query.edit_message_text("ترجیح جدید رو انتخاب کن:", reply_markup=kb_want_gender())
        return
    if data == "edit_lang":
        await query.edit_message_text("زبان جدید رو انتخاب کن:", reply_markup=kb_language())
        return

    # --- منو ---
    if data == "settings":
        await show_settings(chat_id, context, user)
        return

    if data == "back_menu":
        await send_main_menu(chat_id, context)
        return

    if data == "find":
        if user.status == Status.CHATTING:
            await context.bot.send_message(chat_id, "الان داخل یه چت فعال هستی.")
            return
        await start_search(chat_id, user_id, context)
        return

    if data == "cancel_search":
        if user_id in WAITING_POOL:
            WAITING_POOL.remove(user_id)
        user.status = Status.IDLE
        await query.edit_message_text("جستجو لغو شد.")
        await send_main_menu(chat_id, context)
        return

    if data == "stop":
        partner_id = end_chat_session(user_id)
        await query.edit_message_text("چت پایان یافت.")
        await send_main_menu(chat_id, context)
        if partner_id is not None:
            await context.bot.send_message(partner_id, "طرف مقابل چت رو تموم کرد. 🛑")
            await send_main_menu(partner_id, context)
        return

    if data == "next":
        partner_id = end_chat_session(user_id)
        if partner_id is not None:
            await context.bot.send_message(partner_id, "طرف مقابل به دنبال یک نفر جدید رفت. 🛑")
            await send_main_menu(partner_id, context)
        await start_search(chat_id, user_id, context)
        return


# ---------------------------------------------------------------------------
# Relay messages between paired users
# ---------------------------------------------------------------------------

async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    if user.status != Status.CHATTING or user.partner_id is None:
        # اگه هنوز داره پروفایلش رو کامل می‌کنه یا در منوعه، راهنمایی کن
        if user.gender is None:
            await start(update, context)
        else:
            await update.message.reply_text(
                "الان داخل چت نیستی. برای پیدا کردن همراه از منو استفاده کن.",
                reply_markup=kb_main_menu(),
            )
        return

    # copy_message هویت فرستنده رو فاش نمی‌کنه (بر خلاف forward_message)
    await context.bot.copy_message(
        chat_id=user.partner_id,
        from_chat_id=update.effective_chat.id,
        message_id=update.effective_message.message_id,
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner_id = end_chat_session(user_id)
    await update.message.reply_text("چت پایان یافت.", reply_markup=kb_main_menu())
    if partner_id is not None:
        await context.bot.send_message(partner_id, "طرف مقابل چت رو تموم کرد. 🛑")
        await send_main_menu(partner_id, context)


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    partner_id = end_chat_session(user_id)
    if partner_id is not None:
        await context.bot.send_message(partner_id, "طرف مقابل به دنبال یک نفر جدید رفت. 🛑")
        await send_main_menu(partner_id, context)
    await start_search(chat_id, user_id, context)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit(
            "توکن ربات رو تنظیم نکردی! متغیر محیطی BOT_TOKEN رو ست کن یا "
            "مستقیم توی کد جایگزین کن."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay))

    logger.info("ربات در حال اجراست...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
