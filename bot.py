import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters
)
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey,
    Integer, String, create_engine
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# ================= CONFIG =================

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
DATABASE_URL = "postgresql://user:password@localhost/anonymous_chat_db"
ADMIN_IDS = {7810107484}

TRIAL_DAYS = 14
DAILY_LIMIT = 15 * 60
INACTIVE_DAYS = 30
BAN_GRACE_DAYS = 2

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ================= DATABASE =================

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    telegram_id = Column(BigInteger, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active_at = Column(DateTime, default=datetime.utcnow)
    language = Column(String(2), default="fa")
    referred_by = Column(BigInteger)
    referral_count = Column(Integer, default=0)
    is_premium_lifetime = Column(Boolean, default=False)
    daily_chat_seconds_used = Column(Integer, default=0)
    last_chat_reset_date = Column(DateTime, default=datetime.utcnow)

    profile = relationship(
        "Profile", back_populates="user",
        uselist=False, cascade="all, delete"
    )
    ban_info = relationship(
        "BanRecord", back_populates="user",
        uselist=False, cascade="all, delete"
    )

    def trial_active(self):
        return datetime.utcnow() < self.created_at + timedelta(days=TRIAL_DAYS)

    def can_chat(self):
        now = datetime.utcnow()

        if self.trial_active() or self.is_premium_lifetime:
            return True

        if now.date() > self.last_chat_reset_date.date():
            self.daily_chat_seconds_used = 0
            self.last_chat_reset_date = now

        return self.daily_chat_seconds_used < DAILY_LIMIT


class Profile(Base):
    __tablename__ = "profiles"

    telegram_id = Column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    name = Column(String(50))
    gender = Column(String(10))
    age = Column(Integer)
    province = Column(String(50))
    city = Column(String(50))
    likes_count = Column(Integer, default=0)

    user = relationship("User", back_populates="profile")


class BanRecord(Base):
    __tablename__ = "bans"

    telegram_id = Column(
        BigInteger, ForeignKey("users.telegram_id"), primary_key=True
    )
    banned_at = Column(DateTime, default=datetime.utcnow)
    unban_referral_count = Column(Integer, default=0)
    is_data_purged = Column(Boolean, default=False)

    user = relationship("User", back_populates="ban_info")


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    reporter_id = Column(BigInteger, nullable=False)
    reported_id = Column(BigInteger, nullable=False)
    reason = Column(String(255), default="محتوای نامناسب / مزاحمت")
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)

# ================= MEMORY =================

waiting_queue = []
active_chats = {}
rate_limits = {}

SPAM_REGEX = re.compile(
    r"(@[a-zA-Z0-9_]{5,32})|"
    r"(https?://\S+)|"
    r"(www\.\S+)|"
    r"(t\.me/\S+)|"
    r"(telegram\.me/\S+)|"
    r"([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
    re.I
)


def spam(text):
    return bool(text and SPAM_REGEX.search(text))


def rate_limited(uid):
    now = time.time()
    rate_limits[uid] = [
        t for t in rate_limits.get(uid, [])
        if now - t < 5
    ]

    if len(rate_limits[uid]) >= 5:
        return True

    rate_limits[uid].append(now)
    return False


# ================= KEYBOARDS =================

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 چت ناشناس", callback_data="random")],
        [
            InlineKeyboardButton("🔍 جستجو", callback_data="search"),
            InlineKeyboardButton("👤 پروفایل", callback_data="profile")
        ],
        [InlineKeyboardButton("🔗 لینک دعوت", callback_data="ref")]
    ])


def chat_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ پایان چت", callback_data="leave"),
        InlineKeyboardButton("🚨 گزارش", callback_data="report")
    ]])


def queue_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 انصراف", callback_data="cancel")
    ]])


def admin_kb(report_id, user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ مسدودسازی",
                callback_data=f"ban:{report_id}:{user_id}"
            ),
            InlineKeyboardButton(
                "🟢 رد گزارش",
                callback_data=f"ignore:{report_id}"
            )
        ],
        [InlineKeyboardButton(
            "➡️ گزارش بعدی",
            callback_data="next_report"
        )]
    ])


# ================= HELPERS =================

def get_user(db, uid):
    return db.query(User).filter_by(telegram_id=uid).first()


async def end_chat(uid, context, notify=True):
    partner = active_chats.pop(uid, None)

    if partner:
        active_chats.pop(partner, None)

        if notify:
            await context.bot.send_message(
                partner, "❌ هم‌صحبت شما چت را ترک کرد."
            )

    if notify:
        await context.bot.send_message(
            uid, "❌ چت پایان یافت.", reply_markup=main_kb()
        )


async def connect(uid, context, message):
    if uid in active_chats:
        return await message.reply_text("⚠️ شما در حال چت هستید.")

    if uid in waiting_queue:
        return await message.reply_text("🔎 شما در صف انتظار هستید.")

    db = SessionLocal()
    user = get_user(db, uid)

    if not user or not user.can_chat():
        db.close()
        return await message.reply_text(
            "⚠️ مهلت چت رایگان امروز شما تمام شده است."
        )

    db.commit()
    db.close()

    if waiting_queue:
        partner = waiting_queue.pop(0)

        if partner == uid:
            waiting_queue.append(uid)
            return

        active_chats[uid] = partner
        active_chats[partner] = uid

        for x in (uid, partner):
            await context.bot.send_message(
                x,
                "🎉 هم‌صحبت پیدا شد! گفتگو را شروع کنید.",
                reply_markup=chat_kb()
            )
    else:
        waiting_queue.append(uid)
        await message.reply_text(
            "🔎 در حال پیدا کردن هم‌صحبت...",
            reply_markup=queue_kb()
        )


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args

    db = SessionLocal()
    user = get_user(db, uid)

    if not user:
        ref = int(args[0]) if args and args[0].isdigit() else None

        user = User(
            telegram_id=uid,
            referred_by=ref
        )
        db.add(user)
        db.commit()

        if ref and ref != uid:
            inviter = get_user(db, ref)

            if inviter:
                ban = db.query(BanRecord).filter_by(
                    telegram_id=ref
                ).first()

                if ban:
                    ban.unban_referral_count += 1

                    if ban.unban_referral_count >= 3:
                        db.delete(ban)
                else:
                    inviter.referral_count += 1

                    if inviter.referral_count >= 4:
                        inviter.is_premium_lifetime = True

                db.commit()

    user.last_active_at = datetime.utcnow()
    db.commit()

    ban = db.query(BanRecord).filter_by(
        telegram_id=uid
    ).first()

    if ban:
        link = f"https://t.me/{context.bot.username}?start={uid}"

        await update.message.reply_text(
            f"❌ حساب شما مسدود است.\n\n"
            f"برای رفع مسدودی ۳ نفر دعوت کنید.\n"
            f"دعوت‌ها: {ban.unban_referral_count}/3\n\n{link}"
        )
        db.close()
        return

    await update.message.reply_text(
        "👋 به ربات چت ناشناس خوش آمدید!",
        reply_markup=main_kb()
    )

    db.close()


# ================= CALLBACKS =================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    data = q.data

    db = SessionLocal()
    user = get_user(db, uid)

    if user:
        user.last_active_at = datetime.utcnow()
        db.commit()

    if data == "random":
        db.close()
        return await connect(uid, context, q.message)

    if data == "cancel":
        if uid in waiting_queue:
            waiting_queue.remove(uid)
            await q.message.edit_text("❌ از صف خارج شدید.")
        db.close()
        return

    if data == "leave":
        db.close()
        return await end_chat(uid, context)

    if data == "report":
        partner = active_chats.get(uid)

        if partner:
            db.add(Report(
                reporter_id=uid,
                reported_id=partner,
                reason="گزارش مستقیم از چت"
            ))
            db.commit()

            await q.message.reply_text("🚨 گزارش ثبت شد.")
            await end_chat(uid, context)

        db.close()
        return

    if data == "ref":
        link = f"https://t.me/{context.bot.username}?start={uid}"

        await q.message.reply_text(
            f"🔗 لینک دعوت شما:\n{link}\n\n"
            f"تعداد دعوت‌ها: {user.referral_count}/4\n"
            "با دعوت ۴ نفر، دسترسی همیشگی می‌گیرید."
        )

    elif data == "profile":
        p = user.profile

        if p:
            await q.message.reply_text(
                f"👤 پروفایل\n\n"
                f"نام: {p.name}\n"
                f"سن: {p.age}\n"
                f"جنسیت: {p.gender}\n"
                f"استان: {p.province}\n"
                f"شهر: {p.city}"
            )
        else:
            await q.message.reply_text("⚠️ هنوز پروفایلی ثبت نکرده‌اید.")

    elif data == "search":
        await q.message.reply_text(
            "🔍 بخش جستجوی کاربران هنوز تکمیل نشده است."
        )

    db.close()


# ================= MESSAGE RELAY =================

async def relay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid not in active_chats:
        return

    msg = update.message

    if rate_limited(uid):
        return await msg.reply_text("🛑 سرعت ارسال پیام زیاد است.")

    if msg.forward_date or msg.forward_from_chat:
        return await msg.reply_text(
            "⚠️ ارسال پیام فورواردشده مجاز نیست."
        )

    text = msg.text or msg.caption or ""

    if spam(text):
        return await msg.reply_text(
            "⚠️ ارسال لینک، آیدی و تبلیغات ممنوع است."
        )

    partner = active_chats.get(uid)

    try:
        await msg.copy(chat_id=partner)
    except Exception:
        await msg.reply_text("⚠️ ارسال پیام ناموفق بود.")
        await end_chat(uid, context)


# ================= ADMIN =================

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    db = SessionLocal()

    count = db.query(Report).filter_by(status="pending").count()

    db.close()

    await update.message.reply_text(
        f"⚙️ پنل مدیریت\n\nگزارش‌های جدید: {count}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"📥 بررسی گزارش‌ها ({count})",
                callback_data="next_report"
            )
        ]])
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.from_user.id not in ADMIN_IDS:
        return

    data = q.data
    db = SessionLocal()

    if data == "next_report":
        report = (
            db.query(Report)
            .filter_by(status="pending")
            .order_by(Report.created_at.asc())
            .first()
        )

        if not report:
            await q.message.edit_text("✅ گزارش جدیدی وجود ندارد.")
            db.close()
            return

        profile = db.query(Profile).filter_by(
            telegram_id=report.reported_id
        ).first()

        await q.message.edit_text(
            f"🚨 گزارش #{report.id}\n\n"
            f"👤 کاربر: `{report.reported_id}`\n"
            f"نام: {profile.name if profile else '-'}\n"
            f"دلیل: {report.reason}",
            parse_mode="Markdown",
            reply_markup=admin_kb(report.id, report.reported_id)
        )

    elif data.startswith("ban:"):
        _, report_id, uid = data.split(":")
        report_id, uid = int(report_id), int(uid)

        report = db.query(Report).filter_by(id=report_id).first()

        if report:
            report.status = "approved"

            if not db.query(BanRecord).filter_by(
                telegram_id=uid
            ).first():
                db.add(BanRecord(telegram_id=uid))

            db.commit()

            link = f"https://t.me/{context.bot.username}?start={uid}"

            try:
                await context.bot.send_message(
                    uid,
                    "❌ حساب شما مسدود شد.\n\n"
                    f"برای رفع مسدودی ۳ نفر دعوت کنید:\n{link}"
                )
            except Exception:
                pass

            await q.message.reply_text(
                f"⛔ کاربر {uid} مسدود شد."
            )

    elif data.startswith("ignore:"):
        report_id = int(data.split(":")[1])

        report = db.query(Report).filter_by(id=report_id).first()

        if report:
            report.status = "rejected"
            db.commit()

            await q.message.reply_text("🟢 گزارش رد شد.")

    db.close()


# ================= PURGER =================

async def purge_task(app):
    while True:
        try:
            db = SessionLocal()
            now = datetime.utcnow()

            # پایان مهلت ۲ روزه مسدودی
            expired = db.query(BanRecord).filter(
                BanRecord.banned_at <= now - timedelta(days=BAN_GRACE_DAYS),
                BanRecord.is_data_purged == False
            ).all()

            for ban in expired:
                db.query(Profile).filter_by(
                    telegram_id=ban.telegram_id
                ).delete()
                ban.is_data_purged = True

            # حذف پروفایل کاربران غیرفعال
            inactive = db.query(User).filter(
                User.last_active_at <=
                now - timedelta(days=INACTIVE_DAYS)
            ).all()

            for user in inactive:
                db.query(Profile).filter_by(
                    telegram_id=user.telegram_id
                ).delete()

            db.commit()
            db.close()

        except Exception:
            logging.exception("Purge error")

        await asyncio.sleep(3600)


# ================= MAIN =================

async def post_init(app):
    app.create_task(purge_task(app))


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^(next_report|ban:|ignore:)"
        )
    )

    app.add_handler(
        CallbackQueryHandler(callbacks)
    )

    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            relay
        )
    )

    print("🤖 Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
