from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..db import db
from ..services.errors import handler_guard
from ..services.telegram_utils import send_message_safe
from ..ui.screens import render_filter_detail, render_settings_screen

logger = logging.getLogger(__name__)


@handler_guard("handle_text_input")
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик свободного текста (ввод значений после UI-кнопок).

    Используем краткоживущий флаг в `context.user_data`:
      - pending_ui_action: что мы ждём
      - editing_filter_id: id фильтра (для interval/retention/jql)

    Если pending_ui_action нет — делегируем в старый `handle_pending_command`,
    чтобы не ломать текущее поведение.

    ВАЖНО:
    - Навигация через нижнее меню (ReplyKeyboard) обрабатывается отдельным хэндлером:
      bot/handlers/menu_buttons.py
    - Здесь меню не обрабатываем, чтобы не было дублей.
    """
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    pending = context.user_data.get("pending_ui_action")
    if not pending:
        # fallback на старую логику
        from .commands import handle_pending_command  # локальный импорт для избежания циклов
        await handle_pending_command(update, context)
        return

    # Снимаем флаг сразу, чтобы не зациклиться при ошибке
    context.user_data.pop("pending_ui_action", None)

    if pending == "settings_zodiac":
        if not text:
            await send_message_safe(context.application, user_id, "❌ Пустое значение. Попробуйте ещё раз.")
            context.user_data["pending_ui_action"] = "settings_zodiac"
            return

        await db.set_zodiac(user_id, text.lower())
        await send_message_safe(context.application, user_id, f"✅ Знак зодиака сохранён: <b>{text}</b>")
        await render_settings_screen(update, context)
        return

    # --- filter actions ---
    raw_filter_id = context.user_data.get("editing_filter_id")
    try:
        filter_id = int(raw_filter_id)
    except Exception:
        filter_id = None

    if pending in {"filters_interval", "filters_retention", "filters_jql"} and not filter_id:
        await send_message_safe(
            context.application,
            user_id,
            "❌ Не могу определить фильтр. Откройте фильтр заново через меню.",
        )
        return

    if pending == "filters_interval":
        try:
            minutes = int(text)
        except ValueError:
            await send_message_safe(context.application, user_id, "❌ Нужно число. Например: <code>15</code>")
            context.user_data["pending_ui_action"] = "filters_interval"
            return

        if minutes < 1 or minutes > 1440:
            await send_message_safe(context.application, user_id, "❌ Интервал должен быть от 1 до 1440 минут.")
            context.user_data["pending_ui_action"] = "filters_interval"
            return

        info = await db.get_filter_by_id(user_id, filter_id)
        if not info:
            await send_message_safe(context.application, user_id, "❌ Фильтр не найден.")
            return

        ok = await db.update_filter_interval(user_id, info["filter_name"], minutes)
        await send_message_safe(
            context.application,
            user_id,
            f"✅ Интервал обновлён: {minutes} минут." if ok else "❌ Не удалось обновить интервал.",
        )
        await render_filter_detail(update, context, filter_id)
        return

    if pending == "filters_retention":
        try:
            days = int(text)
        except ValueError:
            await send_message_safe(context.application, user_id, "❌ Нужно число. Например: <code>30</code>")
            context.user_data["pending_ui_action"] = "filters_retention"
            return

        if days < 1 or days > 3650:
            await send_message_safe(context.application, user_id, "❌ Значение должно быть от 1 до 3650 дней.")
            context.user_data["pending_ui_action"] = "filters_retention"
            return

        info = await db.get_filter_by_id(user_id, filter_id)
        if not info:
            await send_message_safe(context.application, user_id, "❌ Фильтр не найден.")
            return

        ok = await db.update_filter_retention(user_id, info["filter_name"], days)
        await send_message_safe(
            context.application,
            user_id,
            f"✅ Retention обновлён: {days} дней." if ok else "❌ Не удалось обновить retention.",
        )
        await render_filter_detail(update, context, filter_id)
        return

    if pending == "filters_jql":
        if len(text) < 5:
            await send_message_safe(context.application, user_id, "❌ JQL слишком короткий. Попробуйте ещё раз.")
            context.user_data["pending_ui_action"] = "filters_jql"
            return

        info = await db.get_filter_by_id(user_id, filter_id)
        if not info:
            await send_message_safe(context.application, user_id, "❌ Фильтр не найден.")
            return

        ok = await db.update_filter_jql(user_id, info["filter_name"], text)
        await send_message_safe(context.application, user_id, "✅ JQL обновлён." if ok else "❌ Не удалось обновить JQL.")
        await render_filter_detail(update, context, filter_id)
        return

    logger.warning("Unknown pending_ui_action=%s", pending)
    await send_message_safe(context.application, user_id, "🤔 Не понял ввод. Используйте кнопки меню.")