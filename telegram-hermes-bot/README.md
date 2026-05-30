# Telegram Hermes Bot

Telegram-бот с ИИ-агентом на базе модели **Hermes** (Nous Research), доступ к
модели — через **OpenRouter** (внешний API, GPU не нужен). Бот помнит контекст
диалога (история хранится в SQLite) и готов к хостингу 24/7.

```
Пользователь → Telegram → бот (aiogram) → агент → Hermes (OpenRouter)
```

## Возможности

- Диалог с моделью Hermes с памятью контекста по каждому пользователю.
- Хранение истории в SQLite (переживает перезапуски бота).
- Команды `/start`, `/help`, `/reset`.
- Готовые конфиги для Docker, Render и платформ с Procfile (Railway и т.п.).

## Структура проекта

```
telegram-hermes-bot/
├── src/
│   ├── config.py         # настройки из переменных окружения
│   ├── db.py             # история диалогов в SQLite (aiosqlite)
│   ├── hermes_client.py  # обёртка над Hermes через OpenRouter
│   ├── agent.py          # логика агента: промпт + память + вызов модели
│   └── bot.py            # точка входа, обработчики Telegram
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── Procfile              # для Railway / Heroku-подобных платформ
├── render.yaml           # для Render.com
├── .env.example          # шаблон переменных окружения
└── README.md
```

## Шаг 1. Создать бота в Telegram

1. Открой Telegram и найди **@BotFather**.
2. Отправь `/newbot`, задай имя и username (должен заканчиваться на `bot`).
3. Скопируй выданный **токен** вида `123456789:AAH...`.

## Шаг 2. Получить ключ OpenRouter

1. Зарегистрируйся на https://openrouter.ai
2. Создай API-ключ: https://openrouter.ai/keys
3. Пополни баланс на пару долларов (модели платные, но дешёвые).

> Совет: для экономии можно поставить модель `nousresearch/hermes-3-llama-3.1-8b`
> в переменной `HERMES_MODEL` — она дешевле и быстрее.

## Шаг 3. Настроить переменные окружения

Скопируй `.env.example` в `.env` и заполни значения:

```bash
cp .env.example .env
```

```dotenv
TELEGRAM_BOT_TOKEN=сюда_токен_от_BotFather
OPENROUTER_API_KEY=сюда_ключ_OpenRouter
HERMES_MODEL=nousresearch/hermes-3-llama-3.1-70b
```

## Запуск локально

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.bot
```

Напиши боту в Telegram — он ответит.

## Запуск через Docker

```bash
docker compose up -d --build      # запустить в фоне
docker compose logs -f            # смотреть логи
docker compose down               # остановить
```

База данных сохраняется в папке `./data` на хосте.

---

## Хостинг 24/7

Бот работает в режиме **long polling** — публичный домен/HTTPS не нужен, поэтому
подойдёт почти любой хостинг, где можно запустить фоновый процесс.

### Вариант A. Railway (просто, есть стартовый бесплатный кредит)

1. Залей проект на GitHub.
2. На https://railway.app → **New Project → Deploy from GitHub repo**.
3. В разделе **Variables** добавь `TELEGRAM_BOT_TOKEN` и `OPENROUTER_API_KEY`
   (и при желании `HERMES_MODEL`).
4. Railway сам соберёт образ по `Dockerfile` и запустит процесс из `Procfile`.

> Для сохранения истории подключи **Volume** и смонтируй его в `/app/data`.

### Вариант B. Render.com

1. Залей проект на GitHub.
2. New → **Background Worker**, выбери репозиторий (подхватит `render.yaml`).
3. Введи секреты `TELEGRAM_BOT_TOKEN` и `OPENROUTER_API_KEY` в дашборде.

### Вариант C. Свой VPS (например, Ubuntu)

```bash
git clone <твой-репозиторий>
cd telegram-hermes-bot
cp .env.example .env && nano .env      # вписать секреты
docker compose up -d --build
```

Контейнер с `restart: unless-stopped` сам поднимется после перезагрузки сервера.

---

## Настройка поведения

Все настройки — через переменные окружения (см. `.env.example`):

| Переменная | Назначение | По умолчанию |
|------------|-----------|--------------|
| `TELEGRAM_BOT_TOKEN` | токен бота от BotFather | — (обязательно) |
| `OPENROUTER_API_KEY` | ключ OpenRouter | — (обязательно) |
| `HERMES_MODEL` | модель Hermes на OpenRouter | `nousresearch/hermes-3-llama-3.1-70b` |
| `SYSTEM_PROMPT` | личность/роль агента | дружелюбный ассистент |
| `MAX_HISTORY_MESSAGES` | сколько реплик помнить | `20` |
| `DB_PATH` | путь к файлу SQLite | `data/bot.db` |
| `REQUEST_TIMEOUT` | таймаут запроса к модели (сек) | `60` |

## Идеи для развития

- Function calling / инструменты (погода, поиск, обращение к API).
- Поддержка голосовых сообщений (распознавание через Whisper).
- Ограничение частоты запросов (rate limiting) на пользователя.
- Перенос истории в PostgreSQL/Redis для больших нагрузок.
