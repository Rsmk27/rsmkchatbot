import os
import logging
from flask import Flask, request
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app for webhook
app = Flask(__name__)

# Load tokens from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Create Telegram app
telegram_app = Application.builder().token(BOT_TOKEN).build()

# Command: /start
async def start(update: Update, context):
    await update.message.reply_text("Hello! I’m your AI-powered chatbot 🤖")

# Message handler (AI response)
async def chat(update: Update, context):
    user_message = update.message.text

    # Use Gemini to generate response
    model = genai.GenerativeModel("gemini-pro")
    response = model.generate_content(user_message)

    reply_text = response.text if response and response.text else "Sorry, I couldn’t understand that."
    await update.message.reply_text(reply_text)

# Add handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

# Flask route for Telegram webhook
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Process incoming Telegram updates"""
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put_nowait(update)
    return "ok", 200

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
