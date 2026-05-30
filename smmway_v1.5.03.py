"""
SMMWay FPC Plugin — автоматическая перепродажа SMM-услуг smmway.ru
                    через FunPay Cardinal.

Один файл (как требует архитектура FPC). Логически разбит на секции:
    1. Метаданные плагина
    2. Утилиты: storage, лог, форматирование
    3. SMM Way API клиент с кешем и ретраями
    4. Состояние / стейт-машина обмена с покупателем
    5. Фоновые задачи: авто-цена, авто-деактивация, опрос статусов
    6. Обработчик нового заказа (NEW_ORDER)
    7. Обработчик сообщения покупателя (NEW_MESSAGE) для запроса ссылки
    8. Telegram-меню: главное и подразделы
    9. Хуки FPC: BIND_TO_*

Без лицензий и привязок — MIT, открытый код.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable

import requests

if TYPE_CHECKING:
    from cardinal import Cardinal
    from FunPayAPI.updater.events import (
        NewMessageEvent,
        NewOrderEvent,
    )


# =============================================================================
# 1. МЕТАДАННЫЕ ПЛАГИНА (обязательные поля для FPC)
# =============================================================================

NAME = "SMMWay"
VERSION = "1.5.03"
DESCRIPTION = (
    "Автоматическая перепродажа услуг накрутки через FunPay Cardinal.\n"
    "• Dynamic Workflows — адаптивное управление заказами.\n"
    "• Smart Queue — антифлуд и приоритизация услуг.\n"
    "• Loyalty — бонусы повторным покупателям.\n"
    "• Авто-цена, авто-деактивация, авто-замена лотов.\n"
    "• Команды: !статус !рефилл !отмена.\n"
    "• Аналитика, чёрный список, авто-повтор при ошибках.\n"
    "• Service Health Auto-Recovery — стабилизатор продаж."
)
CREDITS = "@Keilery (форк с нуля на smmway, без чужого кода)"
UUID = "1f5d4c8e-7a3b-4d6c-9f1e-2b8c5a0d3e7a"
SETTINGS_PAGE = True
# COMMANDS — словарь команд плагина, который FPC показывает на странице
# «Команды» этого плагина (в Telegram-боте). Ключ — имя команды без слэша,
# значение — описание. Также эти команды регистрируются в BotFather-меню
# при инициализации плагина (см. init_tg_menu).
COMMANDS = {
    "smmway": "Открыть меню плагина SMMWay",
    "smm": "Алиас /smmway",
    "smmway_menu": "Алиас /smmway",
}

logger = logging.getLogger(f"FPC.{NAME}")


# =============================================================================
# 2. УТИЛИТЫ
# =============================================================================

PLUGIN_DIR = os.path.join("storage", "plugins", "smmway")
LOTS_FILE = os.path.join(PLUGIN_DIR, "lots.json")
ORDERS_FILE = os.path.join(PLUGIN_DIR, "orders.json")
CONFIG_FILE = os.path.join(PLUGIN_DIR, "config.json")
TEMPLATES_FILE = os.path.join(PLUGIN_DIR, "templates.json")
SERVICE_CACHE_FILE = os.path.join(PLUGIN_DIR, "services_cache.json")
LOG_FILE = os.path.join(PLUGIN_DIR, "smmway.log")


def _ensure_plugin_dir() -> None:
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    os.makedirs(os.path.join(PLUGIN_DIR, "backups"), exist_ok=True)


def _backup_file(path: str) -> None:
    if not os.path.exists(path):
        return
    bdir = os.path.join(PLUGIN_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(path)
    try:
        with open(path, "rb") as f_src, open(os.path.join(bdir, f"{base}.{stamp}.bak"), "wb") as f_dst:
            f_dst.write(f_src.read())
    except Exception as ex:
        logger.warning("backup failed for %s: %s", path, ex)
    # rotate: keep last 20 per file
    try:
        files = sorted(
            (f for f in os.listdir(bdir) if f.startswith(base + ".")),
            reverse=True,
        )
        for old in files[20:]:
            os.remove(os.path.join(bdir, old))
    except Exception:
        pass


def _load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        logger.error("failed to read %s: %s — using default", path, ex)
        _backup_file(path)
        return default


def _save_json(path: str, data: Any) -> None:
    _ensure_plugin_dir()
    _backup_file(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _short_id(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Telegram ограничивает текст сообщения 4096 символами. Берём чуть меньше,
# чтобы оставить место под parse_mode и любые служебные хвосты.
_TG_MSG_LIMIT = 3800


def send_long_html(bot, chat_id, text: str, *, limit: int = _TG_MSG_LIMIT) -> None:
    """Отправляет HTML-сообщение, при необходимости разбивая по переводам строки.

    Каждый кусок гарантированно не превышает ``limit`` символов. Если отдельная
    строка длиннее лимита — режется жёстко по символам. Полезно для длинных
    отчётов авто-лотов с большим списком ошибок, чтобы не упереться в
    400 Bad Request «text is too long» от Telegram API.
    """
    if not text:
        return
    if len(text) <= limit:
        try:
            bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as ex:
            logger.warning("send_long_html: single send failed: %s", ex)
        return

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        # +1 за перевод строки между line'ами
        sep = 1 if cur else 0
        if cur and cur_len + sep + len(line) > limit:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
            sep = 0
        # Отдельная строка длиннее лимита — режем жёстко
        if len(line) > limit:
            if cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        cur.append(line)
        cur_len += sep + len(line)
    if cur:
        chunks.append("\n".join(cur))

    for ch in chunks:
        try:
            bot.send_message(chat_id, ch, parse_mode="HTML")
        except Exception as ex:
            logger.warning("send_long_html: chunk send failed (%s chars): %s", len(ch), ex)


def parse_link_or_username(text: str) -> str | None:
    """Достаёт ссылку или ник из произвольного сообщения."""
    if not text:
        return None
    text = text.strip()
    url_m = re.search(r"https?://\S+", text)
    if url_m:
        return url_m.group(0).rstrip(").,!?")
    at_m = re.search(r"@?[A-Za-z0-9_.]{3,}", text)
    if at_m:
        token = at_m.group(0).lstrip("@")
        # ignore pure numbers like "1000"
        if not token.isdigit():
            return token
    return None


# =============================================================================
# 3. SMM WAY API КЛИЕНТ
# =============================================================================


class SMMWayError(Exception):
    pass


class SMMWayAPI:
    BASE_URL = "https://smmway.ru/api/v2"
    PUBLIC_SERVICES_URL = "https://smmway.ru/api/services/quickSearch"
    TIMEOUT = 20
    RETRIES = 3
    BACKOFF = 1.5

    def __init__(self, api_key: str | None = None):
        self.api_key: str = api_key or ""
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "SMMWay-FPC/1.0"})
        self._services_cache: list[dict] = []
        self._services_cache_ts: float = 0.0

    def update_key(self, key: str) -> None:
        self.api_key = key.strip()

    # --- low-level ---

    def _post(self, payload: dict) -> dict:
        last_exc: Exception | None = None
        if not self.api_key:
            raise SMMWayError("API-ключ не задан. Зайди в меню → API ключ.")
        payload = {"key": self.api_key, **payload}
        for attempt in range(1, self.RETRIES + 1):
            try:
                r = self._session.post(self.BASE_URL, data=payload, timeout=self.TIMEOUT)
                if r.status_code >= 500:
                    raise SMMWayError(f"HTTP {r.status_code}")
                try:
                    data = r.json()
                except ValueError as ex:
                    raise SMMWayError(f"Не JSON-ответ: {r.text[:200]}") from ex
                if isinstance(data, dict) and data.get("error"):
                    raise SMMWayError(str(data["error"]))
                return data if isinstance(data, dict) else {"result": data}
            except (requests.RequestException, SMMWayError) as ex:
                last_exc = ex
                if attempt < self.RETRIES:
                    time.sleep(self.BACKOFF ** attempt)
        raise SMMWayError(f"API недоступен после {self.RETRIES} попыток: {last_exc}")

    # --- public API ---

    def services(self, force: bool = False, ttl: float = 600.0) -> list[dict]:
        """Возвращает список услуг. Сначала пробует приватный API (нужны цены под аккаунт),
        при ошибке — публичный quickSearch endpoint (только базовые данные)."""
        now = time.time()
        if not force and self._services_cache and (now - self._services_cache_ts) < ttl:
            return self._services_cache
        # try authenticated services
        try:
            data = self._post({"action": "services"})
            items = data.get("result") if "result" in data else data
            if isinstance(items, list) and items:
                self._services_cache = items
                self._services_cache_ts = now
                _save_json(SERVICE_CACHE_FILE, {"ts": now, "items": items})
                return items
        except SMMWayError as ex:
            logger.warning("services via /api/v2 failed: %s — fallback to quickSearch", ex)
        # fallback
        try:
            r = self._session.get(self.PUBLIC_SERVICES_URL, timeout=self.TIMEOUT)
            r.raise_for_status()
            items = r.json().get("services", [])
            # normalize to API-v2-like shape
            normalized = [
                {
                    "service": str(s.get("id")),
                    "name": s.get("name"),
                    "rate": str(s.get("price")),
                    "min": "",
                    "max": "",
                    "category": (s.get("category") or {}).get("slug", ""),
                }
                for s in items
            ]
            self._services_cache = normalized
            self._services_cache_ts = now
            _save_json(SERVICE_CACHE_FILE, {"ts": now, "items": normalized})
            return normalized
        except Exception as ex:
            cached = _load_json(SERVICE_CACHE_FILE, {})
            if cached.get("items"):
                logger.warning("using stale services cache: %s", ex)
                self._services_cache = cached["items"]
                return self._services_cache
            raise SMMWayError(f"Не удалось получить услуги: {ex}") from ex

    def find_service(self, service_id: int | str) -> dict | None:
        sid = str(service_id)
        for s in self.services():
            if str(s.get("service")) == sid or str(s.get("id")) == sid:
                return s
        return None

    def balance(self) -> float:
        data = self._post({"action": "balance"})
        bal = data.get("balance") or data.get("result")
        try:
            return float(bal)
        except (TypeError, ValueError):
            raise SMMWayError(f"Не удалось разобрать баланс: {data}")

    def add_order(self, service_id: int | str, link: str, quantity: int,
                   extra: dict | None = None) -> int:
        payload = {
            "action": "add",
            "service": str(service_id),
            "link": link,
            "quantity": str(int(quantity)),
        }
        if extra:
            for k, v in extra.items():
                payload[k] = str(v)
        data = self._post(payload)
        oid = data.get("order") or data.get("result")
        if not oid:
            raise SMMWayError(f"Нет order id в ответе: {data}")
        return int(oid)

    def order_status(self, order_id: int | str) -> dict:
        return self._post({"action": "status", "order": str(order_id)})

    def orders_status(self, ids: Iterable[int | str]) -> dict:
        ids_str = ",".join(str(i) for i in ids)
        return self._post({"action": "status", "orders": ids_str})

    def cancel(self, order_id: int | str) -> dict:
        return self._post({"action": "cancel", "orders": str(order_id)})

    def refill(self, order_id: int | str) -> dict:
        return self._post({"action": "refill", "order": str(order_id)})


# =============================================================================
# 4. КОНФИГ / СТОРАДЖ / СОСТОЯНИЕ
# =============================================================================


DEFAULT_CONFIG = {
    "enabled": True,
    "api_key": "",
    "global_markup_pct": 55.0,
    "currency_rate_rub_to_fp": 1.0,  # курс smmway-валюты (RUB) к FP-цене
    "auto_price_enabled": True,
    "auto_price_interval_sec": 120,
    "auto_price_jump_cap_pct": 200.0,
    "auto_deactivate_enabled": True,
    "auto_deactivate_min_balance": 0.0,
    # "докрутка" — за положительный отзыв даём дополнительный заказ на smmway:
    "auto_review_bonus_enabled": True,
    "auto_review_bonus_min_stars": 5,
    "auto_review_bonus_pct": 10.0,
    "notify_order_created": True,
    "notify_order_error": True,
    "notify_balance_before": False,
    "notify_balance_after": True,
    "status_poll_interval_sec": 90,
    "max_buyer_link_wait_sec": 1800,
    # Минимальная цена лота. Формула: (цена за 1 ед.) * (наценка%/100) + цена за 1 ед.
    # Если не получается выставить — пробуем 0.001, потом 1.
    "min_lot_price": 0.001,
    # Принудительный маппинг платформа → ID подкатегории FunPay.
    # Используется, если авто-детект кладёт услуги «не туда». Например,
    # для Twitter правильная подкатегория FunPay имеет id=1260, и без
    # этого override плагин мог положить Twitter-услуги в «Mortal Kombat X»
    # (там просто совпало ключевое слово). Чтобы добавить ещё платформу —
    # отредактируй config.json: {"platform_subcat_overrides": {"twitter": 1260, "vk": 1234}}.
    "platform_subcat_overrides": {"twitter": 1260},
    # Если услуга на smmway пропала из каталога — пытаемся заменить лот
    # случайной другой услугой, которая ещё не выставлена на FunPay
    # (а не просто деактивировать его).
    "auto_replace_missing_service": True,
    # Авто-повтор при ошибке заказа: проверяет ссылку и баланс, пробует ещё раз.
    # Если повторно ошибка — возврат денег, блокировка услуги, замена лота.
    "auto_retry_on_error": True,
    "auto_retry_max_attempts": 2,
    # Чёрный список услуг: ID услуг smmway, которые дали ошибки и были заблокированы.
    "blacklisted_services": [],
    # --- Dynamic Workflows ---
    # Система адаптивного управления: бот анализирует паттерны заказов и автоматически
    # настраивает поведение (тайминги, приоритеты, выбор услуг).
    "dynamic_workflows_enabled": True,
    # Если услуга выполняется медленно (> порога) — снижаем приоритет
    "dw_slow_threshold_sec": 3600,  # 1 час
    # Если услуга фейлится > N раз подряд — временно отключаем (soft blacklist)
    "dw_fail_streak_limit": 3,
    # Авто-бонус за повторные покупки: скидка повторному покупателю
    "loyalty_enabled": True,
    "loyalty_bonus_pct": 5.0,  # % бонусного объёма при повторной покупке
    "loyalty_min_orders": 2,  # мин. кол-во заказов для бонуса
    # Умная очередь: задержка между заказами одной услуги (anti-flood)
    "smart_queue_delay_sec": 5,
    # --- Service Health Auto-Recovery (Стабилизатор) ---
    "service_recovery_enabled": True,
    "service_recovery_check_interval_sec": 300,
    "service_recovery_min_success_rate": 0.6,
    "service_recovery_window_orders": 20,
    "service_recovery_cooldown_sec": 1800,
    "service_recovery_auto_reenable": True,
    "log_level": "INFO",
}


DEFAULT_LOT_TEMPLATE_RU = {
    "title": "🌟 {app} 🌟 | ❤️ {type} ❤️ | ✅БЫСТРО✅ | ⚠️ {tags} ⚠️",
    "description": (
        "{app} — {type}\n"
        "{tags}\n\n"
        "{features}\n\n"
        "📋 Команды:\n"
        "{commands}"
    ),
}

DEFAULT_LOT_TEMPLATE_EN = {
    "title": "🌟 {capp} 🌟 | ❤️ {ctype_en} ❤️ | ✅FAST✅ | ⚠️ {tags_en} ⚠️",
    "description": (
        "{capp} — {type_en}\n"
        "{tags_en}\n\n"
        "{features_en}\n\n"
        "📋 Commands:\n"
        "{commands_en}"
    ),
}

DEFAULT_MSG_TEMPLATES = {
    "await_link": (
        "👋 Здравствуйте! Спасибо за заказ.\n"
        "Чтобы я мог запустить накрутку, отправьте ссылку на профиль/пост/канал "
        "одним сообщением. Если услуга требует ник — напишите его."
    ),
    "order_created": (
        "✅ Заказ принят! Объём: {qty}. "
        "Накрутка запущена автоматически, ждать ничего не нужно."
    ),
    "order_completed": (
        "🎉 Заказ выполнен! Если всё ок — подтвердите выполнение и оставьте отзыв. "
        "Спасибо!"
    ),
    "order_error": (
        "⚠️ Возникла проблема с заказом: {reason}. "
        "Возврат уже инициирован, при необходимости напишите продавцу."
    ),
    "status_reply": "📊 Статус заказа: {status}",
}


@dataclass
class LotEntry:
    funpay_lot_id: int
    service_id: int
    title_ru: str = ""
    title_en: str = ""
    subcategory_id: int | None = None
    markup_pct: float | None = None  # None = use global
    last_price_fp: float | None = None
    custom_template: str | None = None
    active: bool = True
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "LotEntry":
        return cls(
            funpay_lot_id=int(d["funpay_lot_id"]),
            service_id=int(d["service_id"]),
            title_ru=d.get("title_ru", ""),
            title_en=d.get("title_en", ""),
            subcategory_id=d.get("subcategory_id"),
            markup_pct=d.get("markup_pct"),
            last_price_fp=d.get("last_price_fp"),
            custom_template=d.get("custom_template"),
            active=d.get("active", True),
            note=d.get("note", ""),
        )


@dataclass
class OrderEntry:
    funpay_order_id: str
    smm_order_id: int | None = None
    funpay_lot_id: int | None = None
    service_id: int | None = None
    buyer_username: str = ""
    buyer_id: int = 0
    chat_id: int | str = 0
    quantity: int = 0
    link: str = ""
    status: str = "awaiting_link"  # awaiting_link/created/in_progress/completed/error/refunded
    smm_status_raw: str = ""
    created_at: str = field(default_factory=_now_iso)
    last_check_at: str = ""
    error: str = ""
    review_stars: int | None = None
    bonus_smm_order_id: int | None = None
    bonus_quantity: int = 0
    # Финансы (для красивого уведомления при запуске и истории):
    # funpay_price — то, что заплатил покупатель на FunPay (₽).
    # smmway_charge_rub — то, что мы заплатили smmway (₽), считается из rate × qty / 1000.
    # service_name_snapshot — название услуги smmway на момент создания заказа.
    funpay_price: float = 0.0
    smmway_charge_rub: float = 0.0
    service_name_snapshot: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "OrderEntry":
        return cls(**{k: d.get(k, v.default if hasattr(v, "default") else None)
                      for k, v in cls.__dataclass_fields__.items()})


class Storage:
    def __init__(self):
        self.cfg: dict = {**DEFAULT_CONFIG, **_load_json(CONFIG_FILE, {})}
        self.lots: dict[int, LotEntry] = {}
        self.orders: dict[str, OrderEntry] = {}
        self.templates: dict = self._load_templates()
        self.stats = {"sent": 0, "completed": 0, "failed": 0, "spent_rub": 0.0}
        self._lock = threading.RLock()
        self._reload_lots()
        self._reload_orders()

    def _load_templates(self) -> dict:
        t = _load_json(TEMPLATES_FILE, {})
        merged = {
            "lot_ru_title": t.get("lot_ru_title", DEFAULT_LOT_TEMPLATE_RU["title"]),
            "lot_ru_desc": t.get("lot_ru_desc", DEFAULT_LOT_TEMPLATE_RU["description"]),
            "lot_en_title": t.get("lot_en_title", DEFAULT_LOT_TEMPLATE_EN["title"]),
            "lot_en_desc": t.get("lot_en_desc", DEFAULT_LOT_TEMPLATE_EN["description"]),
            "msg_await_link": t.get("msg_await_link", DEFAULT_MSG_TEMPLATES["await_link"]),
            "msg_order_created": t.get("msg_order_created", DEFAULT_MSG_TEMPLATES["order_created"]),
            "msg_order_completed": t.get("msg_order_completed", DEFAULT_MSG_TEMPLATES["order_completed"]),
            "msg_order_error": t.get("msg_order_error", DEFAULT_MSG_TEMPLATES["order_error"]),
            "msg_status_reply": t.get("msg_status_reply", DEFAULT_MSG_TEMPLATES["status_reply"]),
        }
        # Миграция со старых версий шаблонов: убираем хвостовой
        # «[#{service_id}]» (и его уже отрендеренный вариант) из заголовков —
        # ID услуги больше не показываем в кратком описании лота.
        changed = False
        for key in ("lot_ru_title", "lot_en_title"):
            old = merged[key]
            new = re.sub(r"\s*\[#\{service_id\}\]\s*$", "", old)
            new = re.sub(r"\s*\[#\d+\]\s*$", "", new)
            if new != old:
                merged[key] = new
                changed = True
        if changed:
            try:
                _save_json(TEMPLATES_FILE, merged)
                logger.info("templates: мигрировали заголовки лотов — убран хвостовой [#service_id]")
            except Exception as ex:
                logger.warning("templates: миграция не сохранилась: %s", ex)
        return merged

    def _reload_lots(self):
        raw = _load_json(LOTS_FILE, [])
        self.lots = {int(d["funpay_lot_id"]): LotEntry.from_dict(d) for d in raw}

    def _reload_orders(self):
        raw = _load_json(ORDERS_FILE, [])
        self.orders = {d["funpay_order_id"]: OrderEntry.from_dict(d) for d in raw}

    # Persist helpers
    def save_config(self):
        with self._lock:
            _save_json(CONFIG_FILE, self.cfg)

    def save_lots(self):
        with self._lock:
            _save_json(LOTS_FILE, [asdict(v) for v in self.lots.values()])

    def save_orders(self):
        with self._lock:
            _save_json(ORDERS_FILE, [asdict(v) for v in self.orders.values()])

    def save_templates(self):
        with self._lock:
            _save_json(TEMPLATES_FILE, self.templates)

    # Lot operations
    def bind_lot(self, funpay_lot_id: int, service_id: int, **extra) -> LotEntry:
        with self._lock:
            entry = LotEntry(funpay_lot_id=funpay_lot_id, service_id=service_id)
            for k, v in extra.items():
                if hasattr(entry, k):
                    setattr(entry, k, v)
            self.lots[funpay_lot_id] = entry
            self.save_lots()
            return entry

    def unbind_lot(self, funpay_lot_id: int):
        with self._lock:
            self.lots.pop(funpay_lot_id, None)
            self.save_lots()

    def find_lot_by_title(self, description: str) -> LotEntry | None:
        if not description:
            return None
        norm = description.strip().lower()
        # 1. Match by hidden marker [#NNN]
        m = re.search(r"\[#(\d+)\]", description)
        if m:
            sid = int(m.group(1))
            for e in self.lots.values():
                if e.service_id == sid:
                    return e
        # 2. Match by lot_id in description (format "#lot:123")
        lot_m = re.search(r"#lot:(\d+)", description)
        if lot_m:
            lid = int(lot_m.group(1))
            if lid in self.lots:
                return self.lots[lid]
        # 3. Bidirectional title match
        for e in self.lots.values():
            for t in (e.title_ru, e.title_en):
                if not t:
                    continue
                t_norm = t.strip().lower()
                # title is substring of description (main direction)
                if t_norm in norm:
                    return e
                # description is substring of title (reverse - only for non-trivial descriptions)
                if len(norm) >= 10 and norm in t_norm:
                    return e
        return None

    # Order operations
    def add_order(self, entry: OrderEntry):
        with self._lock:
            self.orders[entry.funpay_order_id] = entry
            self.save_orders()

    def update_order(self, funpay_order_id: str, **fields):
        with self._lock:
            o = self.orders.get(funpay_order_id)
            if not o:
                return
            for k, v in fields.items():
                if hasattr(o, k):
                    setattr(o, k, v)
            self.save_orders()


# Per-user state (waiting-for-link, etc.)
class BuyerState:
    def __init__(self):
        self._states: dict[int, dict] = {}
        self._lock = threading.Lock()

    def set_awaiting_link(self, buyer_id: int, funpay_order_id: str, lot_entry: LotEntry,
                          quantity: int, chat_id: int | str):
        with self._lock:
            self._states[buyer_id] = {
                "type": "awaiting_link",
                "funpay_order_id": funpay_order_id,
                "lot_entry": lot_entry,
                "quantity": quantity,
                "chat_id": chat_id,
                "ts": time.time(),
            }

    def pop_awaiting_link(self, buyer_id: int) -> dict | None:
        with self._lock:
            return self._states.pop(buyer_id, None)

    def get(self, buyer_id: int) -> dict | None:
        with self._lock:
            return self._states.get(buyer_id)

    def cleanup(self, ttl: int):
        now = time.time()
        with self._lock:
            for bid, st in list(self._states.items()):
                if now - st.get("ts", 0) > ttl:
                    self._states.pop(bid, None)


# =============================================================================
# 5. ШАБЛОНЫ: рендер
# =============================================================================


# (тип услуги → ключевые слова в smmway name/category). Чем выше в списке —
# тем выше приоритет матча; некоторые типы пересекаются (например, "стрим"
# может быть и "Зрители", и "Просмотры стрима").
SERVICE_TYPE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Зрители", ["зрител", "viewer", "live viewer", "стрим зрител", "twitch зрител"]),
    ("Часы просмотра", ["часы просмотр", "watch hour", "watch time", "час стрим"]),
    ("Подписчики", ["подписч", "follower", "subscriber", "саб ", "sub ", "subs"]),
    ("Просмотры", ["просмотр", "view", "показ"]),
    ("Лайки", ["лайк", "like ", "likes", "сердечк", "heart"]),
    ("Реакции", ["реакц", "react"]),
    ("Комментарии", ["коммент", "comment"]),
    ("Репосты", ["репост", "share", "ретвит", "retweet"]),
    ("Голоса", ["голос", "vote", "poll ", "опрос"]),
    ("Сохранения", ["сохранен", "save ", "bookmark"]),
    ("Истории", ["истор", "story", "stories"]),
    ("Старты бота", ["/start", "start bot", "старт бот", "старты бот"]),
    ("Boost", ["буст", "boost", "premium"]),
    ("Друзья", ["друз", "friend"]),
    ("Подарки", ["подар", "gift", "звезд"]),
    ("Регистрации", ["регистрац", "signup", "sign up"]),
    ("Установки", ["устан", "install"]),
    ("Клики", ["клик", "click"]),
    ("Скачивания", ["скачив", "download"]),
    ("Донаты", ["донат", "donation", "донейш"]),
    ("Стримы", ["стрим", "stream", "трансляц"]),
]

# Названия платформ, которые не нужно тащить в "тип услуги" как fallback-слово.
_PLATFORM_WORDS = {
    "smmway", "smm", "twitch", "telegram", "instagram", "tiktok", "youtube",
    "vk", "вконтакте", "discord", "steam", "rutube", "facebook", "twitter",
    "trovo", "kick", "max", "wibes", "likee", "дзен", "trovo.live",
    "x", "x.com", "yandex", "github",
}


def _service_haystack(service: dict) -> str:
    name = service.get("name", "") or ""
    cat = service.get("category") or {}
    if isinstance(cat, dict):
        cat_str = cat.get("name") or cat.get("slug") or ""
    else:
        cat_str = str(cat or "")
    return f" {name} {cat_str} ".lower()


def _detect_service_type_ru(service: dict) -> str:
    """Возвращает русский тип услуги ("Просмотры", "Подписчики", …) из service.

    Сначала ищет по ключевым словам в name+category. Если ничего не подошло,
    пробует взять первое значащее слово из имени (с capitalize), исключая
    название платформы. Возвращает "Услуга" только если совсем ничего
    осмысленного не найдено.
    """
    hay = _service_haystack(service)
    for type_ru, kws in SERVICE_TYPE_KEYWORDS:
        for k in kws:
            if k in hay:
                return type_ru
    # Fallback: первое непустое слово из name, кроме названия платформы
    name = (service.get("name", "") or "").strip()
    # Берём подстроки между разделителями и тегами
    parts = re.split(r"[\s\-—|/,\[\]()]+", name)
    for w in parts:
        wl = w.strip(".,;!?:").lower()
        if not wl or len(wl) < 3:
            continue
        if wl in _PLATFORM_WORDS:
            continue
        # Не стоит возвращать чисто числовое
        if wl.isdigit():
            continue
        return w.strip(".,;!?:").capitalize()
    return "Услуга"


def _type_keywords_for_subcat(type_ru: str) -> list[str]:
    """Подстроки для поиска подкатегории FunPay по русскому типу услуги.

    Возвращает синонимы (ru+en), чтобы при матчинге FP-подкатегории «Twitch
    Просмотры стрима» наша услуга «Twitch — Просмотры» падала именно туда.
    """
    table = {
        "Подписчики": ["подписч", "follower", "subscriber", "саб", "подп"],
        "Просмотры": ["просмотр", "view", "показ"],
        "Зрители": ["зрител", "viewer", "live"],
        "Лайки": ["лайк", "like", "likes", "сердечк"],
        "Реакции": ["реакц", "react"],
        "Комментарии": ["коммент", "comment"],
        "Репосты": ["репост", "share", "ретвит", "retweet"],
        "Голоса": ["голос", "vote", "опрос"],
        "Сохранения": ["сохранен", "save", "bookmark"],
        "Истории": ["истор", "story", "stories"],
        "Старты бота": ["старт", "/start", "бот"],
        "Boost": ["буст", "boost", "premium"],
        "Друзья": ["друз", "friend"],
        "Подарки": ["подар", "gift", "звезд"],
        "Часы просмотра": ["часы", "watch hour", "hour", "час"],
        "Регистрации": ["регистрац", "signup"],
        "Установки": ["устан", "install"],
        "Клики": ["клик", "click"],
        "Скачивания": ["скачив", "download"],
        "Донаты": ["донат", "donation"],
        "Стримы": ["стрим", "stream"],
    }
    kws = table.get(type_ru)
    if kws is not None:
        return kws
    # Fallback: если тип не из таблицы — используем сам type_ru как подстроку
    # (например, "Drops" → ["drops"]). Это даёт шанс найти подкатегорию вида
    # "Twitch Drops" даже если мы не предусмотрели её в SERVICE_TYPE_KEYWORDS.
    if type_ru and type_ru != "Услуга":
        return [type_ru.lower()]
    return []


def _detect_features(service: dict) -> str:
    """Короткая строка о гарантии/рефилле/отмене — для ⚠️ {tags} ⚠️ в заголовке.

    Парсит ``service.refill``, ``service.cancel`` и подстроки вроде
    «Гарантия 30 дней», «Speed up to 1000/hour» из имени услуги smmway.
    Возвращает строку вида "♻ Рефилл • 🛡 30д • ⛔ Отмена".
    """
    parts: list[str] = []
    name = (service.get("name", "") or "") + " " + str(
        (service.get("category") or {}).get("name", "")
        if isinstance(service.get("category"), dict) else (service.get("category") or "")
    )
    # «Гарантия N (дн|дней|days|часов|hours)»
    m = re.search(
        r"гарант[а-я]*[\s:\-]*(\d+)\s*(дн|дней|день|days?|час|часов|hours?|hr)",
        name, re.IGNORECASE,
    )
    if not m:
        # Английский вариант: "guarantee 30 days"
        m = re.search(
            r"guarantee[\s:\-]*(\d+)\s*(day|days|hour|hours|hr)",
            name, re.IGNORECASE,
        )
    if m:
        n = m.group(1)
        unit = m.group(2).lower()
        unit_short = "д" if unit.startswith(("д", "day")) else "ч"
        parts.append(f"🛡 Гарантия {n}{unit_short}")
    if service.get("refill"):
        parts.append("♻ Рефилл")
    if service.get("cancel"):
        parts.append("⛔ Отмена")
    # «Скорость до N/час» / «Speed up to N/hour»
    m2 = re.search(
        r"скорость[\s:\-]*до\s*([\d\s.,]+)\s*[/\\]\s*(час|сутки|день|ч)",
        name, re.IGNORECASE,
    )
    if not m2:
        m2 = re.search(
            r"speed[\s:\-]+up\s+to\s+([\d\s.,]+)\s*[/\\]\s*(hour|day|hr)",
            name, re.IGNORECASE,
        )
    if m2:
        speed = re.sub(r"\s+", "", m2.group(1)).strip(".,")
        unit = m2.group(2).lower()
        unit_short = "ч" if unit.startswith(("час", "hour", "hr", "ч")) else "д"
        parts.append(f"⚡ до {speed}/{unit_short}")
    return " • ".join(parts)


def _detect_features_en(service: dict) -> str:
    """Английская версия :func:`_detect_features` для подстановки в EN-заголовки.

    FunPay валидирует, что в английском заголовке нет кириллицы (иначе бросает
    ``fields[summary][en]: Составление некорректного английского``). Поэтому
    для ``{tags_en}`` нужно отдельно собрать строку без русских букв.
    """
    parts: list[str] = []
    name = (service.get("name", "") or "") + " " + str(
        (service.get("category") or {}).get("name", "")
        if isinstance(service.get("category"), dict) else (service.get("category") or "")
    )
    m = re.search(
        r"гарант[а-я]*[\s:\-]*(\d+)\s*(дн|дней|день|days?|час|часов|hours?|hr)",
        name, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"guarantee[\s:\-]*(\d+)\s*(day|days|hour|hours|hr)",
            name, re.IGNORECASE,
        )
    if m:
        n = m.group(1)
        unit = m.group(2).lower()
        unit_short = "d" if unit.startswith(("д", "day")) else "h"
        parts.append(f"Guarantee {n}{unit_short}")
    if service.get("refill"):
        parts.append("Refill")
    if service.get("cancel"):
        parts.append("Cancel")
    m2 = re.search(
        r"скорость[\s:\-]*до\s*([\d\s.,]+)\s*[/\\]\s*(час|сутки|день|ч)",
        name, re.IGNORECASE,
    )
    if not m2:
        m2 = re.search(
            r"speed[\s:\-]+up\s+to\s+([\d\s.,]+)\s*[/\\]\s*(hour|day|hr)",
            name, re.IGNORECASE,
        )
    if m2:
        speed = re.sub(r"\s+", "", m2.group(1)).strip(".,")
        unit = m2.group(2).lower()
        unit_short = "h" if unit.startswith(("час", "hour", "hr", "ч")) else "d"
        parts.append(f"up to {speed}/{unit_short}")
    return " | ".join(parts)


_CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")


def _english_safe(text: str) -> bool:
    """``True`` если в строке нет кириллицы (можно класть в EN-заголовок)."""
    return not bool(_CYRILLIC_RE.search(text or ""))


def _sanitize_en(text: str, *, kind: str, service: dict | None = None) -> str:
    """Гарантирует, что в EN-строке нет кириллицы. Если есть — возвращает
    минимальный нейтральный английский fallback. ``kind`` — ``"title"``
    или ``"desc"``, выбирает форму fallback'а.

    Без этой защиты заголовок/описание со словами вроде «Дзен» или
    случайно затёкшим русским текстом из шаблона валится с ошибкой
    ``fields[summary][en]: Составление некорректного английского``.
    """
    if _english_safe(text):
        return text
    # Чистка: пробуем выкинуть кириллические токены
    cleaned = _CYRILLIC_RE.sub("", text or "")
    # Сжимаем повторяющиеся пробелы/разделители
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" |•—-")
    if cleaned and _english_safe(cleaned) and len(cleaned) >= 8:
        return cleaned
    # Fallback — генерим нейтральный текст
    if kind == "title":
        return "🌟 SMM 🌟 | ❤️ Service ❤️ | ✅FAST✅"
    return (
        "SMM service.\nStable, instant start.\n\n"
        "Send the link — the order will start automatically."
    )


def _category_tokens(service: dict) -> tuple[str, str, str, str]:
    """Возвращает ``(платформа, тип услуги, теги_ru, теги_en)`` для шаблона.

    - Платформа: "Twitch" / "Telegram" / "VK" / …
    - Тип услуги: "Просмотры" / "Подписчики" / … (см. SERVICE_TYPE_KEYWORDS)
    - Теги RU: либо текст из квадратных скобок в имени, либо короткая строка
      гарантии/рефилла/скорости (см. _detect_features). Используется в
      шаблоне между ⚠️…⚠️, чтобы заголовок был информативным.
    - Теги EN: то же, но строго без кириллицы — иначе FunPay отказывает
      EN-заголовок с ошибкой «Составление некорректного английского».
    """
    name = service.get("name", "")
    cat = service.get("category") or {}
    if isinstance(cat, dict):
        cat_slug = cat.get("slug") or ""
        cat_name = cat.get("name") or ""
    else:
        cat_slug = str(cat or "")
        cat_name = ""
    hay = f" {name} {cat_slug} {cat_name} ".lower()
    # Платформа: для каждой платформы — набор подстрок и КАНОНИЧЕСКОЕ
    # английское имя для EN-заголовка. Кириллицу в значение класть нельзя,
    # иначе FunPay отвергает поле fields[summary][en] / fields[desc][en] с
    # ошибкой «Составление некорректного английского описания».
    _platforms: list[tuple[str, list[str]]] = [
        ("Telegram", ["telegram", "телеграм"]),
        ("Instagram", ["instagram", "инстаграм", "инстаграмм"]),
        ("TikTok", ["tiktok", "тикток", "тик ток"]),
        ("YouTube", ["youtube", "ютуб", "ютюб"]),
        ("Twitch", ["twitch", "твич"]),
        ("Twitter", ["twitter", "твиттер", "twitter/x", "x.com", " x ", "smm-twitter"]),
        ("Rutube", ["rutube", "рутуб"]),
        ("Facebook", ["facebook", "фейсбук", "фэйсбук"]),
        ("Yandex Zen", ["дзен", "дзэн", "yandex zen", "yandex.dzen"]),
        ("VK", ["вконтакте", "вк ", "vk ", "vk-", " vk", "smm vk", "smm-vk", "vkontakte"]),
        ("MAX", ["мессенджер max", "max мессенджер", "max "]),
        ("Trovo", ["trovo"]),
        ("Kick", ["kick.com", "kick "]),
        ("Wibes", ["wibes"]),
        ("Discord", ["discord", "дискорд"]),
        ("Steam", ["steam", "стим"]),
        ("Likee", ["likee", "ликее"]),
        ("GitHub", ["github", "гитхаб"]),
    ]
    app = "SMM"
    for canonical, kws in _platforms:
        for kw in kws:
            if kw in hay:
                app = canonical
                break
        if app != "SMM":
            break
    # Тип услуги
    type_ = _detect_service_type_ru(service)
    # Теги RU: брекеты в имени → как есть, иначе — короткая фича-строка
    bracket_parts = re.findall(r"\[([^\]]+)\]", name)
    bracket_text = " | ".join(bracket_parts)
    tags_ru = bracket_text if bracket_parts else _detect_features(service)
    # Теги EN: брекеты используем только если в них нет кириллицы, иначе —
    # отдельная английская фича-строка. Так избегаем "incorrect English".
    if bracket_text and _english_safe(bracket_text):
        tags_en = bracket_text
    else:
        tags_en = _detect_features_en(service)
    return app, type_, tags_ru, tags_en


def _to_english_type(type_ru: str) -> str:
    en = {
        "Подписчики": "Followers", "Лайки": "Likes", "Реакции": "Reactions",
        "Просмотры": "Views", "Комментарии": "Comments", "Репосты": "Reposts",
        "Зрители": "Viewers", "Голоса": "Votes", "Сохранения": "Saves",
        "Истории": "Stories", "История": "Stories", "Старты бота": "Bot Starts",
        "Boost": "Boost", "Друзья": "Friends", "Подарки": "Gifts",
        "Часы просмотра": "Watch Hours", "Регистрации": "Sign-ups",
        "Установки": "Installs", "Клики": "Clicks", "Скачивания": "Downloads",
        "Донаты": "Donations", "Стримы": "Streams",
        "Услуга": "Service",
    }
    return en.get(type_ru, type_ru)


def render_lot(template: str, *, service: dict, lot: LotEntry, fp_price: float | None = None) -> str:
    app, type_ru, tags_ru, tags_en = _category_tokens(service)
    type_en = _to_english_type(type_ru)
    features = []
    if service.get("refill"):
        features.append("♻️ Поддержка рефилла")
    if service.get("cancel"):
        features.append("⛔ Возможна отмена")
    min_ = service.get("min", "")
    max_ = service.get("max", "")
    commands = (
        "Отправьте ссылку — заказ запустится автоматически.\n\n"
        "📋 Доступные команды в чате:\n"
        "!статус — подробная информация о заказе\n"
        "!рефилл — повторная накрутка (не на всех услугах)\n"
        "!отмена — отмена заказа (деньги вернутся, если SMM-платформа подтвердит)"
    )
    commands_en = (
        "Send the link — the order will start automatically.\n\n"
        "📋 Available chat commands:\n"
        "!статус — detailed order info\n"
        "!рефилл — re-order (not available for all services)\n"
        "!отмена — cancel order (refund only if confirmed by SMM platform)"
    )
    # Безопасный фоллбек, чтобы заголовок не становился неподтвержденно-английским:
    # если type_en сам по себе содержит кириллицу (например, fallback по первому
    # слову имени услуги) — заменяем на нейтральное "Service".
    if not _english_safe(type_en):
        type_en = "Service"
    ctx = {
        "app": app, "capp": app.upper(), "eapp": _to_english_type(app) if app in ("Услуга",) else app,
        "type": type_ru, "type_en": type_en, "ctype": type_ru.upper(), "ctype_en": type_en.upper(),
        "tags": tags_ru, "tags_en": tags_en,
        "ctags": tags_ru.upper(), "ctags_en": tags_en.upper(),
        "features": "\n".join(features) or "Стабильная услуга, моментальный старт.",
        "features_en": "Stable service, instant start.",
        "commands": commands, "commands_en": commands_en,
        "service_id": lot.service_id,
        "name": service.get("name", ""),
        "fp_field": "",
        "refill": "✓" if service.get("refill") else "✗",
        "cancel": "✓" if service.get("cancel") else "✗",
        "min": min_, "max": max_,
        "price": fp_price if fp_price is not None else "",
    }
    try:
        return template.format(**ctx)
    except KeyError as ex:
        logger.warning("template missing key %s, returning raw", ex)
        return template


# =============================================================================
# 6. PRICE / LOT MANAGEMENT
# =============================================================================


def compute_fp_price(service: dict, *, markup_pct: float, rate: float = 1.0,
                     min_price: float = 1.0) -> float:
    """Цена лота = (цена за 1 ед. * наценка%/100) + цена за 1 ед.

    Пример: rate_per_1000 = 2.7 → за 1 шт = 0.0027.
    Наценка 55% → 0.0027 * 55/100 = 0.001485 → 0.001485 + 0.0027 = 0.004185.

    Если лот не получается выставить — пробуем 0.001.
    Если и так не получается — ставим 1.
    """
    try:
        rate_per_1000 = float(service.get("rate") or service.get("price") or 0)
    except (TypeError, ValueError):
        rate_per_1000 = 0.0
    per_unit = rate_per_1000 / 1000.0
    markup_amount = per_unit * (markup_pct / 100.0)
    price = markup_amount + per_unit
    price = round(price, 6)
    # Если лот не получается выставить — пробуем минимум 0.001
    if price < 0.001:
        price = 0.001
    return price


# =============================================================================
# 7. CARDINAL CONTEXT (singleton за всё время жизни плагина)
# =============================================================================


class PluginContext:
    def __init__(self):
        self.cardinal: "Cardinal" | None = None
        self.storage: Storage | None = None
        self.api: SMMWayAPI | None = None
        self.buyer_state = BuyerState()
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        # Telegram menu state
        self.tg_state: dict[int, dict] = {}  # tg_user_id → state for multi-step input
        self.tg_state_lock = threading.Lock()
        # Принудительная остановка авто-создания лотов: пользователь жмёт
        # кнопку «⛔ Остановить» — событие выставляется, цикл в _run_autolots
        # завершается на следующей итерации.
        self.autolots_cancel = threading.Event()
        self.autolots_running = False

    def stop(self):
        self._stop_event.set()

    def is_running(self) -> bool:
        return not self._stop_event.is_set()


CTX = PluginContext()


# =============================================================================
# 8. NEW_ORDER HANDLER
# =============================================================================


def send_buyer_message(chat_id: int | str, text: str, buyer_username: str = "") -> None:
    """Отправляет сообщение покупателю в чат FunPay.

    chat_id = node_id чата FunPay. Может быть:
    - строка вида "users-XXXXX-YYYYY" (основной формат FPC)
    - целое число (старые версии FPC)
    - 0/None — тогда ищем по buyer_username

    Передаём node_id КАК ЕСТЬ (без int() преобразования!).
    """
    if CTX.cardinal is None:
        return
    node_id = chat_id

    # Если chat_id пуст — ищем по username
    if not node_id and buyer_username:
        node_id = _find_chat_node_by_username(buyer_username)
    if not node_id:
        logger.warning("send_buyer_message: no node_id (chat_id=%s, user=%s)", chat_id, buyer_username)
        return

    # Пробуем отправить — node_id передаём как есть (str или int)
    sent = False
    methods = []

    # Собираем доступные методы отправки
    if hasattr(CTX.cardinal, "send_message"):
        methods.append(("cardinal.send_message", CTX.cardinal.send_message))
    if hasattr(CTX.cardinal, "account") and hasattr(CTX.cardinal.account, "send_message"):
        methods.append(("account.send_message", CTX.cardinal.account.send_message))
    if hasattr(CTX.cardinal, "runner") and hasattr(CTX.cardinal.runner, "send_message"):
        methods.append(("runner.send_message", CTX.cardinal.runner.send_message))
    if hasattr(CTX.cardinal, "account") and hasattr(CTX.cardinal.account, "runner"):
        runner = CTX.cardinal.account.runner
        if hasattr(runner, "send_message"):
            methods.append(("account.runner.send_message", runner.send_message))

    for method_name, method in methods:
        if sent:
            break
        # Пробуем: (node_id, text), (node_id, text, username), (str), (int)
        for nid in (node_id, str(node_id)):
            if sent:
                break
            try:
                method(nid, text)
                sent = True
                logger.debug("sent via %s to %s", method_name, nid)
            except Exception:
                pass
            if not sent:
                try:
                    method(nid, text, buyer_username)
                    sent = True
                    logger.debug("sent via %s(3args) to %s", method_name, nid)
                except Exception:
                    pass

    if not sent:
        logger.error("send_buyer_message FAILED: node_id=%s, text=%s...", node_id, text[:40])
        notify_tg(f"⚠️ Не удалось отправить сообщение покупателю (node={node_id})")


def _find_chat_node_by_username(username: str):
    """Пытается найти node_id чата по имени пользователя. Возвращает str/int или None."""
    if not username or CTX.cardinal is None:
        return None
    # Способ 1: через get_chat_by_name
    if hasattr(CTX.cardinal, "account") and hasattr(CTX.cardinal.account, "get_chat_by_name"):
        try:
            chat = CTX.cardinal.account.get_chat_by_name(username)
            if chat:
                return getattr(chat, "id", None) or getattr(chat, "node_id", None)
        except Exception as ex:
            logger.debug("get_chat_by_name(%s) failed: %s", username, ex)
    # Способ 2: поиск в chats_list
    if hasattr(CTX.cardinal, "account") and hasattr(CTX.cardinal.account, "chats"):
        try:
            for chat in CTX.cardinal.account.chats.values():
                if getattr(chat, "name", "") == username or getattr(chat, "username", "") == username:
                    return getattr(chat, "id", None) or getattr(chat, "node_id", None)
        except Exception:
            pass
    return None


def notify_tg(text: str, parse_mode: str = "HTML") -> None:
    if CTX.cardinal is None or CTX.cardinal.telegram is None:
        return
    try:
        CTX.cardinal.telegram.send_notification(text)
    except Exception as ex:
        logger.warning("tg notify failed: %s", ex)


def on_new_order(c: "Cardinal", e: "NewOrderEvent", *args) -> None:
    if CTX.storage is None or not CTX.storage.cfg.get("enabled", True):
        return
    order = e.order
    try:
        # Пробуем найти лот: сначала по lot_id (если FPC передаёт), потом по описанию
        lot = None
        lot_id = getattr(order, "lot_id", None) or getattr(order, "subcategory_id", None)
        if lot_id and int(lot_id) in CTX.storage.lots:
            lot = CTX.storage.lots[int(lot_id)]
        if not lot:
            lot = CTX.storage.find_lot_by_title(getattr(order, "description", "") or "")
        if not lot or not lot.active:
            return
        service = CTX.api.find_service(lot.service_id) if CTX.api else None
        if not service:
            notify_tg(f"⚠️ Услуга #{lot.service_id} не найдена в каталоге, заказ {order.id} пропущен.")
            return

        # --- Получаем данные покупателя ---
        buyer_username = getattr(order, "buyer_username", "") or getattr(order, "buyer_name", "") or ""
        buyer_id = getattr(order, "buyer_id", 0) or getattr(order, "buyer_node_id", 0) or 0

        # В FPC объект order НЕ содержит chat_id напрямую.
        # node_id чата получаем: 1) из event, 2) из order, 3) ищем по username
        chat_id = (
            getattr(e, "chat_id", None)
            or getattr(order, "chat_id", None)
            or getattr(order, "node_id", None)
            or getattr(order, "buyer_chat_id", None)
            or 0
        )

        # Если chat_id не найден — ищем по buyer_username
        if not chat_id and buyer_username:
            chat_id = _find_chat_node_by_username(buyer_username) or 0

        # Последний fallback — buyer_id может быть node_id чата
        if not chat_id:
            chat_id = buyer_id

        if not chat_id:
            logger.error("on_new_order: cannot determine chat for order %s (buyer=%s)", 
                        getattr(order, "id", "?"), buyer_username)
            notify_tg(
                f"⚠️ SMMWay: заказ {getattr(order, 'id', '?')} — не удалось найти чат покупателя "
                f"({buyer_username}). Сообщение не отправлено."
            )
            return
        # Save order, request link from buyer.
        try:
            fp_price = float(getattr(order, "price", 0) or 0)
        except (TypeError, ValueError):
            fp_price = 0.0
        try:
            qty = int(order.amount or 1)
        except (TypeError, ValueError):
            qty = 1
        try:
            rate_per_1000 = float(service.get("rate") or service.get("price") or 0)
        except (TypeError, ValueError):
            rate_per_1000 = 0.0
        smmway_charge = round(rate_per_1000 * qty / 1000.0, 4)
        service_name = str(service.get("name") or "")
        entry = OrderEntry(
            funpay_order_id=order.id,
            funpay_lot_id=lot.funpay_lot_id,
            service_id=lot.service_id,
            buyer_username=buyer_username,
            buyer_id=buyer_id,
            chat_id=chat_id,
            quantity=qty,
            status="awaiting_link",
            funpay_price=fp_price,
            smmway_charge_rub=smmway_charge,
            service_name_snapshot=service_name,
        )
        CTX.storage.add_order(entry)
        # ask link
        msg = CTX.storage.templates["msg_await_link"]
        send_buyer_message(chat_id, msg, buyer_username)
        # Сохраняем state и по buyer_id, и по chat_id — при получении ответа
        # от покупателя в on_new_message ищем по обоим ключам
        CTX.buyer_state.set_awaiting_link(
            buyer_id, order.id, lot, entry.quantity, chat_id
        )
        if chat_id and chat_id != buyer_id:
            CTX.buyer_state.set_awaiting_link(
                chat_id, order.id, lot, entry.quantity, chat_id
            )
        if CTX.storage.cfg.get("notify_balance_before"):
            try:
                bal = CTX.api.balance()
                notify_tg(f"💰 SMMWay баланс до создания заказа: <code>{bal:.4f}</code> ₽")
            except Exception:
                pass
        logger.info("new order %s: chat_id=%s, buyer_id=%s, buyer=%s, service=%s",
                    order.id, chat_id, buyer_id, buyer_username, lot.service_id)
    except Exception:
        logger.exception("on_new_order failed for order %s", getattr(order, "id", "?"))


# =============================================================================
# 9. NEW_MESSAGE HANDLER (получаем ссылку от покупателя)
# =============================================================================


def on_new_message(c: "Cardinal", e: "NewMessageEvent", *args) -> None:
    if CTX.storage is None or not CTX.storage.cfg.get("enabled", True):
        return
    msg = e.message
    # Review detection (works on system messages "Покупатель X написал отзыв к заказу #..."):
    if _maybe_process_review(c, msg):
        return
    # Ignore own messages
    try:
        if msg.author_id == c.account.id:
            return
    except Exception:
        pass
    text = (msg.text or "").strip()
    if not text:
        return
    # Chat commands (with ! prefix)
    text_lower = text.lower()
    if text_lower in ("!статус", "!status"):
        _handle_status_command(c, msg)
        return
    if text_lower in ("!рефилл", "!refill"):
        _handle_refill_command(c, msg)
        return
    if text_lower in ("!отмена", "!cancel"):
        _handle_cancel_command(c, msg)
        return
    # status query? (backward compat without ! prefix)
    if text_lower in ("статус", "status", "/status", "/статус"):
        _handle_status_query(c, msg)
        return
    # waiting for link?
    buyer_id = getattr(msg, "author_id", None)
    chat_id = getattr(msg, "chat_id", None)
    if buyer_id is None and chat_id is None:
        return
    # Атомарно забираем state (pop) — если кто-то уже забрал, получим None.
    # Это предотвращает обработку второй ссылки от того же покупателя.
    state = None
    if buyer_id is not None:
        state = CTX.buyer_state.pop_awaiting_link(buyer_id)
    if not state and chat_id is not None:
        state = CTX.buyer_state.pop_awaiting_link(chat_id)
    if not state or state.get("type") != "awaiting_link":
        return
    # Дополнительно удаляем второй ключ (если state был по обоим)
    if state and buyer_id is not None:
        CTX.buyer_state.pop_awaiting_link(buyer_id)
    if state and chat_id is not None:
        CTX.buyer_state.pop_awaiting_link(chat_id)
    # Проверяем, что заказ в storage ещё ждёт ссылку (не обработан другим потоком)
    funpay_order_id = state.get("funpay_order_id")
    if funpay_order_id:
        order_entry = CTX.storage.orders.get(funpay_order_id)
        if order_entry and order_entry.status != "awaiting_link":
            # Заказ уже обработан — игнорируем повторную ссылку
            logger.debug("ignoring duplicate link for order %s (status=%s)",
                        funpay_order_id, order_entry.status)
            return
    link = parse_link_or_username(text)
    if not link:
        # State уже удалён, но ссылка не распознана — возвращаем state обратно
        CTX.buyer_state.set_awaiting_link(
            buyer_id or chat_id,
            state["funpay_order_id"],
            state["lot_entry"],
            state["quantity"],
            state["chat_id"],
        )
        return
    _process_smm_order(state, link)


def _maybe_process_review(c: "Cardinal", msg) -> bool:
    """Если сообщение — это отзыв покупателя (NEW_FEEDBACK / FEEDBACK_CHANGED) на наш заказ,
    обрабатывает его (даёт бонусную докрутку при достаточном рейтинге).

    Возвращает True, если сообщение опознано как отзыв (после этого on_new_message не делает ничего).
    """
    try:
        from FunPayAPI.common.enums import MessageTypes
    except Exception:
        return False
    try:
        msg_type = getattr(msg, "type", None)
    except Exception:
        return False
    if msg_type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
        return False
    if getattr(msg, "i_am_buyer", False):
        # на всякий — если "мы" это покупатель, не реагируем
        return True
    try:
        order = c.get_order_from_object(msg)
    except Exception:
        order = None
    if order is None:
        return True
    if not getattr(order, "review", None) or not getattr(order.review, "stars", None):
        return True
    stars = int(order.review.stars)
    # Найти OrderEntry по funpay_order_id
    fp_oid = getattr(order, "id", None) or ""
    o = CTX.storage.orders.get(fp_oid)
    if o is None:
        # Заказ не наш — не обрабатываем
        logger.info("review on non-tracked order %s (%d★)", fp_oid, stars)
        return True
    CTX.storage.update_order(fp_oid, review_stars=stars)
    logger.info("review for order %s: %d stars", fp_oid, stars)
    # Бонусная докрутка
    _maybe_place_review_bonus(o, stars)
    return True


def _maybe_place_review_bonus(o: OrderEntry, stars: int) -> None:
    """Если включена бонус-докрутка за положительный отзыв — создаёт допзаказ на smmway."""
    if not CTX.storage.cfg.get("auto_review_bonus_enabled", True):
        return
    min_stars = int(CTX.storage.cfg.get("auto_review_bonus_min_stars", 5))
    if stars < min_stars:
        return
    if o.bonus_smm_order_id:
        logger.info("bonus already placed for order %s (smm #%s)", o.funpay_order_id, o.bonus_smm_order_id)
        return
    if not o.link or not o.service_id or not o.quantity:
        logger.info("cannot place bonus for %s: missing link/service/qty", o.funpay_order_id)
        return
    pct = float(CTX.storage.cfg.get("auto_review_bonus_pct", 10.0))
    bonus_qty = max(1, int(round(o.quantity * pct / 100.0)))
    # Проверим, что услуга это «количественная»: min/max в каталоге дают границы
    s = CTX.api.find_service(o.service_id) if CTX.api else None
    if s:
        try:
            smin = int(float(s.get("min") or 1))
            smax = int(float(s.get("max") or 0))
        except (TypeError, ValueError):
            smin, smax = 1, 0
        if smin and bonus_qty < smin:
            bonus_qty = smin
        if smax and bonus_qty > smax:
            bonus_qty = smax
    try:
        smm_id = CTX.api.add_order(o.service_id, o.link, bonus_qty)
        CTX.storage.update_order(
            o.funpay_order_id,
            bonus_smm_order_id=smm_id,
            bonus_quantity=bonus_qty,
        )
        notify_tg(
            f"🎁 SMMWay бонус-докрутка за <b>{stars}★</b> отзыв\n"
            f"FP: <code>#{o.funpay_order_id}</code>\n"
            f"Основной SMM: <code>#{o.smm_order_id}</code>\n"
            f"Бонус SMM: <code>#{smm_id}</code>\n"
            f"Объём: <code>{bonus_qty}</code> ({pct}%)\n"
            f"Ссылка: <code>{html_escape(o.link)}</code>"
        )
        send_buyer_message(
            o.chat_id,
            f"🎁 Спасибо за отзыв! Запустили бонусную докрутку (+{bonus_qty}) на тот же заказ.",
            o.buyer_username,
        )
    except SMMWayError as ex:
        logger.warning("review bonus failed for %s: %s", o.funpay_order_id, ex)
        notify_tg(
            f"⚠️ Не удалось запустить бонус-докрутку за отзыв\n"
            f"FP: <code>#{o.funpay_order_id}</code>\n"
            f"Причина: <code>{html_escape(str(ex))}</code>"
        )


def _handle_status_command(c: "Cardinal", msg) -> None:
    """Handle !статус / !status - show detailed order info."""
    buyer_id = getattr(msg, "author_id", None)
    chat_id = getattr(msg, "chat_id", None)
    if buyer_id is None or chat_id is None:
        return
    # Find latest order for this buyer
    found = None
    for o in CTX.storage.orders.values():
        if o.buyer_id == buyer_id:
            if found is None or o.created_at > found.created_at:
                found = o
    if not found:
        send_buyer_message(chat_id, "У вас пока нет заказов.", getattr(msg, "author", ""))
        return
    if not found.smm_order_id:
        send_buyer_message(
            chat_id,
            f"Заказ ещё не запущен.\nСтатус: {found.status}",
            getattr(msg, "author", ""),
        )
        return
    # Get fresh status from SMM API
    try:
        status_data = CTX.api.order_status(found.smm_order_id)
    except Exception as ex:
        logger.warning("status command API error: %s", ex)
        send_buyer_message(
            chat_id,
            f"📊 Статус заказа: {found.smm_status_raw or found.status}\n"
            f"(не удалось получить актуальные данные)",
            getattr(msg, "author", ""),
        )
        return
    status = str(status_data.get("status", found.smm_status_raw or found.status))
    start_count = status_data.get("start_count", "?")
    remains = status_data.get("remains", "?")
    # Estimate time remaining
    time_info = ""
    try:
        from datetime import datetime as _dt
        created = _dt.fromisoformat(found.created_at.replace("Z", "+00:00"))
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
        elapsed_sec = (now - created).total_seconds()
        if isinstance(remains, (int, float)) and isinstance(start_count, (int, float)):
            done = int(start_count) + found.quantity - int(remains)
            if done > 0 and int(remains) > 0:
                rate_per_sec = done / max(elapsed_sec, 1)
                eta_sec = int(int(remains) / rate_per_sec)
                if eta_sec < 3600:
                    time_info = f"\n⏱ Ориентировочно осталось: ~{eta_sec // 60} мин."
                else:
                    time_info = f"\n⏱ Ориентировочно осталось: ~{eta_sec // 3600} ч. {(eta_sec % 3600) // 60} мин."
    except Exception:
        pass
    reply = (
        f"📊 Подробный статус заказа\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 Статус: {status}\n"
        f"📐 Начальный счётчик: {start_count}\n"
        f"📉 Осталось: {remains}\n"
        f"📦 Заказано: {found.quantity}"
        f"{time_info}"
    )
    send_buyer_message(chat_id, reply, getattr(msg, "author", ""))


def _handle_refill_command(c: "Cardinal", msg) -> None:
    """Handle !рефилл / !refill - request re-order for completed service."""
    buyer_id = getattr(msg, "author_id", None)
    chat_id = getattr(msg, "chat_id", None)
    if buyer_id is None or chat_id is None:
        return
    # Find latest completed order for this buyer
    found = None
    for o in CTX.storage.orders.values():
        if o.buyer_id == buyer_id and o.status == "completed" and o.smm_order_id:
            if found is None or o.created_at > found.created_at:
                found = o
    if not found:
        send_buyer_message(chat_id, "Не найден завершённый заказ для рефилла.", getattr(msg, "author", ""))
        return
    # Check if service supports refill
    service = CTX.api.find_service(found.service_id) if CTX.api else None
    if not service or not service.get("refill"):
        send_buyer_message(
            chat_id,
            "К сожалению, эта услуга не поддерживает рефилл (повторную накрутку).",
            getattr(msg, "author", ""),
        )
        return
    # Call refill API
    try:
        result = CTX.api.refill(found.smm_order_id)
        refill_id = result.get("refill") or result.get("result") or ""
        send_buyer_message(
            chat_id,
            f"♻️ Рефилл запрошен!\n"
            f"Повторная накрутка будет запущена автоматически.",
            getattr(msg, "author", ""),
        )
        notify_tg(
            f"♻️ Рефилл по запросу покупателя\n"
            f"FP: <code>#{found.funpay_order_id}</code>\n"
            f"SMM: <code>#{found.smm_order_id}</code>\n"
            f"Refill ID: <code>{refill_id}</code>"
        )
    except SMMWayError as ex:
        logger.warning("refill command failed for order %s: %s", found.smm_order_id, ex)
        send_buyer_message(
            chat_id,
            f"⚠️ Не удалось запросить рефилл: {ex}",
            getattr(msg, "author", ""),
        )


def _handle_cancel_command(c: "Cardinal", msg) -> None:
    """Handle !отмена / !cancel - cancel active order."""
    buyer_id = getattr(msg, "author_id", None)
    chat_id = getattr(msg, "chat_id", None)
    if buyer_id is None or chat_id is None:
        return
    # Find latest active (non-completed) order for this buyer
    found = None
    for o in CTX.storage.orders.values():
        if o.buyer_id == buyer_id and o.smm_order_id:
            if o.status == "completed":
                continue
            if found is None or o.created_at > found.created_at:
                found = o
    if not found:
        # Check if latest order is completed
        latest = None
        for o in CTX.storage.orders.values():
            if o.buyer_id == buyer_id:
                if latest is None or o.created_at > latest.created_at:
                    latest = o
        if latest and latest.status == "completed":
            send_buyer_message(
                chat_id,
                "Завершённые заказы не могут быть отменены.",
                getattr(msg, "author", ""),
            )
        else:
            send_buyer_message(chat_id, "Не найден активный заказ для отмены.", getattr(msg, "author", ""))
        return
    if found.status == "completed":
        send_buyer_message(
            chat_id,
            "Завершённые заказы не могут быть отменены.",
            getattr(msg, "author", ""),
        )
        return
    # Refresh status before cancel to avoid cancelling completed orders
    try:
        fresh_status = CTX.api.order_status(found.smm_order_id)
        fresh_raw = (fresh_status.get("status") or "").lower()
        if fresh_raw in ("completed", "complete", "completed_partial"):
            CTX.storage.update_order(found.funpay_order_id, status="completed", smm_status_raw=fresh_raw)
            send_buyer_message(chat_id, "Завершённые заказы не могут быть отменены.", getattr(msg, "author", ""))
            return
    except Exception:
        pass  # proceed with cancel attempt even if status check fails
    # Call cancel API
    try:
        result = CTX.api.cancel(found.smm_order_id)
        # Check if cancellation was successful
        cancel_status = result.get("status") or result.get("cancel") or ""
        # Determine if refund happened on SMM side
        refunded = False
        # Check for explicit refund confirmation from API
        if result.get("refund"):
            refunded = True
        elif isinstance(cancel_status, str) and cancel_status.lower() in ("cancelled", "canceled"):
            refunded = True
        status_msg = f"✅ Заказ отменён."
        if refunded:
            status_msg += "\n💰 Средства возвращены."
            # Attempt FunPay refund
            try:
                if hasattr(c, "account") and hasattr(c.account, "refund"):
                    c.account.refund(found.funpay_order_id)
                    status_msg += "\n💸 Возврат на FunPay инициирован."
            except Exception as refund_ex:
                logger.warning("FunPay refund failed for %s: %s", found.funpay_order_id, refund_ex)
                status_msg += "\n⚠️ Автоматический возврат на FunPay не удался, обратитесь к продавцу."
        else:
            status_msg += "\n⚠️ Платформа не подтвердила возврат средств."
        CTX.storage.update_order(found.funpay_order_id, status="refunded" if refunded else "error",
                                 smm_status_raw="Cancelled")
        send_buyer_message(chat_id, status_msg, getattr(msg, "author", ""))
        notify_tg(
            f"⛔ Отмена заказа по запросу покупателя\n"
            f"FP: <code>#{found.funpay_order_id}</code>\n"
            f"SMM: <code>#{found.smm_order_id}</code>\n"
            f"Возврат: {'Да' if refunded else 'Нет'}"
        )
    except SMMWayError as ex:
        logger.warning("cancel command failed for order %s: %s", found.smm_order_id, ex)
        send_buyer_message(
            chat_id,
            f"⚠️ Не удалось отменить заказ: {ex}",
            getattr(msg, "author", ""),
        )


def _handle_status_query(c: "Cardinal", msg) -> None:
    buyer_id = getattr(msg, "author_id", None)
    chat_id = getattr(msg, "chat_id", None)
    if buyer_id is None or chat_id is None:
        return
    # Find latest order for this buyer
    found = None
    for o in CTX.storage.orders.values():
        if o.buyer_id == buyer_id:
            if found is None or o.created_at > found.created_at:
                found = o
    if not found or not found.smm_order_id:
        return
    status_text = found.smm_status_raw or found.status
    template = CTX.storage.templates["msg_status_reply"]
    try:
        reply = template.format(smm_id=found.smm_order_id, status=status_text)
    except KeyError:
        reply = template.format(status=status_text)
    send_buyer_message(chat_id, reply, getattr(msg, "author", ""))


def _format_order_started_notification(
    *,
    funpay_order_id: str,
    smm_id: int,
    lot: LotEntry,
    quantity: int,
    link: str,
    funpay_price: float,
    smmway_charge_rub: float,
    service_name: str,
    buyer_username: str,
) -> str:
    """Красивое HTML-уведомление о запуске заказа в Telegram.

    Содержит всё, что просил юзер: сколько заплатил покупатель на FunPay,
    сколько мы платим smmway, чистая прибыль (₽ и %), ссылка для накрутки,
    SMM-id, FP-id, объём, услуга, ник покупателя.
    """
    profit = funpay_price - smmway_charge_rub
    profit_pct = (profit / funpay_price * 100.0) if funpay_price > 0 else 0.0
    margin_emoji = "💎" if profit_pct >= 50 else ("✨" if profit_pct >= 20 else "📈" if profit > 0 else "⚠️")
    profit_sign = "+" if profit >= 0 else "−"
    profit_abs = abs(profit)
    # Имя услуги: максимум ~60 символов, чтобы не растягивать сообщение.
    short_service = (service_name or "—").strip()
    if len(short_service) > 60:
        short_service = short_service[:57] + "…"
    # Ссылку отдаём как кликабельную, но текст обрезаем для аккуратности.
    link_html = html_escape(link or "—")
    if link and len(link) > 60:
        link_label = html_escape(link[:57] + "…")
    else:
        link_label = link_html
    buyer = html_escape(buyer_username or "—") if buyer_username else "—"
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 <b>Запуск заказа</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Покупатель:</b> <code>{buyer}</code>\n"
        f"🧾 <b>FunPay-заказ:</b> <a href=\"https://funpay.com/orders/{html_escape(funpay_order_id)}/\">"
        f"#{html_escape(funpay_order_id)}</a>\n"
        f"🛒 <b>Услуга:</b> {html_escape(short_service)} <i>(#{lot.service_id})</i>\n"
        f"📦 <b>Лот FP:</b> <code>#{lot.funpay_lot_id}</code>\n"
        f"🔢 <b>Объём:</b> <code>{quantity}</code> шт.\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>Финансы</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 На FunPay заплатили: <code>{funpay_price:.2f} ₽</code>\n"
        f"💸 Нам на smmway:        <code>{smmway_charge_rub:.4f} ₽</code>\n"
        f"{margin_emoji} <b>Чистая прибыль:</b> "
        f"<code>{profit_sign}{profit_abs:.4f} ₽</code> "
        f"<i>({profit_sign}{abs(profit_pct):.1f}%)</i>\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>Запуск бота</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>SMMWay-заказ:</b> <code>#{smm_id}</code>\n"
        f"🔗 <b>Цель:</b> <a href=\"{link_html}\">{link_label}</a>\n"
    )


# Категории услуг, которые требуют дополнительных параметров при заказе.
# Ключ — подстрока в имени/категории услуги (lower), значение — dict доп. полей.
# Некоторые SMM-платформы требуют, например, "runs" для Twitch viewers (сколько
# минут стрима), "delay" для отложенных заказов и т.д.
_SERVICE_EXTRA_PARAMS: list[tuple[list[str], dict[str, str]]] = [
    # Twitch viewers (зрители стрима) — обычно требует "runs" (минуты просмотра)
    (["twitch", "зрител", "viewer", "live viewer"], {"runs": "60"}),
    # Twitch chat — иногда требует "runs"
    (["twitch", "чат", "chat"], {"runs": "30"}),
]


def _build_extra_params(service: dict, link: str, quantity: int) -> dict | None:
    """Определяет дополнительные параметры для заказа на SMM-платформе.

    Некоторые услуги (Twitch viewers, стримы) требуют доп. полей (runs, delay и т.д.).
    Возвращает dict с доп. параметрами или None, если они не нужны.
    """
    if not service:
        return None
    hay = _service_haystack(service)
    for keywords, extra in _SERVICE_EXTRA_PARAMS:
        # Все ключевые слова группы должны присутствовать
        if all(kw in hay for kw in keywords):
            return extra.copy()
    return None


def _process_smm_order(state: dict, link: str) -> None:
    lot: LotEntry = state["lot_entry"]
    quantity: int = state["quantity"]
    chat_id = state["chat_id"]
    funpay_order_id = state["funpay_order_id"]

    # Финальная проверка: заказ ещё ждёт ссылку? (защита от дублей)
    order_entry = CTX.storage.orders.get(funpay_order_id)
    if order_entry and order_entry.status != "awaiting_link":
        logger.warning("_process_smm_order: order %s already in status '%s', skipping duplicate",
                       funpay_order_id, order_entry.status)
        return

    # --- Dynamic Workflows: pre-order checks ---
    if CTX.storage.cfg.get("dynamic_workflows_enabled", True):
        # Soft-block: если услуга временно заблокирована — пропускаем без Timer
        if DW.should_soft_block(lot.service_id):
            logger.info("DW: service %s soft-blocked, skipping order %s", lot.service_id, funpay_order_id)
            # Не ставим Timer — просто ждём и пробуем
            time.sleep(5)
            # Проверяем ещё раз — если всё ещё заблокирована, уведомляем
            if DW.should_soft_block(lot.service_id):
                send_buyer_message(chat_id, "⏳ Услуга временно недоступна. Повторите позже или дождитесь автоматического выполнения.")
                return
        # Anti-flood: короткая задержка
        delay = DW.get_queue_delay(lot.service_id)
        if delay > 0:
            time.sleep(min(delay, 10))  # Не более 10 сек

    # --- Dynamic Workflows: loyalty bonus ---
    bonus_qty = 0
    buyer_username_for_loyalty = ""
    if order_entry:
        buyer_username_for_loyalty = order_entry.buyer_username
    if CTX.storage.cfg.get("dynamic_workflows_enabled", True) and buyer_username_for_loyalty:
        DW.record_buyer(buyer_username_for_loyalty)
        bonus_qty = DW.get_loyalty_bonus(buyer_username_for_loyalty, quantity)

    actual_quantity = quantity + bonus_qty

    try:
        # Определяем доп. параметры для услуги (Twitch viewers/стримы и т.д.)
        service = CTX.api.find_service(lot.service_id) if CTX.api else None
        extra = _build_extra_params(service, link, actual_quantity) if service else None
        # Отмечаем отправку (anti-flood)
        DW.mark_order_sent(lot.service_id)
        smm_id = CTX.api.add_order(lot.service_id, link, actual_quantity, extra=extra)
        # Записываем успех в DW
        DW.record_order_result(lot.service_id, success=True)
        CTX.storage.update_order(
            funpay_order_id,
            smm_order_id=smm_id,
            link=link,
            status="created",
        )
        CTX.storage.stats["sent"] += 1
        # Точнее посчитаем smmway-цену по актуальному каталогу — на случай если
        # ставка изменилась между on_new_order и моментом, когда покупатель прислал
        # ссылку (это могут быть часы).
        order_entry = CTX.storage.orders.get(funpay_order_id)
        funpay_price = order_entry.funpay_price if order_entry else 0.0
        service_name = order_entry.service_name_snapshot if order_entry else ""
        smmway_charge = order_entry.smmway_charge_rub if order_entry else 0.0
        buyer_username = order_entry.buyer_username if order_entry else ""
        try:
            svc = CTX.api.find_service(lot.service_id)
            if svc:
                try:
                    rate_per_1000 = float(svc.get("rate") or svc.get("price") or 0)
                    smmway_charge = round(rate_per_1000 * int(quantity) / 1000.0, 4)
                    if not service_name:
                        service_name = str(svc.get("name") or "")
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
        CTX.storage.update_order(
            funpay_order_id,
            smmway_charge_rub=smmway_charge,
            service_name_snapshot=service_name,
        )
        CTX.storage.stats["spent_rub"] = round(
            float(CTX.storage.stats.get("spent_rub", 0.0)) + smmway_charge, 4
        )
        try:
            msg = CTX.storage.templates["msg_order_created"].format(
                smm_id=smm_id, qty=actual_quantity
            )
        except KeyError:
            msg = CTX.storage.templates["msg_order_created"].format(qty=actual_quantity)
        # Добавляем инфо о бонусе для повторного покупателя
        if bonus_qty > 0:
            msg += f"\n🎁 Бонус за повторную покупку: +{bonus_qty} ед."
        send_buyer_message(chat_id, msg)
        if CTX.storage.cfg.get("notify_order_created"):
            notify_tg(_format_order_started_notification(
                funpay_order_id=funpay_order_id,
                smm_id=smm_id,
                lot=lot,
                quantity=quantity,
                link=link,
                funpay_price=funpay_price,
                smmway_charge_rub=smmway_charge,
                service_name=service_name,
                buyer_username=buyer_username,
            ))
        if CTX.storage.cfg.get("notify_balance_after"):
            try:
                bal = CTX.api.balance()
                notify_tg(f"💰 SMMWay баланс после создания: <code>{bal:.4f}</code> ₽")
            except Exception:
                pass
    except SMMWayError as ex:
        # --- Dynamic Workflows: записываем неудачу ---
        DW.record_order_result(lot.service_id, success=False)
        # --- Авто-повтор при ошибке ---
        if CTX.storage.cfg.get("auto_retry_on_error", True):
            retry_result = _retry_order_on_error(lot, link, quantity, funpay_order_id, chat_id, ex)
            if retry_result:
                # Повтор удался — выходим
                return
        # Повтор не помог или выключен — стандартная обработка ошибки
        CTX.storage.update_order(funpay_order_id, status="error", error=str(ex))
        CTX.storage.stats["failed"] += 1
        err_msg = CTX.storage.templates["msg_order_error"].format(reason="Услуга временно недоступна")
        send_buyer_message(chat_id, err_msg)
        if CTX.storage.cfg.get("notify_order_error"):
            notify_tg(
                f"❌ Ошибка заказа\n"
                f"FP: <code>#{funpay_order_id}</code>\n"
                f"Услуга: <code>{lot.service_id}</code>\n"
                f"Ссылка: <code>{html_escape(link)}</code>\n"
                f"Причина: <code>{html_escape(str(ex))}</code>"
            )
        # Возврат денег покупателю
        try:
            CTX.cardinal.account.refund(funpay_order_id)
            notify_tg(f"💸 Возврат FP-заказа <code>#{funpay_order_id}</code> выполнен.")
        except Exception as rex:
            logger.warning("refund failed for %s: %s", funpay_order_id, rex)
        # Блокируем услугу и заменяем лот
        if CTX.storage.cfg.get("auto_retry_on_error", True):
            _blacklist_and_replace_lot(lot)


def _retry_order_on_error(lot: LotEntry, link: str, quantity: int,
                           funpay_order_id: str, chat_id, original_error) -> bool:
    """Пробует повторить заказ после ошибки.

    1. Проверяет валидность ссылки (формат)
    2. Проверяет баланс на SMM-платформе
    3. Пробует заказ ещё раз

    Возвращает True если повтор удался.
    """
    max_attempts = int(CTX.storage.cfg.get("auto_retry_max_attempts", 2))

    # Проверка 1: ссылка валидна?
    if not link or not (link.startswith("http") or re.match(r"^[A-Za-z0-9_.]{3,}$", link)):
        logger.info("retry: link invalid, skipping retry: %s", link)
        return False

    # Проверка 2: баланс достаточен?
    try:
        service = CTX.api.find_service(lot.service_id)
        if service:
            rate_per_1000 = float(service.get("rate") or service.get("price") or 0)
            needed = rate_per_1000 * quantity / 1000.0
            balance = CTX.api.balance()
            if balance < needed:
                logger.info("retry: balance %.4f < needed %.4f, skipping", balance, needed)
                notify_tg(f"⚠️ Повтор заказа невозможен: баланс ({balance:.4f}) меньше стоимости ({needed:.4f})")
                return False
    except Exception as ex:
        logger.warning("retry: balance check failed: %s", ex)

    # Проверка 3: услуга не в чёрном списке?
    blacklist = CTX.storage.cfg.get("blacklisted_services", [])
    if lot.service_id in blacklist:
        logger.info("retry: service %s is blacklisted, skipping", lot.service_id)
        return False

    # Пробуем повторить
    for attempt in range(1, max_attempts + 1):
        try:
            time.sleep(2 * attempt)  # пауза перед повтором
            service = CTX.api.find_service(lot.service_id) if CTX.api else None
            extra = _build_extra_params(service, link, quantity) if service else None
            smm_id = CTX.api.add_order(lot.service_id, link, quantity, extra=extra)
            # Успех!
            CTX.storage.update_order(
                funpay_order_id,
                smm_order_id=smm_id,
                link=link,
                status="created",
            )
            CTX.storage.stats["sent"] += 1
            try:
                msg = CTX.storage.templates["msg_order_created"].format(smm_id=smm_id, qty=quantity)
            except KeyError:
                msg = CTX.storage.templates["msg_order_created"].format(qty=quantity)
            send_buyer_message(chat_id, msg)
            notify_tg(f"✅ Повтор заказа #{funpay_order_id} удался с попытки {attempt}")
            return True
        except SMMWayError as ex:
            logger.warning("retry attempt %d/%d failed for order %s: %s",
                          attempt, max_attempts, funpay_order_id, ex)
            continue

    # Все попытки исчерпаны
    return False


def _blacklist_and_replace_lot(lot: LotEntry) -> None:
    """Блокирует услугу (добавляет в чёрный список), деактивирует лот и пытается заменить."""
    service_id = lot.service_id

    # Добавляем в чёрный список
    blacklist = CTX.storage.cfg.get("blacklisted_services", [])
    if service_id not in blacklist:
        blacklist.append(service_id)
        CTX.storage.cfg["blacklisted_services"] = blacklist
        CTX.storage.save_config()
        logger.info("service %s added to blacklist", service_id)

    # Деактивируем лот
    lot.active = False
    CTX.storage.save_lots()

    # Пробуем заменить другой услугой (если функция потеряшка включена)
    if CTX.storage.cfg.get("auto_replace_missing_service"):
        try:
            services_map = {str(s.get("service")): s for s in CTX.api.services()}
            # Исключаем заблокированные
            for bl_id in blacklist:
                services_map.pop(str(bl_id), None)
            replacement = _pick_replacement_service(lot, services_map)
            if replacement:
                success = _replace_lot_inplace(lot, replacement)
                if success:
                    notify_tg(
                        f"🔄 Услуга <code>#{service_id}</code> заблокирована.\n"
                        f"Лот заменён на услугу <code>#{replacement.get('service')}</code>."
                    )
                    return
        except Exception as ex:
            logger.warning("blacklist replace failed: %s", ex)

    # Замена не удалась — удаляем лот полностью
    try:
        CTX.storage.unbind_lot(lot.funpay_lot_id)
        # Пробуем деактивировать на FunPay
        if hasattr(CTX.cardinal, "account") and hasattr(CTX.cardinal.account, "get_lot_fields"):
            try:
                fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
                fields.active = False
                CTX.cardinal.account.save_lot(fields)
            except Exception:
                pass
        notify_tg(
            f"🗑 Услуга <code>#{service_id}</code> заблокирована.\n"
            f"Замена не найдена — лот <code>#{lot.funpay_lot_id}</code> удалён."
        )
    except Exception as ex:
        logger.warning("lot deletion failed: %s", ex)
        notify_tg(
            f"⛔ Услуга <code>#{service_id}</code> заблокирована.\n"
            f"Лот деактивирован, удалить не удалось."
        )


# =============================================================================
# 10. DYNAMIC WORKFLOWS ENGINE
# =============================================================================


class DynamicWorkflows:
    """Адаптивная система управления заказами.

    Анализирует паттерны:
    - Скорость выполнения каждой услуги
    - Процент ошибок по услугам
    - Повторные покупатели (лояльность)
    - Нагрузка (сколько заказов в очереди)

    На основе данных автоматически:
    - Регулирует приоритеты услуг (быстрые - выше)
    - Временно приостанавливает проблемные услуги (soft blacklist)
    - Выдаёт бонусы повторным покупателям
    - Управляет задержками между заказами (anti-flood)
    """

    def __init__(self):
        self._service_stats: dict[int, dict] = {}  # service_id → stats
        self._buyer_history: dict[str, dict] = {}  # buyer_username → history
        self._soft_blacklist: dict[int, float] = {}  # service_id → unblock_ts
        self._order_queue: list[dict] = []
        self._last_order_ts: dict[int, float] = {}  # service_id → last order timestamp
        self._lock = threading.RLock()

    def record_order_result(self, service_id: int, success: bool, duration_sec: float = 0.0):
        """Записывает результат заказа для аналитики."""
        with self._lock:
            if service_id not in self._service_stats:
                self._service_stats[service_id] = {
                    "total": 0, "success": 0, "failed": 0,
                    "fail_streak": 0, "avg_duration": 0.0,
                    "last_success_ts": 0.0, "last_fail_ts": 0.0,
                }
            st = self._service_stats[service_id]
            st["total"] += 1
            if success:
                st["success"] += 1
                st["fail_streak"] = 0
                st["last_success_ts"] = time.time()
                # Скользящее среднее длительности
                if duration_sec > 0:
                    old_avg = st["avg_duration"]
                    st["avg_duration"] = old_avg * 0.7 + duration_sec * 0.3 if old_avg else duration_sec
            else:
                st["failed"] += 1
                st["fail_streak"] += 1
                st["last_fail_ts"] = time.time()

    def record_buyer(self, buyer_username: str):
        """Записывает покупку для системы лояльности."""
        with self._lock:
            if buyer_username not in self._buyer_history:
                self._buyer_history[buyer_username] = {"orders": 0, "first_ts": time.time()}
            self._buyer_history[buyer_username]["orders"] += 1

    def get_buyer_order_count(self, buyer_username: str) -> int:
        """Возвращает количество заказов покупателя."""
        with self._lock:
            h = self._buyer_history.get(buyer_username)
            return h["orders"] if h else 0

    def get_loyalty_bonus(self, buyer_username: str, quantity: int) -> int:
        """Рассчитывает бонусный объём для повторного покупателя."""
        if not CTX.storage or not CTX.storage.cfg.get("loyalty_enabled", True):
            return 0
        min_orders = int(CTX.storage.cfg.get("loyalty_min_orders", 2))
        bonus_pct = float(CTX.storage.cfg.get("loyalty_bonus_pct", 5.0))
        count = self.get_buyer_order_count(buyer_username)
        if count >= min_orders:
            bonus = int(quantity * bonus_pct / 100.0)
            return max(bonus, 1) if bonus_pct > 0 else 0
        return 0

    def should_soft_block(self, service_id: int) -> bool:
        """Проверяет, нужно ли временно заблокировать услугу (слишком много ошибок подряд)."""
        if not CTX.storage or not CTX.storage.cfg.get("dynamic_workflows_enabled", True):
            return False
        limit = int(CTX.storage.cfg.get("dw_fail_streak_limit", 3))
        with self._lock:
            st = self._service_stats.get(service_id)
            if not st:
                return False
            if st["fail_streak"] >= limit:
                # Soft block на 30 минут
                self._soft_blacklist[service_id] = time.time() + 1800
                return True
            # Проверяем текущий soft blacklist
            unblock_ts = self._soft_blacklist.get(service_id, 0)
            if unblock_ts > time.time():
                return True
            elif unblock_ts > 0:
                # Время вышло — разблокируем
                self._soft_blacklist.pop(service_id, None)
            return False

    def get_queue_delay(self, service_id: int) -> float:
        """Возвращает задержку перед следующим заказом (anti-flood)."""
        if not CTX.storage:
            return 0.0
        base_delay = float(CTX.storage.cfg.get("smart_queue_delay_sec", 5))
        with self._lock:
            last_ts = self._last_order_ts.get(service_id, 0)
            elapsed = time.time() - last_ts
            if elapsed < base_delay:
                return base_delay - elapsed
            return 0.0

    def mark_order_sent(self, service_id: int):
        """Отмечает что заказ на услугу отправлен (для anti-flood)."""
        with self._lock:
            self._last_order_ts[service_id] = time.time()

    def get_service_health_score(self, service_id: int) -> float:
        """Возвращает 'здоровье' услуги 0.0-1.0 (1.0 = отлично)."""
        with self._lock:
            st = self._service_stats.get(service_id)
            if not st or st["total"] == 0:
                return 1.0  # нет данных = считаем ок
            success_rate = st["success"] / max(st["total"], 1)
            # Штраф за текущую серию ошибок
            streak_penalty = min(st["fail_streak"] * 0.15, 0.5)
            # Штраф за медленность
            slow_threshold = float(CTX.storage.cfg.get("dw_slow_threshold_sec", 3600)) if CTX.storage else 3600
            speed_penalty = 0.0
            if st["avg_duration"] > slow_threshold:
                speed_penalty = min((st["avg_duration"] - slow_threshold) / slow_threshold * 0.3, 0.3)
            score = success_rate - streak_penalty - speed_penalty
            return max(0.0, min(1.0, score))

    def get_analytics_summary(self) -> dict:
        """Возвращает сводку аналитики для TG-меню."""
        with self._lock:
            total_services = len(self._service_stats)
            total_buyers = len(self._buyer_history)
            soft_blocked = sum(1 for ts in self._soft_blacklist.values() if ts > time.time())
            # Топ услуг по здоровью
            scored = [(sid, self.get_service_health_score(sid)) for sid in self._service_stats]
            scored.sort(key=lambda x: x[1], reverse=True)
            top_healthy = scored[:5]
            top_unhealthy = [x for x in scored if x[1] < 0.5][:5]
            # Повторные покупатели
            loyal = sum(1 for h in self._buyer_history.values()
                       if h["orders"] >= int(CTX.storage.cfg.get("loyalty_min_orders", 2)))
            return {
                "total_services_tracked": total_services,
                "total_buyers": total_buyers,
                "soft_blocked": soft_blocked,
                "loyal_buyers": loyal,
                "top_healthy": top_healthy,
                "top_unhealthy": top_unhealthy,
            }

    def reset_service_stats(self, service_id: int):
        """Сбрасывает статистику услуги."""
        with self._lock:
            self._service_stats.pop(service_id, None)
            self._soft_blacklist.pop(service_id, None)


# Глобальный экземпляр Dynamic Workflows
DW = DynamicWorkflows()

# Трекинг услуг в процессе авто-восстановления: service_id -> timestamp начала
_recovering_services: dict[int, float] = {}


# =============================================================================
# 11. STATUS POLLER / AUTO PRICE / AUTO DEACTIVATE
# =============================================================================


def status_poller_loop() -> None:
    while CTX.is_running():
        try:
            time.sleep(max(15, CTX.storage.cfg.get("status_poll_interval_sec", 90)))
            if CTX.storage is None or CTX.api is None:
                continue
            active_orders = [o for o in CTX.storage.orders.values()
                             if o.smm_order_id and o.status in ("created", "in_progress")]
            if not active_orders:
                continue
            ids = [o.smm_order_id for o in active_orders]
            resp = CTX.api.orders_status(ids)
            # Response shape: {"order_id": {"charge","start_count","status","remains","currency"}}
            for o in active_orders:
                key = str(o.smm_order_id)
                data = resp.get(key) if isinstance(resp, dict) else None
                if not data:
                    continue
                status_raw = (data.get("status") or "").lower()
                if not status_raw:
                    continue
                if status_raw in ("completed", "complete", "completed_partial"):
                    CTX.storage.update_order(o.funpay_order_id, status="completed",
                                             smm_status_raw=status_raw,
                                             last_check_at=_now_iso())
                    CTX.storage.stats["completed"] += 1
                    # Dynamic Workflows: записываем длительность выполнения
                    try:
                        from datetime import datetime as _dt
                        created = _dt.fromisoformat(o.created_at.replace("Z", "+00:00"))
                        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
                        duration = (now - created).total_seconds()
                        DW.record_order_result(o.service_id, success=True, duration_sec=duration)
                    except Exception:
                        DW.record_order_result(o.service_id, success=True)
                    send_buyer_message(o.chat_id,
                                       CTX.storage.templates["msg_order_completed"],
                                       o.buyer_username)
                elif status_raw in ("canceled", "cancelled", "partial"):
                    CTX.storage.update_order(o.funpay_order_id, status="error",
                                             smm_status_raw=status_raw,
                                             last_check_at=_now_iso(),
                                             error=status_raw)
                else:
                    CTX.storage.update_order(o.funpay_order_id, status="in_progress",
                                             smm_status_raw=status_raw,
                                             last_check_at=_now_iso())
        except Exception:
            logger.exception("status poller iteration failed")


def auto_price_loop() -> None:
    while CTX.is_running():
        try:
            time.sleep(max(30, CTX.storage.cfg.get("auto_price_interval_sec", 120)))
            if not CTX.storage.cfg.get("auto_price_enabled"):
                continue
            update_all_prices(force=False)
        except Exception:
            logger.exception("auto price loop iteration failed")


def update_all_prices(force: bool = False) -> tuple[int, int]:
    """Возвращает (updated, total)."""
    if CTX.cardinal is None or CTX.api is None or CTX.storage is None:
        return 0, 0
    updated = 0
    total = len(CTX.storage.lots)
    services = {str(s.get("service")): s for s in CTX.api.services()}
    global_markup = CTX.storage.cfg.get("global_markup_pct", 55.0)
    rate = CTX.storage.cfg.get("currency_rate_rub_to_fp", 1.0)
    min_price = CTX.storage.cfg.get("min_lot_price", 1.0)
    jump_cap = CTX.storage.cfg.get("auto_price_jump_cap_pct", 200.0) / 100.0
    for lot in list(CTX.storage.lots.values()):
        if not lot.active:
            continue
        s = services.get(str(lot.service_id))
        if not s:
            continue
        markup = lot.markup_pct if lot.markup_pct is not None else global_markup
        new_price = compute_fp_price(s, markup_pct=markup, rate=rate, min_price=min_price)
        if lot.last_price_fp:
            ratio = new_price / max(lot.last_price_fp, 0.0001)
            if ratio > 1 + jump_cap or ratio < (1 / (1 + jump_cap)):
                logger.warning("skip price jump for lot %s: %.4f → %.4f",
                               lot.funpay_lot_id, lot.last_price_fp, new_price)
                continue
        try:
            fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
            fields.price = new_price
            CTX.cardinal.account.save_lot(fields)
            lot.last_price_fp = new_price
            updated += 1
            time.sleep(1.5)  # rate limit FP
        except Exception as ex:
            # Если не получилось выставить — пробуем цену 1
            logger.warning("price update failed for lot %s (price=%.6f): %s — retrying with price=1",
                           lot.funpay_lot_id, new_price, ex)
            try:
                fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
                fields.price = 1.0
                CTX.cardinal.account.save_lot(fields)
                lot.last_price_fp = 1.0
                updated += 1
                time.sleep(1.5)
            except Exception as ex2:
                logger.warning("price update failed for lot %s even with price=1: %s",
                               lot.funpay_lot_id, ex2)
    if updated:
        CTX.storage.save_lots()
    return updated, total


def _pick_replacement_service(old_lot: LotEntry, services_map: dict[str, dict]) -> dict | None:
    """Подбирает случайную услугу smmway для замены пропавшей.

    Критерии:
      * услуга не привязана ни к одному другому FunPay-лоту в storage;
      * платформа совпадает с пропавшей (чтобы новая услуга гарантированно
        подходила к той же FunPay-подкатегории — у разных платформ свои
        подкатегории и поменять «по-горячему» не получится);
      * желательно тот же тип услуги (Подписчики/Зрители/…), но если
        подходящих по типу нет — берём любую с совпадающей платформой.

    Возвращает dict-услугу smmway или None, если кандидата нет.
    """
    # Чтобы знать «бывшую» платформу/тип услуги, нам нужны те же данные,
    # что были у пропавшей. У нас в storage хранится только service_id —
    # извлечь платформу можно из title_ru (если он информативен) или
    # сделать наоборот: подобрать ЛЮБУЮ услугу той же подкатегории на FunPay.
    # Самое надёжное — взять fields текущего лота и сравнить node_id.
    if not services_map:
        return None
    bound_ids = {str(l.service_id) for l in CTX.storage.lots.values()
                 if l.funpay_lot_id != old_lot.funpay_lot_id}
    # Определяем платформу/тип старой услуги через subcategory лота, если
    # она у нас сохранена. Если subcategory_id не сохранён — берём первую
    # подходящую по платформе свободную услугу (через сравнение
    # find_subcategory_candidates).
    old_subcat = old_lot.subcategory_id
    candidates: list[dict] = []
    for sid, srv in services_map.items():
        if sid in bound_ids:
            continue
        if old_subcat is None:
            candidates.append(srv)
            continue
        try:
            subs = find_subcategory_candidates(srv)
        except Exception:
            continue
        if any(int(getattr(s, "id", -1)) == int(old_subcat) for s in subs[:3]):
            candidates.append(srv)
    if not candidates:
        return None
    return random.choice(candidates)


def _replace_lot_inplace(lot: LotEntry, new_service: dict) -> bool:
    """Перерисовывает FunPay-лот ``lot`` под новую услугу ``new_service``.

    Делает edit_fields с новыми title/desc/price, оставляя лот активным.
    Сохраняет новый service_id в storage. Возвращает True при успехе.
    """
    try:
        fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
    except Exception as ex:
        logger.warning("replace lot %s: get_lot_fields failed: %s", lot.funpay_lot_id, ex)
        return False
    new_sid = int(new_service.get("service") or new_service.get("id") or 0)
    if not new_sid:
        return False
    fake_lot = LotEntry(funpay_lot_id=lot.funpay_lot_id, service_id=new_sid)
    title_ru = render_lot(CTX.storage.templates["lot_ru_title"], service=new_service, lot=fake_lot)
    title_en = render_lot(CTX.storage.templates["lot_en_title"], service=new_service, lot=fake_lot)
    desc_ru = render_lot(CTX.storage.templates["lot_ru_desc"], service=new_service, lot=fake_lot)
    desc_en = render_lot(CTX.storage.templates["lot_en_desc"], service=new_service, lot=fake_lot)
    title_en = _sanitize_en(title_en, kind="title", service=new_service)
    desc_en = _sanitize_en(desc_en, kind="desc", service=new_service)
    markup = lot.markup_pct if lot.markup_pct is not None else CTX.storage.cfg.get("global_markup_pct", 55.0)
    rate = CTX.storage.cfg.get("currency_rate_rub_to_fp", 1.0)
    min_price = CTX.storage.cfg.get("min_lot_price", 1.0)
    price = compute_fp_price(new_service, markup_pct=markup, rate=rate, min_price=min_price)
    patch = {
        "fields[summary][ru]": title_ru[:80],
        "fields[summary][en]": title_en[:80],
        "fields[desc][ru]": desc_ru,
        "fields[desc][en]": desc_en,
        "price": f"{price:.4f}",
        "active": "on",
        "amount": "999999",
    }
    try:
        if hasattr(fields, "edit_fields"):
            fields.edit_fields(patch)
        else:
            fields.fields.update(patch)
        fields.active = True
        fields.price = price
        CTX.cardinal.account.save_lot(fields)
    except Exception as ex:
        # Если не получилось — пробуем цену 1
        logger.warning("replace lot %s: save_lot failed (price=%.6f): %s — retrying with price=1",
                       lot.funpay_lot_id, price, ex)
        try:
            price = 1.0
            patch["price"] = f"{price:.4f}"
            if hasattr(fields, "edit_fields"):
                fields.edit_fields(patch)
            else:
                fields.fields.update(patch)
            fields.price = price
            CTX.cardinal.account.save_lot(fields)
        except Exception as ex2:
            logger.warning("replace lot %s: save_lot failed even with price=1: %s",
                           lot.funpay_lot_id, ex2)
            return False
    # Обновляем привязку в storage
    CTX.storage.unbind_lot(lot.funpay_lot_id)
    new_lot = LotEntry(
        funpay_lot_id=lot.funpay_lot_id,
        service_id=new_sid,
        title_ru=title_ru[:80],
        title_en=title_en[:80],
        subcategory_id=lot.subcategory_id,
        markup_pct=lot.markup_pct,
        last_price_fp=price,
        custom_template=lot.custom_template,
        active=True,
        note=lot.note,
    )
    CTX.storage.lots[lot.funpay_lot_id] = new_lot
    CTX.storage.save_lots()
    return True


def auto_deactivate_loop() -> None:
    while CTX.is_running():
        try:
            time.sleep(180)
            if not CTX.storage.cfg.get("auto_deactivate_enabled"):
                continue
            min_bal = float(CTX.storage.cfg.get("auto_deactivate_min_balance", 0.0))
            try:
                bal = CTX.api.balance()
            except Exception:
                continue
            services = {str(s.get("service")): s for s in CTX.api.services()}
            replace_enabled = bool(CTX.storage.cfg.get("auto_replace_missing_service"))
            for lot in list(CTX.storage.lots.values()):
                service_missing = str(lot.service_id) not in services
                target_active = True
                if bal <= min_bal:
                    target_active = False
                elif service_missing:
                    target_active = False
                # ── Замена пропавшей услуги ──
                # Если услуга пропала со smmway и включено auto_replace —
                # пытаемся подобрать рандомную свободную услугу той же
                # платформы и переписать лот in-place вместо деактивации.
                if service_missing and replace_enabled and lot.active:
                    repl = _pick_replacement_service(lot, services)
                    if repl is not None:
                        new_sid = int(repl.get("service") or repl.get("id") or 0)
                        if _replace_lot_inplace(lot, repl):
                            notify_tg(
                                f"🔄 Заменил услугу в лоте <code>#{lot.funpay_lot_id}</code>: "
                                f"<code>{lot.service_id}</code> → <code>{new_sid}</code> "
                                f"(старая пропала со smmway). Лот остался активным."
                            )
                            time.sleep(1.5)
                            continue  # дальше переходим к следующему лоту
                        else:
                            logger.info(
                                "replace failed for lot %s, will just deactivate",
                                lot.funpay_lot_id,
                            )
                if target_active != lot.active:
                    try:
                        fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
                        fields.active = target_active
                        if hasattr(fields, "edit_fields"):
                            fields.edit_fields({"active": "on" if target_active else ""})
                        CTX.cardinal.account.save_lot(fields)
                        lot.active = target_active
                        notify_tg(
                            f"{'🟢 Активировал' if target_active else '🔴 Деактивировал'} лот "
                            f"<code>#{lot.funpay_lot_id}</code> (услуга {lot.service_id})"
                            + (" — услуга пропала со smmway, замены не нашлось"
                               if service_missing and replace_enabled else "")
                        )
                        time.sleep(1.5)
                    except Exception as ex:
                        logger.warning("activate/deactivate failed for %s: %s", lot.funpay_lot_id, ex)
            CTX.storage.save_lots()
        except Exception:
            logger.exception("auto deactivate iteration failed")


def service_recovery_loop() -> None:
    """Background loop: monitors service health, pauses unhealthy services, auto-recovers after cooldown."""
    while CTX.is_running():
        try:
            interval = max(60, CTX.storage.cfg.get("service_recovery_check_interval_sec", 300))
            time.sleep(interval)
            if not CTX.storage.cfg.get("service_recovery_enabled", True):
                continue
            min_rate = float(CTX.storage.cfg.get("service_recovery_min_success_rate", 0.6))
            cooldown = float(CTX.storage.cfg.get("service_recovery_cooldown_sec", 1800))
            now = time.time()

            # Phase 1: Check active services for health issues
            for lot in list(CTX.storage.lots.values()):
                if not lot.active:
                    continue
                if lot.service_id in _recovering_services:
                    continue
                score = DW.get_service_health_score(lot.service_id)
                if score < min_rate:
                    try:
                        fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
                        fields.active = False
                        if hasattr(fields, "edit_fields"):
                            fields.edit_fields({"active": ""})
                        CTX.cardinal.account.save_lot(fields)
                        lot.active = False
                        _recovering_services[lot.service_id] = now
                        notify_tg(
                            f"🛡 <b>Стабилизатор:</b> услуга <code>#{lot.service_id}</code> "
                            f"приостановлена (здоровье {score:.0%} < {min_rate:.0%}). "
                            f"Авто-восстановление через {int(cooldown // 60)} мин."
                        )
                        logger.info("recovery: paused service %s (health %.2f)", lot.service_id, score)
                    except Exception as ex:
                        logger.warning("recovery: failed to pause lot %s: %s", lot.funpay_lot_id, ex)

            # Phase 2: Check recovering services for cooldown expiry
            if not CTX.storage.cfg.get("service_recovery_auto_reenable", True):
                continue
            expired = [sid for sid, ts in _recovering_services.items() if now - ts >= cooldown]
            for sid in expired:
                lot = None
                for l in CTX.storage.lots.values():
                    if l.service_id == sid and not l.active:
                        lot = l
                        break
                if lot is None:
                    _recovering_services.pop(sid, None)
                    continue
                try:
                    fields = CTX.cardinal.account.get_lot_fields(lot.funpay_lot_id)
                    fields.active = True
                    if hasattr(fields, "edit_fields"):
                        fields.edit_fields({"active": "on"})
                    CTX.cardinal.account.save_lot(fields)
                    lot.active = True
                    DW.reset_service_stats(sid)
                    _recovering_services.pop(sid, None)
                    notify_tg(
                        f"🛡 <b>Стабилизатор:</b> услуга <code>#{sid}</code> "
                        f"восстановлена и снова активна ✅"
                    )
                    logger.info("recovery: re-enabled service %s after cooldown", sid)
                except Exception as ex:
                    logger.warning("recovery: failed to re-enable lot %s: %s", lot.funpay_lot_id, ex)
                    _recovering_services.pop(sid, None)
        except Exception:
            logger.exception("service recovery loop iteration failed")


def state_cleanup_loop() -> None:
    while CTX.is_running():
        time.sleep(60)
        try:
            CTX.buyer_state.cleanup(CTX.storage.cfg.get("max_buyer_link_wait_sec", 1800))
        except Exception:
            pass


# =============================================================================
# 11. AUTO LOTS CREATION
# =============================================================================


def find_template_lot(subcategory_id: int) -> int | None:
    """Возвращает FP lot_id, подходящий как шаблон для подкатегории.

    Используется только при принудительном режиме `tpl:<id>` в команде автолотов
    и при автоматическом выборе подкатегории как "tie-break". Создание новых
    лотов больше НЕ требует существующего шаблона — см. get_lot_fields_by_node.
    """
    if CTX.cardinal is None:
        return None
    try:
        my_lots = CTX.cardinal.account.get_my_subcategory_lots(subcategory_id)
    except Exception:
        return None
    if not my_lots:
        return None
    return int(my_lots[0].id)


def get_lot_fields_by_node(
    subcategory_id: int,
) -> tuple[Any, str, dict[str, list[tuple[str, str]]]]:
    """Возвращает ``(LotFields | None, reason, select_options)`` для формы.

    Делает GET ``/lots/offerEdit?node={subcategory_id}`` и парсит форму так же,
    как ``FunPayAPI.account.Account.get_lot_fields``. Нужно, чтобы создавать
    автолоты "с нуля" — без существующего лота-шаблона в этой подкатегории.

    ``select_options`` — словарь ``{<имя select>: [(value, text_lowercase), ...]}``
    со ВСЕМИ вариантами каждого <select> формы (включая первую плейсхолдер-опцию
    с пустым value). Используется в :func:`create_lot_from_service`, чтобы
    подобрать option для поля «Тип услуги» по тексту, а не по индексу.

    Если форму получить не удалось, в ``reason`` будет человеко-читаемая
    причина (текст FunPay-ошибки, HTTP-код и т.п.) — её показываем юзеру.
    """
    if CTX.cardinal is None:
        return None, "no cardinal", {}
    acc = CTX.cardinal.account
    try:
        from bs4 import BeautifulSoup
        from FunPayAPI import types as fp_types
        from FunPayAPI.common.enums import SubCategoryTypes
        from FunPayAPI.common.utils import parse_currency
    except Exception as ex:
        logger.error("get_lot_fields_by_node: import error: %s", ex)
        return None, f"import: {ex}", {}
    try:
        resp = acc.method(
            "get", f"lots/offerEdit?node={subcategory_id}", {}, {}, raise_not_200=True,
        )
    except Exception as ex:
        logger.warning("get_lot_fields_by_node(%s): HTTP error: %s", subcategory_id, ex)
        return None, f"HTTP: {str(ex)[:200]}", {}
    html_text = resp.content.decode("utf-8", errors="replace")
    bs = BeautifulSoup(html_text, "lxml")
    form = bs.find("form", class_="form-offer-editor")
    if form is None:
        # FunPay показывает <p class="lead"> с человеко-читаемой ошибкой, если
        # пользователю нельзя постить в этой подкатегории (требуется верификация,
        # бан, лимит лотов, неактивная подкатегория и т.п.).
        lead = bs.find("p", class_="lead")
        lead_text = (lead.get_text(strip=True) if lead else "")[:200]
        title = (bs.find("title").get_text(strip=True) if bs.find("title") else "")[:120]
        login_form = bs.find("form", class_="form-login") is not None
        logger.warning(
            "get_lot_fields_by_node(%s): no form-offer-editor (title=%r, lead=%r, login=%s)",
            subcategory_id, title, lead_text, login_form,
        )
        if login_form:
            reason = "FunPay требует авторизацию (сессия истекла)"
        elif lead_text:
            reason = lead_text
        elif title:
            reason = f"FunPay вернул страницу «{title}» (нет формы создания лота)"
        else:
            reason = "FunPay не вернул форму создания лота"
        return None, reason, {}
    result: dict[str, str] = {}
    for field in form.find_all("input"):
        name = field.get("name")
        if not name or name == "query":
            continue
        classes = field.get("class") or []
        # FunPay часто использует «radio-box» виджет: скрытый <input>, который
        # заполняется JS при клике по соседней <button>. В исходном HTML такой
        # input имеет class="lot-field-input radio-box-value hidden" и value="".
        # Чтобы поле не отправилось пустым (FunPay вернёт «Заполните это поле»),
        # подбираем value первой <button> с непустым value (placeholder вида
        # «Все» обычно value="" — он пропускается).
        if "radio-box-value" in classes:
            parent = field.find_parent(class_="lot-field-radio-box") or field.parent
            chosen = ""
            if parent is not None:
                for btn in parent.find_all("button"):
                    v = btn.get("value", "") or ""
                    if v.strip():
                        chosen = v
                        break
            result[name] = chosen
            continue
        result[name] = field.get("value") or ""
    for field in form.find_all("textarea"):
        name = field.get("name")
        if not name:
            continue
        result[name] = field.text or ""
    select_options: dict[str, list[tuple[str, str]]] = {}
    for field in form.find_all("select"):
        name = field.get("name")
        if not name:
            continue
        parent = field.find_parent(class_="form-group")
        if parent and "hidden" in (parent.get("class") or []):
            continue
        # Запомним ВСЕ опции (включая placeholder "") для последующего
        # текстового мэтчинга в create_lot_from_service.
        opts: list[tuple[str, str]] = []
        for opt in field.find_all("option"):
            v = opt.get("value", "") or ""
            t = (opt.get_text(strip=True) or "").lower()
            opts.append((v, t))
        select_options[name] = opts
        # 1. Если есть option с selected — берём её.
        sel = field.find("option", selected=True)
        value = sel.get("value") if sel is not None else None
        # 2. Если selected нет или там пустая строка (placeholder вида
        #    <option value="">Выберите ...</option>) — берём первую option
        #    с непустым value. Это нужно для обязательных полей типа
        #    fields[game], fields[server], fields[side], которые FunPay
        #    проверяет на пустоту и иначе отвечает «Заполните это поле».
        if not value:
            for opt in field.find_all("option"):
                v = opt.get("value", "")
                if v:
                    value = v
                    break
        result[name] = value or ""
    # Чекбоксы. Группируем по имени — у FunPay могут быть как одиночные
    # чекбоксы (флажок "автоматическая выдача"), так и группы вида
    # ``fields[reactions][]`` (выбор реакций для VK-постов, типов покупателей
    # для аккаунтов и т.п.).
    #   1. Все pre-checked чекбоксы сохраняем (старое поведение).
    #   2. Для групп, где НИ ОДИН пункт не отмечен И поле помечено как
    #      обязательное (class "required" в form-group / label, либо знак
    #      "*"), отмечаем ПЕРВЫЙ чекбокс в группе. Иначе FunPay вернёт
    #      «fields[reactions]: Заполните это поле».
    checkbox_groups: dict[str, list[Any]] = {}
    for field in form.find_all("input", {"type": "checkbox"}):
        name = field.get("name")
        if not name:
            continue
        # Скрытые form-group не трогаем
        parent = field.find_parent(class_="form-group")
        if parent and "hidden" in (parent.get("class") or []):
            continue
        checkbox_groups.setdefault(name, []).append(field)
        if field.has_attr("checked"):
            # сохраняем как и раньше — "on" перезатрётся таким же значением
            result[name] = "on"

    def _is_required_group(items: list[Any]) -> bool:
        """``True`` если рядом с группой чекбоксов есть индикатор обязательности.
        Смотрим form-group родителя и его label/span на класс ``required`` или
        текстовую звёздочку — стандартная разметка FunPay для обязательных полей.
        """
        for it in items:
            parent = it.find_parent(class_="form-group")
            if parent is None:
                continue
            classes = " ".join(parent.get("class") or [])
            if "required" in classes:
                return True
            label = parent.find("label")
            if label is not None:
                lcls = " ".join(label.get("class") or [])
                if "required" in lcls:
                    return True
                if label.find(class_="required") is not None:
                    return True
                if "*" in (label.get_text() or ""):
                    return True
            if parent.find(class_="required") is not None:
                return True
        return False

    for name, items in checkbox_groups.items():
        if name in result and result[name]:
            continue  # уже что-то выбрано
        if not _is_required_group(items):
            continue
        # Берём первый чекбокс группы с непустым value (или "on")
        first = items[0]
        val = first.get("value") or "on"
        result[name] = val
        logger.info(
            "form: required checkbox group %r — авто-чек первого пункта (%r)",
            name, val,
        )
    # Обязательные служебные поля
    result.setdefault("node_id", str(subcategory_id))
    result["offer_id"] = "0"  # новый лот
    result.setdefault("deleted", "")
    result.setdefault("price", "")
    result.setdefault("amount", "")
    result.setdefault("active", "on")
    # Валюта (символ слева/справа от поля цены)
    currency = fp_types.Currency.UNKNOWN
    span_curr = form.find("span", class_="form-control-feedback")
    if span_curr and span_curr.text:
        try:
            currency = parse_currency(span_curr.text)
        except Exception:
            currency = fp_types.Currency.UNKNOWN
    # CSRF-токен — обновим, если форма дала свежий
    if result.get("csrf_token"):
        try:
            acc.csrf_token = result["csrf_token"]
        except Exception:
            pass
    # Объект SubCategory (если доступен)
    sub = None
    try:
        sub = acc.get_subcategory(SubCategoryTypes.COMMON, int(subcategory_id))
    except Exception:
        sub = None
    # Диагностика: какие select-поля заполнились, какие остались пустыми
    select_status = {
        k: v for k, v in result.items()
        if k.startswith("fields[") and k.endswith("]") and k not in (
            "fields[summary][ru]", "fields[summary][en]",
            "fields[desc][ru]", "fields[desc][en]",
            "fields[payment_msg][ru]", "fields[payment_msg][en]",
        )
    }
    logger.info(
        "get_lot_fields_by_node(%s): subcat-specific fields = %s",
        subcategory_id, select_status,
    )
    return fp_types.LotFields(0, result, sub, currency, None, None), "", select_options


# Маппинг платформы smmway → ключевые слова в названиях подкатегорий FunPay.
# Сначала идут более специфичные ключи, чтобы не схватить "VK Видео" вместо "VK"
# и т.п. Каждое значение — список альтернатив; совпадение хотя бы одного достаточно.
PLATFORM_KEYWORDS: list[tuple[str, list[str]]] = [
    ("telegram", ["telegram", "телеграм"]),
    ("instagram", ["instagram", "инстаграм", "инстаграмм"]),
    ("tiktok", ["tiktok", "тикток", "тик ток"]),
    ("youtube", ["youtube", "ютуб", "ютюб"]),
    ("twitch", ["twitch", "твич"]),
    # "x "/" x" без обоих пробелов ловили чужие названия вроде "CarX" и
    # "Roblox" — поэтому для Twitter оставляем только строго
    # граниченные подстроки (или явно "twitter"/"твиттер").
    ("twitter", ["twitter", "твиттер", "twitter/x", "x.com", " x "]),
    ("rutube", ["rutube", "рутуб"]),
    ("facebook", ["facebook", "фейсбук", "фэйсбук"]),
    ("dzen", ["дзен", "дзэн", "yandex дзен", "yandex.dzen", "yandex zen"]),
    # VK: добавляем больше написаний, потому что smmway-каталог иногда
    # называет VK-услуги "Smm-vk", "vk-...". Без этих ключей платформа
    # не определялась и услуга падала в дефолт "SMM" / другую подкатегорию.
    ("vk", ["вконтакте", "вк ", "vk ", "vk-", " vk", "smm vk", "smm-vk", "vkontakte"]),
    ("max", ["мессенджер max", "max мессенджер", "max "]),
    ("trovo", ["trovo"]),
    ("kick", ["kick.com", "kick "]),
    ("wibes", ["wibes"]),
    ("discord", ["discord", "дискорд"]),
    ("steam", ["steam", "стим"]),
    ("likee", ["likee", "ликее"]),
]


def detect_platform(service: dict) -> str | None:
    """Возвращает platform-ключ ('telegram', 'instagram', ...) из услуги smmway, либо None.

    Сматчиваем по имени услуги, slug-у И name-у её smmway-категории —
    раньше category.name не учитывался, и услуги вида ``{"name":"Subscribers",
    "category":{"slug":"smm-vk","name":"VK"}}`` падали в None.
    """
    name = (service.get("name") or "").lower()
    cat = service.get("category")
    if isinstance(cat, dict):
        cat_parts = [(cat.get("slug") or ""), (cat.get("name") or "")]
        cat_str = " ".join(p for p in cat_parts if p).lower()
    else:
        cat_str = (cat or "").lower()
    haystack = f" {name} {cat_str} "
    for key, words in PLATFORM_KEYWORDS:
        for w in words:
            if w in haystack:
                return key
    return None


def _platform_name_keywords(platform: str) -> list[str]:
    """Возвращает поисковые подстроки для подкатегорий FP по ключу платформы."""
    keys = {
        "telegram": ["telegram", "телеграм"],
        "instagram": ["instagram", "инстаграм"],
        "tiktok": ["tiktok", "тикток", "тик ток"],
        "youtube": ["youtube", "ютуб"],
        "twitch": ["twitch", "твич"],
        # НЕ используем "x "/" x" — они ловили подкатегории типа
        # "CarX Drift Racing" / "Roblox" и плагин клал в них Twitter-услуги.
        # Строго граниченная версия " x " (с пробелами с обеих сторон)
        # безопасна, тк hay паддится пробелами и сливные слова вроде "carx"
        # или "roblox" не содержат " x " в форме с двумя пробелами.
        "twitter": ["twitter", "твиттер", " x ", "x.com", "twitter/x"],
        "rutube": ["rutube", "рутуб"],
        "facebook": ["facebook", "фейсбук"],
        "dzen": ["дзен", "дзэн", "yandex"],
        "vk": ["вконтакте", "вк ", "vk ", "vk-", " vk", "smm vk", "smm-vk", "vkontakte"],
        "max": ["max "],
        "trovo": ["trovo"],
        "kick": ["kick"],
        "wibes": ["wibes"],
        "discord": ["discord", "дискорд"],
        "steam": ["steam"],
        "likee": ["likee"],
    }
    return keys.get(platform, [])


def find_subcategory_for_platform(platform: str):
    """Ищет FP-подкатегорию (SubCategory) по платформе. Возвращает SubCategory или None.

    Логика:
      1. Берём все подкатегории текущего FP-аккаунта.
      2. Фильтруем по ключевым словам платформы (в name + category.name).
      3. Если кандидатов больше одного — приоритет подкатегории, где у пользователя
         уже есть лоты (можем взять шаблон).
      4. Если совпадений нет — None.
    """
    if CTX.cardinal is None:
        return None
    try:
        subs = list(CTX.cardinal.account.subcategories)
    except Exception:
        return None
    kws = _platform_name_keywords(platform)
    if not kws:
        return None
    candidates = []
    for sub in subs:
        try:
            name = (sub.name or "").lower()
            cat_name = (sub.category.name or "").lower() if sub.category else ""
        except Exception:
            continue
        hay = f" {name} {cat_name} "
        if any(k in hay for k in kws):
            # Только COMMON-подкатегории (обычные лоты, не chips)
            if getattr(sub, "is_common", True):
                candidates.append(sub)
    if not candidates:
        return None
    # Выбираем ту, где у юзера уже есть лоты
    for sub in candidates:
        try:
            lots = CTX.cardinal.account.get_my_subcategory_lots(sub.id)
            if lots:
                return sub
        except Exception:
            continue
    return candidates[0]


# SMM-индикаторы в имени подкатегории FunPay: если есть хоть один — это
# точно подкатегория для накрутки, можно постить туда smmway-услуги.
_SMM_INDICATORS = ("услуги", "накрутк", "smm", "продвижен", "реклам", "промо")

# Анти-индикаторы: имена, в которых ТОЧНО НЕ нужно постить SMM (это разделы
# продажи аккаунтов, подписок, валют и т.п.). Проверка — по имени подкатегории
# (`sub.name`), а не по родительской категории, чтобы не отсечь, например,
# "Kick / Услуги" из-за того что в "Kick" есть подкатегория "Аккаунты".
_ANTI_INDICATORS = (
    "аккаунт", "магазин акк",
    " подписка", "подписки ", "premium ", "премиум", " про ", " pro ",
    "валют", "монет", "звёзд", "звезд", " stars",
    "ключ", "key ", "пополнен", "top-up", "топап",
    "товар", "предмет", "ваучер",
)


def _is_smm_friendly(sub) -> tuple[bool, str]:
    """``(подходит ли подкатегория для накрутки, причина)``.

    Подкатегория подходит, если её имя содержит SMM-индикатор и НЕ содержит
    анти-индикатор. Анти-индикаторы проверяются строго в имени подкатегории
    (sub.name), не в родительской категории.
    """
    try:
        sname = (sub.name or "").lower()
    except Exception:
        return False, "no-name"
    sname_padded = f" {sname} "
    for anti in _ANTI_INDICATORS:
        if anti in sname_padded:
            return False, f"anti:{anti.strip()}"
    return True, "ok"


def find_subcategory_candidates(service: dict) -> list[Any]:
    """Возвращает упорядоченный список подкатегорий-кандидатов.

    Подкатегория-кандидат должна:
      * соответствовать **платформе** (twitch/telegram/…) — по
        ``platform_kws``,
      * быть **SMM-разделом** — либо имя содержит конкретный тип услуги
        (Просмотры/Подписчики/…), либо общий SMM-индикатор
        (Услуги/Накрутка/SMM/Продвижение), и при этом
      * НЕ содержать анти-индикатор: «Аккаунты», «Подписка»,
        «Premium», «Звёзды», «Ключи», «Валюта», «Товары» и т.п. —
        эти разделы для продажи аккаунтов/подписок/валют, не для
        накрутки.

    Если ни одной подходящей подкатегории нет — возвращается ``[]``,
    услуга пропускается. Лот не создаётся «куда попало».
    """
    platform = detect_platform(service)
    if not platform or CTX.cardinal is None:
        return []
    try:
        subs = list(CTX.cardinal.account.subcategories)
    except Exception:
        return []
    platform_kws = _platform_name_keywords(platform)
    if not platform_kws:
        return []
    type_ru = _detect_service_type_ru(service)
    type_kws = _type_keywords_for_subcat(type_ru)
    # Принудительный маппинг platform → subcategory_id из конфига.
    # Если задан и такая подкатегория реально есть у юзера в FunPay —
    # ставим её первым кандидатом. Остальные авто-детект-кандидаты идут
    # после (как fallback), чтобы плагин не упал, если override указан
    # с ошибкой или подкатегория временно не отдалась FunPay.
    forced_sub: Any | None = None
    try:
        overrides = (CTX.storage.cfg.get("platform_subcat_overrides") or {}) if CTX.storage else {}
    except Exception:
        overrides = {}
    forced_id = overrides.get(platform)
    if forced_id:
        try:
            forced_id_int = int(forced_id)
            for sub in subs:
                if int(getattr(sub, "id", -1)) == forced_id_int:
                    forced_sub = sub
                    break
            if forced_sub is None:
                logger.warning(
                    "subcat: platform_subcat_overrides[%s]=%s — такой подкатегории "
                    "нет среди subcategories аккаунта; игнорирую override",
                    platform, forced_id,
                )
        except (TypeError, ValueError) as ex:
            logger.warning("subcat: bad override for %s: %s (%s)", platform, forced_id, ex)
    matched: list[tuple[Any, str, str]] = []  # (sub, sname, match_reason)
    for sub in subs:
        if not getattr(sub, "is_common", True):
            continue
        try:
            sname = (sub.name or "").lower()
            cat_name = (sub.category.name or "").lower() if sub.category else ""
        except Exception:
            continue
        hay_full = f" {sname} {cat_name} "
        # Платформа: совпадение по имени подкатегории или её родителю
        if not any(k in hay_full for k in platform_kws):
            continue
        # Анти-индикатор (Аккаунты, Подписка, Premium и т.п.) — пропускаем
        ok, why = _is_smm_friendly(sub)
        if not ok:
            continue
        sname_padded = f" {sname} "
        # Должен быть либо конкретный тип услуги, либо общий SMM-индикатор
        has_type = bool(type_kws) and any(t in sname_padded for t in type_kws)
        has_smm = any(s in sname_padded for s in _SMM_INDICATORS)
        if not (has_type or has_smm):
            continue
        reason = "type" if has_type else "smm"
        matched.append((sub, sname, reason))
    if not matched and forced_sub is None:
        logger.info(
            "subcat: sid=%s platform=%s type=%s — нет SMM-подкатегории FunPay",
            service.get("id"), platform, type_ru,
        )
        return []
    has_lots: set[int] = set()
    for sub, _, _ in matched:
        try:
            if CTX.cardinal.account.get_my_subcategory_lots(sub.id):
                has_lots.add(sub.id)
        except Exception:
            continue
    # Приоритет: совпадение по типу → has-lots → SMM-индикатор.
    def _rank(item):
        sub, _, reason = item
        return (
            0 if reason == "type" else 1,
            0 if sub.id in has_lots else 1,
        )

    matched.sort(key=_rank)
    out = [m[0] for m in matched]
    # Если задан force-override — ставим его ПЕРВЫМ кандидатом, остальные
    # авто-кандидаты сохраняем как fallback'и (на случай если subcategory
    # окажется кривой / закрытой).
    if forced_sub is not None:
        out = [forced_sub] + [s for s in out if int(s.id) != int(forced_sub.id)]
        logger.info(
            "subcat: sid=%s platform=%s → force-override subcat=%s (#%s)",
            service.get("id"), platform,
            getattr(forced_sub, "name", "?"), forced_sub.id,
        )
    logger.info(
        "subcat: sid=%s platform=%s type=%s → candidates=%s",
        service.get("id"), platform, type_ru,
        [(s.id, s.name) for s in out[:3]],
    )
    return out


def find_subcategory_for_service(service: dict):
    """Возвращает наиболее подходящую FunPay-подкатегорию или ``None``.

    Тонкая обёртка над :func:`find_subcategory_candidates` — берёт первого
    кандидата из ранжированного списка. Оставлено для совместимости со
    старыми callsite'ами.
    """
    candidates = find_subcategory_candidates(service)
    return candidates[0] if candidates else None


# Все ключевые слова из SERVICE_TYPE_KEYWORDS — для определения,
# является ли FunPay-<select> полем «Тип услуги». Если несколько option-ов
# этого <select> содержат подобные слова — это точно «Тип услуги», и мы
# должны выставить туда наш детектированный тип услуги.
_ALL_TYPE_KEYWORDS: set[str] = {
    k for _, kws in SERVICE_TYPE_KEYWORDS for k in kws
}


def _apply_type_select(
    new_fields: dict,
    select_options: dict[str, list[tuple[str, str]]],
    service: dict,
) -> None:
    """Сопоставляет тип услуги из smmway с option-ом FunPay-<select>.

    Логика:
      1. Детектим русский тип услуги (Подписчики/Зрители/...).
      2. Ищем select, в котором НЕСКОЛЬКО option-ов выглядят как типы
         услуг (содержат слова из SERVICE_TYPE_KEYWORDS) — это и есть
         «Тип услуги».
      3. Для такого select пытаемся выбрать option, чей текст совпадает
         с нашим типом или его синонимами. Если не нашли — оставляем
         «Прочее»/«Другое»/«Other», иначе не трогаем.

    Поля типа «Сервер», «Сторона», «Игра» и т.п. не трогаем — у них
    option-ы НЕ выглядят как типы услуг.
    """
    type_ru = _detect_service_type_ru(service)
    if not type_ru or type_ru == "Услуга":
        return
    candidates = [type_ru.lower()] + [
        kw.lower() for kw in _type_keywords_for_subcat(type_ru)
    ]
    # дедуп с сохранением порядка
    seen: set[str] = set()
    candidates = [c for c in candidates if c and not (c in seen or seen.add(c))]

    for sel_name, options in select_options.items():
        non_empty = [(v, t) for v, t in options if v]
        if len(non_empty) < 2:
            continue
        # Сколько option-ов выглядят как типы услуг
        type_like = sum(
            1 for _, t in non_empty
            if any(kw in t for kw in _ALL_TYPE_KEYWORDS)
        )
        if type_like < 2:
            # Это не «Тип услуги» (скорее всего «Сервер», «Сторона» и т.п.) —
            # пропускаем, чтобы не сломать обязательные поля.
            continue
        chosen: str | None = None
        chosen_text: str = ""
        for kw in candidates:
            for v, t in non_empty:
                if kw in t:
                    chosen = v
                    chosen_text = t
                    break
            if chosen:
                break
        if not chosen:
            # Fallback на «Прочее»/«Другое»/«Other»
            for v, t in non_empty:
                if "прочее" in t or "друг" in t or "other" in t:
                    chosen = v
                    chosen_text = t
                    break
        if chosen and new_fields.get(sel_name) != chosen:
            logger.info(
                "auto-lot: select %s := %r (%s) для type_ru=%s",
                sel_name, chosen, chosen_text, type_ru,
            )
            new_fields[sel_name] = chosen


def create_lot_from_service(service: dict, *, subcategory_id: int,
                            markup_pct: float, rate: float, min_price: float,
                            template_lot_id: int | None = None) -> tuple[int | None, str]:
    """Создаёт новый лот FunPay в подкатегории ``subcategory_id``.

    Если ``template_lot_id`` передан — поля копируются из существующего лота
    (старое поведение, "лот-шаблон"). Если же ``template_lot_id`` равен None
    (это поведение по умолчанию для нового авто-создания лотов), плагин сам
    получает пустую форму создания лота из FunPay через
    :func:`get_lot_fields_by_node` и наполняет её — то есть лот создаётся
    "с нуля", даже если у юзера никогда не было лотов в этой подкатегории.

    Возвращает кортеж ``(lot_id, msg)``.
    """
    if CTX.cardinal is None:
        return None, "no cardinal"

    select_options: dict[str, list[tuple[str, str]]] = {}
    if template_lot_id:
        try:
            fields = CTX.cardinal.account.get_lot_fields(template_lot_id)
        except Exception as ex:
            return None, f"шаблон #{template_lot_id} недоступен: {ex}"
    else:
        fields, fetch_err, select_options = get_lot_fields_by_node(subcategory_id)
        if fields is None:
            return None, (
                f"подкатегория #{subcategory_id}: {fetch_err}"
                if fetch_err else
                f"не удалось получить пустую форму создания лота для "
                f"подкатегории #{subcategory_id}"
            )

    new_fields = dict(fields.fields)
    # Чтобы FunPay воспринял запрос как создание НОВОГО лота, offer_id = 0
    new_fields["offer_id"] = "0"
    # node_id должен соответствовать целевой подкатегории
    new_fields["node_id"] = str(subcategory_id)
    # Build title & description
    sid = service.get("service") or service.get("id")
    fake_lot = LotEntry(funpay_lot_id=0, service_id=int(sid))
    title_ru = render_lot(CTX.storage.templates["lot_ru_title"], service=service, lot=fake_lot)
    title_en = render_lot(CTX.storage.templates["lot_en_title"], service=service, lot=fake_lot)
    desc_ru = render_lot(CTX.storage.templates["lot_ru_desc"], service=service, lot=fake_lot)
    desc_en = render_lot(CTX.storage.templates["lot_en_desc"], service=service, lot=fake_lot)
    price = compute_fp_price(service, markup_pct=markup_pct, rate=rate, min_price=min_price)
    # Пред-валидация EN: FunPay режет лот, если в [en]-полях есть
    # кириллица («Составление некорректного английского описания…»).
    # На всякий случай ещё раз гоняем рендеренные строки через фильтр —
    # шаблон может содержать переменную вида ``{app}`` со значением
    # «Дзен» (Cyrillic) и т.п. Если кириллица найдена — собираем
    # минимальный нейтральный EN-fallback.
    title_en = _sanitize_en(title_en, kind="title", service=service)
    desc_en = _sanitize_en(desc_en, kind="desc", service=service)
    new_fields["fields[summary][ru]"] = title_ru[:80]
    new_fields["fields[summary][en]"] = title_en[:80]
    new_fields["fields[desc][ru]"] = desc_ru
    new_fields["fields[desc][en]"] = desc_en
    new_fields["price"] = f"{price:.4f}"
    new_fields["active"] = "on"
    new_fields["amount"] = "999999"
    # Подобрать option для select-поля «Тип услуги» (если оно есть)
    # по русскому типу услуги из smmway. Без этого FunPay подставлял
    # первое непустое значение из dropdown'а, что приводило к рассинхрону
    # между заголовком лота («Зрители») и реальным «Тип услуги» («Подписчики»).
    if select_options:
        _apply_type_select(new_fields, select_options, service)
    # Build LotFields
    from FunPayAPI.types import LotFields  # local import — runs inside FPC
    lf = LotFields(lot_id=0, fields=new_fields, subcategory=fields.subcategory,
                   currency=fields.currency)
    try:
        CTX.cardinal.account.save_lot(lf)
    except Exception as ex:
        # Разворачиваем детали LotSavingError, чтобы юзер увидел
        # конкретно какое поле/ошибка были отвергнуты FunPay.
        err_msg = getattr(ex, "error_message", None)
        err_dict = getattr(ex, "errors", None) or {}
        parts = []
        if err_msg:
            parts.append(str(err_msg))
        if err_dict:
            for k, v in err_dict.items():
                parts.append(f"{k}: {v}")
        if not parts:
            parts.append(str(ex))
        detail = "; ".join(parts)[:300]
        logger.warning(
            "save_lot failed (sid=%s, node=%s, title_ru=%r): %s",
            sid, subcategory_id, title_ru[:60], detail,
        )
        return None, f"save_lot: {detail}"
    # Find newly-created lot ID by listing subcategory lots
    try:
        my_lots = CTX.cardinal.account.get_my_subcategory_lots(subcategory_id)
        for ml in my_lots:
            if title_ru[:60] in getattr(ml, "title", "") or title_ru[:60] in getattr(ml, "description", ""):
                return int(ml.id), "ok"
        if my_lots:
            return int(my_lots[0].id), "ok (best-guess)"
    except Exception as ex:
        return None, f"lots-list: {ex}"
    return None, "lot saved, id unknown"


# =============================================================================
# 12. TELEGRAM MENU
# =============================================================================


# Callback prefixes (uniquely namespaced)
CB = "smw"


def _kbd():
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_text() -> str:
    s = CTX.storage
    if s is None:
        return "<b>SMMWay</b>\nИнициализация...\n"
    active_lots = sum(1 for l in s.lots.values() if l.active)
    total_lots = len(s.lots)
    o = s.orders
    completed = sum(1 for x in o.values() if x.status == "completed")
    in_progress = sum(1 for x in o.values() if x.status in ("created", "in_progress"))
    failed = sum(1 for x in o.values() if x.status == "error")
    try:
        bal = CTX.api.balance() if (CTX.api and CTX.api.api_key) else None
    except Exception:
        bal = None
    bal_txt = f"<b>Баланс:</b> <code>{bal:.4f}</code> ₽" if bal is not None else "<b>Баланс:</b> —"
    return (
        f"<b>🪄 SMMWay v{VERSION}</b>\n"
        f"<i>Автоматическая перепродажа услуг smmway.ru</i>\n\n"
        f"📦 Лотов: <b>{active_lots}</b> активно / <b>{total_lots}</b> всего\n"
        f"🧾 Заказов: <b>{len(o)}</b>\n"
        f"  ✅ Выполнено: {completed}\n"
        f"  🔄 В работе: {in_progress}\n"
        f"  ❌ Ошибок: {failed}\n"
        f"💸 Потрачено: <code>{s.stats.get('spent_rub', 0):.4f}</code>\n\n"
        f"💼 SMMWay аккаунт:\n  • {bal_txt}\n"
    )


def main_menu_kb():
    K, B = _kbd()
    kb = K(row_width=2)
    s = CTX.storage
    enabled = s and s.cfg.get("enabled", True)
    kb.add(B(("🟢" if enabled else "🔴") + " Вкл/Выкл", callback_data=f"{CB}:toggle"))
    kb.add(B("📦 Лоты", callback_data=f"{CB}:lots:0"))
    kb.add(
        B("🚀 Авто-лоты", callback_data=f"{CB}:autolots"),
        B("⭐ Авто-цена", callback_data=f"{CB}:autoprice"),
    )
    kb.add(B("🔔 Уведомления", callback_data=f"{CB}:notif"))
    kb.add(
        B(("🟢" if s and s.cfg.get("auto_replace_missing_service") else "🔴") + " Потеряшка",
          callback_data=f"{CB}:toggle_replace"),
        B(("🟢" if s and s.cfg.get("auto_review_bonus_enabled", True) else "🔴") +
          f" Отзыв (+{int(s.cfg.get('auto_review_bonus_pct', 10) if s else 10)}%)",
          callback_data=f"{CB}:autoreview"),
    )
    kb.add(
        B("✉️ Шаблоны", callback_data=f"{CB}:msgtpl"),
        B("🔑 API ключ", callback_data=f"{CB}:apikey"),
    )
    kb.add(B("📋 Заказы", callback_data=f"{CB}:orders:0"))
    kb.add(B("📊 Аналитика", callback_data=f"{CB}:analytics"))
    kb.add(B("⚙️ Настройки", callback_data=f"{CB}:settings"))
    kb.add(B("❤️ Health-check", callback_data=f"{CB}:health"))
    kb.add(B("🛡 Стабилизатор", callback_data=f"{CB}:recovery"))
    kb.add(B("🔄 Обновить", callback_data=f"{CB}:main"))
    return kb


def init_tg_menu(crd: "Cardinal", *args) -> None:
    if crd.telegram is None:
        return
    CTX.cardinal = crd
    if CTX.storage is None:
        CTX.storage = Storage()
    if CTX.api is None:
        CTX.api = SMMWayAPI(CTX.storage.cfg.get("api_key", ""))

    tg = crd.telegram
    bot = tg.bot
    K, B = _kbd()
    def open_main(call_or_msg):
        """Открывает главное меню плагина.

        Если объект — CallbackQuery (например, юзер кликнул «Настройки» в
        FPC-странице плагина), сначала пытаемся отредактировать существующее
        сообщение. Если редактирование не удалось (старое сообщение, разные
        chat_id, message_id уже редактировался другим, и т.п.) — посылаем
        новое сообщение, чтобы юзер всё равно увидел меню, а не «висящий»
        callback с popup'ом callback_data.
        """
        text = main_menu_text()
        kb = main_menu_kb()
        if hasattr(call_or_msg, "data"):  # CallbackQuery
            chat_id = getattr(getattr(call_or_msg, "message", None), "chat", None)
            chat_id = getattr(chat_id, "id", None)
            msg_id = getattr(getattr(call_or_msg, "message", None), "id", None)
            cb_id = getattr(call_or_msg, "id", None)
            edited = False
            if chat_id is not None and msg_id is not None:
                try:
                    bot.edit_message_text(text, chat_id, msg_id,
                                          reply_markup=kb, parse_mode="HTML")
                    edited = True
                except Exception:
                    logger.warning("open_main: edit_message_text failed, will send new", exc_info=True)
            if not edited and chat_id is not None:
                try:
                    bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    logger.exception("open_main: send_message fallback failed")
            if cb_id is not None:
                try:
                    bot.answer_callback_query(cb_id)
                except Exception:
                    pass
        else:
            try:
                bot.send_message(call_or_msg.chat.id, text,
                                 reply_markup=kb, parse_mode="HTML")
            except Exception:
                logger.exception("open_main: send_message failed")

    # /smmway command (+ алиасы для удобства)
    def _cmd_smmway(m):
        if hasattr(tg, "is_authorized_user") and not tg.is_authorized_user(m.from_user.id):
            return
        open_main(m)

    # callback router
    def on_callback(c):
        try:
            data = c.data or ""
            # FPC plugin settings page callback (47:UUID:offset) — обработать
            # ПЕРВЫМ и гарантированно ответить на callback, чтобы Telegram
            # не оставлял «висящий» спиннер. Раньше при ошибке edit_message_text
            # юзер мог увидеть popup с raw callback_data ("47:UUID:0").
            if data.startswith("47:") and UUID in data:
                open_main(c)
                return
            if data == f"{CB}:main":
                open_main(c)
                return
            if data == f"{CB}:toggle":
                CTX.storage.cfg["enabled"] = not CTX.storage.cfg.get("enabled", True)
                CTX.storage.save_config()
                open_main(c)
                return
            if data == f"{CB}:toggle_replace":
                CTX.storage.cfg["auto_replace_missing_service"] = not CTX.storage.cfg.get("auto_replace_missing_service", True)
                CTX.storage.save_config()
                open_main(c)
                return
            if data == f"{CB}:autoreview":
                _show_autoreview_menu(c)
                return
            if data == f"{CB}:autoreview_bonus_toggle":
                CTX.storage.cfg["auto_review_bonus_enabled"] = not CTX.storage.cfg.get("auto_review_bonus_enabled", True)
                CTX.storage.save_config()
                _show_autoreview_menu(c)
                return
            if data == f"{CB}:autoreview_pct_set":
                _ask_value(c, "auto_review_bonus_pct",
                           "Введи процент бонуса (объём допзаказа от основного), напр. <code>10</code>", cast=float)
                return
            if data == f"{CB}:autoreview_stars_set":
                _ask_value(c, "auto_review_bonus_min_stars",
                           "Введи минимальный рейтинг отзыва для бонуса (1–5), напр. <code>5</code>", cast=int)
                return
            if data == f"{CB}:health":
                _show_health(c)
                return
            if data == f"{CB}:recovery":
                _show_recovery_menu(c)
                return
            if data == f"{CB}:recovery_toggle":
                CTX.storage.cfg["service_recovery_enabled"] = not CTX.storage.cfg.get("service_recovery_enabled", True)
                CTX.storage.save_config()
                _show_recovery_menu(c)
                return
            if data == f"{CB}:recovery_threshold_set":
                _ask_value(c, "service_recovery_min_success_rate",
                           "Введи порог здоровья (0.0–1.0), напр. <code>0.6</code>", cast=float)
                return
            if data == f"{CB}:recovery_cooldown_set":
                _ask_value(c, "service_recovery_cooldown_sec",
                           "Введи кулдаун восстановления в секундах, напр. <code>1800</code>", cast=int)
                return
            if data == f"{CB}:settings":
                _show_settings_menu(c)
                return
            if data == f"{CB}:analytics":
                _show_analytics(c)
                return
            if data == f"{CB}:dw_toggle":
                CTX.storage.cfg["dynamic_workflows_enabled"] = not CTX.storage.cfg.get("dynamic_workflows_enabled", True)
                CTX.storage.save_config()
                _show_analytics(c)
                return
            if data == f"{CB}:loyalty_toggle":
                CTX.storage.cfg["loyalty_enabled"] = not CTX.storage.cfg.get("loyalty_enabled", True)
                CTX.storage.save_config()
                _show_analytics(c)
                return
            if data == f"{CB}:settings_toggle_retry":
                CTX.storage.cfg["auto_retry_on_error"] = not CTX.storage.cfg.get("auto_retry_on_error", True)
                CTX.storage.save_config()
                _show_settings_menu(c)
                return
            if data == f"{CB}:settings_reset_blacklist":
                CTX.storage.cfg["blacklisted_services"] = []
                CTX.storage.save_config()
                bot.answer_callback_query(c.id, "Чёрный список сброшен!")
                _show_settings_menu(c)
                return
            if data.startswith(f"{CB}:bl_remove:"):
                # Удаление конкретного ID из чёрного списка
                try:
                    sid = int(data.split(":")[2])
                    blacklist = CTX.storage.cfg.get("blacklisted_services", [])
                    if sid in blacklist:
                        blacklist.remove(sid)
                        CTX.storage.cfg["blacklisted_services"] = blacklist
                        CTX.storage.save_config()
                        bot.answer_callback_query(c.id, f"Услуга #{sid} удалена из чёрного списка")
                    else:
                        bot.answer_callback_query(c.id, f"#{sid} не в списке")
                except (ValueError, IndexError):
                    bot.answer_callback_query(c.id, "Ошибка")
                _show_settings_menu(c)
                return
            if data == f"{CB}:bl_remove_manual":
                bot.send_message(c.message.chat.id,
                                 "Введи ID услуги (или несколько через запятую/пробел) для удаления из чёрного списка:")
                _set_state(c.from_user.id, kind="bl_remove_ids")
                bot.answer_callback_query(c.id)
                return
            if data == f"{CB}:apikey":
                _show_apikey_menu(c)
                return
            if data == f"{CB}:apikey_set":
                _ask_apikey(c)
                return
            if data == f"{CB}:lots:0" or data.startswith(f"{CB}:lots:"):
                offset = int(data.split(":")[2])
                _show_lots_list(c, offset)
                return
            if data.startswith(f"{CB}:lot:"):
                lot_id = int(data.split(":")[2])
                _show_lot_card(c, lot_id)
                return
            if data.startswith(f"{CB}:lot_unbind:"):
                lot_id = int(data.split(":")[2])
                CTX.storage.unbind_lot(lot_id)
                bot.answer_callback_query(c.id, "Отвязано")
                _show_lots_list(c, 0)
                return
            if data == f"{CB}:lot_bind":
                _ask_lot_bind(c)
                return
            if data == f"{CB}:autolots":
                _show_autolots_menu(c)
                return
            if data == f"{CB}:autolots_run_by_ids":
                _ask_autolots_ids(c)
                return
            if data == f"{CB}:autolots_tpl":
                _show_lot_templates(c)
                return
            if data == f"{CB}:autolots_tpl_ru_t":
                _ask_template(c, "lot_ru_title", "Введи шаблон названия (RU):")
                return
            if data == f"{CB}:autolots_tpl_ru_d":
                _ask_template(c, "lot_ru_desc", "Введи шаблон описания (RU):")
                return
            if data == f"{CB}:autolots_tpl_en_t":
                _ask_template(c, "lot_en_title", "Введи шаблон названия (EN):")
                return
            if data == f"{CB}:autolots_tpl_en_d":
                _ask_template(c, "lot_en_desc", "Введи шаблон описания (EN):")
                return
            if data == f"{CB}:autolots_tpl_reset":
                CTX.storage.templates.update({
                    "lot_ru_title": DEFAULT_LOT_TEMPLATE_RU["title"],
                    "lot_ru_desc": DEFAULT_LOT_TEMPLATE_RU["description"],
                    "lot_en_title": DEFAULT_LOT_TEMPLATE_EN["title"],
                    "lot_en_desc": DEFAULT_LOT_TEMPLATE_EN["description"],
                })
                CTX.storage.save_templates()
                bot.answer_callback_query(c.id, "Сброшено")
                _show_lot_templates(c)
                return
            if data == f"{CB}:autolots_markup":
                _show_markup_menu(c)
                return
            if data == f"{CB}:autolots_markup_set":
                _ask_value(c, "global_markup_pct",
                           "Введи глобальную наценку, %: (например 55)",
                           cast=float, after_cb=f"{CB}:autolots_markup")
                return
            if data == f"{CB}:autolots_stop":
                if CTX.autolots_running:
                    CTX.autolots_cancel.set()
                    bot.answer_callback_query(c.id, "⛔ Останавливаю...", show_alert=False)
                else:
                    bot.answer_callback_query(c.id, "Сейчас авто-создание не запущено", show_alert=False)
                _show_autolots_menu(c)
                return
            if data == f"{CB}:autolots_purge":
                _ask_autolots_purge(c)
                return
            if data == f"{CB}:autolots_purge_confirm":
                _do_autolots_purge(c)
                return
            if data == f"{CB}:autoprice":
                _show_autoprice_menu(c)
                return
            if data == f"{CB}:autoprice_toggle":
                CTX.storage.cfg["auto_price_enabled"] = not CTX.storage.cfg.get("auto_price_enabled", True)
                CTX.storage.save_config()
                _show_autoprice_menu(c)
                return
            if data == f"{CB}:autoprice_now":
                bot.answer_callback_query(c.id, "Обновляю...")
                upd, total = update_all_prices(force=True)
                bot.send_message(c.message.chat.id, f"Готово: обновил {upd}/{total} лотов.")
                return
            if data == f"{CB}:autoprice_interval":
                _ask_value(c, "auto_price_interval_sec",
                           "Введи интервал в секундах (≥30):",
                           cast=int, after_cb=f"{CB}:autoprice")
                return
            if data == f"{CB}:notif":
                _show_notif_menu(c)
                return
            if data.startswith(f"{CB}:notif_toggle:"):
                key = data.split(":", 2)[2]
                if key in CTX.storage.cfg:
                    CTX.storage.cfg[key] = not bool(CTX.storage.cfg[key])
                    CTX.storage.save_config()
                _show_notif_menu(c)
                return
            if data == f"{CB}:msgtpl":
                _show_msg_templates(c)
                return
            if data.startswith(f"{CB}:msgtpl_edit:"):
                key = data.split(":", 2)[2]
                _ask_template(c, key, f"Введи новый шаблон для «{key}»:")
                return
            if data == f"{CB}:msgtpl_reset":
                CTX.storage.templates.update({
                    f"msg_{k}": v for k, v in DEFAULT_MSG_TEMPLATES.items()
                })
                CTX.storage.save_templates()
                bot.answer_callback_query(c.id, "Сброшено")
                _show_msg_templates(c)
                return
            if data.startswith(f"{CB}:orders:"):
                offset = int(data.split(":")[2])
                _show_orders_list(c, offset)
                return
            if data.startswith(f"{CB}:order:"):
                order_id = data.split(":", 2)[2]
                _show_order_card(c, order_id)
                return
            # Если ни один из паттернов не подошёл — на всякий случай
            # отвечаем на callback, чтобы Telegram не оставлял спиннер.
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
        except Exception:
            logger.exception("callback failed: %s", c.data)
            try:
                bot.answer_callback_query(c.id, "Ошибка")
            except Exception:
                pass

    # state-based message handlers (multi-step inputs)
    def _is_pending(m):
        with CTX.tg_state_lock:
            return m.from_user.id in CTX.tg_state

    def on_state_message(m):
        with CTX.tg_state_lock:
            st = CTX.tg_state.pop(m.from_user.id, None)
        if not st:
            return
        try:
            kind = st["kind"]
            if kind == "set_cfg":
                value = st["cast"](m.text)
                CTX.storage.cfg[st["key"]] = value
                CTX.storage.save_config()
                bot.reply_to(m, f"Сохранено: <code>{st['key']}</code> = <code>{value}</code>",
                             parse_mode="HTML")
            elif kind == "set_template":
                CTX.storage.templates[st["key"]] = m.text
                CTX.storage.save_templates()
                bot.reply_to(m, "Шаблон сохранён.")
            elif kind == "set_apikey":
                CTX.storage.cfg["api_key"] = m.text.strip()
                CTX.storage.save_config()
                CTX.api.update_key(m.text.strip())
                bot.reply_to(m, "API-ключ сохранён.")
            elif kind == "lot_bind":
                # Parse format "lot_id service_id [markup%]"
                parts = m.text.split()
                if len(parts) < 2:
                    bot.reply_to(m, "Формат: <lot_id> <service_id> [markup%]")
                    return
                lot_id = int(parts[0])
                service_id = int(parts[1])
                markup = float(parts[2]) if len(parts) > 2 else None
                service = CTX.api.find_service(service_id)
                if not service:
                    bot.reply_to(m, f"Услуга #{service_id} не найдена в smmway. Сохраняю как есть.")
                CTX.storage.bind_lot(lot_id, service_id, markup_pct=markup,
                                     title_ru=service.get("name", "") if service else "")
                bot.reply_to(m, f"Привязано: лот #{lot_id} ↔ услуга #{service_id}")
            elif kind == "autolots_ids":
                _run_autolots(m, m.text)
            elif kind == "bl_remove_ids":
                # Удаление ID из чёрного списка вручную
                raw = m.text.strip()
                ids_to_remove = []
                for token in re.split(r"[,\s;]+", raw):
                    token = token.strip()
                    if token.isdigit():
                        ids_to_remove.append(int(token))
                if not ids_to_remove:
                    bot.reply_to(m, "Не найдено ни одного числового ID. Введи числа через запятую или пробел.")
                    return
                blacklist = CTX.storage.cfg.get("blacklisted_services", [])
                removed = []
                not_found = []
                for sid in ids_to_remove:
                    if sid in blacklist:
                        blacklist.remove(sid)
                        removed.append(str(sid))
                    else:
                        not_found.append(str(sid))
                CTX.storage.cfg["blacklisted_services"] = blacklist
                CTX.storage.save_config()
                reply_parts = []
                if removed:
                    reply_parts.append(f"✅ Удалены из чёрного списка: {', '.join(removed)}")
                if not_found:
                    reply_parts.append(f"⚠️ Не были в списке: {', '.join(not_found)}")
                reply_parts.append(f"Осталось в списке: {len(blacklist)} шт.")
                bot.reply_to(m, "\n".join(reply_parts))
        except Exception as ex:
            bot.reply_to(m, f"Ошибка: {ex}")
            logger.exception("on_state_message failed")

    # Register handlers using register_message_handler / register_callback_query_handler
    # (DesslyHub pattern) instead of decorators + manual reordering.
    tg.bot.register_message_handler(_cmd_smmway, commands=["smmway", "smm", "smmway_menu"])
    tg.bot.register_callback_query_handler(
        on_callback,
        func=lambda c: c.data and (c.data.startswith(f"{CB}:") or (c.data.startswith("47:") and UUID in c.data))
    )
    tg.bot.register_message_handler(on_state_message, func=_is_pending)

    if hasattr(crd, "add_telegram_commands"):
        crd.add_telegram_commands(UUID, [
            ("smmway", "Открыть меню плагина SMMWay", True),
        ])


def _set_state(tg_user_id: int, **kwargs):
    with CTX.tg_state_lock:
        CTX.tg_state[tg_user_id] = kwargs


# --- submenus ---


def _show_analytics(c):
    """Показывает аналитику Dynamic Workflows."""
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()

    dw_enabled = CTX.storage.cfg.get("dynamic_workflows_enabled", True)
    loyalty_enabled = CTX.storage.cfg.get("loyalty_enabled", True)
    summary = DW.get_analytics_summary()

    lines = [
        "<b>📊 Аналитика &amp; Dynamic Workflows</b>",
        "",
        f"<b>🧠 Dynamic Workflows:</b> {'🟢 Вкл' if dw_enabled else '🔴 Выкл'}",
        f"<b>🎁 Лояльность:</b> {'🟢 Вкл' if loyalty_enabled else '🔴 Выкл'}"
        f" (бонус {CTX.storage.cfg.get('loyalty_bonus_pct', 5)}%"
        f" после {CTX.storage.cfg.get('loyalty_min_orders', 2)} заказов)",
        "",
        f"📈 Отслеживается услуг: <b>{summary['total_services_tracked']}</b>",
        f"👥 Покупателей: <b>{summary['total_buyers']}</b>",
        f"⭐ Лояльных: <b>{summary['loyal_buyers']}</b>",
        f"⏸ Soft-block: <b>{summary['soft_blocked']}</b>",
    ]

    if summary["top_healthy"]:
        lines.append("")
        lines.append("<b>✅ Топ здоровых услуг:</b>")
        for sid, score in summary["top_healthy"][:5]:
            svc = CTX.api.find_service(sid) if CTX.api else None
            name = (svc.get("name", "")[:25] if svc else f"#{sid}")
            bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
            lines.append(f"  {bar} {score:.0%} — {html_escape(name)}")

    if summary["top_unhealthy"]:
        lines.append("")
        lines.append("<b>⚠️ Проблемные услуги:</b>")
        for sid, score in summary["top_unhealthy"][:5]:
            svc = CTX.api.find_service(sid) if CTX.api else None
            name = (svc.get("name", "")[:25] if svc else f"#{sid}")
            bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
            lines.append(f"  {bar} {score:.0%} — {html_escape(name)}")

    # Общая статистика из storage
    lines.append("")
    lines.append("<b>📦 Общая статистика:</b>")
    stats = CTX.storage.stats
    lines.append(f"  Отправлено: <b>{stats.get('sent', 0)}</b>")
    lines.append(f"  Выполнено: <b>{stats.get('completed', 0)}</b>")
    lines.append(f"  Ошибок: <b>{stats.get('failed', 0)}</b>")
    total = stats.get('sent', 0)
    if total > 0:
        success_rate = stats.get('completed', 0) / total * 100
        lines.append(f"  Успешность: <b>{success_rate:.1f}%</b>")

    kb = K(row_width=2)
    kb.add(
        B(("🟢" if dw_enabled else "🔴") + " DW", callback_data=f"{CB}:dw_toggle"),
        B(("🟢" if loyalty_enabled else "🔴") + " Лояльность", callback_data=f"{CB}:loyalty_toggle"),
    )
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))

    bot.edit_message_text("\n".join(lines), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_settings_menu(c):
    """Показывает меню настроек плагина."""
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()

    retry_enabled = CTX.storage.cfg.get("auto_retry_on_error", True)
    blacklist = CTX.storage.cfg.get("blacklisted_services", [])
    max_attempts = CTX.storage.cfg.get("auto_retry_max_attempts", 2)

    lines = [
        "<b>⚙️ Настройки</b>",
        "",
        f"<b>🔄 Авто-повтор при ошибке:</b> {'🟢 Вкл' if retry_enabled else '🔴 Выкл'}",
        f"   Попыток: {max_attempts}",
        "",
        f"<b>🚫 Чёрный список услуг:</b> {len(blacklist)} шт.",
    ]
    if blacklist:
        lines.append("   Нажми на ID чтобы убрать из списка:")
        for sid in blacklist[:20]:
            svc = CTX.api.find_service(sid) if CTX.api else None
            name = (svc.get("name", "")[:30] if svc else "неизвестна")
            lines.append(f"   • <code>{sid}</code> — {html_escape(name)}")
        if len(blacklist) > 20:
            lines.append(f"   ... и ещё {len(blacklist) - 20}")
    lines.append("")
    lines.append("<i>При ошибке заказа бот проверяет ссылку и баланс, "
                 "пробует ещё раз. Если повторно ошибка — возврат денег, "
                 "блокировка услуги и замена лота.</i>")
    lines.append("")
    lines.append("<i>Чтобы удалить конкретный ID — нажми кнопку ниже "
                 "или отправь команду «Удалить ID» и введи номер.</i>")

    kb = K(row_width=1)
    kb.add(
        B(("🟢" if retry_enabled else "🔴") + " Авто-повтор", callback_data=f"{CB}:settings_toggle_retry"),
    )
    # Кнопки для удаления отдельных услуг из чёрного списка (показываем до 10)
    if blacklist:
        row = []
        for sid in blacklist[:10]:
            row.append(B(f"❌ {sid}", callback_data=f"{CB}:bl_remove:{sid}"))
            if len(row) == 3:
                kb.row(*row)
                row = []
        if row:
            kb.row(*row)
        kb.add(B(f"🗑 Сбросить весь список ({len(blacklist)})", callback_data=f"{CB}:settings_reset_blacklist"))
    kb.add(B("✏️ Удалить ID вручную", callback_data=f"{CB}:bl_remove_manual"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))

    bot.edit_message_text("\n".join(lines), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_health(c):
    bot = CTX.cardinal.telegram.bot
    lines = ["<b>❤️ Health-check</b>"]
    if not CTX.api.api_key:
        lines.append("🔴 API-ключ не задан")
    else:
        try:
            bal = CTX.api.balance()
            lines.append(f"🟢 API работает, баланс <code>{bal:.4f}</code> ₽")
        except Exception as ex:
            lines.append(f"🔴 API ошибка: {html_escape(str(ex))}")
    try:
        services = CTX.api.services()
        lines.append(f"🟢 Услуг в каталоге: <b>{len(services)}</b>")
    except Exception as ex:
        lines.append(f"🔴 Услуги: {html_escape(str(ex))}")
    lines.append(f"📦 Лотов в базе: <b>{len(CTX.storage.lots)}</b>")
    lines.append(f"🧾 Заказов в базе: <b>{len(CTX.storage.orders)}</b>")
    K, B = _kbd()
    kb = K().add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text("\n".join(lines), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_recovery_menu(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    cfg = CTX.storage.cfg
    on = cfg.get("service_recovery_enabled", True)
    threshold = float(cfg.get("service_recovery_min_success_rate", 0.6))
    cooldown = int(cfg.get("service_recovery_cooldown_sec", 1800))
    recovering_count = len(_recovering_services)
    lines = [
        "<b>🛡 Стабилизатор продаж</b>",
        "",
        f"Статус: {'🟢 вкл' if on else '🔴 выкл'}",
        f"Порог здоровья: <b>{threshold:.0%}</b>",
        f"Кулдаун: <b>{cooldown // 60}</b> мин",
        f"Сейчас на восстановлении: <b>{recovering_count}</b>",
    ]
    if _recovering_services:
        now = time.time()
        for sid, ts in list(_recovering_services.items()):
            remaining = max(0, cooldown - (now - ts))
            lines.append(f"  • #{sid} — осталось {int(remaining // 60)} мин")
    kb = K(row_width=1)
    kb.add(B(("🔴 Выкл" if on else "🟢 Вкл") + " стабилизатор",
             callback_data=f"{CB}:recovery_toggle"))
    kb.add(B(f"📊 Порог ({threshold:.0%})", callback_data=f"{CB}:recovery_threshold_set"))
    kb.add(B(f"⏱ Кулдаун ({cooldown // 60} мин)", callback_data=f"{CB}:recovery_cooldown_set"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text("\n".join(lines), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_apikey_menu(c):
    bot = CTX.cardinal.telegram.bot
    key = CTX.storage.cfg.get("api_key", "")
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("задан" if key else "не задан")
    K, B = _kbd()
    kb = K(row_width=1)
    kb.add(B("🔑 Ввести API-ключ", callback_data=f"{CB}:apikey_set"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text(
        f"<b>🔑 API ключ smmway</b>\n\nТекущий: <code>{html_escape(masked)}</code>\n\n"
        f"Получить ключ: на smmway.ru → Профиль → API.",
        c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _ask_apikey(c):
    bot = CTX.cardinal.telegram.bot
    bot.send_message(c.message.chat.id, "Отправь API-ключ одним сообщением.")
    _set_state(c.from_user.id, kind="set_apikey")
    bot.answer_callback_query(c.id)


def _show_lots_list(c, offset: int):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    PAGE = 10
    lots = list(CTX.storage.lots.values())
    lots.sort(key=lambda l: -l.funpay_lot_id)
    page = lots[offset:offset + PAGE]
    text = [f"<b>📦 Лоты ({sum(1 for l in lots if l.active)} акт. / {len(lots)} всего)</b>"]
    if not page:
        text.append("\n<i>Лотов пока нет. Привяжи существующий через «Привязать лот».</i>")
    kb = K(row_width=1)
    for l in page:
        dot = "🟢" if l.active else "🔴"
        kb.add(B(f"{dot} #{l.funpay_lot_id} | svc #{l.service_id} | {l.last_price_fp or '?'}₽",
                 callback_data=f"{CB}:lot:{l.funpay_lot_id}"))
    if offset > 0:
        kb.add(B("⬅️ Назад", callback_data=f"{CB}:lots:{max(0, offset - PAGE)}"))
    if offset + PAGE < len(lots):
        kb.add(B("Вперёд ➡️", callback_data=f"{CB}:lots:{offset + PAGE}"))
    kb.add(B("➕ Привязать лот", callback_data=f"{CB}:lot_bind"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text("\n".join(text), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_lot_card(c, lot_id: int):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    l = CTX.storage.lots.get(lot_id)
    if not l:
        bot.answer_callback_query(c.id, "Не найдено")
        return
    text = (
        f"<b>📦 Лот #{l.funpay_lot_id}</b>\n"
        f"Услуга: <code>{l.service_id}</code>\n"
        f"Активен: {'🟢' if l.active else '🔴'}\n"
        f"Наценка: <code>{l.markup_pct if l.markup_pct is not None else 'глоб.'}</code>%\n"
        f"Последняя цена: <code>{l.last_price_fp or '—'}</code>\n"
        f"Название: <i>{html_escape(l.title_ru or '')[:200]}</i>\n"
    )
    kb = K(row_width=2)
    kb.add(B("🔗 На лот FunPay", url=f"https://funpay.com/lots/offerEdit?offer={l.funpay_lot_id}"))
    kb.add(B("❌ Отвязать", callback_data=f"{CB}:lot_unbind:{l.funpay_lot_id}"))
    kb.add(B("◀️ К списку", callback_data=f"{CB}:lots:0"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _ask_lot_bind(c):
    bot = CTX.cardinal.telegram.bot
    bot.send_message(c.message.chat.id,
                     "Привяжи лот.\nФормат: <code>&lt;funpay_lot_id&gt; &lt;service_id&gt; [markup%]</code>\n\n"
                     "Пример: <code>65776519 5429 55</code>",
                     parse_mode="HTML")
    _set_state(c.from_user.id, kind="lot_bind")
    bot.answer_callback_query(c.id)


def _show_autolots_menu(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    try:
        n = len(CTX.api.services())
    except Exception:
        n = "?"
    markup = CTX.storage.cfg.get("global_markup_pct", 55.0)
    bound = len(CTX.storage.lots)
    running = CTX.autolots_running
    status_line = ""
    if running:
        status_line = "\n⏳ <b>Сейчас идёт авто-создание</b> — можно остановить кнопкой ниже.\n"
    text = (
        f"<b>🚀 Авто-лоты</b>\n"
        f"{status_line}\n"
        f"📦 Доступно услуг: <b>{n}</b>\n"
        f"🔗 Привязано лотов: <b>{bound}</b>\n"
        f"📈 Наценка: <b>{markup}%</b>\n\n"
        f"Создаёт лоты RU+EN по выбранным ID услуг smmway. Подкатегория FunPay "
        f"определяется автоматически по платформе (Telegram, Instagram, TikTok…). "
        f"Лоты создаются <b>с нуля</b>, без существующего лота-шаблона.\n\n"
        f"Если нужно скопировать поля из своего лота — укажи <code>tpl:&lt;lot_id&gt;</code> "
        f"в строке запуска."
    )
    kb = K(row_width=1)
    kb.add(B("🚀 Запуск (ввести ID услуг)", callback_data=f"{CB}:autolots_run_by_ids"))
    if running:
        kb.add(B("⛔ Остановить создание", callback_data=f"{CB}:autolots_stop"))
    kb.add(B("✏️ Шаблоны лотов", callback_data=f"{CB}:autolots_tpl"))
    kb.add(B("💰 Наценка", callback_data=f"{CB}:autolots_markup"))
    if bound:
        kb.add(B(f"🗑 Удалить все созданные лоты ({bound})",
                 callback_data=f"{CB}:autolots_purge"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _ask_autolots_purge(c):
    """Подтверждение массового удаления привязанных лотов."""
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    bound = len(CTX.storage.lots)
    if not bound:
        bot.answer_callback_query(c.id, "Нет привязанных лотов", show_alert=True)
        return
    text = (
        f"<b>🗑 Удалить все созданные лоты?</b>\n\n"
        f"Будет удалено <b>{bound}</b> лотов(а) на FunPay и удалены привязки в плагине.\n\n"
        f"<b>Это действие необратимо.</b> Лоты, созданные не плагином, не трогаем."
    )
    kb = K(row_width=1)
    kb.add(B(f"🗑 Да, удалить {bound}", callback_data=f"{CB}:autolots_purge_confirm"))
    kb.add(B("◀️ Отмена", callback_data=f"{CB}:autolots"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _do_autolots_purge(c):
    """Удаляет все привязанные лоты на FunPay и в storage."""
    bot = CTX.cardinal.telegram.bot
    chat_id = c.message.chat.id
    msg_id = c.message.id
    if CTX.cardinal is None or CTX.cardinal.account is None:
        bot.answer_callback_query(c.id, "Cardinal не готов", show_alert=True)
        return
    bot.answer_callback_query(c.id)
    lot_ids = list(CTX.storage.lots.keys())
    total = len(lot_ids)
    if not total:
        bot.edit_message_text("Нечего удалять.", chat_id, msg_id)
        return
    deleted = 0
    failed: list[tuple[int, str]] = []
    last_edit_ts = 0.0

    def _render(i: int) -> str:
        return (
            f"<b>🗑 Удаление лотов</b>\n\n"
            f"Прогресс: <b>{i}</b>/<b>{total}</b>\n"
            f"✅ Удалено: <b>{deleted}</b>\n"
            f"❌ Ошибок: <b>{len(failed)}</b>"
        )

    try:
        bot.edit_message_text(_render(0), chat_id, msg_id, parse_mode="HTML")
    except Exception:
        pass

    for idx, lot_id in enumerate(lot_ids, start=1):
        try:
            CTX.cardinal.account.delete_lot(int(lot_id))
            deleted += 1
            CTX.storage.unbind_lot(int(lot_id))
        except Exception as ex:
            why = str(ex)[:120]
            failed.append((int(lot_id), why))
            logger.warning("autolots purge: delete_lot(%s) failed: %s", lot_id, why)
        now = time.time()
        if now - last_edit_ts > 1.2 or idx == total:
            last_edit_ts = now
            try:
                bot.edit_message_text(_render(idx), chat_id, msg_id, parse_mode="HTML")
            except Exception:
                pass
        time.sleep(1.0)  # rate limit FunPay

    summary = [
        "<b>🗑 Удаление лотов — готово</b>",
        f"✅ Удалено: <b>{deleted}</b> / {total}",
        f"❌ Ошибок: <b>{len(failed)}</b>",
    ]
    if failed:
        summary.append("\n<b>Подробности:</b>")
        for lid, why in failed[:30]:
            summary.append(f"  • <code>#{lid}</code>: {html_escape(why)[:140]}")
        if len(failed) > 30:
            summary.append(f"  …и ещё <b>{len(failed) - 30}</b>")
    try:
        bot.edit_message_text("\n".join(summary), chat_id, msg_id, parse_mode="HTML")
    except Exception:
        send_long_html(bot, chat_id, "\n".join(summary))


def _ask_autolots_ids(c):
    bot = CTX.cardinal.telegram.bot
    bot.send_message(
        c.message.chat.id,
        "Введи <b>только ID услуг smmway</b> через пробел — плагин <b>сам определит</b>, "
        "под какую подкатегорию FunPay какая услуга подходит, и создаст лоты "
        "<b>с нуля</b> (даже если у тебя ещё нет ни одного лота в этой подкатегории).\n\n"
        "Формат: <code>&lt;sid1&gt; &lt;sid2&gt; ... &lt;sidN&gt;</code>\n\n"
        "Пример: <code>5306 5429 4174 6188 6307 4923 4906 6050 5326 4672</code>\n\n"
        "Опционально: <code>tpl:&lt;lot_id&gt;</code> — взять поля из конкретного "
        "своего лота как шаблон вместо пустой формы FunPay.",
        parse_mode="HTML",
    )
    _set_state(c.from_user.id, kind="autolots_ids")
    bot.answer_callback_query(c.id)


def _autolots_progress_text(*, total: int, idx: int, created: int, failed: int,
                            last_lines: list[str]) -> str:
    """Рендер прогресс-сообщения авто-лотов (обновляется через edit_message_text)."""
    pct = int((idx / total) * 100) if total else 0
    bar_len = 16
    fill = int(bar_len * idx / total) if total else 0
    bar = "█" * fill + "░" * (bar_len - fill)
    head = (
        f"<b>🚀 Авто-лоты</b>\n\n"
        f"<code>{bar}</code> {pct}% ({idx}/{total})\n"
        f"✅ Создано: <b>{created}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>"
    )
    if last_lines:
        head += "\n\n<i>Последние события:</i>\n" + "\n".join(last_lines[-5:])
    return head


def _run_autolots(m, text: str):
    """Обёртка над :func:`_run_autolots_body`, гарантирующая, что флаг
    ``autolots_running`` снимется даже при необработанном исключении.
    Без этого после краша процесса пользователю пришлось бы перезапускать
    плагин, чтобы снова запустить авто-создание."""
    try:
        _run_autolots_body(m, text)
    finally:
        CTX.autolots_running = False
        CTX.autolots_cancel.clear()


def _run_autolots_body(m, text: str):
    bot = CTX.cardinal.telegram.bot
    if CTX.autolots_running:
        bot.reply_to(m, "Уже идёт авто-создание лотов. Дождись окончания или нажми «⛔ Остановить» в меню.", parse_mode="HTML")
        return
    parts = text.split()
    # Поддерживаем и старый формат tpl:NNN, и новый — только ID услуг.
    forced_tpl = next((p for p in parts if p.startswith("tpl:")), None)
    forced_tpl_id = int(forced_tpl.split(":", 1)[1]) if forced_tpl else None
    sids = [int(p) for p in parts if p.isdigit() and not p.startswith("tpl:")]
    if not sids:
        bot.reply_to(m, "Не вижу ни одного ID услуги. Пример: <code>5306 5429 4174</code>", parse_mode="HTML")
        return
    markup = CTX.storage.cfg.get("global_markup_pct", 55.0)
    rate = CTX.storage.cfg.get("currency_rate_rub_to_fp", 1.0)
    min_price = CTX.storage.cfg.get("min_lot_price", 1.0)
    try:
        services_map = {str(s.get("service")): s for s in CTX.api.services()}
    except Exception as ex:
        bot.reply_to(m, f"Не получилось загрузить каталог smmway: {html_escape(str(ex))}", parse_mode="HTML")
        return

    # Сбрасываем флаг отмены перед новым запуском и помечаем, что мы пошли.
    CTX.autolots_cancel.clear()
    CTX.autolots_running = True
    cancelled = False

    total = len(sids)
    # Стартовое сообщение с прогресс-баром. Дальше мы его будем
    # edit_message_text — это и есть "сообщения не повторяются, а заменяются".
    try:
        status_msg = bot.reply_to(
            m,
            _autolots_progress_text(total=total, idx=0, created=0, failed=0, last_lines=[]),
            parse_mode="HTML",
        )
        status_chat = status_msg.chat.id
        status_id = status_msg.message_id
    except Exception as ex:
        logger.warning("autolots: failed to send initial status: %s", ex)
        status_chat, status_id = m.chat.id, None

    created = 0
    failed: list[tuple[int, str]] = []
    skipped_no_subcat: list[tuple[int, str]] = []
    sub_cache: dict[int, Any] = {}
    last_lines: list[str] = []
    last_edit_ts = 0.0

    def _push(line: str):
        last_lines.append(line)
        del last_lines[:-10]

    def _update_status(force: bool = False):
        nonlocal last_edit_ts
        if status_id is None:
            return
        now = time.time()
        # Сбиваем частоту edit до ~1 в секунду (лимиты Telegram).
        if not force and now - last_edit_ts < 1.2:
            return
        last_edit_ts = now
        try:
            bot.edit_message_text(
                _autolots_progress_text(
                    total=total,
                    idx=created + len(failed) + len(skipped_no_subcat),
                    created=created,
                    failed=len(failed) + len(skipped_no_subcat),
                    last_lines=last_lines,
                ),
                status_chat, status_id, parse_mode="HTML",
            )
        except Exception:
            # Обычно это "message is not modified" — безопасно игнорируем.
            pass

    for sid in sids:
        if CTX.autolots_cancel.is_set():
            cancelled = True
            _push("⛔ Принудительная остановка пользователем")
            logger.info("autolots: cancelled by user after %d processed", created + len(failed) + len(skipped_no_subcat))
            break
        s = services_map.get(str(sid))
        if not s:
            failed.append((sid, "услуга не найдена в каталоге smmway"))
            _push(f"❌ <code>{sid}</code> — нет в каталоге smmway")
            _update_status()
            continue
        platform = detect_platform(s)
        if not platform:
            why = f"не распознал платформу по «{s.get('name', '')[:40]}»"
            failed.append((sid, why))
            _push(f"❌ <code>{sid}</code> — {html_escape(why)}")
            _update_status()
            continue

        tpl_lot_id: int | None = None
        if forced_tpl_id is not None:
            try:
                sub = CTX.cardinal.account.get_lot_fields(forced_tpl_id).subcategory
            except Exception as ex:
                why = f"шаблон #{forced_tpl_id} недоступен: {ex}"
                failed.append((sid, why))
                _push(f"❌ <code>{sid}</code> — {html_escape(why)[:80]}")
                _update_status()
                continue
            tpl_lot_id = forced_tpl_id
        else:
            type_ru_for_cache = _detect_service_type_ru(s)
            cache_key = hash((platform, type_ru_for_cache))
            candidates = sub_cache.get(cache_key)
            if candidates is None:
                candidates = find_subcategory_candidates(s)
                sub_cache[cache_key] = candidates
            if not candidates:
                skipped_no_subcat.append((sid, f"{platform} / {type_ru_for_cache}"))
                _push(f"⚠️ <code>{sid}</code> — нет подкатегории ({platform}/{type_ru_for_cache})")
                _update_status()
                continue
            sub = candidates[0]

        subcat_id = sub.id
        try:
            sub_label = sub.fullname if hasattr(sub, "fullname") and sub.fullname else (sub.name or f"#{subcat_id}")
        except Exception:
            sub_label = f"#{subcat_id}"
        logger.info(
            "autolots: sid=%s platform=%s type=%s → subcat=%s (#%s)",
            sid, platform,
            _detect_service_type_ru(s) if forced_tpl_id is None else "tpl",
            sub_label, subcat_id,
        )
        # Перебираем кандидатов (для forced_tpl — только один вариант).
        attempt_subs = [sub] if forced_tpl_id is not None else candidates[:3]
        new_lot_id = None
        msg = ""
        for attempt_idx, attempt_sub in enumerate(attempt_subs):
            attempt_id = attempt_sub.id
            new_lot_id, msg = create_lot_from_service(
                s, subcategory_id=attempt_id, template_lot_id=tpl_lot_id,
                markup_pct=markup, rate=rate, min_price=min_price,
            )
            if new_lot_id:
                # Обновляем sub/subcat_id/sub_label на фактически использованные
                sub = attempt_sub
                subcat_id = attempt_id
                try:
                    sub_label = sub.fullname if hasattr(sub, "fullname") and sub.fullname else (sub.name or f"#{subcat_id}")
                except Exception:
                    sub_label = f"#{subcat_id}"
                if attempt_idx > 0:
                    logger.info(
                        "autolots: sid=%s succeeded on retry #%s in subcat %s",
                        sid, attempt_idx, sub_label,
                    )
                break
            logger.info(
                "autolots: sid=%s attempt #%s in subcat #%s failed: %s",
                sid, attempt_idx, attempt_id, msg[:120],
            )
        if new_lot_id:
            CTX.storage.bind_lot(
                new_lot_id, sid,
                title_ru=render_lot(CTX.storage.templates["lot_ru_title"], service=s,
                                    lot=LotEntry(funpay_lot_id=new_lot_id, service_id=sid)),
                title_en=render_lot(CTX.storage.templates["lot_en_title"], service=s,
                                    lot=LotEntry(funpay_lot_id=new_lot_id, service_id=sid)),
                subcategory_id=subcat_id,
            )
            created += 1
            _push(f"✅ <code>{sid}</code> → FP #{new_lot_id} <i>{html_escape(str(sub_label))[:40]}</i>")
        else:
            failed.append((sid, msg))
            _push(f"❌ <code>{sid}</code> — {html_escape(str(msg))[:90]}")
        _update_status()
        # Прерываемая пауза между лотами: ждём до 2с, но просыпаемся
        # моментально, если пользователь нажал «⛔ Остановить».
        if CTX.autolots_cancel.wait(timeout=2.0):
            cancelled = True
            _push("⛔ Принудительная остановка пользователем")
            logger.info("autolots: cancelled by user after %d processed", created + len(failed) + len(skipped_no_subcat))
            break

    # Снимаем флаг «авто-лоты в процессе» вне зависимости от того, как
    # завершился цикл — нормально, по отмене или из-за исключения.
    CTX.autolots_running = False
    CTX.autolots_cancel.clear()

    # Финальный отчёт. Сначала пытаемся впихнуть всё в это же сообщение через
    # edit_message_text. Если не влезает (лимит 4096) — в этот редактируемый
    # статус пишем summary, а детали отправляем рядом отдельным сообщением.
    _WHY_MAX = 180
    total_failed = len(failed) + len(skipped_no_subcat)
    header = "<b>⛔ Авто-лоты — остановлены</b>" if cancelled else "<b>🚀 Авто-лоты — готово</b>"
    summary_lines = [
        header,
        f"✅ Создано: <b>{created}</b> / {total}",
        f"❌ Ошибок: <b>{total_failed}</b>",
    ]
    if cancelled:
        remaining = total - (created + len(failed) + len(skipped_no_subcat))
        summary_lines.append(f"⏭ Не обработано: <b>{max(remaining, 0)}</b>")
    details: list[str] = []
    if skipped_no_subcat:
        details.append("\n⚠️ Пропущены (нет подкатегории FunPay):")
        shown = skipped_no_subcat[:50]
        for sid, why in shown:
            details.append(f"  • <code>{sid}</code>: {html_escape(str(why))[:_WHY_MAX]}")
        if len(skipped_no_subcat) > len(shown):
            details.append(f"  …и ещё <b>{len(skipped_no_subcat) - len(shown)}</b>")
    if failed:
        details.append("\n❌ Ошибки создания:")
        shown_f = failed[:50]
        for sid, why in shown_f:
            details.append(f"  • <code>{sid}</code>: {html_escape(str(why))[:_WHY_MAX]}")
        if len(failed) > len(shown_f):
            details.append(f"  …и ещё <b>{len(failed) - len(shown_f)}</b>")

    full_text = "\n".join(summary_lines + details)
    if status_id is not None and len(full_text) <= 3800:
        try:
            bot.edit_message_text(full_text, status_chat, status_id, parse_mode="HTML")
            return
        except Exception as ex:
            logger.info("autolots: final edit failed, sending fresh: %s", ex)
    # Иначе: в status пишем summary, а детали шлём новым сообщением.
    if status_id is not None:
        try:
            bot.edit_message_text("\n".join(summary_lines), status_chat, status_id, parse_mode="HTML")
        except Exception:
            pass
    if details:
        send_long_html(bot, m.chat.id, "\n".join(details))


def _show_lot_templates(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    t = CTX.storage.templates
    text = (
        f"<b>✏️ Шаблоны авто-лотов</b>\n\n"
        f"<b>RU title</b>:\n<code>{html_escape(t['lot_ru_title'])[:500]}</code>\n\n"
        f"<b>RU desc</b>:\n<code>{html_escape(t['lot_ru_desc'])[:500]}</code>\n\n"
        f"<b>EN title</b>:\n<code>{html_escape(t['lot_en_title'])[:500]}</code>\n\n"
        f"<b>EN desc</b>:\n<code>{html_escape(t['lot_en_desc'])[:500]}</code>\n\n"
        "<b>Переменные</b>: {app}, {capp}, {type}, {type_en}, {ctype}, {ctype_en}, "
        "{tags}, {tags_en}, {features}, {features_en}, {commands}, {commands_en}, "
        "{service_id}, {name}, {refill}, {cancel}, {min}, {max}"
    )
    kb = K(row_width=2)
    kb.add(B("✏ RU title", callback_data=f"{CB}:autolots_tpl_ru_t"),
           B("✏ RU desc", callback_data=f"{CB}:autolots_tpl_ru_d"))
    kb.add(B("✏ EN title", callback_data=f"{CB}:autolots_tpl_en_t"),
           B("✏ EN desc", callback_data=f"{CB}:autolots_tpl_en_d"))
    kb.add(B("🔄 Сбросить", callback_data=f"{CB}:autolots_tpl_reset"))
    kb.add(B("◀️ Авто-лоты", callback_data=f"{CB}:autolots"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _ask_template(c, key: str, prompt: str):
    bot = CTX.cardinal.telegram.bot
    bot.send_message(c.message.chat.id, prompt)
    _set_state(c.from_user.id, kind="set_template", key=key)
    bot.answer_callback_query(c.id)


def _show_markup_menu(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    pct = CTX.storage.cfg.get("global_markup_pct", 55.0)
    text = f"<b>💰 Наценка</b>\nГлобальная: <b>{pct}%</b>"
    kb = K(row_width=1)
    kb.add(B("✏️ Изменить", callback_data=f"{CB}:autolots_markup_set"))
    kb.add(B("◀️ Авто-лоты", callback_data=f"{CB}:autolots"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _ask_value(c, key: str, prompt: str, cast=str, after_cb=None):
    bot = CTX.cardinal.telegram.bot
    bot.send_message(c.message.chat.id, prompt)
    _set_state(c.from_user.id, kind="set_cfg", key=key, cast=cast)
    bot.answer_callback_query(c.id)


def _show_autoprice_menu(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    cfg = CTX.storage.cfg
    on = cfg.get("auto_price_enabled", True)
    text = (
        f"<b>⭐ Авто-цена</b>\n"
        f"Вкл: {'🟢' if on else '🔴'}\n"
        f"Наценка: <b>{cfg.get('global_markup_pct')}%</b>\n"
        f"Интервал: <b>{cfg.get('auto_price_interval_sec')}с</b>\n"
        f"Кэп скачка: <b>{cfg.get('auto_price_jump_cap_pct')}%</b>"
    )
    kb = K(row_width=2)
    kb.add(B(("🔴" if on else "🟢") + " Вкл/Выкл", callback_data=f"{CB}:autoprice_toggle"))
    kb.add(B("⏱ Интервал", callback_data=f"{CB}:autoprice_interval"),
           B("🔄 Пересчитать сейчас", callback_data=f"{CB}:autoprice_now"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_autoreview_menu(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    cfg = CTX.storage.cfg
    on_bonus = cfg.get("auto_review_bonus_enabled", True)
    pct = cfg.get("auto_review_bonus_pct", 10.0)
    min_stars = cfg.get("auto_review_bonus_min_stars", 5)
    text = (
        "<b>🎁 Бонус за отзыв</b>\n\n"
        f"Бонус-докрутка за {min_stars}+\u2b50 отзыв: {'🟢 вкл' if on_bonus else '🔴 выкл'}\n"
        f"Объём: <b>{pct}%</b> от исходного заказа\n"
        "<i>Например, если заказ был на 1000 — за хороший отзыв запустим ещё +"
        f"{int(1000 * float(pct) / 100)} на тот же линк.</i>"
    )
    kb = K(row_width=1)
    kb.add(B(("🔴 Выкл" if on_bonus else "🟢 Вкл") + " бонус-докрутку",
             callback_data=f"{CB}:autoreview_bonus_toggle"))
    kb.add(B(f"💯 % бонуса (сейчас {pct}%)", callback_data=f"{CB}:autoreview_pct_set"))
    kb.add(B(f"⭐ Мин. рейтинг (сейчас {min_stars}★)", callback_data=f"{CB}:autoreview_stars_set"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_notif_menu(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    cfg = CTX.storage.cfg
    keys = [
        ("notify_order_created", "О создании заказа"),
        ("notify_order_error", "Об ошибке создания"),
        ("notify_balance_before", "Баланс до"),
        ("notify_balance_after", "Баланс после"),
    ]
    text = "<b>🔔 Уведомления в Telegram</b>"
    kb = K(row_width=1)
    for k, label in keys:
        kb.add(B(("🟢 " if cfg.get(k) else "🔴 ") + label,
                 callback_data=f"{CB}:notif_toggle:{k}"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_msg_templates(c):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    t = CTX.storage.templates
    labels = {
        "msg_await_link": "Ожидание ссылки",
        "msg_order_created": "Заказ создан",
        "msg_order_completed": "Заказ выполнен",
        "msg_order_error": "Ошибка",
        "msg_status_reply": "Статус заказа",
    }
    text = ["<b>✉️ Шаблоны сообщений</b>\n"]
    for k, label in labels.items():
        text.append(f"<b>{label}</b>:\n<code>{html_escape(t.get(k, ''))[:300]}</code>\n")
    kb = K(row_width=1)
    for k, label in labels.items():
        kb.add(B(f"✏ {label}", callback_data=f"{CB}:msgtpl_edit:{k}"))
    kb.add(B("🔄 Сбросить все", callback_data=f"{CB}:msgtpl_reset"))
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text("\n".join(text), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_orders_list(c, offset: int):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    PAGE = 8
    orders = list(CTX.storage.orders.values())
    orders.sort(key=lambda o: o.created_at, reverse=True)
    page = orders[offset:offset + PAGE]
    text = [f"<b>📋 Заказы (стр. {offset // PAGE + 1}/{max(1, (len(orders) + PAGE - 1) // PAGE)})</b>"]
    if not page:
        text.append("\n<i>Заказов пока нет.</i>")
    kb = K(row_width=1)
    for o in page:
        status_emoji = {
            "awaiting_link": "🕓", "created": "🟢", "in_progress": "🔄",
            "completed": "✅", "error": "❌", "refunded": "💸",
        }.get(o.status, "❔")
        kb.add(B(f"{status_emoji} #{o.funpay_order_id} | svc {o.service_id}",
                 callback_data=f"{CB}:order:{o.funpay_order_id}"))
    nav = []
    if offset > 0:
        nav.append(B("⬅️", callback_data=f"{CB}:orders:{max(0, offset - PAGE)}"))
    if offset + PAGE < len(orders):
        nav.append(B("➡️", callback_data=f"{CB}:orders:{offset + PAGE}"))
    if nav:
        kb.row(*nav)
    kb.add(B("◀️ Меню", callback_data=f"{CB}:main"))
    bot.edit_message_text("\n".join(text), c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


def _show_order_card(c, order_id: str):
    bot = CTX.cardinal.telegram.bot
    K, B = _kbd()
    o = CTX.storage.orders.get(order_id)
    if not o:
        bot.answer_callback_query(c.id, "Не найдено")
        return
    # Финансы (могут быть нулями для старых заказов без новых полей).
    fp_price = float(getattr(o, "funpay_price", 0.0) or 0.0)
    smm_charge = float(getattr(o, "smmway_charge_rub", 0.0) or 0.0)
    profit = fp_price - smm_charge
    profit_pct = (profit / fp_price * 100.0) if fp_price > 0 else 0.0
    profit_sign = "+" if profit >= 0 else "−"
    profit_emoji = "💎" if profit_pct >= 50 else ("✨" if profit_pct >= 20 else "📈" if profit > 0 else "⚠️")
    svc_name = getattr(o, "service_name_snapshot", "") or ""
    if len(svc_name) > 60:
        svc_name = svc_name[:57] + "…"
    text = (
        f"<b>📋 Заказ #{html_escape(o.funpay_order_id)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 Услуга: {html_escape(svc_name) if svc_name else '—'} <i>(#{o.service_id})</i>\n"
        f"👤 Покупатель: <code>{html_escape(o.buyer_username)}</code>\n"
        f"🔢 Объём: <code>{o.quantity}</code>\n"
        f"🔗 Ссылка: <code>{html_escape(o.link or '—')}</code>\n"
        f"📊 Статус: <b>{o.status}</b> ({html_escape(o.smm_status_raw or '—')})\n"
        f"🤖 SMMWay #: <code>{o.smm_order_id or '—'}</code>\n"
        f"🕐 Создан: <code>{o.created_at}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 На FunPay: <code>{fp_price:.2f} ₽</code>\n"
        f"💸 На SMMWay: <code>{smm_charge:.4f} ₽</code>\n"
        f"{profit_emoji} Прибыль: <code>{profit_sign}{abs(profit):.4f} ₽</code> "
        f"<i>({profit_sign}{abs(profit_pct):.1f}%)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Ошибка: <code>{html_escape(o.error or '—')}</code>"
    )
    kb = K(row_width=1)
    kb.add(B("🔗 Открыть на FunPay",
            url=f"https://funpay.com/orders/{o.funpay_order_id}/"))
    kb.add(B("◀️ К списку", callback_data=f"{CB}:orders:0"))
    bot.edit_message_text(text, c.message.chat.id, c.message.id,
                          reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)


# =============================================================================
# 13. INIT / SHUTDOWN
# =============================================================================


def init_plugin(crd: "Cardinal", *args) -> None:
    """BIND_TO_PRE_INIT — инициализация плагина."""
    try:
        _ensure_plugin_dir()
    except Exception:
        pass
    # File logging (may fail if no write permission — not critical)
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass
    logger.setLevel(logging.INFO)
    logger.info("SMMWay plugin init: v%s", VERSION)

    CTX.cardinal = crd
    CTX.storage = Storage()
    CTX.api = SMMWayAPI(CTX.storage.cfg.get("api_key", ""))

    # Register Telegram menu
    init_tg_menu(crd)

    # Background loops
    if CTX.storage.cfg.get("enabled", True):
        for fn, name in (
            (status_poller_loop, "smmway-status-poller"),
            (auto_price_loop, "smmway-auto-price"),
            (auto_deactivate_loop, "smmway-auto-deact"),
            (service_recovery_loop, "smmway-service-recovery"),
            (state_cleanup_loop, "smmway-state-cleanup"),
        ):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            CTX._threads.append(t)
    logger.info("SMMWay plugin started")


def on_delete(*args, **kwargs) -> None:
    """BIND_TO_DELETE — вызывается при удалении плагина из FPC."""
    CTX.stop()
    logger.info("SMMWay plugin stopped (DELETE)")


# =============================================================================
# 14. FPC HOOKS
# =============================================================================

BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = [on_delete]
