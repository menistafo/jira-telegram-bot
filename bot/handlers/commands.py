from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..config import (
    DEFAULT_CHECK_INTERVAL,
    ADMIN_USER_IDS,
    REMINDER_INTERVAL_MINUTES,
    MENTIONS_ENABLED,
    MENTIONS_CHECK_INTERVAL,
    NIGHT_MODE_ENABLED,
    NIGHT_START_HOUR,
    NIGHT_END_HOUR,
    WEBAPP_URL,
)
from ..db import db
from ..jira_manager import jira_manager
from ..jira_client import JiraAuthError
from ..fun import (
    get_random_meme,
    get_random_horoscope,
    get_random_fact,
    ROLE_PREFIX,
    ZODIAC_MAP,
)
from ..digest import generate_user_digest
from ..utils import (
    get_msk_time,
    get_msk_time_obj,
    should_send_reminder_check,
    format_time_until_morning,
)
from ..ui.screens import (
    render_dashboard,
    render_notifications_screen,
    render_notification_detail,
    render_mentions_screen,
    render_mention_detail,
    render_filters_screen,
    render_filter_detail,
    render_blockers_screen,
    render_settings_screen,
)
from ..ui.callbacks import ui_cb
from ..services.telegram_utils import send_message_safe, clean_text
from ..services.user import get_user_data
from ..services.notifications import handle_jira_auth_error, send_mention_notification
from ..services.blockers import collect_active_blockers_now
from ..services.errors import handler_guard
from ..services.ratelimit import rate_limit
from ..mentions import mentions_tracker
from ..watcher import smart_watcher

logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
SETUP_JIRA_USER, SETUP_JIRA_TOKEN, ADD_FILTER_NAME, ADD_FILTER_JQL = range(4)


# ---------------------------
# Нижнее меню (кнопки внизу)
# ---------------------------

def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Постоянные кнопки внизу бота (ReplyKeyboardMarkup).
    """
    keyboard = [
        [KeyboardButton("Настройки"), KeyboardButton("Фильтры")],
        [KeyboardButton("Блокирующие задачи")],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        is_persistent=True,
    )


async def ensure_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Telegram показывает ReplyKeyboard после первого сообщения с reply_markup.
    Чтобы кнопки были всегда видны — ставим её при /start (и можно при /menu).
    """
    try:
        if update.effective_user:
            await send_message_safe(
                context.application,
                update.effective_user.id,
                "Выбери действие:",
                reply_markup=main_reply_keyboard(),
            )
    except Exception:
        # Не критично, не валим обработчик
        pass


# ---------------------------
# Base commands
# ---------------------------

@handler_guard()
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена любого диалога"""
    user_id = update.effective_user.id
    session_id = context.user_data.get("session_id")
    if session_id:
        await db.delete_session(session_id)
    if "session_id" in context.user_data:
        del context.user_data["session_id"]
    await send_message_safe(context.application, user_id, "❌ Операция отменена.", reply_markup=main_reply_keyboard())
    return ConversationHandler.END


@handler_guard()
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start: дашборд"""
    await render_dashboard(update, context)
    # ✅ ВАЖНО: ставим нижние кнопки
    await ensure_main_keyboard(update, context)


@handler_guard()
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/menu: просто показать нижние кнопки (на случай, если Telegram их скрыл)"""
    await ensure_main_keyboard(update, context)


# ---------------------------
# Setup flow
# ---------------------------

@handler_guard()
async def setup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id

    if user_data.get("jira_user") and user_data.get("jira_token"):
        await send_message_safe(
            context.application,
            user_id,
            "✅ Вы уже настроили подключение к Jira.\n\n"
            "Если хотите изменить настройки, используйте:\n"
            "/reconfigure - перенастроить Jira\n"
            "/filters - управление фильтрами",
            reply_markup=main_reply_keyboard(),
        )
        return ConversationHandler.END

    await send_message_safe(
        context.application,
        user_id,
        "🔧 Настройка подключения к Jira\n\n"
        "Для работы бота нужны:\n"
        "1. Jira Username (ваш логин в Jira)\n"
        "2. Jira API Token (можно получить в настройках Jira)\n\n"
        "Шаг 1 из 2:\n"
        "Отправьте мне ваш Jira Username (обычно это email или логин):",
        reply_markup=main_reply_keyboard(),
    )

    session_id = await db.create_session(
        user_id=user_data["user_id"],
        step="setup_jira_user",
    )
    context.user_data["session_id"] = session_id
    return SETUP_JIRA_USER


@handler_guard()
async def setup_jira_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jira_user = (update.message.text or "").strip()
    session_id = context.user_data.get("session_id")
    user_id = update.effective_user.id

    if not session_id:
        await send_message_safe(context.application, user_id, "❌ Ошибка сессии. Начните заново: /setup", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    await db.update_session(session_id, step="setup_jira_token", data={"jira_user": jira_user})

    await send_message_safe(
        context.application,
        user_id,
        f"✅ Username сохранен: {jira_user}\n\n"
        "Шаг 2 из 2:\n"
        "Отправьте мне ваш Jira API Token:\n\n"
        "Как получить токен:\n"
        "1. Откройте Jira в браузере\n"
        "2. Профиль → Управление аккаунта\n"
        "3. Безопасность → API токены\n"
        "4. Создать токен\n\n"
        "Внимание: Токен будет сохранен в зашифрованном виде.",
        reply_markup=main_reply_keyboard(),
    )
    return SETUP_JIRA_TOKEN


@handler_guard()
async def setup_jira_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jira_token = (update.message.text or "").strip()
    session_id = context.user_data.get("session_id")
    user_id = update.effective_user.id

    if not session_id:
        await send_message_safe(context.application, user_id, "❌ Ошибка сессии. Начните заново: /setup", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    session = await db.get_session(session_id)
    if not session:
        await send_message_safe(context.application, user_id, "❌ Сессия устарела. Начните заново: /setup", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    jira_user = session["data"].get("jira_user")
    user_id_db = session["user_id"]

    await send_message_safe(context.application, user_id, "🔄 Проверяю подключение к Jira...", reply_markup=main_reply_keyboard())

    if await jira_manager.test_user_connection(user_id_db, jira_user, jira_token):
        await db.update_user_jira_credentials(user_id_db, jira_user, jira_token)
        await jira_manager.create_client(user_id_db, jira_user, jira_token)

        await send_message_safe(
            context.application,
            user_id,
            "✅ Подключение к Jira успешно настроено!\n\n"
            f"Напоминания будут приходить каждые {REMINDER_INTERVAL_MINUTES} минут, если не подтвердить уведомление.\n"
            "Ночью (22:00-09:00 МСК) напоминания приостанавливаются.\n\n"
            f"Бот также будет проверять упоминания @{jira_user} каждые {MENTIONS_CHECK_INTERVAL} минут.\n\n"
            "Теперь вы можете:\n"
            "• /addfilter - Добавить фильтр для отслеживания\n"
            "• /filters - Просмотреть ваши фильтры\n"
            "• /checkmentions - Проверить упоминания\n"
            "• /help - Все команды",
            reply_markup=main_reply_keyboard(),
        )

        await db.delete_session(session_id)
        context.user_data.pop("session_id", None)
        return ConversationHandler.END

    await send_message_safe(
        context.application,
        user_id,
        "❌ Не удалось подключиться к Jira\n\n"
        "Проверьте:\n"
        "1. Правильность username и токена\n"
        "2. Доступность Jira сервера\n"
        "3. Не истек ли срок действия токена\n\n"
        "Попробуйте снова: /setup",
        reply_markup=main_reply_keyboard(),
    )
    await db.delete_session(session_id)
    context.user_data.pop("session_id", None)
    return ConversationHandler.END


@handler_guard()
async def reconfigure_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    await db.update_user_jira_credentials(user_data["user_id"], None, None)

    # ✅ более безопасно, чем remove_client, т.к. в твоей ветке уже используется invalidate_client
    try:
        await jira_manager.invalidate_client(user_data["user_id"])
    except Exception:
        try:
            jira_manager.remove_client(user_data["user_id"])  # на случай старой реализации
        except Exception:
            pass

    await send_message_safe(
        context.application,
        user_id,
        "🧹 Настройки Jira сброшены.\n\nТеперь настройте заново: /setup",
        reply_markup=main_reply_keyboard(),
    )


# ---------------------------
# Filters
# ---------------------------

@rate_limit("addfilter_cmd")
@handler_guard()
async def addfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id

    if not user_data.get("jira_user") or not user_data.get("jira_token"):
        await send_message_safe(context.application, user_id, "❌ Сначала настройте подключение к Jira:\n/setup", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    await send_message_safe(
        context.application,
        user_id,
        "➕ Добавление нового фильтра\n\n"
        "Примеры JQL для отслеживания блокировок:\n"
        "-- Все заблокированные задачи, назначенные на меня\n"
        "assignee = currentUser() AND resolution = Unresolved AND issuelinks in blocks()\n\n"
        "-- Задачи, которые я блокирую\n"
        "issuelinks in outwards('Blocks') AND resolution = Unresolved\n\n"
        "-- Активные блокировки в проекте\n"
        "project = PROJ AND status in ('Блокировано', 'В ожидании') AND resolution = Unresolved\n\n"
        "Шаг 1 из 2:\n"
        "Отправьте мне название фильтра (например: Мои блокировки, Блокирующие задачи и т.д.):",
        reply_markup=main_reply_keyboard(),
    )

    session_id = await db.create_session(user_id=user_data["user_id"], step="add_filter_name")
    context.user_data["session_id"] = session_id
    return ADD_FILTER_NAME


@handler_guard()
async def add_filter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filter_name = (update.message.text or "").strip()
    session_id = context.user_data.get("session_id")
    user_id = update.effective_user.id

    if not session_id:
        await send_message_safe(context.application, user_id, "❌ Ошибка сессии. Начните заново: /addfilter", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    await db.update_session(session_id, step="add_filter_jql", data={"filter_name": filter_name})

    await send_message_safe(
        context.application,
        user_id,
        f"✅ Название сохранено: {filter_name}\n\n"
        "Шаг 2 из 2:\n"
        "Отправьте мне JQL запрос для этого фильтра.\n\n"
        "Примеры JQL для блокировок:\n"
        "assignee = currentUser() AND resolution = Unresolved AND issuelinks in blocks()\n"
        "project = PROJ AND status = Блокировано\n"
        "created >= -7d AND issuelinks is not empty ORDER BY created DESC\n\n"
        f"Фильтр будет проверяться каждые {DEFAULT_CHECK_INTERVAL} минут.\n"
        "Бонус: Бот автоматически проанализирует все связанные блокирующие задачи!",
        reply_markup=main_reply_keyboard(),
    )
    return ADD_FILTER_JQL


@handler_guard()
async def add_filter_jql(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jql = (update.message.text or "").strip()
    session_id = context.user_data.get("session_id")
    user_id = update.effective_user.id

    if not session_id:
        await send_message_safe(context.application, user_id, "❌ Ошибка сессии. Начните заново: /addfilter", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    session = await db.get_session(session_id)
    if not session:
        await send_message_safe(context.application, user_id, "❌ Сессия устарела. Начните заново: /addfilter", reply_markup=main_reply_keyboard())
        return ConversationHandler.END

    filter_name = session["data"].get("filter_name")
    user_id_db = session["user_id"]

    if await db.add_user_filter(user_id_db, filter_name, jql):
        await send_message_safe(
            context.application,
            user_id,
            "✅ Фильтр добавлен!\n\n"
            f"Название: {filter_name}\n"
            f"JQL: {jql[:100]}{'...' if len(jql) > 100 else ''}\n\n"
            f"Теперь бот будет проверять этот фильтр каждые {DEFAULT_CHECK_INTERVAL} минут.\n"
            f"Напоминания будут приходить каждые {REMINDER_INTERVAL_MINUTES} минут, если не подтвердить.\n\n"
            "Особенность: Бот автоматически проанализирует все блокирующие задачи!",
            reply_markup=main_reply_keyboard(),
        )
    else:
        await send_message_safe(
            context.application,
            user_id,
            "❌ Ошибка при добавлении фильтра\n\n"
            "Возможно, фильтр с таким названием уже существует.\n"
            "Попробуйте снова: /addfilter",
            reply_markup=main_reply_keyboard(),
        )

    await db.delete_session(session_id)
    context.user_data.pop("session_id", None)
    return ConversationHandler.END


@rate_limit("filters_cmd")
@handler_guard()
async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Перенаправляем на экран фильтров
    await render_filters_screen(update, context)
    await ensure_main_keyboard(update, context)


@rate_limit("deletefilter_cmd")
@handler_guard()
async def deletefilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await send_message_safe(context.application, user_id, "❌ Укажите название фильтра:\n/deletefilter НАЗВАНИЕ", reply_markup=main_reply_keyboard())
        return

    user_data = await get_user_data(update)
    filter_name = " ".join(context.args)
    if await db.delete_user_filter(user_data["user_id"], filter_name):
        await send_message_safe(context.application, user_id, f"✅ Фильтр '{clean_text(filter_name)}' удален.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    else:
        await send_message_safe(context.application, user_id, f"❌ Фильтр '{clean_text(filter_name)}' не найден.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@rate_limit("togglefilter_cmd")
@handler_guard()
async def togglefilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await send_message_safe(context.application, user_id, "❌ Укажите название фильтра:\n/togglefilter НАЗВАНИЕ", reply_markup=main_reply_keyboard())
        return

    user_data = await get_user_data(update)
    filter_name = " ".join(context.args)
    user_filters = await db.get_user_filters(user_data["user_id"], active_only=False)
    target_filter = next((f for f in user_filters if f["filter_name"] == filter_name), None)
    if not target_filter:
        await send_message_safe(context.application, user_id, f"❌ Фильтр '{clean_text(filter_name)}' не найден.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
        return

    new_state = not target_filter["is_active"]
    if await db.toggle_filter_active(user_data["user_id"], filter_name, new_state):
        status = "включен" if new_state else "выключен"
        await send_message_safe(context.application, user_id, f"✅ Фильтр '{clean_text(filter_name)}' {status}.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    else:
        await send_message_safe(context.application, user_id, f"❌ Ошибка изменения фильтра '{clean_text(filter_name)}'.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@handler_guard()
async def interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Изменить интервал.
    Поддерживает режим pending_cmd (когда команда вызвана из меню/экрана).
    """
    user_id = update.effective_user.id

    # Проверяем, не пришло ли из pending_cmd
    if "editing_filter_id" in context.user_data:
        filter_id = context.user_data.pop("editing_filter_id")
        filter_info = await db.get_filter_by_id(user_id, filter_id)
        if not filter_info:
            await send_message_safe(context.application, user_id, "❌ Фильтр не найден.", reply_markup=main_reply_keyboard())
            return
        try:
            interval = int((update.message.text or "").strip())
        except Exception:
            await send_message_safe(context.application, user_id, "❌ Введите число.", reply_markup=main_reply_keyboard())
            return
        if interval < 1 or interval > 1440:
            await send_message_safe(context.application, user_id, "❌ Интервал должен быть от 1 до 1440 минут.", reply_markup=main_reply_keyboard())
            return
        if await db.update_filter_interval(user_id, filter_info["filter_name"], interval):
            await send_message_safe(context.application, user_id, f"✅ Интервал для фильтра '{filter_info['filter_name']}' изменён на {interval} мин.", reply_markup=main_reply_keyboard())
        else:
            await send_message_safe(context.application, user_id, "❌ Ошибка.", reply_markup=main_reply_keyboard())
        await render_filter_detail(update, context, filter_id)
        return

    # Старая логика с аргументами
    if len(context.args) < 2:
        await send_message_safe(
            context.application,
            user_id,
            "❌ Укажите название фильтра и интервал:\n/interval НАЗВАНИЕ МИНУТЫ",
            reply_markup=main_reply_keyboard(),
        )
        return

    user_data = await get_user_data(update)
    filter_name = context.args[0]
    try:
        interval = int(context.args[1])
        if interval < 1 or interval > 1440:
            await send_message_safe(context.application, user_id, "❌ Интервал должен быть от 1 до 1440 минут.", reply_markup=main_reply_keyboard())
            return
    except ValueError:
        await send_message_safe(context.application, user_id, "❌ Укажите число минут.", reply_markup=main_reply_keyboard())
        return

    if await db.update_filter_interval(user_data["user_id"], filter_name, interval):
        await send_message_safe(context.application, user_id, f"✅ Интервал для фильтра '{clean_text(filter_name)}' изменен на {interval} мин.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    else:
        await send_message_safe(context.application, user_id, f"❌ Фильтр '{clean_text(filter_name)}' не найден.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@handler_guard()
async def retention_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Изменить срок сброса истории фильтра.
    """
    user_id = update.effective_user.id

    if "editing_filter_id" in context.user_data:
        filter_id = context.user_data.pop("editing_filter_id")
        filter_info = await db.get_filter_by_id(user_id, filter_id)
        if not filter_info:
            await send_message_safe(context.application, user_id, "❌ Фильтр не найден.", reply_markup=main_reply_keyboard())
            return
        try:
            days = int((update.message.text or "").strip())
        except Exception:
            await send_message_safe(context.application, user_id, "❌ Введите число.", reply_markup=main_reply_keyboard())
            return
        if days < 1 or days > 365:
            await send_message_safe(context.application, user_id, "❌ Количество дней должно быть от 1 до 365.", reply_markup=main_reply_keyboard())
            return
        if await db.update_filter_retention(user_id, filter_info["filter_name"], days):
            await send_message_safe(context.application, user_id, f"✅ Срок хранения для фильтра '{filter_info['filter_name']}' изменён на {days} дней.", reply_markup=main_reply_keyboard())
        else:
            await send_message_safe(context.application, user_id, "❌ Ошибка.", reply_markup=main_reply_keyboard())
        await render_filter_detail(update, context, filter_id)
        return

    if len(context.args) < 2:
        await send_message_safe(
            context.application,
            user_id,
            "❌ Укажите название фильтра и количество дней:\n/retention НАЗВАНИЕ ДНИ\n\nПример: /retention Мои задачи 10",
            reply_markup=main_reply_keyboard(),
        )
        return

    user_data = await get_user_data(update)
    try:
        days = int(context.args[-1])
        if days < 1 or days > 365:
            await send_message_safe(context.application, user_id, "❌ Количество дней должно быть от 1 до 365.", reply_markup=main_reply_keyboard())
            return
    except ValueError:
        await send_message_safe(context.application, user_id, "❌ Последним аргументом должно быть число дней.", reply_markup=main_reply_keyboard())
        return

    filter_name = " ".join(context.args[:-1])
    if await db.update_filter_retention(user_data["user_id"], filter_name, days):
        await send_message_safe(
            context.application,
            user_id,
            f"✅ Срок сброса для фильтра '{clean_text(filter_name)}' установлен на {days} дней.\n"
            f"Теперь задачи старше {days} дней будут считаться новыми и вы снова получите по ним уведомления.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
    else:
        await send_message_safe(context.application, user_id, f"❌ Фильтр '{clean_text(filter_name)}' не найден.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@handler_guard()
async def workstart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить время начала рабочего дня для автоматической очистки старых данных."""
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    user_id_db = user_data["user_id"]

    if not context.args:
        current = await db.get_work_start_time(user_id_db)
        if current:
            await send_message_safe(context.application, user_id, f"⏰ Ваше время начала рабочего дня: {current} МСК", reply_markup=main_reply_keyboard())
        else:
            await send_message_safe(
                context.application,
                user_id,
                "⏰ Время начала рабочего дня не задано.\n"
                "Если установить, каждый день в это время будут удаляться старые данные по вашим фильтрам.\n\n"
                "Установите: /workstart 09:00",
                reply_markup=main_reply_keyboard(),
            )
        return

    time_str = context.args[0].strip()
    if not re.match(r"^([01]?[0-9]|2[0-3]):[0-5][0-9]$", time_str):
        await send_message_safe(context.application, user_id, "❌ Неверный формат. Используйте ЧЧ:ММ (например, 09:00)", reply_markup=main_reply_keyboard())
        return

    await db.set_work_start_time(user_id_db, time_str)
    await send_message_safe(
        context.application,
        user_id,
        f"✅ Время начала рабочего дня установлено: {time_str} МСК\n"
        "Каждый день в это время будут удаляться старые данные по вашим фильтрам.",
        reply_markup=main_reply_keyboard(),
    )


@rate_limit("editfilter_cmd")
@handler_guard()
async def editfilter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактировать JQL фильтра"""
    user_id = update.effective_user.id
    user_data = await get_user_data(update)

    if not context.args:
        user_filters = await db.get_user_filters(user_data["user_id"], active_only=False)
        if not user_filters:
            await send_message_safe(context.application, user_id, "❌ У вас нет фильтров для редактирования.", reply_markup=main_reply_keyboard())
            return

        if len(user_filters) == 1:
            filter_id = user_filters[0]["id"]
            context.user_data["editing_filter_id"] = filter_id
            context.user_data["pending_cmd"] = "editfilter_jql"
            await send_message_safe(
                context.application,
                user_id,
                f"✏️ Редактирование фильтра {user_filters[0]['filter_name']}\nВведите новый JQL запрос:",
                parse_mode=ParseMode.HTML,
                reply_markup=main_reply_keyboard(),
            )
        else:
            keyboard = []
            for f in user_filters:
                keyboard.append(
                    [InlineKeyboardButton(f"{'' if f['is_active'] else '⏸️'} {f['filter_name']}", callback_data=f"filter:edit:{f['id']}")]
                )
            await send_message_safe(
                context.application,
                user_id,
                "Выберите фильтр для редактирования:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return

    filter_name = " ".join(context.args)
    user_filters = await db.get_user_filters(user_data["user_id"], active_only=False)
    target = next((f for f in user_filters if f["filter_name"] == filter_name), None)
    if not target:
        await send_message_safe(context.application, user_id, f"❌ Фильтр '{filter_name}' не найден.", reply_markup=main_reply_keyboard())
        return

    context.user_data["editing_filter_id"] = target["id"]
    context.user_data["pending_cmd"] = "editfilter_jql"
    await send_message_safe(
        context.application,
        user_id,
        f"✏️ Редактирование фильтра {filter_name}\nВведите новый JQL запрос:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_reply_keyboard(),
    )


@handler_guard()
async def editfilter_jql_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ввода нового JQL при редактировании фильтра"""
    user_id = update.effective_user.id
    filter_id = context.user_data.get("editing_filter_id")
    if not filter_id:
        await send_message_safe(context.application, user_id, "❌ Ошибка: не выбран фильтр.", reply_markup=main_reply_keyboard())
        return

    new_jql = (update.message.text or "").strip()
    if not new_jql:
        await send_message_safe(context.application, user_id, "❌ JQL не может быть пустым.", reply_markup=main_reply_keyboard())
        return

    filter_info = await db.get_filter_by_id(user_id, filter_id)
    if not filter_info:
        await send_message_safe(context.application, user_id, "❌ Фильтр не найден.", reply_markup=main_reply_keyboard())
        return

    success = await db.update_filter_jql(user_id, filter_info["filter_name"], new_jql)
    if success:
        await send_message_safe(context.application, user_id, f"✅ JQL фильтра {filter_info['filter_name']} успешно обновлён.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    else:
        await send_message_safe(context.application, user_id, f"❌ Не удалось обновить фильтр {filter_info['filter_name']}. Проверьте название.", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())

    context.user_data.pop("editing_filter_id", None)
    context.user_data.pop("pending_cmd", None)
    await render_filters_screen(update, context)


# ---------------------------
# Blockers
# ---------------------------

@rate_limit("blockers_cmd")
@handler_guard()
async def blockers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать активные блокировки через экран"""
    await render_blockers_screen(update, context)
    await ensure_main_keyboard(update, context)


@rate_limit("analyze_blockers_cmd")
@handler_guard()
async def analyze_blockers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await send_message_safe(context.application, user_id, "❌ Укажите ключ задачи:\n/blockersinfo TASK-123", reply_markup=main_reply_keyboard())
        return

    user_data = await get_user_data(update)
    issue_key = context.args[0].upper()
    client = jira_manager.get_client(user_data["user_id"])
    if not client:
        await send_message_safe(context.application, user_id, "❌ Не настроено подключение к Jira", reply_markup=main_reply_keyboard())
        return

    await send_message_safe(context.application, user_id, f"🔎 Анализирую блокирующие задачи для {issue_key}...", reply_markup=main_reply_keyboard())
    try:
        try:
            issue = await client.get_issue(issue_key)
        except JiraAuthError:
            await handle_jira_auth_error(context.application, user_data["user_id"])
            return
        except Exception as e:
            logger.error(f"Error getting issue: {e}")
            await send_message_safe(context.application, user_id, "❌ Ошибка при получении задачи", reply_markup=main_reply_keyboard())
            return

        if not issue:
            await send_message_safe(context.application, user_id, f"❌ Задача {issue_key} не найдена", reply_markup=main_reply_keyboard())
            return

        blockers_result = smart_watcher.analyze_blockers(user_data["user_id"], "manual_check", issue, client)
        blockers = await blockers_result if inspect.iscoroutine(blockers_result) else blockers_result

        if not blockers:
            text = (
                f"✅ {issue_key} не имеет блокирующих задач\n\n"
                "Эта задача не блокирует другие задачи и не заблокирована другими.\n\n"
                f"Прямая ссылка: {client.base_url}/browse/{issue_key}"
            )
            keyboard = [[InlineKeyboardButton("🔗 Открыть в Jira", url=f"{client.base_url}/browse/{issue_key}")]]
            await send_message_safe(context.application, user_id, text, reply_markup=InlineKeyboardMarkup(keyboard), reply_markup2=main_reply_keyboard() if False else None)
            # ↑ reply_markup2 не используется; оставляем только inline, а нижняя клавиатура уже поставлена /start
            return

        text = f"🔗 Связанные задачи для {issue_key}:\n\n"
        active_blockers = [b for b in blockers if not b.get("is_resolved", True)]
        resolved_blockers = [b for b in blockers if b.get("is_resolved", False)]

        if active_blockers:
            text += "🚧 Активные блокировки:\n"
            for blocker in active_blockers:
                assignee_emoji = "👤" if blocker.get("assignee") and blocker["assignee"] != "Не назначен" else ""
                text += f" • {blocker['key']}\n"
                text += f"   📝 {blocker['summary'][:80]}...\n"
                text += f"   📌 {blocker['status']}\n"
                text += f"   {assignee_emoji} {blocker['assignee']}\n"
                text += f"   🕒 {blocker['updated']}\n\n"

        if resolved_blockers:
            text += "✅ Разрешённые блокировки:\n"
            for blocker in resolved_blockers:
                text += f" • {blocker['key']}\n"
                text += f"   📝 {blocker['summary'][:80]}...\n"
                text += f"   🏁 Решение: {blocker.get('resolution', 'Завершена')}\n"
                text += f"   🕒 {blocker['updated']}\n\n"

        keyboard = [[InlineKeyboardButton("🔗 Открыть основную задачу", url=f"{client.base_url}/browse/{issue_key}")]]
        keyboard.append([InlineKeyboardButton("Связанные задачи:", callback_data="noop")])
        for blocker in blockers[:3]:
            status_emoji = "✅" if blocker.get("is_resolved") else "🚧"
            keyboard.append([InlineKeyboardButton(f"{status_emoji} {blocker['key']}", url=f"{client.base_url}/browse/{blocker['key']}")])

        await send_message_safe(context.application, user_id, text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error analyzing blockers: {e}")
        await send_message_safe(context.application, user_id, "❌ Ошибка при анализе блокирующих задач", reply_markup=main_reply_keyboard())


# ---------------------------
# Mentions
# ---------------------------

@rate_limit("mentions_cmd")
@handler_guard()
async def mentions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_mentions_screen(update, context)
    await ensure_main_keyboard(update, context)


@rate_limit("checkmentions_cmd")
@handler_guard()
async def checkmentions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id

    if not MENTIONS_ENABLED:
        await send_message_safe(context.application, user_id, "⚠️ Отслеживание упоминания отключено в настройках бота.", reply_markup=main_reply_keyboard())
        return

    client = jira_manager.get_client(user_data["user_id"])
    if not client:
        await send_message_safe(
            context.application,
            user_id,
            "❌ Не настроено подключение к Jira.\nСначала настройте бота: /setup",
            reply_markup=main_reply_keyboard(),
        )
        return

    await send_message_safe(context.application, user_id, "🔎 Проверяю упоминания...", reply_markup=main_reply_keyboard())
    try:
        user_info = await db.get_user(user_data["user_id"])
        jira_username = user_info.get("jira_user")
        if not jira_username:
            await send_message_safe(
                context.application,
                user_id,
                "❌ Не удалось определить ваш Jira username.\nПроверьте настройки: /reconfigure",
                reply_markup=main_reply_keyboard(),
            )
            return

        mentions_result = mentions_tracker.check_user_mentions(user_data["user_id"], client, jira_username)
        mentions = await mentions_result if inspect.iscoroutine(mentions_result) else mentions_result

        if not mentions:
            await send_message_safe(
                context.application,
                user_id,
                "✅ Новых упоминаний не найдено.\n\n"
                "Бот будет уведомлять вас автоматически, когда вас упомянут (@username) "
                "в комментариях задач.",
                reply_markup=main_reply_keyboard(),
            )
            return

        notified_count = 0
        for mention_info in mentions:
            message_id = await send_mention_notification(context.application, user_data["user_id"], mention_info)
            if message_id:
                notified_count += 1

        await send_message_safe(
            context.application,
            user_id,
            f"✅ Найдено {len(mentions)} задач с упоминаниями.\n"
            f"Отправлено {notified_count} уведомлений (по одному на задачу).\n\n"
            f"🔔 Всего упоминаний: {sum(m.get('total_mentions', 0) for m in mentions)}",
            reply_markup=main_reply_keyboard(),
        )
    except JiraAuthError:
        await handle_jira_auth_error(context.application, user_data["user_id"])
    except Exception as e:
        logger.error(f"Error checking mentions: {e}")
        await send_message_safe(context.application, user_id, "❌ Ошибка при проверке упоминаний", reply_markup=main_reply_keyboard())


# ---------------------------
# Digest
# ---------------------------

@rate_limit("digest_cmd")
@handler_guard()
async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id

    msg = await send_message_safe(context.application, user_id, "🧾 Собираю твой дайджест...", reply_markup=main_reply_keyboard())
    text = await generate_user_digest(user_data["user_id"])
    if text:
        if msg:
            await send_message_safe(context.application, user_id, text, parse_mode=ParseMode.HTML, message_id=msg, reply_markup=main_reply_keyboard())
        else:
            await send_message_safe(context.application, user_id, text, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    else:
        await send_message_safe(context.application, user_id, "❌ Не удалось собрать данные.", reply_markup=main_reply_keyboard())


# ---------------------------
# Status / stats / pending / nightmode
# ---------------------------

@rate_limit("status_cmd")
@handler_guard()
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id

    stats = await db.get_user_stats(user_data["user_id"])
    jira_configured = bool(user_data.get("jira_user") and user_data.get("jira_token"))
    jira_status = "✅ Настроено" if jira_configured else "❌ Не настроено"
    muted_status = await db.is_notifications_muted(user_data["user_id"])
    current_time = get_msk_time()

    user_filters = await db.get_user_filters(user_data["user_id"], active_only=True)

    total_blockers = 0
    blockers_note = ""
    if user_filters:
        client = jira_manager.get_client(user_data["user_id"])
        if client:
            try:
                all_blockers, truncated = await collect_active_blockers_now(user_data["user_id"], client, user_filters)
                total_blockers = len(all_blockers)
                if truncated:
                    blockers_note = " (есть ограничение по объёму; см. /blockers)"
            except JiraAuthError:
                await handle_jira_auth_error(context.application, user_data["user_id"])
            except Exception:
                pass

    mention_data = await mentions_tracker.get_mention_notification_data(user_data["user_id"])
    unread_mentions = len(mention_data) if mention_data else 0

    text = f"""📌 Ваш статус

Jira: {jira_status}
Уведомления: {'🔇 Выключены' if muted_status else '🔔 Включены'}
Упоминания: {'🔔 Включены' if MENTIONS_ENABLED else '🔇 Выключены'}
Текущее время: {current_time}

📊 Статистика:
• Фильтров: {stats.get('total_filters', 0)} (активных: {stats.get('active_filters', 0)})
• Отслеживаемых задач: {stats.get('total_tracked', 0)}
• Всего уведомлений: {stats.get('total_notifications', 0)}
• Неподтвержденных: {stats.get('pending_notifications', 0)}
• Непрочитанных упоминаний: {unread_mentions}
• Активных блокировок: {total_blockers}{blockers_note}

⏰ Настройки:
• Интервал напоминаний: {REMINDER_INTERVAL_MINUTES} минут
• Интервал проверки упоминаний: {MENTIONS_CHECK_INTERVAL} минут
• Ночной режим: {'🌙 Включен (22:00-09:00 МСК)' if NIGHT_MODE_ENABLED else '☀️ Выключен'}

🧭 Управление:
• /filters - Ваши фильтры
• /blockers - Активные блокировки
• /mentions - Ваши упоминания
• /digest - Утренняя сводка
• /mute - Вкл/выкл все уведомления
• /stats - Подробная статистика
• /pending - Неподтвержденные уведомления
• /nightmode - Информация о ночном режиме
• /workstart - Установить время очистки
"""
    await send_message_safe(context.application, user_id, text, reply_markup=main_reply_keyboard())


@handler_guard()
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    user_id_db = user_data["user_id"]

    current_state = await db.is_notifications_muted(user_id_db)
    new_state = not current_state
    await db.set_notifications_muted(user_id_db, new_state)

    status = "🔇 Все уведомления выключены" if new_state else "🔔 Все уведомления включены"
    await send_message_safe(context.application, user_id, status, reply_markup=main_reply_keyboard())
    logger.info("User %s muted: %s", user_id_db, new_state)


@handler_guard()
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    user_filters = await db.get_user_filters(user_data["user_id"])

    text = "📊 Подробная статистика\n\n"
    if not user_filters:
        text += "У вас пока нет фильтров.\nДобавьте первый: /addfilter"
        await send_message_safe(context.application, user_id, text, reply_markup=main_reply_keyboard())
        return

    total_blockers = 0
    active_blockers = 0

    notifications = await db.get_user_notifications(user_data["user_id"], limit=100)

    for f in user_filters:
        status = "✅ Активен" if f["is_active"] else "⏸️ Неактивен"
        retention = f.get("retention_days") or 30
        text += f"• {f['filter_name']} ({status})\n"
        text += f"  ⏱ Интервал: {f['check_interval']} мин\n"
        text += f"  🧹 Очистка: каждые {retention} дней\n"
        text += f"  🔎 JQL: {f['jql'][:60]}{'...' if len(f['jql']) > 60 else ''}\n"

        filter_notifications = [n for n in notifications if n["filter_name"] == f["filter_name"]]
        filter_blockers = 0
        filter_active = 0
        for notif in filter_notifications:
            details = notif.get("details", {}) or {}
            blockers = details.get("blockers", []) or []
            filter_blockers += len(blockers)
            filter_active += len([b for b in blockers if not b.get("is_resolved", True)])

        text += f"  🚧 Блокировок: {filter_blockers} (активных: {filter_active})\n\n"
        total_blockers += filter_blockers
        active_blockers += filter_active

    text += "\n📌 Общая статистика блокировок:\n"
    text += f"• Всего связанных задач: {total_blockers}\n"
    text += f"• Активных блокировок: {active_blockers}\n"
    text += f"• Решённых: {total_blockers - active_blockers}\n\n"

    try:
        active_filters = [f for f in user_filters if f.get("is_active")]
        client = jira_manager.get_client(user_data["user_id"])
        if client and active_filters:
            all_blockers_now, truncated = await collect_active_blockers_now(user_data["user_id"], client, active_filters)
            text += f"⏱ Сейчас в Jira активных блокировок: {len(all_blockers_now)}"
            if truncated:
                text += " (есть ограничение по объёму; см. /blockers)"
            text += "\n\n"
    except Exception:
        pass

    mention_data = await mentions_tracker.get_mention_notification_data(user_data["user_id"])
    unread_mentions = len(mention_data) if mention_data else 0
    total_mentions = await db.get_total_mentions(user_data["user_id"])

    text += "🔔 Статистика упоминаний:\n"
    text += f"• Всего упоминаний: {total_mentions}\n"
    text += f"• Непрочитанных: {unread_mentions}"

    await send_message_safe(context.application, user_id, text, reply_markup=main_reply_keyboard())


@handler_guard()
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_notifications_screen(update, context)
    await ensure_main_keyboard(update, context)


@handler_guard()
async def nightmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = get_msk_time()
    time_until_morning = format_time_until_morning()

    if NIGHT_MODE_ENABLED:
        text = f"""🌙 Ночной режим бота

Статус: Включен ✅
Текущее время: {current_time}
Ночное время: с {NIGHT_START_HOUR:02d}:00 до {NIGHT_END_HOUR:02d}:00 (МСК)

В ночное время бот:
• ✅ Отправляет уведомления о новых изменениях
• ✅ Отправляет уведомления об упоминаниях
• ❌ Не отправляет напоминания о неподтвержденных задачах

До утра: {time_until_morning}

Интервал напоминаний: {REMINDER_INTERVAL_MINUTES} минут (в рабочее время)
Интервал упоминаний: {MENTIONS_CHECK_INTERVAL} минут
"""
    else:
        text = f"""☀️ Ночной режим бота

Статус: Выключен
Текущее время: {current_time}

Интервал напоминаний: {REMINDER_INTERVAL_MINUTES} минут
Интервал упоминаний: {MENTIONS_CHECK_INTERVAL} минут
"""

    await send_message_safe(context.application, user_id, text, reply_markup=main_reply_keyboard())


# ---------------------------
# Fun commands (как в твоей ветке)
# ---------------------------

@handler_guard()
async def funmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    arg = " ".join(context.args).strip().lower() if context.args else ""
    mode = _normalize_fun_mode(arg)
    if not mode:
        current = (await _get_fun_settings(user_data["user_id"])).get("fun_mode", "off")
        mode = {"off": "light", "light": "full", "full": "off"}.get(current, "light")
    await db.set_fun_mode(user_data["user_id"], mode)

    if mode == "off":
        txt = "😐 Fun-режим выключен. Только работа и никаких мемов."
    elif mode == "light":
        txt = "✨ Fun-режим: LIGHT. Небольшие реакции + аккуратные приколы."
    else:
        txt = "🔥 Fun-режим: FULL. Бот включает характер, реакции и геймификацию."
    await send_message_safe(context.application, user_id, txt, reply_markup=main_reply_keyboard())


@handler_guard()
async def personality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    arg = " ".join(context.args).strip() if context.args else ""
    p = _normalize_personality(arg)
    if not p:
        current = (await _get_fun_settings(user_data["user_id"])).get("personality", "neutral")
        variants = ["neutral", "developer", "tester", "support"]
        try:
            p = variants[(variants.index(current) + 1) % len(variants)]
        except Exception:
            p = "neutral"
    await db.set_personality(user_data["user_id"], p)
    labels = {"neutral": "🙂 нейтральный", "developer": "👨‍💻 разработчик", "tester": "🧪 тестировщик", "support": "🎧 саппорт"}
    await send_message_safe(context.application, user_id, f"🎭 Персона бота: {labels.get(p, p)}", reply_markup=main_reply_keyboard())


@handler_guard()
async def setzodiac_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    zodiac = " ".join(context.args).strip().lower() if context.args else ""
    if not zodiac:
        await send_message_safe(context.application, user_id, "Напиши знак зодиака: /setzodiac овен (или телец, близнецы...)", reply_markup=main_reply_keyboard())
        return
    if zodiac not in ZODIAC_MAP:
        await send_message_safe(context.application, user_id, "❌ Не узнал знак. Пример: /setzodiac скорпион", reply_markup=main_reply_keyboard())
        return
    await db.set_zodiac(user_data["user_id"], zodiac)
    await send_message_safe(context.application, user_id, f"✅ Окей, записал: {zodiac.capitalize()}", reply_markup=main_reply_keyboard())


@handler_guard()
async def horoscope_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    settings = await _get_fun_settings(user_data["user_id"])
    zodiac = settings.get("zodiac")

    if context.args:
        z = " ".join(context.args).strip().lower()
        if z in ZODIAC_MAP:
            zodiac = z
            await db.set_zodiac(user_data["user_id"], z)

    text = await get_random_horoscope(zodiac or "")
    if _is_fun_enabled(settings) and settings.get("whisper_enabled", 1) and not should_send_reminder_check():
        text = _format_whisper(text)
    await send_message_safe(context.application, user_id, text, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@handler_guard()
async def meme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    settings = await _get_fun_settings(user_data["user_id"])

    role = (settings.get("personality") or "common")
    if role == "neutral":
        role = "common"

    if context.args:
        r = context.args[0].strip().lower()
        if r in ROLE_PREFIX:
            role = r

    caption, url = await get_random_meme(role=role)

    if _is_fun_enabled(settings) and settings.get("whisper_enabled", 1) and not should_send_reminder_check():
        caption = _format_whisper(caption)

    try:
        if url:
            await context.bot.send_photo(chat_id=user_id, photo=url, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await send_message_safe(context.application, user_id, caption, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())
    except Exception:
        await send_message_safe(context.application, user_id, caption, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@handler_guard()
async def fact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    settings = await _get_fun_settings(user_data["user_id"])
    text = await get_random_fact()
    if _is_fun_enabled(settings) and settings.get("whisper_enabled", 1) and not should_send_reminder_check():
        text = _format_whisper(text)
    await send_message_safe(context.application, user_id, f"📚 Факт дня:\n{text}", parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


@handler_guard()
async def whisper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    settings = await _get_fun_settings(user_data["user_id"])
    cur = bool(settings.get("whisper_enabled", 1))
    arg = " ".join(context.args).strip().lower() if context.args else ""
    if arg in ("on", "1", "true", "да", "вкл"):
        cur = True
    elif arg in ("off", "0", "false", "нет", "выкл"):
        cur = False
    else:
        cur = not cur
    await db.set_whisper_enabled(user_data["user_id"], cur)
    await send_message_safe(context.application, user_id, f"🌙 Ночной «шёпот»: {'включён' if cur else 'выключен'}", reply_markup=main_reply_keyboard())


@handler_guard()
async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    s = await _get_fun_settings(user_data["user_id"])
    mode = s.get("fun_mode", "off")
    pers = s.get("personality", "neutral")
    zodiac = s.get("zodiac") or "не задан"
    xp = s.get("xp", 0)
    level = s.get("level", 1)
    streak = s.get("streak", 0)
    whisper = "on" if s.get("whisper_enabled", 1) else "off"
    text = (
        "👤 Профиль\n\n"
        f"Fun-режим: {mode}\n"
        f"Персона: {pers}\n"
        f"Зодиак: {zodiac}\n"
        f"XP: {xp} | lvl {level} | streak {streak}\n"
        f"Ночной «шёпот»: {whisper}\n\n"
        "Команды:\n"
        "• /funmode — переключить режим\n"
        "• /personality — сменить персону\n"
        "• /meme — мем\n"
        "• /horoscope — гороскоп\n"
        "• /setzodiac овен — задать знак\n"
        "• /whisper — шёпот ночью\n"
    )
    await send_message_safe(context.application, user_id, text, parse_mode=ParseMode.HTML, reply_markup=main_reply_keyboard())


# ---------------------------
# Help / admin
# ---------------------------

@handler_guard()
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    help_text = f"""📚 Команды Jira Monitor Bot

Настройка:
• /setup - Настроить подключение к Jira
• /reconfigure - Перенастроить Jira

Фильтры:
• /addfilter - Добавить новый фильтр
• /filters - Показать ваши фильтры
• /deletefilter <название> - Удалить фильтр
• /togglefilter <название> - Вкл/выкл фильтр
• /interval <название> <минуты> - Изменить интервал
• /retention <название> <дни> - Изменить срок сброса истории фильтра
• /editfilter <название> - Редактировать JQL фильтра
• /workstart <ЧЧ:ММ> - Установить время очистки старых данных

Блокирующие задачи:
• /blockers - Показать все активные блокировки
• /blockersinfo TASK-123 - Анализ блокировок для конкретной задачи

Упоминания:
• /mentions - Показать ваши упоминания (одно уведомление на задачу)
• /checkmentions - Вручную проверить упоминания

Управление и информация:
• /digest - Вызвать утреннюю сводку
• /status - Ваш статус
• /mute - Вкл/выкл все уведомления
• /pending - Показать неподтвержденные уведомления
• /stats - Подробная статистика
• /nightmode - Информация о ночном режиме
• /menu - Показать нижнее меню
• /help - Эта справка

Расписание:
• Первое уведомление - сразу при изменении
• Напоминания - каждые {REMINDER_INTERVAL_MINUTES} минут в рабочее время
• Упоминания - каждые {MENTIONS_CHECK_INTERVAL} минут
• Ночное время (22:00-09:00 МСК) - напоминания не отправляются
"""
    await send_message_safe(context.application, user_id, help_text, reply_markup=main_reply_keyboard())


@handler_guard()
async def adminstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = await get_user_data(update)
    user_id = update.effective_user.id
    if user_data["user_id"] not in ADMIN_USER_IDS:
        await send_message_safe(context.application, user_id, "❌ Эта команда только для администраторов.", reply_markup=main_reply_keyboard())
        return

    active_users = await db.get_all_active_users()
    text = (
        "🛠 Админская статистика\n"
        f"Всего активных пользователей: {len(active_users)}\n"
        f"Интервал напоминаний: {REMINDER_INTERVAL_MINUTES} минут\n"
        f"Интервал упоминаний: {MENTIONS_CHECK_INTERVAL} минут\n"
        f"Ночной режим: {'Включен' if NIGHT_MODE_ENABLED else 'Выключен'}\n"
        f"Упоминания: {'Включены' if MENTIONS_ENABLED else 'Выключены'}\n\n"
        "Пользователи:\n"
    )
    for user in active_users:
        stats = await db.get_user_stats(user["user_id"])
        username = user.get("username", "нет")
        first_name = user.get("first_name", "Пользователь")
        text += (
            f"\n• {first_name} (@{username})\n"
            f"  ID: {user['user_id']}\n"
            f"  Фильтров: {stats.get('total_filters', 0)}\n"
            f"  Задач: {stats.get('total_tracked', 0)}\n"
            f"  Уведомлений: {stats.get('total_notifications', 0)}\n"
            f"  Неподтвержденных: {stats.get('pending_notifications', 0)}\n"
            f"  Упоминаний: {stats.get('mentions_count', 0)}\n"
        )

    await send_message_safe(context.application, user_id, text, reply_markup=main_reply_keyboard())


@handler_guard()
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправить сообщение всем активным пользователям (только для админов)"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await send_message_safe(context.application, user_id, "❌ Эта команда только для администраторов.", reply_markup=main_reply_keyboard())
        return

    raw = (update.message.text or "").strip() if update.message else ""
    if not raw:
        await send_message_safe(context.application, user_id, "❌ Пустое сообщение.", reply_markup=main_reply_keyboard())
        return

    m = re.match(r"^/broadcast(?:@\w+)?\s*(.*)$", raw, flags=re.IGNORECASE | re.DOTALL)
    message_text = (m.group(1) if m else "").strip()
    if not message_text:
        await send_message_safe(context.application, user_id, "❌ Укажите текст для рассылки:\n/broadcast Сообщение", reply_markup=main_reply_keyboard())
        return

    active_users = await db.get_all_active_users()
    sent_count = 0
    failed_count = 0

    for user in active_users:
        uid = user["user_id"]
        try:
            ok = await send_message_safe(context.application, uid, f"📣 Рассылка:\n\n{message_text}", parse_mode=ParseMode.HTML)
            if ok:
                sent_count += 1
            else:
                failed_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send broadcast to {uid}: {e}")
            failed_count += 1

    await send_message_safe(
        context.application,
        user_id,
        f"✅ Рассылка завершена.\nОтправлено: {sent_count}\nНе удалось: {failed_count}",
        reply_markup=main_reply_keyboard(),
    )


@handler_guard()
async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправить сообщение конкретному пользователю по username или user_id (только для админов)"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await send_message_safe(context.application, user_id, "❌ Эта команда только для администраторов.", reply_markup=main_reply_keyboard())
        return

    if len(context.args) < 2:
        await send_message_safe(
            context.application,
            user_id,
            "❌ Укажите получателя и текст:\n/send @username Сообщение\nили\n/send 123456789 Сообщение",
            reply_markup=main_reply_keyboard(),
        )
        return

    target = context.args[0]
    message_text = " ".join(context.args[1:])

    if target.startswith("@"):
        username = target[1:].lower()
        conn = await db._get_conn()
        cursor = await conn.execute("SELECT user_id, chat_id FROM users WHERE username = ?", (username,))
        row = await cursor.fetchone()
        if not row:
            await send_message_safe(context.application, user_id, f"❌ Пользователь {target} не найден в базе.", reply_markup=main_reply_keyboard())
            return
        target_user_id = row["user_id"]
    else:
        try:
            target_user_id = int(target)
        except ValueError:
            await send_message_safe(context.application, user_id, "❌ Неверный формат получателя. Используйте @username или числовой ID.", reply_markup=main_reply_keyboard())
            return

    success = await send_message_safe(context.application, target_user_id, f"📩 Сообщение от админа:\n\n{message_text}")
    if success:
        await send_message_safe(context.application, user_id, f"✅ Сообщение отправлено пользователю {target}.", reply_markup=main_reply_keyboard())
    else:
        await send_message_safe(context.application, user_id, f"❌ Не удалось отправить сообщение пользователю {target}.", reply_markup=main_reply_keyboard())


# ---------------------------
# Text handler: pending_cmd + нижние кнопки
# ---------------------------

COMMAND_HANDLERS = {
    "interval": interval_cmd,
    "retention": retention_cmd,
    "editfilter_jql": editfilter_jql_handler,
    "blockersinfo": analyze_blockers_cmd,
}


@handler_guard()
async def handle_pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает:
    1) ввод параметров для pending_cmd (команды, требующие аргументов),
    2) нажатия нижних кнопок (Настройки/Фильтры/Блокирующие задачи).
    """
    if not update.message or not update.message.text:
        return

    # 1) pending_cmd
    if "pending_cmd" in context.user_data:
        cmd = context.user_data.pop("pending_cmd")
        args_text = update.message.text.strip()
        context.args = args_text.split()
        handler = COMMAND_HANDLERS.get(cmd)
        if handler:
            await handler(update, context)
        else:
            await send_message_safe(context.application, update.effective_user.id, f"Команда /{cmd} не найдена.", reply_markup=main_reply_keyboard())
        return

    # 2) нижнее меню
    t = update.message.text.strip().lower()
    if t == "настройки":
        await render_settings_screen(update, context)
        await ensure_main_keyboard(update, context)
        return
    if t == "фильтры":
        await render_filters_screen(update, context)
        await ensure_main_keyboard(update, context)
        return
    if t == "блокирующие задачи":
        await render_blockers_screen(update, context)
        await ensure_main_keyboard(update, context)
        return

    # Иначе — подсказка
    await update.message.reply_text("Используйте кнопки меню для навигации.", reply_markup=main_reply_keyboard())


# ---------------------------
# Helpers (как в твоей ветке)
# ---------------------------

def _normalize_fun_mode(mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode in ("0", "off", "disable", "none"):
        return "off"
    if mode in ("1", "light", "lite", "low"):
        return "light"
    if mode in ("2", "full", "max", "on"):
        return "full"
    return ""


def _normalize_personality(p: str) -> str:
    p = (p or "").strip().lower()
    if p in ("dev", "developer", "разраб", "разработчик"):
        return "developer"
    if p in ("qa", "tester", "тестер"):
        return "tester"
    if p in ("support", "саппорт", "поддержка"):
        return "support"
    if p in ("neutral", "обычный", "default"):
        return "neutral"
    return ""


async def _get_fun_settings(user_id: int) -> dict:
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


def _format_whisper(text: str) -> str:
    return f"🤫 {text}"


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
