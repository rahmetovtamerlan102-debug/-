import os
import telebot
import sqlite3
import random
import string
import time
import logging
from datetime import date
from flask import Flask, request

# ================= TOKEN =================
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise Exception("BOT_TOKEN not found")

bot = telebot.TeleBot(TOKEN)

# ================= FLASK =================
app = Flask(__name__)

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= DATABASE =================
conn = sqlite3.connect("auron.db", check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    premium_until INTEGER DEFAULT 0,
    searches_today INTEGER DEFAULT 0,
    last_date TEXT
)
""")
conn.commit()

# ================= USER SYSTEM =================
FREE_LIMIT = 10

def get_user(uid):
    c.execute("SELECT premium_until, searches_today, last_date FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()

    if not row:
        c.execute("INSERT INTO users (user_id, last_date) VALUES (?, ?)", (uid, str(date.today())))
        conn.commit()
        return False, 0

    premium_until, searches, last = row

    is_premium = premium_until > int(time.time())
    today = str(date.today())

    if last != today:
        searches = 0
        c.execute("UPDATE users SET searches_today=0, last_date=? WHERE user_id=?", (today, uid))
        conn.commit()

    return is_premium, searches


def can_search(uid):
    is_premium, searches = get_user(uid)
    if is_premium:
        return True
    return searches < FREE_LIMIT


def increment_search(uid):
    is_premium, searches = get_user(uid)
    if not is_premium:
        c.execute("UPDATE users SET searches_today=? WHERE user_id=?", (searches + 1, uid))
        conn.commit()

# ================= GENERATOR =================
def gen_username(length):
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(length))

# ================= HANDLERS =================
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "🚀 Бот работает на Webhook!\n\nНажми /search"
    )

@bot.message_handler(commands=["search"])
def search(message):
    uid = message.from_user.id

    if not can_search(uid):
        bot.send_message(message.chat.id, "❌ Лимит исчерпан")
        return

    username = gen_username(6)
    increment_search(uid)

    bot.send_message(message.chat.id, f"✅ Найдено:\n\n`@{username}`", parse_mode="Markdown")

@bot.message_handler(commands=["ping"])
def ping(message):
    bot.send_message(message.chat.id, "🏓 pong")

# ================= WEBHOOK ROUTE =================
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

# ================= SET WEBHOOK =================
@app.route("/")
def index():
    bot.remove_webhook()

    # ВАЖНО: сюда вставь URL Render
    bot.set_webhook(url=f"https://YOUR-RENDER-URL.onrender.com/{TOKEN}")

    return "Webhook set", 200

# ================= RUN =================
if __name__ == "__main__":
    logger.info("🚀 Bot running (webhook mode)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
