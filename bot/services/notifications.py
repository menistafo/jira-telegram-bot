from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from ..config import ADMIN_USER_IDS
from ..db import db
from ..jira_manager import jira_manager
from ..utils import should_send_reminder_check
from ..jira_client import JiraAuthError
from ..mentions import mentions_tracker
from ..watcher import smart_watcher
from ..ui.callbacks import ui_cb
from .telegram_utils import send_message_safe, clean_text

logger = logging.getLogger(__name__)


async def _get_fun_settings(user_id: int) -> dict:
    """Получить fun-настройки пользователя.
    Вынесено локально, чтобы избежать циклических импортов между handlers ↔ services ↔ ui.
    """
    try:
        return await db.get_fun_settings(user_id)
    except Exception:
        return {
            "fun_mode": "off",
            "personality": "neutral",
            "whisper_enabled": 1,
            "zodiac": None,
            "xp": 0,
            "level": 1,
            "streak": 0,
        }


def _is_fun_enabled(settings: dict) -> bool:
    return (settings or {}).get("fun_mode") in ("light", "full")


def _is_full_fun(settings: dict) -> bool:
    return (settings or {}).get("fun_mode") == "full"


def _status_emotion(status: str) -> str:
    s = (status or "").lower()
    if any(x in s for x in ("done", "готов", "закры", "resolved", "выполн")):
        return "🎉 Ура, оно закрыто! (почти поверил)"
    if any(x in s for x in ("in progress", "в работе", "doing")):
        return "🚀 Поехали, держим темп"
    if any(x in s for x in ("blocked", "заблок", "ожида", "hold")):
        return "🧱 Опа, блокер. Дышим, не паникуем"
    if any(x in s for x in ("to do", "откры", "backlog")):
        return "📝 Задача живёт. Пока что."
    return ""


async def _maybe_add_fun_snippet(
    user_id: int,
    base_text: str,
    status: str,
    is_reminder: bool,
    notification_hash: str,
) -> str:
    """Добавить небольшую "fun"-приписку к уведомлению (если включено)."""
    settings = await _get_fun_settings(user_id)
    if not _is_fun_enabled(settings):
        return base_text

    try:
        show_progress = (int(notification_hash[:2], 16) % 10 == 0) and not is_reminder
    except Exception:
        show_progress = False

    extra_lines: List[str] = []
    if _is_full_fun(settings) and not is_reminder:
        extra_lines.append(_status_emotion(status))

    if show_progress:
        xp = settings.get("xp", 0)
        level = settings.get("level", 1)
        streak = settings.get("streak", 0)
        extra_lines.append(f"✨ XP: {xp} | lvl {level} | streak {streak}")

    if extra_lines:
        return base_text.rstrip() + "\n" + "\n".join(extra_lines) + "\n"
    return base_text


async def check_all_task_mutes(user_id: int, issue_key: str) -> bool:
    all_notifications = await db.get_notifications_by_issue(user_id, issue_key)
    for notif in all_notifications:
        filter_name = notif["filter_name"]
        if await db.is_task_muted(user_id, issue_key, filter_name):
            logger.info(f"🔇 Task {issue_key} is muted in filter '{filter_name}'")
            return True
    return False


async def handle_jira_auth_error(application: Application, user_id: int, context_msg: str = ""):
    """Обрабатывает ошибку 401: уведомляет пользователя и админов (не чаще раза в час), удаляет клиент."""
    last_error = await db.get_last_auth_error_time(user_id)
    now = datetime.now()
    send_notification = False

    if last_error is None:
        send_notification = True
    else:
        try:
            last_time = datetime.fromisoformat(last_error)
            if (now - last_time) > timedelta(hours=1):
                send_notification = True
        except Exception:
            send_notification = True  # на случай битого формата

    if send_notification:
        await db.update_last_auth_error_time(user_id, now.isoformat())

        # Пользователю
        await send_message_safe(
            application,
            user_id,
            "❌ Проблема с авторизацией в Jira. Возможно, истек срок действия токена.\n"
            "Пожалуйста, настройте подключение заново: /setup",
        )

        # Админам
        for admin_id in ADMIN_USER_IDS:
            await send_message_safe(
                application,
                admin_id,
                f"⚠️ У пользователя {user_id} возникла ошибка авторизации в Jira (401).",
            )

    # Удаляем/инвалидируем клиент (он всё равно нерабочий)
    await jira_manager.invalidate_client(user_id)


async def send_notification_with_changes(
    application: Application,
    user_id: int,
    filter_name: str,
    change_info: Dict[str, Any],
    is_reminder: bool = False,
    previous_message_id: Optional[int] = None,
) -> Optional[int]:
    """Отправить уведомление с информацией об изменениях (компактный формат)"""
    if await db.is_notifications_muted(user_id):
        logger.debug(f"🔇 Skipping notification for muted user {user_id}")
        return None

    issue = change_info["issue"]
    issue_key = change_info["issue_key"]
    change_type = change_info["change_type"]
    notification_hash = change_info["notification_hash"]
    blockers = change_info.get("blockers", [])
    changes = change_info.get("changes", {})

    if await db.is_task_muted(user_id, issue_key, filter_name):
        logger.info(f"🔇 Task {issue_key} (filter: {filter_name}) is muted for user {user_id}")
        return None

    if await db.has_user_reacted(user_id, issue_key, notification_hash) and not is_reminder:
        logger.debug(f"✅ User {user_id} already reacted to {issue_key}")
        return None

    if is_reminder and not should_send_reminder_check():
        logger.info(f"🌙 Пропускаем напоминание для user {user_id} - ночное время по МСК")
        return None

    client = jira_manager.get_client(user_id)
    if not client:
        logger.error(f"❌ No Jira client for user {user_id}")
        return None

    fields = issue.get("fields", {})
    summary = fields.get("summary", "Без названия")
    status = fields.get("status", {}).get("name", "Неизвестно")
    priority = fields.get("priority", {}).get("name", "Не указан")
    assignee_data = fields.get("assignee")
    assignee = assignee_data.get("displayName", "Не назначен") if assignee_data else "Не назначен"

    if is_reminder:
        title = f"⏰ Напоминание: задача {issue_key}"
    else:
        title, _ = smart_watcher.get_change_description(changes)

    # Ночной «шёпот» (если fun включен)
    fun_settings = await _get_fun_settings(user_id)
    if _is_fun_enabled(fun_settings) and fun_settings.get("whisper_enabled", 1) and not should_send_reminder_check():
        title = f"🤫 Шёпотом: {title}"

    changes_text = ""
    if changes:
        changes_text = "\n📌 Что изменилось:\n"
        if "status_changed" in changes:
            status_info = changes["status_changed"]
            from_status = status_info["from"] or "нет статуса"
            to_status = status_info["to"] or "неизвестно"
            changes_text += f"• Статус: {from_status} → {to_status}\n"
        if "new_comments" in changes:
            cnt = len(changes["new_comments"])
            changes_text += f"• Новых комментариев: {cnt}\n"
        if "updated_comments" in changes:
            cnt = len(changes["updated_comments"])
            changes_text += f"• ✏️ Отредактировано комментариев: {cnt}\n"
        if "new_links" in changes:
            cnt = len(changes["new_links"])
            changes_text += f"• Новых связей: {cnt}\n"
        if "assignee_changed" in changes:
            assignee_info = changes["assignee_changed"]
            from_assignee = assignee_info["from"] or "не назначен"
            to_assignee = assignee_info["to"] or "не назначен"
            changes_text += f"• Исполнитель: {from_assignee} → {to_assignee}\n"
        if "priority_changed" in changes:
            priority_info = changes["priority_changed"]
            from_priority = priority_info["from"] or "не указан"
            to_priority = priority_info["to"] or "не указан"
            emoji = "⬆️" if priority_info.get("priority_increased") else ""
            changes_text += f"• {emoji} Приоритет: {from_priority} → {to_priority}\n"
        if "blockers_resolved" in changes:
            resolved = changes["blockers_resolved"]
            cnt = len(resolved)
            changes_text += f"• Разрешено блокировок: {cnt}\n"
        if "labels_changed" in changes:
            added = changes["labels_changed"].get("added", [])
            if added:
                changes_text += f"• 🏷️ Добавлены метки: {', '.join(added[:3])}\n"
        if "due_date_changed" in changes:
            due = changes["due_date_changed"]
            if due.get("to"):
                changes_text += f"• ⏰ Срок: {due['to']}\n"
        if "components_changed" in changes:
            added = changes["components_changed"].get("added", [])
            if added:
                changes_text += f"• 🧩 Добавлены компоненты: {', '.join(added[:3])}\n"

    blockers_text = ""
    active_blockers = [b for b in blockers if not b.get("is_resolved", True)]
    resolved_blockers_list = [b for b in blockers if b.get("is_resolved", False)]

    if active_blockers:
        blockers_text += f"\n🚧 Активные блокировки ({len(active_blockers)}):\n"
        for blocker in active_blockers[:3]:
            summary_short = clean_text(blocker.get("summary", ""))[:50]
            assignee_short = clean_text(blocker.get("assignee", "Не назначен"))[:20]
            blockers_text += f" • {blocker['key']}: {summary_short}… ({assignee_short})\n"
        if len(active_blockers) > 3:
            blockers_text += f" … и еще {len(active_blockers) - 3}\n"

    if resolved_blockers_list:
        blockers_text += f"\n✅ Разрешённые блокировки ({len(resolved_blockers_list)}):\n"
        for blocker in resolved_blockers_list[:2]:
            summary_short = clean_text(blocker.get("summary", ""))[:50]
            blockers_text += f" • {blocker['key']}: {summary_short}…\n"
        if len(resolved_blockers_list) > 2:
            blockers_text += f" … и еще {len(resolved_blockers_list) - 2}\n"

    safe_title = clean_text(title)
    safe_filter_name = clean_text(filter_name)
    safe_summary = clean_text(summary)[:100]

    text = (
        f"{safe_title}\n"
        f"🔎 Фильтр: {safe_filter_name}\n"
        f"🧩 Задача: {issue_key}\n"
        f"📝 Сводка: {safe_summary}\n"
        f"📌 Статус: {clean_text(status)}\n"
        f"⚡ Приоритет: {clean_text(priority)}\n"
        f"👤 Назначена: {clean_text(assignee)}\n"
        f"{changes_text}{blockers_text}\n"
    )

    text = await _maybe_add_fun_snippet(user_id, text, status, is_reminder, notification_hash)

    filter_id = await db.get_filter_id_by_name(user_id, filter_name) or 0

    # Используем новый формат callback
    keyboard = [
        [InlineKeyboardButton("🔗 Открыть в Jira", url=f"{client.base_url}/browse/{issue_key}")],
        [InlineKeyboardButton("✅ Получил", callback_data=f"notif:ack:0")],  # временно, заменим после записи
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_id = await send_message_safe(
        application,
        user_id,
        text,
        reply_markup=reply_markup,
        message_id=previous_message_id,
    )

    if not message_id:
        logger.error(f"❌ Failed to send notification to user {user_id}")
        return None

    notification_details = {
        "summary": safe_summary,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "changes": changes,
        "blockers_count": len(blockers),
        "active_blockers": len(active_blockers),
        "resolved_blockers": len(resolved_blockers_list),
        "blockers": blockers,
        "is_reminder": is_reminder,
        "message_id": message_id,
        "changes_description": smart_watcher.get_change_description(changes)[1],
    }

    notif_id = await db.record_notification(
        user_id=user_id,
        filter_name=filter_name,
        issue_key=issue_key,
        notification_hash=notification_hash,
        change_type=change_type,
        details=notification_details,
    )

    # Обновляем клавиатуру с правильным notif_id
    if notif_id and message_id:
        try:
            new_keyboard = [
                [InlineKeyboardButton("🔗 Открыть в Jira", url=f"{client.base_url}/browse/{issue_key}")],
                [
                    InlineKeyboardButton("✅ Получил", callback_data=f"notif:ack:{notif_id}"),
                    InlineKeyboardButton("🔕 Не напоминать сутки", callback_data=f"notif:mute:{filter_id}:{issue_key}"),
                ],
            ]
            await application.bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=message_id,
                reply_markup=InlineKeyboardMarkup(new_keyboard),
            )
        except Exception:
            pass

    logger.info(f"✅ Notification sent to user {user_id}: {issue_key} with changes: {list(changes.keys())}")
    return message_id


async def send_mention_notification(
    application: Application,
    user_id: int,
    mention_info: Dict[str, Any],
) -> Optional[int]:
    """
    ✅ FIX по требованию:
    Упоминания учитываем/показываем ТОЛЬКО из комментариев.
    (mention_type == 'comment')
    """
    try:
        issue = mention_info["issue"]
        issue_key = mention_info["issue_key"]
        fields = issue.get("fields", {})

        # --- ✅ ONLY COMMENTS ---
        all_mentions = mention_info.get("mentions", []) or []
        mentions = [m for m in all_mentions if m.get("mention_type") == "comment"]

        # Пересчитываем счетчики под "comment only"
        total_mentions = len(mentions)
        new_mentions_count = mention_info.get("new_mentions_count", total_mentions)
        # если new_mentions_count передан "общий", обрежем до comment-only
        new_mentions_count = min(int(new_mentions_count or 0), total_mentions)

        if new_mentions_count == 0 or total_mentions == 0:
            logger.info(f"🔕 No NEW comment-mentions for issue {issue_key}, skipping notification")
            return None

        if await db.is_notifications_muted(user_id):
            logger.debug(f"🔇 Skipping mention notification for muted user {user_id}")
            return None

        summary = fields.get("summary", "Без названия")
        status = fields.get("status", {}).get("name", "Неизвестно")
        priority = fields.get("priority", {}).get("name", "Не указан")

        client = jira_manager.get_client(user_id)
        if not client:
            return None

        unique_mentioners = set()
        for mention in mentions:
            mention_details = mention.get("mention_details", {}) or {}
            author = mention_details.get("author") or mention.get("mentioned_by")
            if author:
                unique_mentioners.add(clean_text(author))

        # Берём последнее упоминание ТОЛЬКО из comment
        last_mention = mentions[0]
        mention_details = last_mention.get("mention_details", {}) or {}
        last_mention_text = (mention_details.get("body", "") or "")[:200]
        last_mention_author = mention_details.get("author", "Неизвестно")
        if len((mention_details.get("body", "") or "")) > 200:
            last_mention_text += "..."

        text = f"""👋 Вас упомянули в задаче Jira

🧩 Задача: {issue_key} ({client.base_url}/browse/{issue_key})
📝 Сводка: {clean_text(summary)}
📌 Статус: {clean_text(status)}
⚡ Приоритет: {clean_text(priority)}

🔔 Новых упоминаний (в комментариях): {new_mentions_count} (всего: {total_mentions})
👤 Упомянули: {', '.join(list(unique_mentioners)[:5])}{f" и еще {len(unique_mentioners) - 5}" if len(unique_mentioners) > 5 else ""}

💬 Последнее упоминание от {clean_text(last_mention_author)}:
{clean_text(last_mention_text)}

Нажмите на кнопку ниже, чтобы открыть задачу и увидеть все упоминания.
"""

        keyboard = [
            [InlineKeyboardButton("🔗 Открыть задачу", url=f"{client.base_url}/browse/{issue_key}")],
            [InlineKeyboardButton("✅ Увидел", callback_data=ui_cb("mention", "read", issue_key))],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_id = await send_message_safe(application, user_id, text, reply_markup=reply_markup)
        if message_id:
            logger.info(
                f"✅ Mention notification sent for {issue_key} ({new_mentions_count} NEW comment-mentions) to user {user_id}"
            )
            await db.mark_mentions_as_notified(user_id, issue_key)
        return message_id

    except Exception as e:
        logger.error(f"❌ Error sending mention notification: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None
