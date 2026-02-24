from __future__ import annotations

import logging
from typing import List

from telegram import Update
from telegram.ext import ContextTypes

from ..db import db
from ..services.errors import handler_guard
from ..services.telegram_utils import send_message_safe
from ..ui.callbacks import UI_PREFIX
from ..ui.screens import (
    render_blockers_screen,
    render_dashboard,
    render_filter_detail,
    render_filters_screen,
    render_mention_detail,
    render_mentions_screen,
    render_notification_detail,
    render_notifications_screen,
    render_settings_screen,
)

logger = logging.getLogger(__name__)


def _normalize_legacy_callback(data: str) -> str:
    """
    Нормализует старые callback_data в новый формат `ui:<scope>:<action>:...`.

    После рефакторинга UI должен использовать единый формат:
        ui:<scope>:<action>[:args...]

    Но:
    - в части кнопок могли остаться старые форматы (например filter:open:123),
    - в чате у пользователя уже есть старые сообщения с такими кнопками.

    Поэтому мы поддерживаем legacy, конвертируя его в `ui:*` “на лету”.
    """
    data = data or ""

    # Already in new format
    if data.startswith(UI_PREFIX):
        return data

    # Legacy patterns
    if data.startswith("filter:"):
        # filter:<action>:<id>
        parts = data.split(":")
        scope = "filters"  # important: plural
        rest = parts[1:]
        return UI_PREFIX + ":".join([scope, *rest])

    if data.startswith("mention:"):
        # mention:<action>:<issue_key>
        parts = data.split(":")
        scope = "mentions"
        rest = parts[1:]
        return UI_PREFIX + ":".join([scope, *rest])

    if data.startswith("settings:"):
        # settings:<action>
        parts = data.split(":")
        scope = "settings"
        rest = parts[1:]
        return UI_PREFIX + ":".join([scope, *rest])

    if data.startswith("notif:"):
        # notif:<action>:...
        parts = data.split(":")
        scope = "notif"
        rest = parts[1:]
        return UI_PREFIX + ":".join([scope, *rest])

    # Old reaction callbacks might be handled separately (ack_/mute_)
    return data


@handler_guard("handle_reaction_callback")
async def handle_reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Backward-compat handler for very old callbacks (ack_/mute_).

    Новый UI должен использовать `ui:*`, но старые сообщения в чате могут жить долго.
    """
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    if data.startswith("ack_"):
        # legacy: ack_<ISSUEKEY>_<HASH>
        parts = data.split("_")
        if len(parts) >= 3:
            issue_key = parts[1]
            notification_hash = parts[2]
            await db.record_user_reaction(user_id, issue_key, notification_hash)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                # не критично
                pass
            await send_message_safe(
                context.application,
                user_id,
                "✅ Вы подтвердили получение уведомления. Я больше не буду напоминать об этой задаче.",
            )
        return

    if data.startswith("mute_"):
        # legacy: mute_<ISSUEKEY>_<FILTER_ID or FILTER_NAME>
        parts = data.split("_", 2)
        if len(parts) >= 3:
            issue_key = parts[1]
            filter_part = parts[2]
            filter_name: str | None = None

            if filter_part.isdigit():
                try:
                    filter_id = int(filter_part)
                    filter_name = await db.get_filter_name_by_id(user_id, filter_id)
                except Exception:
                    filter_name = None
            else:
                filter_name = filter_part

            if filter_name:
                await db.mute_issue_for_filter(user_id, issue_key, filter_name)
                await send_message_safe(
                    context.application,
                    user_id,
                    f"🔕 Окей, больше не буду напоминать по задаче {issue_key} для фильтра <b>{filter_name}</b>.",
                )
        return


@handler_guard("handle_ui_callback")
async def handle_ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Единый роутер для всех UI callback'ов (новых и legacy через normalize)."""
    query = update.callback_query
    if not query:
        return

    await query.answer()
    raw = query.data or ""
    data = _normalize_legacy_callback(raw)

    # Если это не UI — отдаём в legacy обработчик
    if not data.startswith(UI_PREFIX):
        await handle_reaction_callback(update, context)
        return

    # data: ui:<scope>:<action>[:args...]
    parts: List[str] = data.split(":")
    scope = parts[1] if len(parts) > 1 else ""
    action = parts[2] if len(parts) > 2 else ""
    args = parts[3:] if len(parts) > 3 else []

    user_id = query.from_user.id

    # ---------------- Dashboard navigation
    if scope == "nav":
        if action == "home":
            await render_dashboard(update, context)
            return
        await send_message_safe(context.application, user_id, "🤔 Не знаю такой экран. Вернёмся в меню.")
        await render_dashboard(update, context)
        return

    if scope == "dashboard":
        if action == "notifications":
            await render_notifications_screen(update, context)
            return
        if action == "mentions":
            await render_mentions_screen(update, context)
            return
        if action == "filters":
            await render_filters_screen(update, context)
            return
        if action == "blockers":
            await render_blockers_screen(update, context)
            return
        if action == "settings":
            await render_settings_screen(update, context)
            return

        await send_message_safe(context.application, user_id, "🤔 Неизвестное действие.")
        return

    # ---------------- Filters
    if scope == "filters":
        if action == "open" and args:
            try:
                filter_id = int(args[0])
            except ValueError:
                await send_message_safe(context.application, user_id, "❌ Некорректный фильтр.")
                return
            await render_filter_detail(update, context, filter_id)
            return

        if action in {"toggle", "delete", "interval", "retention", "edit"} and args:
            try:
                filter_id = int(args[0])
            except ValueError:
                await send_message_safe(context.application, user_id, "❌ Некорректный фильтр.")
                return

            info = await db.get_filter_by_id(user_id, filter_id)
            if not info:
                await send_message_safe(context.application, user_id, "❌ Фильтр не найден.")
                return

            filter_name = info["filter_name"]

            if action == "toggle":
                new_state = 0 if info.get("is_active") else 1
                await db.toggle_filter_active(user_id, filter_name, bool(new_state))
                await render_filter_detail(update, context, filter_id)
                return

            if action == "delete":
                await db.delete_user_filter(user_id, filter_name)
                await send_message_safe(context.application, user_id, f"🗑 Фильтр <b>{filter_name}</b> удалён.")
                await render_filters_screen(update, context)
                return

            # Действия, требующие ввода от пользователя
            context.user_data["editing_filter_id"] = filter_id

            if action == "interval":
                context.user_data["pending_ui_action"] = "filters_interval"
                await send_message_safe(
                    context.application,
                    user_id,
                    "⏱ Введите новый интервал проверки (в минутах), например: <code>15</code>",
                )
                return

            if action == "retention":
                context.user_data["pending_ui_action"] = "filters_retention"
                await send_message_safe(
                    context.application,
                    user_id,
                    "♻️ Введите срок хранения истории (в днях), например: <code>30</code>",
                )
                return

            if action == "edit":
                context.user_data["pending_ui_action"] = "filters_jql"
                await send_message_safe(
                    context.application,
                    user_id,
                    "✏️ Отправьте новый JQL для фильтра одним сообщением.",
                )
                return

        await send_message_safe(context.application, user_id, "🤔 Не понял действие с фильтрами.")
        return

    # ---------------- Mentions
    if scope == "mentions":
        if action == "open" and args:
            issue_key = args[0]
            await render_mention_detail(update, context, issue_key)
            return

        if action == "read" and args:
            issue_key = args[0]
            await db.mark_mentions_as_notified(user_id, issue_key)
            await render_mentions_screen(update, context)
            return

        await send_message_safe(context.application, user_id, "🤔 Неизвестное действие по упоминаниям.")
        return

    # ---------------- Notifications
    if scope == "notif":
        if action == "open" and len(args) >= 2:
            filter_id = int(args[0])
            issue_key = args[1]
            await render_notification_detail(update, context, filter_id, issue_key)
            return

        if action == "ack" and args:
            notif_id = int(args[0])
            notif = await db.get_notification_by_id(notif_id)
            if notif:
                await db.record_user_reaction(user_id, notif["issue_key"], notif["notification_hash"])
            await render_notifications_screen(update, context)
            return

        if action == "mute" and len(args) >= 2:
            filter_id = int(args[0])
            issue_key = args[1]
            filter_name = await db.get_filter_name_by_id(user_id, filter_id)
            if filter_name:
                await db.mute_issue_for_filter(user_id, issue_key, filter_name)
                await send_message_safe(
                    context.application,
                    user_id,
                    f"🔕 Мьют: {issue_key} для фильтра <b>{filter_name}</b>.",
                )
            await render_notifications_screen(update, context)
            return

        await send_message_safe(context.application, user_id, "🤔 Неизвестное действие по уведомлениям.")
        return

    # ---------------- Settings
    if scope == "settings":
        if action == "funmode":
            settings = await db.get_fun_settings(user_id)
            current = settings.get("fun_mode", "off")
            next_mode = {"off": "light", "light": "full", "full": "off"}.get(current, "light")
            await db.set_fun_mode(user_id, next_mode)
            await render_settings_screen(update, context)
            return

        if action == "personality":
            settings = await db.get_fun_settings(user_id)
            current = settings.get("personality", "neutral")
            variants = ["neutral", "developer", "tester", "support"]
            try:
                next_p = variants[(variants.index(current) + 1) % len(variants)]
            except ValueError:
                next_p = "neutral"
            await db.set_personality(user_id, next_p)
            await render_settings_screen(update, context)
            return

        if action == "whisper":
            settings = await db.get_fun_settings(user_id)
            current = int(settings.get("whisper_enabled", 1) or 0)
            await db.set_whisper_enabled(user_id, 0 if current else 1)
            await render_settings_screen(update, context)
            return

        if action == "zodiac":
            context.user_data["pending_ui_action"] = "settings_zodiac"
            await send_message_safe(
                context.application,
                user_id,
                "🔮 Напишите знак зодиака (например: <code>скорпион</code>).",
            )
            return

        await send_message_safe(context.application, user_id, "🤔 Неизвестное действие настроек.")
        return

    await send_message_safe(context.application, user_id, "🤔 Неизвестный UI callback.")