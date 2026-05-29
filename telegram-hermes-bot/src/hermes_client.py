"""Обёртка над моделью Hermes через OpenAI-совместимый API OpenRouter."""
from openai import AsyncOpenAI

from .config import HERMES_MODEL, OPENROUTER_API_KEY, REQUEST_TIMEOUT

# OpenRouter использует тот же протокол, что и OpenAI, — меняем только base_url.
_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    timeout=REQUEST_TIMEOUT,
    default_headers={
        # Необязательные заголовки OpenRouter (для статистики/рейтинга приложения).
        "HTTP-Referer": "https://github.com/your-name/telegram-hermes-bot",
        "X-Title": "Telegram Hermes Bot",
    },
)


async def ask_hermes(messages: list[dict]) -> str:
    """Отправить список сообщений модели и вернуть текст ответа.

    Формат `messages` — как в OpenAI Chat API:
        [{"role": "system"|"user"|"assistant", "content": "..."}]
    """
    response = await _client.chat.completions.create(
        model=HERMES_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=1000,
    )
    content = response.choices[0].message.content
    return content.strip() if content else "(модель вернула пустой ответ)"
