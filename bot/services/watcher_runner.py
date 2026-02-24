from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, Dict, List

from telegram.ext import Application

from ..config import DEFAULT_CHECK_INTERVAL, MENTIONS_ENABLED, MENTIONS_CHECK_INTERVAL
from ..db import db
from ..jira_manager import jira_manager
from ..jira_client import JiraAuthError
from ..mentions import mentions_tracker
from ..watcher import smart_watcher

from .notifications import (
    send_notification_with_changes,
    check_all_task_mutes,
    handle_jira_auth_error,
    send_mention_notification,
)

logger = logging.getLogger(__name__)


async def initialize_user_clients() -> None:
    """Поднимаем Jira-клиенты для всех активных пользователей при старте."""
    active_users = await db.get_all_active_users()
    logger.info("🔧 Initializing Jira clients for %s users...", len(active_users))
    for user in active_users:
        client = await jira_manager.create_client(
            user_id=user["user_id"],
            jira_user=user["jira_user"],
            jira_token=user["jira_token"],
        )
        if client:
            logger.info("✅ Client initialized for user %s", user["user_id"])
        else:
            logger.error("❌ Failed to initialize client for user %s", user["user_id"])
    logger.info("✅ User clients initialization completed")


async def check_user_mentions(application: Application, user_id: int) -> int:
    """Проверка упоминаний пользователя в Jira (если включено)."""
    if not MENTIONS_ENABLED:
        return 0

    try:
        client = jira_manager.get_client(user_id)
        if not client:
            return 0

        user_info = await db.get_user(user_id)
        jira_username = (user_info or {}).get("jira_user")
        if not jira_username:
            return 0

        try:
            mentions_result = mentions_tracker.check_user_mentions(user_id, client, jira_username)
            mentions = await mentions_result if inspect.iscoroutine(mentions_result) else mentions_result
        except JiraAuthError:
            await handle_jira_auth_error(application, user_id)
            return 0
        except Exception as e:
            logger.error("Error checking mentions: %s", e)
            return 0

        if not mentions:
            return 0

        sent = 0
        for mention in mentions:
            ok = await send_mention_notification(application, user_id, mention)
            if ok:
                sent += 1
        return sent

    except Exception:
        logger.exception("check_user_mentions failed for user %s", user_id)
        return 0


async def check_user_filters(application: Application, user_id: int) -> int:
    """Проверяем фильтры пользователя и отправляем уведомления.

    ⚠️ Оптимизация памяти:
    - Jira search идёт постранично (pagination), чтобы не грузить все задачи фильтра в память.
    - Внутри страницы обрабатываем с ограничением параллелизма.

    Настройки:
      JIRA_SEARCH_PAGE_SIZE (default 50)
      JIRA_SEARCH_MAX_TOTAL (default 2000) — верхняя граница обработанных задач на фильтр за цикл
    """
    try:
        user_filters = await db.get_user_filters(user_id, active_only=True)
        if not user_filters:
            return 0

        client = jira_manager.get_client(user_id)
        if not client:
            logger.error("❌ No Jira client for user %s", user_id)
            return 0

        total_notified = 0
        issue_sem = asyncio.Semaphore(3)

        async def process_issue(issue: Dict[str, Any], filter_name: str) -> int:
            async with issue_sem:
                issue_key = issue.get("key")
                if issue_key and await check_all_task_mutes(user_id, issue_key):
                    logger.info("🔇 SKIPPING ENTIRELY: Task %s is muted in SOME filter for user %s", issue_key, user_id)
                    return 0

                notified = 0

                # reminders
                should_remind, prev_msg_id = await db.should_send_reminder(user_id, filter_name, issue_key)
                if should_remind:
                    last_change_info = await db.get_last_notification_info(user_id, filter_name, issue_key)
                    if last_change_info:
                        logger.info("⏰ Sending reminder for %s to user %s", issue_key, user_id)
                        msg_id = await send_notification_with_changes(
                            application,
                            user_id,
                            filter_name,
                            last_change_info,
                            is_reminder=True,
                            previous_message_id=prev_msg_id,
                        )
                        if msg_id:
                            notified += 1

                # changes
                change_info = await smart_watcher.detect_changes_with_blockers(user_id, filter_name, issue, client)
                if change_info:
                    logger.info("📨 Change detected with blockers for user %s: %s", user_id, change_info.get("issue_key"))
                    msg_id = await send_notification_with_changes(application, user_id, filter_name, change_info)
                    if msg_id:
                        notified += 1

                return notified

        page_size = int(os.getenv("JIRA_SEARCH_PAGE_SIZE", "50"))
        max_total = int(os.getenv("JIRA_SEARCH_MAX_TOTAL", "2000"))

        for filter_data in user_filters:
            filter_name = filter_data["filter_name"]
            jql = filter_data["jql"]
            logger.info("🔍 Checking filter '%s' for user %s", filter_name, user_id)

            start_at = 0
            processed = 0
            while True:
                if processed >= max_total:
                    logger.warning(
                        "Filter '%s' for user %s exceeded max_total=%s in one cycle; truncating.",
                        filter_name,
                        user_id,
                        max_total,
                    )
                    break

                try:
                    page = await client.search_with_details_page(jql, start_at=start_at, max_results=page_size)
                except JiraAuthError:
                    logger.error("❌ Jira auth error for user %s", user_id)
                    await handle_jira_auth_error(application, user_id)
                    break
                except Exception as e:
                    logger.error("Error searching issues for filter %s: %s", filter_name, e)
                    break

                issues: List[Dict[str, Any]] = page["issues"]
                total: int = page["total"]
                if not issues:
                    break

                processed += len(issues)
                start_at += len(issues)

                tasks = [process_issue(issue, filter_name) for issue in issues]
                results = await asyncio.gather(*tasks, return_exceptions=False)
                total_notified += sum(results)

                if start_at >= total:
                    break

        return total_notified

    except Exception:
        logger.exception("check_user_filters failed for user %s", user_id)
        return 0


async def jira_watcher(application: Application) -> None:
    """Фоновый процесс: периодическая проверка Jira по всем активным пользователям."""
    logger.info("🚀 Starting multi-user Jira watcher with blocker tracking and mentions...")
    await asyncio.sleep(10)
    logger.info("✅ Watcher started successfully")
    check_counter = 0

    while True:
        try:
            logger.info("🔄 Starting check cycle #%s for all users...", check_counter)
            active_users = await db.get_all_active_users()

            if not active_users:
                logger.info("📭 No active users found")
                await asyncio.sleep(60)
                continue

            logger.info("👥 Checking %s active users", len(active_users))

            semaphore = asyncio.Semaphore(5)

            async def process_user(user: Dict[str, Any]) -> None:
                async with semaphore:
                    user_id = user["user_id"]
                    try:
                        notified = await check_user_filters(application, user_id)
                        if notified:
                            logger.info("✅ Notified user %s about %s changes", user_id, notified)

                        # mentions (реже, чтобы не грузить Jira)
                        if MENTIONS_ENABLED and (check_counter % max(1, int(MENTIONS_CHECK_INTERVAL)) == 0):
                            await check_user_mentions(application, user_id)

                    except Exception:
                        logger.exception("Error processing user %s", user_id)

            await asyncio.gather(*(process_user(u) for u in active_users))

            check_counter += 1

            # Интервал проверки задаётся в конфиге (минуты).
            interval_seconds = max(1, int(DEFAULT_CHECK_INTERVAL)) * 60
            await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            logger.info("Watcher cancelled")
            raise
        except Exception:
            logger.exception("Watcher loop error")
            await asyncio.sleep(10)
