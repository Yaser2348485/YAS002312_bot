# -*- coding: utf-8 -*-
"""
ربات تلگرام جستجوگر رایگان (بدون نیاز به API پولی)
----------------------------------------------------
این ربات هر پیامی که کاربر بفرسته رو با DuckDuckGo جستجو می‌کنه
و نتایج (عنوان + خلاصه + لینک) رو برمی‌گردونه.
کاملاً رایگانه، فقط به توکن تلگرام نیاز داره.

نصب پیش‌نیازها:
    pip install python-telegram-bot duckduckgo-search --upgrade

قبل از اجرا، توکن زیر رو ست کن:
    TELEGRAM_BOT_TOKEN -> از @BotFather می‌گیری

اجرا:
    python bot.py
"""

import logging
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from duckduckgo_search import DDGS

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "توکن-ربات-تلگرام-اینجا")
MAX_RESULTS = 5

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def search_web(query: str) -> str:
    """جستجو در DuckDuckGo و ساخت متن پاسخ از نتایج."""
    try:
        results = list(DDGS().text(query, region="ir-fa", max_results=MAX_RESULTS))
    except Exception:
        logger.exception("خطا در جستجو")
        return "متاسفانه جستجو با خطا مواجه شد. چند لحظه دیگه دوباره امتحان کن."

    if not results:
        return "چیزی پیدا نشد. لطفاً سوالت رو واضح‌تر یا با کلمات دیگه بپرس."

    lines = [f"🔎 نتایج جستجو برای: {query}\n"]
    for i, r in enumerate(results, start=1):
        title = r.get("title", "بدون عنوان")
        body = r.get("body", "")
        url = r.get("href", "")
        if len(body) > 200:
            body = body[:200] + "..."
        lines.append(f"{i}. {title}\n{body}\n{url}\n")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! هر چی بخوای رو برات تو اینترنت می‌گردم. فقط بنویس و بفرست."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    await update.message.chat.send_action(action="typing")

    answer = search_web(user_text)

    # تلگرام محدودیت طول پیام داره (۴۰۹۶ کاراکتر)
    if len(answer) > 4000:
        answer = answer[:4000] + "..."

    await update.message.reply_text(answer, disable_web_page_preview=True)


def main():
    if "توکن" in TELEGRAM_BOT_TOKEN:
        print(
            "⚠️ لطفاً اول TELEGRAM_BOT_TOKEN رو تو فایل یا به‌عنوان "
            "environment variable تنظیم کن."
        )
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("ربات روشن شد و منتظر پیام‌هاست...")
    app.run_polling()


if __name__ == "__main__":
    main()
