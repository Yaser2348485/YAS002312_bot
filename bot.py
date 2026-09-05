import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

from telegram import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# ---------------------------------------------------------
# ۱. تنظیمات اولیه و دیتابیس
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"  # توکن ربات تلگرام خود را وارد کنید
DATABASE_URL = "postgresql://user:password@localhost/chatogram_db"  # آدرس دیتابیس PostgreSQL
ADMIN_IDS = [123456789]  # آی‌دی ادمین‌ها

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# مراحل ثبت نام (Conversation States)
GENDER, AGE, PROVINCE = range(3)

# ---------------------------------------------------------
# ۲. مدل‌های دیتابیس
# ---------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)

    referred_by = Column(BigInteger, nullable=True)
    referral_count = Column(Integer, default=0)
    is_vip = Column(Boolean, default=False)

    profile = relationship(
        "Profile", back_populates="user", uselist=False, cascade="all, delete"
    )


class Profile(Base):
    __tablename__ = "profiles"

    telegram_id = Column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    gender = Column(String(10), default="نامشخص")
    age = Column(Integer, default=0)
    province = Column(String(50), default="نامشخص")
    likes_count = Column(Integer, default=0)
    is_completed = Column(Boolean, default=False)

    user = relationship("User", back_populates="profile")


# ---------------------------------------------------------
# ۳. حافظه موقت چت‌ها و فیلترها
# ---------------------------------------------------------
waiting_queue = []  # صف جستجو
active_chats = {}   # چت‌های فعال {user_id: partner_id}
rate_limit_store = {}

SPAM_REGEX = re.compile(
    r"(@[a-zA-C0-9_]{5,32})|(https?://[^\s]+)|(t\.me/[^\s]+)", re.IGNORECASE
)

def is_rate_limited(user_id: int) -> bool:
    """کنترل نرخ ارسال پیام جهت جلوگیری از اسپم"""
    now = time.time()
    if user_id not in rate_limit_store:
        rate_limit_store[user_id] = []
    rate_limit_store[user_id] = [
        t for t in rate_limit_store[user_id] if now - t < 5
    ]
    if len(rate_limit_store[user_id]) >= 5:
        return True
    rate_limit_store[user_id].append(now)
    return False

# ---------------------------------------------------------
# ۴. کیبوردهای چتوگرام
# ---------------------------------------------------------
def main_menu_keyboard():
    keyboard = [
        [KeyboardButton("🚀 به یک ناشناس متصل شو")],
        [KeyboardButton("🔍 جستجوی هم‌صحبت"), KeyboardButton("👤 پروفایل من")],
        [KeyboardButton("🔗 دعوت دوستان (VIP)"), KeyboardButton("⚙️ تنظیمات")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def search_menu_keyboard():
    keyboard = [
        [KeyboardButton("👦 اتصال به پسر"), KeyboardButton("👧 اتصال به دختر")],
        [KeyboardButton("📍 هم‌شهری / هم-استانی")],
        [KeyboardButton("🔙 بازگشت به منوی اصلی")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def in_chat_keyboard():
    keyboard = [
        [KeyboardButton("✂️ قطع ارتباط"), KeyboardButton("➡️ بعدی (هم‌صحبت جدید)")],
        [KeyboardButton("👤 مشاهده پروفایل"), KeyboardButton("🚨 گزارش متخلف")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def cancel_queue_keyboard():
    keyboard = [[KeyboardButton("❌ انصراف از جستجو")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------------------------------------------------------
# ۵. سیستم ثبت‌نام اولیه
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    db = SessionLocal()

    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()

        if not user:
            referrer_id = int(args[0]) if (args and args[0].isdigit()) else None
            user = User(telegram_id=user_id, referred_by=referrer_id)
            profile = Profile(telegram_id=user_id)
            db.add(user)
            db.add(profile)
            db.commit()

            if referrer_id and referrer_id != user_id:
                ref_user = db.query(User).filter(User.telegram_id == referrer_id).first()
                if ref_user:
                    ref_user.referral_count += 1
                    if ref_user.referral_count >= 4:
                        ref_user.is_vip = True
                    db.commit()

        p = db.query(Profile).filter(Profile.telegram_id == user_id).first()

        if not p or not p.is_completed:
            reply_keyboard = [["پسر 👦", "دختر 👧"]]
            await update.message.reply_text(
                "👋 به چتوگرام خوش آمدید!\n\nلطفاً برای شروع جنسیت خود را انتخاب کنید:",
                reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True)
            )
            return GENDER

        user.last_active_at = datetime.utcnow()
        db.commit()

        await update.message.reply_text(
            "به منوی اصلی چتوگرام خوش آمدید.", reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END
    except Exception as e:
        db.rollback()
        logging.error(f"Error in start: {e}")
        await update.message.reply_text("خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        return ConversationHandler.END
    finally:
        db.close()

async def set_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text not in ["پسر 👦", "دختر 👧"]:
        await update.message.reply_text("لطفاً یکی از دکمه‌های زیر را انتخاب کنید.")
        return GENDER

    context.user_data['gender'] = "پسر" if "پسر" in text else "دختر"
    await update.message.reply_text("لطفاً سن خود را به عدد وارد کنید (مثال: 22):", reply_markup=ReplyKeyboardRemove())
    return AGE

async def set_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text.isdigit() or not (10 <= int(text) <= 80):
        await update.message.reply_text("لطفاً یک عدد معتبر برای سن وارد کنید (بین ۱۰ تا ۸۰):")
        return AGE

    context.user_data['age'] = int(text)
    await update.message.reply_text("لطفاً نام استان خود را وارد کنید (مثال: تهران، اصفهان، فارس...):")
    return PROVINCE

async def set_province(update: Update, context: ContextTypes.DEFAULT_TYPE):
    province = update.message.text.strip()
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        p = db.query(Profile).filter(Profile.telegram_id == user_id).first()
        if p:
            p.gender = context.user_data['gender']
            p.age = context.user_data['age']
            p.province = province
            p.is_completed = True
            db.commit()

        await update.message.reply_text(
            "🎉 پروفایل شما با موفقیت تکمیل شد!",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        db.rollback()
        logging.error(f"Error in set_province: {e}")
    finally:
        db.close()

    return ConversationHandler.END

async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ثبت‌نام لغو شد. جهت شروع مجدد /start را بزنید.")
    return ConversationHandler.END

# ---------------------------------------------------------
# ۶. مدیریت تمامی پیام‌ها (متن، عکس، ویس، ویدیو و دکمه‌ها)
# ---------------------------------------------------------
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    text = message.text or message.caption or ""
    db = SessionLocal()

    try:
        # ۱. اگر کاربر در چت فعال باشد
        if user_id in active_chats:
            if text == "✂️ قطع ارتباط":
                await end_chat(user_id, context)
                return

            elif text == "➡️ بعدی (هم‌صحبت جدید)":
                await end_chat(user_id, context)
                await start_random_chat(update, context, user_id)
                return

            elif text == "🚨 گزارش متخلف":
                await update.message.reply_text("🚨 گزارش شما ثبت شد و چت پایان یافت.")
                await end_chat(user_id, context)
                return

            elif text == "👤 مشاهده پروفایل":
                partner_id = active_chats[user_id]
                p = db.query(Profile).filter(Profile.telegram_id == partner_id).first()
                if p:
                    info = f"👤 **مشخصات هم‌صحبت:**\n\nجنسیت: {p.gender}\nسن: {p.age}\nاستان: {p.province}"
                    await update.message.reply_text(info, parse_mode="Markdown")
                return

            else:
                # ریت لیمیت
                if is_rate_limited(user_id):
                    await update.message.reply_text("🛑 سرعت ارسال پیام بسیار بالاست.")
                    return

                # جلوگیری از ارسال پیام‌های فورواردشده (برای حفظ امنیت هویت)
                if message.forward_date or message.forward_from_chat or message.forward_from:
                    await update.message.reply_text("⚠️ ارسال پیام‌های فورواردشده مجاز نیست!")
                    return

                # بررسی فیلتر لینک و آیدی
                if text and SPAM_REGEX.search(text):
                    await update.message.reply_text("⚠️ ارسال آیدی و لینک مجاز نیست!")
                    return

                # کپی دقیق تمامی انواع رسانه (ویدیو، عکس، ویس، متن) برای طرف مقابل
                partner_id = active_chats[user_id]
                try:
                    await message.copy(chat_id=partner_id)
                except Exception:
                    await update.message.reply_text("❌ هم‌صحبت شما چت را ترک کرده است.")
                    await end_chat(user_id, context)
                return

        # ۲. اگر کاربر در چت نیست و از دکمه‌های منو استفاده می‌کند
        if text == "🚀 به یک ناشناس متصل شو":
            await start_random_chat(update, context, user_id)

        elif text == "❌ انصراف از جستجو":
            if user_id in waiting_queue:
                waiting_queue.remove(user_id)
                await update.message.reply_text("❌ از صف جستجو خارج شدید.", reply_markup=main_menu_keyboard())

        elif text == "🔍 جستجوی هم‌صحبت":
            await update.message.reply_text("فیلتر مورد نظر خود را انتخاب کنید:", reply_markup=search_menu_keyboard())

        elif text == "🔙 بازگشت به منوی اصلی":
            await update.message.reply_text("به منوی اصلی بازگشتید:", reply_markup=main_menu_keyboard())

        elif text in ["👦 اتصال به پسر", "👧 اتصال به دختر", "📍 هم‌شهری / هم-استانی"]:
            u = db.query(User).filter(User.telegram_id == user_id).first()
            if u and not u.is_vip:
                ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
                await update.message.reply_text(
                    f"🔒 **این بخش مخصوص کاربران VIP است.**\n\n"
                    f"برای استفاده از فیلترهای جستجو، ۴ نفر را با لینک خود دعوت کنید:\n`{ref_link}`",
                    parse_mode="Markdown"
                )
            else:
                await start_random_chat(update, context, user_id)

        elif text == "👤 پروفایل من":
            p = db.query(Profile).filter(Profile.telegram_id == user_id).first()
            u = db.query(User).filter(User.telegram_id == user_id).first()
            if p and u:
                status = "VIP 🌟" if u.is_vip else "عادی 👤"
                msg = (
                    f"👤 **پروفایل کاربری شما:**\n\n"
                    f"🔸 وضعیت حساب: {status}\n"
                    f"🔸 جنسیت: {p.gender}\n"
                    f"🔸 سن: {p.age}\n"
                    f"🔸 استان: {p.province}\n"
                    f"👥 تعداد دعوتی‌ها: {u.referral_count} نفر"
                )
                await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu_keyboard())

        elif text == "🔗 دعوت دوستان (VIP)":
            ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
            await update.message.reply_text(
                f"🔗 **لینک دعوت اختصاصی شما:**\n`{ref_link}`\n\n"
                f"🎁 با دعوت **۴ نفر** به ربات، حساب شما VIP شده و می‌توانید جنسیت و شهر هم‌صحبت را انتخاب کنید.",
                parse_mode="Markdown"
            )

    except Exception as e:
        logging.error(f"Error in handle_all_messages: {e}")
    finally:
        db.close()

# ---------------------------------------------------------
# ۷. توابع مدیریت اتصال چت
# ---------------------------------------------------------
async def start_random_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if user_id in active_chats:
        await update.message.reply_text("شما در حال حاضر در یک گفتگو هستید!")
        return

    if user_id in waiting_queue:
        await update.message.reply_text("شما در صف جستجو قرار دارید...")
        return

    if waiting_queue:
        partner_id = waiting_queue.pop(0)
        active_chats[user_id] = partner_id
        active_chats[partner_id] = user_id

        await context.bot.send_message(
            user_id,
            "🎉 **به هم‌صحبت متصل شدید!**\nارسال پیام را شروع کنید:",
            reply_markup=in_chat_keyboard(),
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            partner_id,
            "🎉 **به هم‌صحبت متصل شدید!**\nارسال پیام را شروع کنید:",
            reply_markup=in_chat_keyboard(),
            parse_mode="Markdown"
        )
    else:
        waiting_queue.append(user_id)
        await update.message.reply_text(
            "🔎 در حال جستجوی هم‌صحبت...\nلطفاً منتظر بمانید.",
            reply_markup=cancel_queue_keyboard()
        )

async def end_chat(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    partner_id = active_chats.get(user_id)

    if user_id in active_chats:
        del active_chats[user_id]
    if partner_id and partner_id in active_chats:
        del active_chats[partner_id]

    await context.bot.send_message(user_id, "❌ گفتگو پایان یافت.", reply_markup=main_menu_keyboard())
    if partner_id:
        await context.bot.send_message(partner_id, "❌ هم‌صحبت شما گفتگو را ترک کرد.", reply_markup=main_menu_keyboard())

# ---------------------------------------------------------
# ۸. اجرای اصلی ربات
# ---------------------------------------------------------
if __name__ == "__main__":
    Base.metadata.create_all(engine)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # مدیریت ثبت نام
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_gender)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_age)],
            PROVINCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_province)],
        },
        fallbacks=[CommandHandler("cancel", cancel_registration)],
    )

    app.add_handler(conv_handler)
    
    # اضافه شدن پشتیبانی از تمام رسانه‌ها (عکس، ویس، فیلم و...)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_all_messages))

    print("🤖 ربات چتوگرام بدون مشکل در حال اجراست...")
    app.run_polling()
