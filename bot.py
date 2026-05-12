#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auron Search Bot - TEST VERSION
Просто проверяет, что бот работает и отвечает на /start
"""

import os
import asyncio
import logging
from aiohttp import web

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN not set!")
    exit(1)

logger.info("=" * 50)
logger.info("🚀 TEST BOT STARTING...")
logger.info(f"   BOT_TOKEN: {'✅' if BOT_TOKEN else '❌'}")
logger.info(f"   DATABASE_URL: {'✅' if DATABASE_URL else '❌'}")
logger.info(f"   PORT: {PORT}")
logger.info("=" * 50)

# ================= КЛАВИАТУРА =================
def get_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Тест"), KeyboardButton(text="ℹ️ Инфо")]
        ],
        resize_keyboard=True
    )

# ================= ХЕНДЛЕРЫ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "🤖 *Бот работает!*\n\n"
        "✅ Тестовая версия успешно запущена.\n\n"
        "Нажмите кнопку внизу для проверки.",
        parse_mode="Markdown",
        reply_markup=get_keyboard()
    )
    logger.info(f"User {m.from_user.id} sent /start")

@dp.message(F.text == "🔍 Тест")
async def cmd_test(m: Message):
    await m.answer("✅ Бот отвечает на сообщения! Всё работает.")
    logger.info(f"User {m.from_user.id} pressed Test button")

@dp.message(F.text == "ℹ️ Инфо")
async def cmd_info(m: Message):
    await m.answer(
        f"📊 *Информация*\n\n"
        f"🔹 Bot: @{os.getenv('BOT_USERNAME', 'Auron_Search_Bot')}\n"
        f"🔹 Статус: ✅ Активен\n"
        f"🔹 Версия: Test v1.0\n"
        f"🔹 База данных: {'✅' if DATABASE_URL else '❌'}",
        parse_mode="Markdown"
    )

# ================= HTTP HEALTH CHECK =================
async def health(request):
    return web.Response(text="OK")

async def start_http_server():
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 HTTP Health Check server started on port {PORT}")

# ================= MAIN =================
async def main():
    logger.info("✅ Starting bot polling...")
    
    # Запускаем HTTP сервер
    await start_http_server()
    
    # Запускаем бота
    await dp.start_polling(bot)
    logger.info("✅ Bot polling started")

if __name__ == "__main__":
    asyncio.run(main())
