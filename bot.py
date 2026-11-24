"""Хи-Хи: лёгкий Telegram-бот для запуска Mini App.

Переменные окружения:
  BOT_TOKEN — токен бота (хранить только в секретах хостинга)
  MINI_APP_URL — URL конструктора (необязательно)
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MINI_APP_URL = os.environ.get(
    "MINI_APP_URL", "https://greengergreen.github.io/hihi-miniapp/"
).strip()

router = Router()

WELCOME = (
    "🎉 <b>Добро пожаловать в «Хи-Хи»!</b>\n\n"
    "Здесь начинается ваш идеальный праздник! 🥳\n\n"
    "Соберите его прямо здесь: выберите меню, развлечения, оформление "
    "и всё, что сделает этот день незабываемым.\n\n"
    "✨ Укажите количество детей, добавьте понравившиеся услуги "
    "и получите готовую смету.\n\n"
    "👇 <b>Нажмите кнопку ниже, чтобы собрать свой праздник!</b>"
)


def app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🎉 Собрать свой праздник",
                web_app=WebAppInfo(url=MINI_APP_URL),
            )
        ]]
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=app_keyboard(), parse_mode="HTML")


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("В Bothost необходимо задать BOT_TOKEN в переменных окружения")
    if not MINI_APP_URL.startswith("https://"):
        raise RuntimeError("MINI_APP_URL должен начинаться с https://")

    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        await bot.set_my_commands([BotCommand(command="start", description="🎉 Собрать праздник")])
        # Отображаем ту же Web App кнопку в меню Telegram.
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🎉 Собрать праздник", web_app=WebAppInfo(url=MINI_APP_URL)
            )
        )
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
