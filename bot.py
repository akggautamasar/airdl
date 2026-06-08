from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    await update.message.reply_text("Downloading...")

app = Application.builder().token("BOT_TOKEN").build()
app.add_handler(MessageHandler(filters.TEXT, handle))
app.run_polling()
