"""Загрузка и валидация настроек из переменных окружения."""
import os

from dotenv import load_dotenv

# Подхватываем переменные из файла .env (если он есть)
load_dotenv()


def _require(name: str) -> str:
    """Вернуть обязательную переменную окружения или упасть с понятной ошибкой."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Скопируй .env.example в .env и заполни значения "
            f"(или задай переменные в панели хостинга)."
        )
    return value


# --- Обязательные секреты ---
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY: str = _require("OPENROUTER_API_KEY")

# --- Настройки модели ---
HERMES_MODEL: str = os.getenv("HERMES_MODEL", "nousresearch/hermes-3-llama-3.1-70b")
REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "60"))

# --- Поведение агента ---
SYSTEM_PROMPT: str = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — Hermes, дружелюбный и полезный ИИ-ассистент в Telegram. "
    "Отвечай ясно, по делу и на языке пользователя.",
)
MAX_HISTORY_MESSAGES: int = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))

# --- Хранилище ---
DB_PATH: str = os.getenv("DB_PATH", "data/bot.db")
