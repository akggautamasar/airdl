from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, CommandHandler, ContextTypes, filters
import requests
import re
import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = "https://yt--air--78q4gfqgnf5j.code.run"

user_links = {}

def extract_video_id(url):
    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"v=([A-Za-z0-9_-]{11})"
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 AirTube Bot\n\nSend any YouTube link and choose a format."
    )

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "youtube.com" not in text and "youtu.be" not in text:
        await update.message.reply_text("Please send a valid YouTube link.")
        return

    user_links[update.effective_user.id] = text

    keyboard = [
        [InlineKeyboardButton("🎵 MP3", callback_data="audio")],
        [InlineKeyboardButton("🎥 720p", callback_data="720")],
        [InlineKeyboardButton("🎥 1080p", callback_data="1080")],
        [InlineKeyboardButton("🎥 4K", callback_data="2160")]
    ]

    await update.message.reply_text(
        "Choose download format:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id

    if uid not in user_links:
        await query.message.reply_text("Please send a YouTube link first.")
        return

    url = user_links[uid]
    video_id = extract_video_id(url)

    if not video_id:
        await query.message.reply_text("Unable to extract video ID.")
        return

    mode = "audio" if query.data == "audio" else "video"

    payload = {
        "id": video_id,
        "mode": mode,
        "telegram": True
    }

    if mode == "video":
        payload["height"] = int(query.data)

    try:
        response = requests.post(
            API_URL + "/download",
            json=payload,
            timeout=30
        )

        data = response.json()

        if data.get("status") != "success":
            await query.message.reply_text("Download request failed.")
            return

        await query.message.reply_text(
            f"✅ Download started\n\nJob ID: {data['job']}\n\nThe file will arrive automatically when ready."
        )

    except Exception as e:
        await query.message.reply_text(f"Error:\n{e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    app.add_handler(
        CallbackQueryHandler(button_click)
    )

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
