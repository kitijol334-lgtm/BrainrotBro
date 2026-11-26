import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo
)

# =========================
# НАСТРОЙКИ
# =========================

TOKEN = "ВСТАВЬ_СЮДА_НОВЫЙ_ТОКЕН"

WEB_APP_URL = "https://ТВОЙ_САЙТ.com"

# =========================
# СОЗДАНИЕ БОТА
# =========================

bot = Bot(token=TOKEN)
dp = Dispatcher()

# =========================
# КНОПКА ИГРЫ
# =========================

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🎮 Играть в BrainrotBro",
                web_app=WebAppInfo(url=WEB_APP_URL)
            )
        ]
    ],
    resize_keyboard=True,
    is_persistent=True
)

# =========================
# КОМАНДА START
# =========================

@dp.message(CommandStart())
async def start(message: Message):

    await message.answer(
        "👋 Добро пожаловать в BrainrotBro!\n\n"
        "🎮 Чтобы играть, нажми на голубую кнопку снизу слева.\n\n"
        "Удачи и крутых дропов! 🍀",
        reply_markup=keyboard
    )

# =========================
# ЗАПУСК
# =========================

async def main():
    print("BrainrotBro запущен!")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())