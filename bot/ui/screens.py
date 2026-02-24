from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..db import db
from ..services.telegram_utils import send_message_safe
from ..ui.callbacks import ui_cb

logger = logging.getLogger(__name__)


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔔 Уведомления", callback_data=ui_cb("dashboard", "notifications"))],
            [InlineKeyboardButton("📌 Упоминания", callback_data=ui_cb("dashboard", "mentions"))],
            [InlineKeyboardButton("🧩 Фильтры", callback_data=ui_cb("dashboard", "filters"))],
            [InlineKeyboardButton("⛔ Блокирующие", callback_data=ui_cb("dashboard", "blockers"))],
            [InlineKeyboardButton("⚙️ Настройка", callback_data=ui_cb("dashboard", "settings"))],
        ]
    )


def _get_user_and_target_message_id(update: Update) -> tuple[Optional[int], Optional[int]]:
    """
    КЛЮЧЕВАЯ ЛОГИКА:

    1) Если пришли из callback_query (InlineKeyboard) -> редактируем сообщение,
       в котором нажали кнопку (edit message).

    2) Если пришли из обычного текста (ReplyKeyboard снизу) -> НЕ редактируем старое,
       а всегда отправляем новое сообщение (send message), т.е. message_id=None.
    """
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return None, None

    if update.callback_query and update.callback_query.message:
        return user_id, update.callback_query.message.message_id

    # Любое обычное сообщение (включая ReplyKeyboard) -> всегда новое сообщение
    return user_id, None


async def _render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """
    Унифицированный рендер:
    - callback_query -> редактируем сообщение
    - message -> шлём новое
    """
    user_id, message_id = _get_user_and_target_message_id(update)
    if not user_id:
        return

    # Для callback_query лучше ответить, чтобы Telegram убрал "часики"
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    await send_message_safe(
        context.application,
        user_id,
        text,
        reply_markup=reply_markup,
        message_id=message_id,  # <-- None => send new message
        parse_mode="HTML",
    )


async def render_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 <b>Jira Bot</b>\n\n"
        "Используйте кнопки ниже для навигации.\n"
        "Если вы ещё не настраивали Jira — выполните /setup"
    )
    await _render(update, context, text, reply_markup=_menu_keyboard())


async def render_filters_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    filters_list = await db.get_user_filters(user_id)
    if not filters_list:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))]])
        await _render(
            update,
            context,
            "🧩 <b>Фильтры</b>\n\nУ вас пока нет фильтров.\nДобавьте через /addfilter",
            reply_markup=keyboard,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for f in filters_list:
        filter_id = f.get("id")
        name = f.get("filter_name", "без_имени")
        is_active = bool(f.get("is_active", 1))
        emoji = "✅" if is_active else "⛔"
        if filter_id is None:
            continue
        rows.append([InlineKeyboardButton(f"{emoji} {name}", callback_data=ui_cb("filters", "open", str(filter_id)))])

    rows.append([InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))])

    await _render(
        update,
        context,
        "🧩 <b>Фильтры</b>\n\nНажмите на фильтр, чтобы открыть детали.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def render_filter_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, filter_id: int) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    f = await db.get_filter_by_id(user_id, filter_id)
    if not f:
        await _render(update, context, "❌ Фильтр не найден.", reply_markup=_menu_keyboard())
        return

    name = f.get("filter_name", "без_имени")
    jql = f.get("jql", "")
    is_active = bool(f.get("is_active", 1))
    interval = f.get("check_interval_minutes", 30)
    retention = f.get("retention_days", 30)

    text = (
        f"🧩 <b>Фильтр:</b> <code>{name}</code>\n"
        f"Статус: {'✅ активен' if is_active else '⛔ выключен'}\n"
        f"Интервал: <b>{interval}</b> мин\n"
        f"Retention: <b>{retention}</b> дн\n\n"
        f"<b>JQL:</b>\n<code>{jql}</code>"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔁 Вкл/Выкл", callback_data=ui_cb("filters", "toggle", str(filter_id))),
                InlineKeyboardButton("🗑 Удалить", callback_data=ui_cb("filters", "delete", str(filter_id))),
            ],
            [
                InlineKeyboardButton("⏱ Интервал", callback_data=ui_cb("filters", "interval", str(filter_id))),
                InlineKeyboardButton("♻️ Retention", callback_data=ui_cb("filters", "retention", str(filter_id))),
            ],
            [InlineKeyboardButton("✏️ Изменить JQL", callback_data=ui_cb("filters", "edit", str(filter_id)))],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data=ui_cb("dashboard", "filters")),
                InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home")),
            ],
        ]
    )

    await _render(update, context, text, reply_markup=keyboard)


async def render_mentions_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    mentions = await db.get_unnotified_mentions(user_id)
    if not mentions:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))]])
        await _render(update, context, "📌 <b>Упоминания</b>\n\nНет новых упоминаний.", reply_markup=keyboard)
        return

    rows: list[list[InlineKeyboardButton]] = []
    for m in mentions[:20]:
        issue_key = m.get("issue_key")
        summary = m.get("summary") or ""
        if not issue_key:
            continue
        title = summary[:50] + ("…" if len(summary) > 50 else "")
        rows.append([InlineKeyboardButton(f"📌 {issue_key}: {title}", callback_data=ui_cb("mentions", "open", issue_key))])

    rows.append([InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))])

    await _render(
        update,
        context,
        "📌 <b>Упоминания</b>\n\nНажмите на задачу, чтобы открыть детали.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def render_mention_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, issue_key: str) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    mention = await db.get_mention_by_issue(user_id, issue_key)
    if not mention:
        await _render(update, context, "❌ Упоминание не найдено.", reply_markup=_menu_keyboard())
        return

    summary = mention.get("summary", "")
    excerpt = mention.get("excerpt", "")

    text = f"📌 <b>{issue_key}</b>\n{summary}\n\n<b>Фрагмент:</b>\n{excerpt}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Отметить прочитанным", callback_data=ui_cb("mentions", "read", issue_key)),
                InlineKeyboardButton("⬅️ Назад", callback_data=ui_cb("dashboard", "mentions")),
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))],
        ]
    )

    await _render(update, context, text, reply_markup=keyboard)


async def _notif_to_ui_item(user_id: int, item: dict) -> Optional[dict]:
    issue_key = item.get("issue_key")
    filter_name = item.get("filter_name")
    if not issue_key or not filter_name:
        return None

    filter_id = await db.get_filter_id_by_name(user_id, filter_name)
    if not filter_id:
        return None

    info = await db.get_last_notification_info(user_id, filter_name, issue_key)
    summary = ""
    if info:
        summary = (info.get("issue", {}).get("fields", {}) or {}).get("summary", "") or ""

    return {"issue_key": issue_key, "filter_id": filter_id, "summary": summary}


async def render_notifications_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    raw = await db.get_pending_notifications(user_id)
    raw = raw[:20]

    if not raw:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))]])
        await _render(update, context, "🔔 <b>Уведомления</b>\n\nНет неподтверждённых уведомлений.", reply_markup=keyboard)
        return

    normalized = await asyncio.gather(*(_notif_to_ui_item(user_id, x) for x in raw))
    notifs = [x for x in normalized if x]

    if not notifs:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))]])
        await _render(update, context, "🔔 <b>Уведомления</b>\n\nНет неподтверждённых уведомлений.", reply_markup=keyboard)
        return

    rows: list[list[InlineKeyboardButton]] = []
    for n in notifs:
        issue_key = n["issue_key"]
        filter_id = n["filter_id"]
        summary = n.get("summary") or ""
        title = summary[:50] + ("…" if len(summary) > 50 else "")
        rows.append(
            [InlineKeyboardButton(f"🔔 {issue_key}: {title}", callback_data=ui_cb("notif", "open", str(filter_id), issue_key))]
        )

    rows.append([InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))])

    await _render(
        update,
        context,
        "🔔 <b>Уведомления</b>\n\nНажмите на уведомление, чтобы открыть детали.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def render_notification_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, filter_id: int, issue_key: str) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    filter_name = await db.get_filter_name_by_id(user_id, filter_id)
    if not filter_name:
        await _render(update, context, "❌ Фильтр не найден.", reply_markup=_menu_keyboard())
        return

    notifs = await db.get_notifications_by_issue(user_id, issue_key)
    relevant = [n for n in notifs if n.get("filter_name") == filter_name and not n.get("user_reaction")]
    if not relevant:
        await _render(update, context, "Нет неподтверждённых уведомлений для этой задачи.", reply_markup=_menu_keyboard())
        return

    last = relevant[0]
    summary = last.get("summary", "")
    changes = last.get("changes", "")

    text = (
        f"🔔 <b>{issue_key}</b>\n"
        f"<b>Фильтр:</b> <code>{filter_name}</code>\n\n"
        f"{summary}\n\n"
        f"<b>Изменения:</b>\n{changes}"
    )

    notif_id = last.get("id")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=ui_cb("notif", "ack", str(notif_id))),
                InlineKeyboardButton("🔕 Мьют", callback_data=ui_cb("notif", "mute", str(filter_id), issue_key)),
            ],
            [
                InlineKeyboardButton("⬅️ Назад", callback_data=ui_cb("dashboard", "notifications")),
                InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home")),
            ],
        ]
    )

    await _render(update, context, text, reply_markup=keyboard)


async def render_blockers_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    notifs = await db.get_notifications_with_blockers(user_id, limit=100)

    seen: set[str] = set()
    blockers_items: list[dict] = []
    for n in notifs:
        issue_key = n.get("issue_key")
        details = n.get("details") or {}
        if not issue_key:
            continue

        active_cnt = 0
        summary = ""
        if isinstance(details, dict):
            active_cnt = int(details.get("active_blockers") or 0)
            summary = details.get("summary") or ""

        if active_cnt <= 0:
            continue

        if issue_key in seen:
            continue
        seen.add(issue_key)

        blockers_items.append({"issue_key": issue_key, "summary": summary})
        if len(blockers_items) >= 20:
            break

    if not blockers_items:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))]])
        await _render(update, context, "⛔ <b>Блокирующие</b>\n\nНет данных.", reply_markup=keyboard)
        return

    lines = ["⛔ <b>Блокирующие</b>\n"]
    for b in blockers_items:
        issue_key = b.get("issue_key", "")
        summary = b.get("summary", "")
        lines.append(f"• <b>{issue_key}</b> — {summary}")

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))]])
    await _render(update, context, "\n".join(lines), reply_markup=keyboard)


async def render_settings_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        return

    fun = await db.get_fun_settings(user_id)
    fun_mode = fun.get("fun_mode", "off")
    personality = fun.get("personality", "neutral")
    whisper_enabled = bool(fun.get("whisper_enabled", 1))
    zodiac = fun.get("zodiac", "")

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🎛 Fun mode: <b>{fun_mode}</b>\n"
        f"🧠 Personality: <b>{personality}</b>\n"
        f"🤫 Whisper: <b>{'on' if whisper_enabled else 'off'}</b>\n"
        f"🔮 Zodiac: <b>{zodiac or 'не задан'}</b>\n"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎛 Fun mode", callback_data=ui_cb("settings", "funmode")),
                InlineKeyboardButton("🧠 Personality", callback_data=ui_cb("settings", "personality")),
            ],
            [
                InlineKeyboardButton("🤫 Whisper", callback_data=ui_cb("settings", "whisper")),
                InlineKeyboardButton("🔮 Zodiac", callback_data=ui_cb("settings", "zodiac")),
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data=ui_cb("nav", "home"))],
        ]
    )

    await _render(update, context, text, reply_markup=keyboard)