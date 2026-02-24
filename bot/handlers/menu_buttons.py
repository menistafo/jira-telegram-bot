from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict

from telegram import Update
from telegram.ext import ContextTypes

from ..services.errors import handler_guard
from ..ui.screens import (
    render_blockers_screen,
    render_dashboard,
    render_filters_screen,
    render_mentions_screen,
    render_notifications_screen,
    render_settings_screen,
)

logger = logging.getLogger(__name__)

RenderFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _norm(text: str) -> str:
    """
    Нормализация текста с ReplyKeyboard.
    Telegram иногда присылает:
    - разные пробелы
    - разные emoji / без emoji
    """
    return " ".join((text or "").strip().lower().split())


# Все реальные подписи твоих нижних кнопок + варианты с emoji/без.
_MENU_TEXT_TO_SCREEN: Dict[str, RenderFn] = {
    # Домой / меню
    "меню": render_dashboard,
    "главная": render_dashboard,
    "🏠 меню": render_dashboard,
    "🏠 главная": render_dashboard,

    # Основные разделы
    "уведомления": render_notifications_screen,
    "🔔 уведомления": render_notifications_screen,

    "упоминания": render_mentions_screen,
    "📌 упоминания": render_mentions_screen,

    "фильтры": render_filters_screen,
    "🧩 фильтры": render_filters_screen,

    # У тебя кнопка называется именно так:
    "блокирующие задачи": render_blockers_screen,
    "блокирующие": render_blockers_screen,
    "⛔ блокирующие": render_blockers_screen,
    "⛔ блокирующие задачи": render_blockers_screen,

    # Настройки/управление
    "настройка": render_settings_screen,
    "настройки": render_settings_screen,
    "⚙️ настройка": render_settings_screen,
    "⚙️ настройки": render_settings_screen,
    "управление": render_settings_screen,

    # Развлечения — пока ведём в настройки (там fun/personality/whisper/zodiac)
    "развлечения": render_settings_screen,

    # Справка — ведём на главный экран (там есть подсказка /help),
    # либо можно заменить на отдельный help screen позже
    "справка": render_dashboard,
    "❓ справка": render_dashboard,
}


async def dispatch_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Пытается обработать текст как кнопку нижнего меню.
    Возвращает True если обработали, иначе False.
    """
    if not update.message or not update.effective_user:
        return False

    text = _norm(update.message.text or "")
    if not text:
        return False

    render_fn = _MENU_TEXT_TO_SCREEN.get(text)
    if not render_fn:
        return False

    logger.info("ReplyKeyboard menu navigation: '%s' -> %s", text, render_fn.__name__)
    await render_fn(update, context)
    return True


@handler_guard("handle_menu_buttons")
async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отдельный хэндлер под ReplyKeyboard (нижнее меню).
    """
    await dispatch_menu_text(update, context)