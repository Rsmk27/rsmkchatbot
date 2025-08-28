"""
app.py

Flask webhook for Telegram that forwards user messages to Gemini (Google GenAI SDK)
and returns model responses back to the user. Stores per-user conversation history
in a lightweight SQLite DB so chat context persists across restarts.

Ready for deployment to Render (or any container / WSGI host).
"""

import os
import sqlite3
import logging
from typing import List, Tuple
from flask import Flask, request, jsonify, abort
import requests
import google.generativeai as genai
import time

# -----------------------
# Configuration (env vars)
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # BotFather token
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # Google Gen AI SDK key
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")  # model override
SQLITE_PATH = os.getenv("SQLITE_PATH", "chat_history.db")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT",
                          "You are a friendly, professional and empathetic assistant that replies "
                          "like a human. Keep answers concise, helpful, and polite. Use natural language.")
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "12"))  # number of past utterances to store (each user)

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Please set TELEGRAM_TOKEN and GEMINI_API_KEY environment variables.")

# -----------------------
# Initialize
# -----------------------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------
# SQLite helpers
# -----------------------
def init_db():
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL, -- 'user' or 'assistant'
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    """)
    conn.commit()
    conn.close()

def add_message(user_id: int, role: str, text: str):
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, text, int(time.time())))
    conn.commit()
    conn.close()

def get_history(user_id: int, limit: int = MAX_HISTORY_MESSAGES) -> List[Tuple[str, str]]:
    """
    Returns a list of (role, text) ordered oldest->newest for a given user.
    """
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT role, text FROM messages
        WHERE user_id = ?
        ORDER BY created_at ASC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def clear_history(user_id: int):
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# Initialize DB on start
init_db()

# -----------------------
# Gemini / Chat helpers
# -----------------------
def build_history_for_gemini(user_id: int):
    """
    Convert stored history to the simple list of dicts accepted by model.start_chat(history=...).
    Format used: {"role": "user"/"assistant"/"system", "content": "<text>"}
    """
    history = [{"role": "system", "content": SYSTEM_PROMPT}]
    rows = get_history(user_id, limit=MAX_HISTORY_MESSAGES)
    # rows are in oldest->newest order
    for role, text in rows:
        # Gemini examples use roles 'user' and 'assistant'
        history.append({"role": role, "content": text})
    return history

def ask_gemini(user_id: int, user_text: str) -> str:
    """
    Create a chat session with limited history and get Gemini reply.
    We rebuild chat using stored history so it persists across restarts.
    """
    history = build_history_for_gemini(user_id)
    # Start a chat with the history
    try:
        chat = model.start_chat(history=history)
        # send the new user message
        response = chat.send_message(user_text)
        # response may have .text attribute
        reply_text = getattr(response, "text", None)
        if reply_text is None:
            # fallback: try to stringify
            reply_text = str(response)
        return reply_text.strip()
    except Exception as e:
        logging.exception("Gemini API error")
        return "Sorry — I'm having trouble talking to my brain (API). Please try again shortly."

# -----------------------
# Telegram helpers
# -----------------------
def send_telegram_message(chat_id: int, text: str, reply_to_message_id: int = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    resp = requests.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload, timeout=15)
    if not resp.ok:
        logging.error("Telegram sendMessage failed: %s %s", resp.status_code, resp.text)
    return resp

# -----------------------
# Webhook endpoint
# -----------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    # Telegram will POST updates here
    if request.headers.get("content-type") != "application/json":
        abort(400)
    update = request.get_json()
    if not update:
        return jsonify({"ok": False}), 400

    # Basic update parsing; handle messages only
    message = update.get("message") or update.get("edited_message")
    if not message:
        # We don't process non-message updates in this simple example
        return jsonify({"ok": True})

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user = message.get("from", {})
    user_id = user.get("id")
    text = message.get("text", "")
    message_id = message.get("message_id")

    if text is None:
        # ignore non-text messages
        send_telegram_message(chat_id, "I only understand text for now.")
        return jsonify({"ok": True})

    text = text.strip()

    # Commands handling
    if text.startswith("/reset"):
        clear_history(user_id)
        send_telegram_message(chat_id, "Conversation history cleared. Let's start fresh!", reply_to_message_id=message_id)
        return jsonify({"ok": True})

    if text.startswith("/help"):
        help_text = ("I am an AI chat bot. Just type anything and I'll reply like a human.\n\n"
                     "Commands:\n"
                     "/help - show this message\n"
                     "/reset - clear our conversation history\n")
        send_telegram_message(chat_id, help_text, reply_to_message_id=message_id)
        return jsonify({"ok": True})

    # Add user message to history
    add_message(user_id, "user", text)

    # Query Gemini
    reply = ask_gemini(user_id, text)

    # Save assistant reply to DB
    add_message(user_id, "assistant", reply)

    # Send reply back to Telegram
    send_telegram_message(chat_id, reply, reply_to_message_id=message_id)
    return jsonify({"ok": True})

# Simple healthcheck
@app.route("/health", methods=["GET"])
def health():
    return "OK"

# -----------------------
# Entrypoint for local debug
# -----------------------
if __name__ == "__main__":
    # For local testing only (use Gunicorn on Render)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
