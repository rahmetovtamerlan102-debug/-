#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import telebot
import sqlite3
import random
import string
import time
import logging
from datetime import date, datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery

# ================= КОНФИГУРАЦИЯ =================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise Exception("❌ BOT_TOKEN не найден в переменных окружения Render")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8276815852"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@Auron_search")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Auronsearchesbot")
CHECK_SUBSCRIPTION = os.getenv("CHECK_SUBSCRIPTION", "True").lower() == "true"

USDT_WALLET = os.getenv("USDT_WALLET", "#IV50644032")

USDT_PRICES = {"1d":0.6, "3d":1.6, "7d":2.8, "30d":6.0, "365d":24.0, "forever":40.0}
USDT_DAYS = {"1d":1, "3d":3, "7d":7, "30d":30, "365d":365, "forever":0}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)

# ================= БАЗА ДАННЫХ =================
# Для Render используем файловую БД с абсолютным путём
DB_PATH = "/tmp/auron.db"  # /tmp сохраняется между рестартами, но не постоянно
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
c = conn.cursor()

# Таблица пользователей
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    premium_until INTEGER DEFAULT 0,
    searches_today INTEGER DEFAULT 0,
    last_date TEXT,
    referrer_id INTEGER DEFAULT 0,
    referals_count INTEGER DEFAULT 0
)
""")

# Таблица сохранённых юзернеймов
c.execute("""
CREATE TABLE IF NOT EXISTS saved_usernames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    created_at INTEGER
)
""")

# Таблица заявок USDT
c.execute("""
CREATE TABLE IF NOT EXISTS pending_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    tariff TEXT,
    amount REAL,
    days INTEGER,
    created_at INTEGER,
    status TEXT DEFAULT 'waiting'
)
""")

# Таблица сообщений поддержки
c.execute("""
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    message TEXT,
    created_at INTEGER,
    is_read INTEGER DEFAULT 0
)
""")

conn.commit()
logger.info(f"База данных инициализирована. Путь: {DB_PATH}")

# ================= ЭКРАНИРОВАНИЕ MARKDOWN =================
def escape_md(text):
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in special_chars else c for c in text)

# ================= ПРОВЕРКА ПОДПИСКИ =================
def is_subscribed(user_id):
    if not CHECK_SUBSCRIPTION:
        return True
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning(f"Ошибка проверки подписки: {e}")
        return False

def subscription_required(func):
    def wrapper(message):
        uid = message.from_user.id
        if not is_subscribed(uid):
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
            safe_channel = escape_md(CHANNEL_USERNAME)
            bot.send_message(message.chat.id, f"⚠️ *Подпишитесь на канал:* {safe_channel}", reply_markup=markup, parse_mode="Markdown")
            return
        return func(message)
    return wrapper

def subscription_required_callback(func):
    def wrapper(call):
        uid = call.from_user.id
        if not is_subscribed(uid):
            bot.answer_callback_query(call.id, "❌ Подпишитесь на канал!", show_alert=True)
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
            safe_channel = escape_md(CHANNEL_USERNAME)
            try:
                bot.send_message(call.message.chat.id, f"⚠️ *Подпишитесь на канал:* {safe_channel}", reply_markup=markup, parse_mode="Markdown")
            except:
                pass
            return
        return func(call)
    return wrapper

# ================= ПОЛЬЗОВАТЕЛЬСКИЕ ФУНКЦИИ =================
def get_user(uid):
    c.execute("SELECT premium_until, searches_today, last_date, referals_count FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, last_date, referals_count, premium_until) VALUES (?, ?, 0, 0)",
                  (uid, str(date.today())))
        conn.commit()
        return False, 0, str(date.today()), 0, 0
    premium_until, searches, last, ref_count = row
    is_premium = premium_until > int(time.time())
    today = str(date.today())
    if last != today:
        searches = 0
        last = today
        c.execute("UPDATE users SET searches_today=?, last_date=? WHERE user_id=?", (searches, last, uid))
        conn.commit()
    return is_premium, searches, last, premium_until, ref_count

def get_max_searches(uid):
    _, _, _, _, ref_count = get_user(uid)
    return FREE_LIMIT + ref_count

def can_search(uid):
    is_premium, searches, _, _, _ = get_user(uid)
    if is_premium:
        return True
    return searches < get_max_searches(uid)

def increment_search(uid):
    is_premium, searches, _, _, _ = get_user(uid)
    if not is_premium:
        c.execute("UPDATE users SET searches_today=? WHERE user_id=?", (searches + 1, uid))
        conn.commit()

def get_remaining(uid):
    is_premium, searches, _, _, _ = get_user(uid)
    if is_premium:
        return float('inf')
    return get_max_searches(uid) - searches

def set_premium(uid, days):
    now = int(time.time())
    new_until = 2147483647 if days == 0 else now + days * 86400
    c.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if row:
        if new_until > row[0]:
            c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new_until, uid))
    else:
        c.execute("INSERT INTO users (user_id, premium_until) VALUES (?, ?)", (uid, new_until))
    conn.commit()

def add_referral(new_uid, referrer_id):
    if referrer_id == 0 or referrer_id == new_uid:
        return False
    c.execute("SELECT referrer_id FROM users WHERE user_id=?", (new_uid,))
    row = c.fetchone()
    if row and row[0] == 0:
        c.execute("UPDATE users SET referrer_id=? WHERE user_id=?", (referrer_id, new_uid))
        c.execute("UPDATE users SET referals_count = referals_count + 1 WHERE user_id=?", (referrer_id,))
        conn.commit()
        logger.info(f"Реферал {new_uid} привязан к {referrer_id}")
        return True
    return False

def get_referral_link(uid):
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

def save_username(uid, username):
    c.execute("INSERT INTO saved_usernames (user_id, username, created_at) VALUES (?, ?, ?)",
              (uid, username, int(time.time())))
    conn.commit()

def get_saved_usernames(uid):
    c.execute("SELECT username, created_at FROM saved_usernames WHERE user_id=? ORDER BY created_at DESC", (uid,))
    return c.fetchall()

def save_support_message(user_id, text):
    c.execute("INSERT INTO support_messages (user_id, message, created_at, is_read) VALUES (?, ?, ?, 0)",
              (user_id, text, int(time.time())))
    conn.commit()

def get_unread_support_messages():
    c.execute("SELECT id, user_id, message, created_at FROM support_messages WHERE is_read=0 ORDER BY created_at DESC")
    return c.fetchall()

def mark_support_read(msg_id):
    c.execute("UPDATE support_messages SET is_read=1 WHERE id=?", (msg_id,))
    conn.commit()

# ================= ГЕНЕРАТОРЫ =================
def gen_letters(length):
    return ''.join(random.choice(string.ascii_lowercase) for _ in range(length))

def gen_mixed(length):
    letters = string.ascii_lowercase
    digits = string.digits
    pos = random.randint(0, length - 1)
    result = []
    for i in range(length):
        result.append(random.choice(digits) if i == pos else random.choice(letters))
    return ''.join(result)

# ================= КЛАВИАТУРЫ =================
def start_keyboard(uid):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔍 Найти юзернейм", "💎 Купить Premium")
    kb.row("👤 Личный кабинет", "🆘 Поддержка")
    if uid == ADMIN_ID:
        kb.row("🔧 Админ панель")
    return kb

def admin_main_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📩 Сообщения поддержки", callback_data="admin_support_messages"),
        InlineKeyboardButton("💸 Подтвердить USDT", callback_data="admin_usdt_list")
    )
    return kb

def length_keyboard(uid):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("5 (Premium)", callback_data="len_5"),
        InlineKeyboardButton("6", callback_data="len_6"),
        InlineKeyboardButton("7", callback_data="len_7")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_to_start"))
    return kb

def mode_keyboard(length):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🔤 Без цифр", callback_data=f"mode_{length}_letters"),
        InlineKeyboardButton("🔢 С цифрой", callback_data=f"mode_{length}_mixed")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_to_length"))
    return kb

def result_keyboard_56(length, mode, username):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
        InlineKeyboardButton("🔄 Ещё", callback_data=f"mode_{length}_{mode}")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data=f"back_to_mode_{length}"))
    return kb

def result_keyboard_7(username):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("💾 Сохранить", callback_data=f"save_{username}"),
        InlineKeyboardButton("🔄 Ещё", callback_data="gen_7")
    )
    kb.row(InlineKeyboardButton("◀ Назад", callback_data="back_to_length"))
    return kb

def payment_method_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("⭐ Оплата Stars", callback_data="pay_method_stars"),
        InlineKeyboardButton("🪙 Оплата USDT", callback_data="pay_method_usdt")
    )
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_to_start"))
    return kb

def stars_tariffs_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("1 день – 30 ★", callback_data="stars_1_30"),
        InlineKeyboardButton("3 дня – 80 ★", callback_data="stars_3_80"),
        InlineKeyboardButton("7 дней – 140 ★", callback_data="stars_7_140"),
        InlineKeyboardButton("30 дней – 300 ★", callback_data="stars_30_300"),
        InlineKeyboardButton("365 дней – 1200 ★", callback_data="stars_365_1200"),
        InlineKeyboardButton("Навсегда – 2000 ★", callback_data="stars_0_2000")
    )
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_to_payment_method"))
    return kb

def usdt_tariffs_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"1 день – {USDT_PRICES['1d']} USDT", callback_data="usdt_1d"),
        InlineKeyboardButton(f"3 дня – {USDT_PRICES['3d']} USDT", callback_data="usdt_3d"),
        InlineKeyboardButton(f"7 дней – {USDT_PRICES['7d']} USDT", callback_data="usdt_7d"),
        InlineKeyboardButton(f"30 дней – {USDT_PRICES['30d']} USDT", callback_data="usdt_30d"),
        InlineKeyboardButton(f"365 дней – {USDT_PRICES['365d']} USDT", callback_data="usdt_365d"),
        InlineKeyboardButton(f"Навсегда – {USDT_PRICES['forever']} USDT", callback_data="usdt_forever")
    )
    kb.add(InlineKeyboardButton("◀ Назад", callback_data="back_to_payment_method"))
    return kb

def profile_text(uid):
    is_premium, searches, _, premium_until, ref_count = get_user(uid)
    remaining = get_remaining(uid)
    remaining_str = f"{remaining}" if remaining != float('inf') else "∞ (Premium)"
    status = "🔷 Обычный" if not is_premium else "👑 Premium"
    if is_premium and premium_until != 2147483647:
        expiry_str = f"до {datetime.fromtimestamp(premium_until).strftime('%d.%m.%Y')}"
    elif is_premium:
        expiry_str = "Навсегда"
    else:
        expiry_str = "Не активен"
    link = get_referral_link(uid)
    return (
        f"╔ 🔷 Статус: {status}\n"
        f"╠ 🔷 Попытки сегодня: {remaining_str}\n"
        f"╠ 🔷 Приглашено друзей: {ref_count}\n"
        f"╠ 🔷 Реф. ссылка: {link}\n"
        f"╠ 🔷 Premium: {expiry_str}\n"
        f"╚ 🔷 Сохранённые юзернеймы: /my_usernames\n\n"
        f"💡 Попытки восстанавливаются автоматически (+{FREE_LIMIT} шт. каждые 24 часа) + {ref_count} бонусных за рефералов."
    )

# ================= ХЕНДЛЕРЫ =================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if len(message.text.split()) > 1:
        arg = message.text.split()[1]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.split("_")[1])
                add_referral(message.from_user.id, referrer_id)
            except:
                pass
    bot.send_message(
        message.chat.id,
        "🔷 *Приветствуем в Auron Search!*\n\n"
        "Наш бот — это профессиональный инструмент для поиска свободных юзернеймов в Telegram.\n\n"
        "Выберите нужное действие в меню ниже 💙",
        reply_markup=start_keyboard(message.from_user.id),
        parse_mode="Markdown"
    )

@bot.message_handler(commands=["my_usernames"])
@subscription_required
def show_saved_usernames(message):
    uid = message.from_user.id
    saved = get_saved_usernames(uid)
    if not saved:
        bot.send_message(message.chat.id, "📭 У вас пока нет сохранённых юзернеймов.")
        return
    text = "*💾 Ваши сохранённые юзернеймы:*\n\n"
    for username, ts in saved[:20]:
        text += f"• `@{username}` — {datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M')}\n"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "🔍 Найти юзернейм")
@subscription_required
def menu_length(message):
    uid = message.from_user.id
    remaining = get_remaining(uid)
    is_premium, _, _, _, _ = get_user(uid)
    if is_premium:
        text = "Выберите длину юзернейм:"
    else:
        text = f"🎫 У вас осталось *{remaining}* из {FREE_LIMIT} попыток сегодня.\n\nВыберите длину юзернейм:"
    bot.send_message(message.chat.id, text, reply_markup=length_keyboard(uid), parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "💎 Купить Premium")
@subscription_required
def buy_premium_start(message):
    bot.send_message(message.chat.id, "🌟 *Выберите способ оплаты Premium:*", reply_markup=payment_method_keyboard(), parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "👤 Личный кабинет")
@subscription_required
def profile_command(message):
    bot.send_message(message.chat.id, profile_text(message.from_user.id), parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "🆘 Поддержка")
@subscription_required
def support_command(message):
    bot.send_message(
        message.chat.id,
        "📝 *Как обратиться в поддержку?*\n\n"
        "Просто напишите ваше сообщение ниже.\n"
        "Я перешлю его администратору.\n\n"
        "📌 *Пример:*\n"
        "• У меня проблема с оплатой\n"
        "• Не пришли бонусы за реферала\n"
        "• Вопрос по работе бота",
        parse_mode="Markdown"
    )
    waiting_for_support[message.from_user.id] = True

@bot.message_handler(func=lambda m: m.text == "🔧 Админ панель")
def admin_button(message):
    if message.from_user.id != ADMIN_ID:
        return
    bot.send_message(message.chat.id, "🔧 *Админ панель*", reply_markup=admin_main_keyboard(), parse_mode="Markdown")

waiting_for_support = {}

@bot.message_handler(func=lambda m: waiting_for_support.get(m.from_user.id, False))
def forward_to_admin(message):
    uid = message.from_user.id
    save_support_message(uid, message.text)
    admin_text = (
        f"🆘 *Новое сообщение в поддержку!*\n\n"
        f"👤 Пользователь: [{message.from_user.first_name}](tg://user?id={uid})\n"
        f"🆔 ID: `{uid}`\n"
        f"💬 Сообщение:\n{message.text}"
    )
    bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
    bot.send_message(
        message.chat.id,
        "✅ *Ваше сообщение отправлено администратору!*\n\nОтвет придет сюда, ожидайте.",
        parse_mode="Markdown"
    )
    waiting_for_support[uid] = False

@bot.message_handler(commands=["reply"])
def reply_to_user(message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split(maxsplit=2)
        user_id = int(parts[1])
        reply_text = parts[2]
        bot.send_message(user_id, f"💬 *Ответ поддержки:*\n\n{reply_text}", parse_mode="Markdown")
        bot.reply_to(message, f"✅ Ответ отправлен пользователю {user_id}")
    except:
        bot.reply_to(message, "❌ Ошибка. Использование: /reply <user_id> <текст>")

# ================= CALLBACK-ЗАПРОСЫ =================
@bot.callback_query_handler(func=lambda call: True)
@subscription_required_callback
def handle_callback(call):
    try:
        uid = call.from_user.id
        data = call.data

        # Сохранение юзернейма
        if data.startswith("save_"):
            username = data[5:]
            save_username(uid, username)
            bot.answer_callback_query(call.id, f"✅ @{username} сохранён!")
            return

        # Навигация
        if data == "back_to_start":
            bot.delete_message(call.message.chat.id, call.message.message_id)
            cmd_start(call.message)
            bot.answer_callback_query(call.id)
            return

        if data == "back_to_length":
            remaining = get_remaining(uid)
            is_premium, _, _, _, _ = get_user(uid)
            if is_premium:
                text = "Выберите длину юзернейм:"
            else:
                text = f"🎫 У вас осталось *{remaining}* попыток сегодня.\nДлина *5* доступна только Premium.\n\nВыберите длину юзернейм:"
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=length_keyboard(uid), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data.startswith("back_to_mode_"):
            length = int(data.split("_")[3])
            bot.edit_message_text(f"Выбрана длина: {length}\nВыберите режим:", call.message.chat.id, call.message.message_id, reply_markup=mode_keyboard(length))
            bot.answer_callback_query(call.id)
            return

        # Выбор длины
        if data.startswith("len_"):
            length = int(data.split("_")[1])
            is_premium, _, _, _, _ = get_user(uid)
            if length == 5 and not is_premium:
                bot.answer_callback_query(call.id)
                bot.send_message(call.message.chat.id, "❌ *Длина 5 букв доступна только Premium!*\n\n💎 Купите Premium, чтобы получить доступ к 5-буквенным юзернеймам.", parse_mode="Markdown")
                return
            if length == 7:
                if not can_search(uid):
                    remaining = get_remaining(uid)
                    bot.answer_callback_query(call.id, f"❌ Лимит исчерпан! Осталось: {remaining}. Завтра обнулится.", show_alert=True)
                    return
                username = gen_letters(7)
                increment_search(uid)
                remaining_after = get_remaining(uid)
                is_premium_flag, _, _, _, _ = get_user(uid)
                extra = "" if is_premium_flag else f"\n\n🎫 Осталось попыток: {remaining_after}"
                bot.edit_message_text(f"✅ *Найден юзернейм:*\n\n`@{username}`{extra}", call.message.chat.id, call.message.message_id, reply_markup=result_keyboard_7(username), parse_mode="Markdown")
                bot.answer_callback_query(call.id)
                return
            bot.edit_message_text(f"Выбрана длина: {length}\nТеперь выберите режим:", call.message.chat.id, call.message.message_id, reply_markup=mode_keyboard(length))
            bot.answer_callback_query(call.id)
            return

        # Режимы генерации
        if data.startswith("mode_"):
            parts = data.split("_")
            length = int(parts[1])
            mode = parts[2]
            if not can_search(uid):
                remaining = get_remaining(uid)
                bot.answer_callback_query(call.id, f"❌ Лимит исчерпан! Осталось: {remaining}. Завтра обнулится.", show_alert=True)
                return
            username = gen_letters(length) if mode == "letters" else gen_mixed(length)
            increment_search(uid)
            remaining_after = get_remaining(uid)
            is_premium_flag, _, _, _, _ = get_user(uid)
            extra = "" if is_premium_flag else f"\n\n🎫 Осталось попыток: {remaining_after}"
            bot.edit_message_text(f"✅ *Найден юзернейм:*\n\n`@{username}`{extra}", call.message.chat.id, call.message.message_id, reply_markup=result_keyboard_56(length, mode, username), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "gen_7":
            if not can_search(uid):
                remaining = get_remaining(uid)
                bot.answer_callback_query(call.id, f"❌ Лимит исчерпан! Осталось: {remaining}. Завтра обнулится.", show_alert=True)
                return
            username = gen_letters(7)
            increment_search(uid)
            remaining_after = get_remaining(uid)
            is_premium_flag, _, _, _, _ = get_user(uid)
            extra = "" if is_premium_flag else f"\n\n🎫 Осталось попыток: {remaining_after}"
            bot.edit_message_text(f"✅ *Найден юзернейм:*\n\n`@{username}`{extra}", call.message.chat.id, call.message.message_id, reply_markup=result_keyboard_7(username), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        # Админ-панель
        if data == "admin_support_messages":
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "⛔ Нет прав")
                return
            msgs = get_unread_support_messages()
            if not msgs:
                bot.edit_message_text("📭 *Нет новых сообщений в поддержке.*", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=admin_main_keyboard())
                bot.answer_callback_query(call.id)
                return
            msg_id, user_id, text, ts = msgs[0]
            date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
            text_preview = (text[:200] + '…') if len(text) > 200 else text
            msg_text = f"📩 *Сообщение от* [`{user_id}`](tg://user?id={user_id})\n🕒 {date_str}\n\n{text_preview}"
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("✅ Отметить прочитанным", callback_data=f"support_read_{msg_id}"),
                InlineKeyboardButton("➡️ Далее", callback_data="admin_support_messages_next")
            )
            markup.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back_to_main"))
            bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "admin_support_messages_next":
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "⛔ Нет прав")
                return
            msgs = get_unread_support_messages()
            if msgs:
                msg_id, user_id, text, ts = msgs[0]
                mark_support_read(msg_id)
            msgs = get_unread_support_messages()
            if not msgs:
                bot.edit_message_text("📭 *Нет новых сообщений в поддержке.*", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=admin_main_keyboard())
                bot.answer_callback_query(call.id)
                return
            msg_id, user_id, text, ts = msgs[0]
            date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
            text_preview = (text[:200] + '…') if len(text) > 200 else text
            msg_text = f"📩 *Сообщение от* [`{user_id}`](tg://user?id={user_id})\n🕒 {date_str}\n\n{text_preview}"
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("✅ Отметить прочитанным", callback_data=f"support_read_{msg_id}"),
                InlineKeyboardButton("➡️ Далее", callback_data="admin_support_messages_next")
            )
            markup.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back_to_main"))
            bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data.startswith("support_read_"):
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "⛔ Нет прав")
                return
            msg_id = int(data.split("_")[2])
            mark_support_read(msg_id)
            bot.answer_callback_query(call.id, "✅ Сообщение отмечено прочитанным")
            msgs = get_unread_support_messages()
            if not msgs:
                bot.edit_message_text("📭 *Нет новых сообщений в поддержке.*", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=admin_main_keyboard())
            else:
                msg_id, user_id, text, ts = msgs[0]
                date_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
                text_preview = (text[:200] + '…') if len(text) > 200 else text
                msg_text = f"📩 *Сообщение от* [`{user_id}`](tg://user?id={user_id})\n🕒 {date_str}\n\n{text_preview}"
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton("✅ Отметить прочитанным", callback_data=f"support_read_{msg_id}"),
                    InlineKeyboardButton("➡️ Далее", callback_data="admin_support_messages_next")
                )
                markup.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back_to_main"))
                bot.edit_message_text(msg_text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            return

        if data == "admin_usdt_list":
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "⛔ Нет прав")
                return
            c.execute("SELECT id, user_id, amount, days, created_at FROM pending_payments WHERE status='waiting' ORDER BY created_at DESC")
            rows = c.fetchall()
            if not rows:
                bot.edit_message_text("💰 *Нет ожидающих заявок USDT.*", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=admin_main_keyboard())
                bot.answer_callback_query(call.id)
                return
            payment_id, user_id, amount, days, created = rows[0]
            date_str = datetime.fromtimestamp(created).strftime("%d.%m.%Y %H:%M")
            text = f"💰 *Заявка USDT #{payment_id}*\n👤 Пользователь: `{user_id}`\n📅 Создана: {date_str}\n💵 Сумма: {amount} USDT\n📆 Тариф: {days if days!=0 else 'навсегда'} дней"
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_usdt_approve_{payment_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_usdt_reject_{payment_id}")
            )
            if len(rows) > 1:
                markup.add(InlineKeyboardButton("➡️ Следующая", callback_data="admin_usdt_next"))
            markup.add(InlineKeyboardButton("◀ Назад", callback_data="admin_back_to_main"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "admin_usdt_next":
            if uid != ADMIN_ID:
                bot.answer_callback_query(call.id, "⛔ Нет прав")
                return
            c.execute("SELECT id, user_id, amount, days, created_at FROM pending_payments WHERE status='waiting' ORDER BY created_at DESC")
            rows = c.fetchall()
            if len(rows) <= 1:
                bot.edit_message_text("💰 *Нет других заявок.*", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=admin_main_keyboard())
                bot.answer_callback_query(call.id)
                return
            payment_id, user_id, amount, days, created = rows[1]
            date_str = datetime.fromtimestamp(created).strftime("%d.%m.%Y %H:%M")
            text = f"💰 *Заявка USDT #{payment_id}*\n👤 Пользователь: `{user_id}`\n📅 Создана: {date_str}\n💵 Сумма: {amount} USDT\n📆 Тариф: {days if days!=0 else 'навсегда'} дней"
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_usdt_approve_{payment_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_usdt_reject_{payment_id}")
            )
            if len(rows) > 2:
                markup.add(InlineKeyboardButton("➡️ След
