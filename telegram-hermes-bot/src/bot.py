"""Точка входа: Telegram-бот на aiogram, отвечающий через агента Hermes."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from . import agent, db
from .config import TELEGRAM_BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("hermes-bot")

dp = Dispatcher()

# Telegram не принимает сообщения длиннее 4096 символов.
MAX_TG_LEN = 4096


async def _send_long(message: Message, text: str) -> None:
    """Отправить длинный ответ, разбив его на части. Без разметки, чтобы
    спецсимволы из ответа модели не ломали отправку."""
    for start in range(0, len(text), MAX_TG_LEN):
        await message.answer(text[start:start + MAX_TG_LEN], parse_mode=None)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я <b>Hermes</b> — твой ИИ-ассистент.\n\n"
        "Просто напиши мне сообщение, и я отвечу.\n\n"
        "Команды:\n"
        "/reset — очистить историю диалога\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Я отвечаю на твои сообщения с помощью модели Hermes и помню контекст диалога.\n\n"
        "/reset — забыть наш разговор и начать с чистого листа."
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    await agent.reset(message.from_user.id)
    await message.answer("История диалога очищена. Начнём заново!")


@dp.message()
async def on_message(message: Message) -> None:
    if not message.text:
        await message.answer("Пока я понимаю только текстовые сообщения.")
        return

    # Показываем «печатает...», пока ждём ответ модели.
    await message.bot.send_chat_action(message.chat.id, "typing")

    reply = await agent.handle_message(message.from_user.id, message.text)
    await _send_long(message, reply)


async def main() -> None:
    await db.init_db()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("Бот запущен. Ожидаю сообщения...")
    # drop_pending_updates=True — игнорируем сообщения, накопившиеся пока бот лежал.
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
