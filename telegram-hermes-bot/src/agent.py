"""Логика агента Hermes: память диалога + системный промпт + вызов модели."""
import logging

from . import db
from .config import MAX_HISTORY_MESSAGES, SYSTEM_PROMPT
from .hermes_client import ask_hermes

logger = logging.getLogger(__name__)


async def handle_message(user_id: int, text: str) -> str:
    """Обработать сообщение пользователя и вернуть ответ агента."""
    # 1. Сохраняем входящее сообщение пользователя.
    await db.add_message(user_id, "user", text)

    # 2. Собираем контекст: системный промпт + последние реплики диалога.
    history = await db.get_history(user_id, MAX_HISTORY_MESSAGES)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

    # 3. Запрашиваем ответ у модели.
    try:
        answer = await ask_hermes(messages)
    except Exception:
        logger.exception("Ошибка обращения к модели Hermes")
        return "Не удалось получить ответ от модели. Попробуй ещё раз чуть позже."

    # 4. Сохраняем ответ ассистента, чтобы он стал частью контекста.
    await db.add_message(user_id, "assistant", answer)
    return answer


async def reset(user_id: int) -> None:
    """Очистить историю диалога пользователя."""
    await db.clear_history(user_id)
