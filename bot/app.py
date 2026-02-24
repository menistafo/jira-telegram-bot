from __future__ import annotations

import asyncio
import logging
import os
import signal

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from .config import BOT_TOKEN
from .db import db
from .digest import daily_digest_scheduler
from .fun import close_http_client
from .handlers.callbacks import handle_reaction_callback, handle_ui_callback
from .handlers.commands import (
    ADD_FILTER_JQL,
    ADD_FILTER_NAME,
    SETUP_JIRA_TOKEN,
    SETUP_JIRA_USER,
    add_filter_jql,
    add_filter_name,
    addfilter_cmd,
    adminstats_cmd,
    analyze_blockers_cmd,
    blockers_cmd,
    broadcast_cmd,
    cancel_cmd,
    checkmentions_cmd,
    deletefilter_cmd,
    digest_cmd,
    editfilter_cmd,
    editfilter_jql_handler,
    fact_cmd,
    filters_cmd,
    funmode_cmd,
    help_cmd,
    horoscope_cmd,
    interval_cmd,
    mentions_cmd,
    meme_cmd,
    mute_cmd,
    nightmode_cmd,
    pending_cmd,
    personality_cmd,
    profile_cmd,
    reconfigure_cmd,
    retention_cmd,
    send_cmd,
    setup_cmd,
    setup_jira_token,
    setup_jira_user,
    setzodiac_cmd,
    start,
    stats_cmd,
    status_cmd,
    togglefilter_cmd,
    whisper_cmd,
    workstart_cmd,
)
from .handlers.inline import inline_query_handler
from .handlers.menu_buttons import handle_menu_buttons
from .handlers.text_input import handle_text_input
from .jira_manager import jira_manager
from .services.errors import on_application_error
from .services.state import AppTasks
from .services.watcher_runner import initialize_user_clients, jira_watcher

logger = logging.getLogger(__name__)


def create_application() -> Application:
    """
    Собираем Application и регистрируем все хэндлеры.

    Важно:
    - Нижнее меню (ReplyKeyboard) присылает обычный текст. Для него отдельный handler:
      handle_menu_buttons
    - Он должен стоять ПЕРЕД общим обработчиком текстового ввода (handle_text_input),
      иначе общий хэндлер будет “съедать” меню и отвечать не тем.
    """
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Conversation: setup jira + add filter ---
    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", setup_cmd)],
        states={
            SETUP_JIRA_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_jira_user)],
            SETUP_JIRA_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_jira_token)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="setup_conversation",
        persistent=False,
    )

    add_filter_conv = ConversationHandler(
        entry_points=[CommandHandler("addfilter", addfilter_cmd)],
        states={
            ADD_FILTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_filter_name)],
            ADD_FILTER_JQL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_filter_jql)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="addfilter_conversation",
        persistent=False,
    )

    edit_filter_conv = ConversationHandler(
        entry_points=[CommandHandler("editfilter", editfilter_cmd)],
        states={
            ADD_FILTER_JQL: [MessageHandler(filters.TEXT & ~filters.COMMAND, editfilter_jql_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
        name="editfilter_conversation",
        persistent=False,
    )

    # --- Basic ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(setup_conv)
    application.add_handler(add_filter_conv)
    application.add_handler(edit_filter_conv)

    application.add_handler(CommandHandler("reconfigure", reconfigure_cmd))
    application.add_handler(CommandHandler("filters", filters_cmd))
    application.add_handler(CommandHandler("deletefilter", deletefilter_cmd))
    application.add_handler(CommandHandler("togglefilter", togglefilter_cmd))
    application.add_handler(CommandHandler("interval", interval_cmd))
    application.add_handler(CommandHandler("retention", retention_cmd))
    application.add_handler(CommandHandler("workstart", workstart_cmd))

    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("mute", mute_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("nightmode", nightmode_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    # Fun
    application.add_handler(CommandHandler("funmode", funmode_cmd))
    application.add_handler(CommandHandler("personality", personality_cmd))
    application.add_handler(CommandHandler("profile", profile_cmd))
    application.add_handler(CommandHandler("meme", meme_cmd))
    application.add_handler(CommandHandler("horoscope", horoscope_cmd))
    application.add_handler(CommandHandler("setzodiac", setzodiac_cmd))
    application.add_handler(CommandHandler("fact", fact_cmd))
    application.add_handler(CommandHandler("whisper", whisper_cmd))
    application.add_handler(CommandHandler("adminstats", adminstats_cmd))

    # Jira features
    application.add_handler(CommandHandler("blockers", blockers_cmd))
    application.add_handler(CommandHandler("blockersinfo", analyze_blockers_cmd))
    application.add_handler(CommandHandler("mentions", mentions_cmd))
    application.add_handler(CommandHandler("checkmentions", checkmentions_cmd))
    application.add_handler(CommandHandler("digest", digest_cmd))

    # Admin/broadcast
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(CommandHandler("send", send_cmd))

    # Inline + callbacks
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(CallbackQueryHandler(handle_reaction_callback, pattern=r"^(ack|mute|resolve|reopen):"))
    application.add_handler(CallbackQueryHandler(handle_ui_callback))

    # ---- ТЕКСТОВЫЕ ХЭНДЛЕРЫ (ВАЖЕН ПОРЯДОК!) ----
    # 1) Нижнее меню (ReplyKeyboard) -> текст ("Настройка", "Фильтры", ...)
    # Ставим раньше общего текстового обработчика.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons), group=0)

    # 2) Общий обработчик текстового ввода (pending UI actions, ввод JQL/чисел и т.п.)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input), group=1)

    # Global error handler
    application.add_error_handler(on_application_error)

    # Init bot_data
    application.bot_data.setdefault("tasks", AppTasks())

    return application


async def _graceful_shutdown(application: Application) -> None:
    """Аккуратное завершение: отменяем фоновые задачи и закрываем ресурсы."""
    logger.info("🛑 Shutting down...")
    tasks: AppTasks = application.bot_data.get("tasks")  # type: ignore[assignment]
    if tasks:
        for t in (tasks.watcher, tasks.digest):
            if t and not t.done():
                t.cancel()
        for t in (tasks.watcher, tasks.digest):
            if t:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Background task failed during shutdown")

    await jira_manager.close_all_clients()

    try:
        await close_http_client()
    except Exception:
        logger.exception("close_http_client failed during shutdown")

    await db.close()
    logger.info("👋 Shutdown complete")


async def _wait_for_stop_signal() -> None:
    """
    Надёжная "idle" реализация для Docker/async.
    Держит процесс живым до SIGTERM/SIGINT.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _set_stop() -> None:
        if not stop_event.is_set():
            stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _set_stop)
        except NotImplementedError:
            # На некоторых платформах add_signal_handler недоступен.
            pass

    await stop_event.wait()


async def main() -> None:
    """Основная точка входа."""
    logger.info("🤖 Starting Multi-User Jira Telegram Bot...")
    logger.info("📂 Current working directory: %s", os.getcwd())

    # Optional DB backup (если метод существует в твоём db.py)
    try:
        if hasattr(db, "backup_database"):
            backup_path = await db.backup_database()
            logger.info("📦 Database backup created: %s", backup_path)
    except Exception as e:
        logger.warning("📦 Database backup failed (continuing): %s", e)

    # Cleanup
    try:
        await db.cleanup_old_data(days=30)
    except Exception:
        logger.exception("cleanup_old_data failed (continuing)")

    application = create_application()

    try:
        await application.initialize()
        await application.start()

        # PTB v20+ polling запускается через updater
        await application.updater.start_polling()
        logger.info("✅ Telegram bot started and listening")

        # Инициализация пользователей и запуск фоновых задач
        await initialize_user_clients()

        tasks: AppTasks = application.bot_data["tasks"]
        tasks.watcher = asyncio.create_task(jira_watcher(application))
        tasks.digest = asyncio.create_task(daily_digest_scheduler(application))

        logger.info("✅ Bot is fully operational with blocker tracking, mentions, digests and inline mode!")

        # Держим процесс до остановки контейнера/ctrl+c
        await _wait_for_stop_signal()

    finally:
        # Останавливаем polling до stop/shutdown
        try:
            await application.updater.stop()
        except Exception:
            logger.exception("updater.stop failed during shutdown")

        await _graceful_shutdown(application)

        try:
            await application.stop()
        finally:
            await application.shutdown()