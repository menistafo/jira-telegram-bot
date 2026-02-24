from __future__ import annotations

import html
import inspect
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..jira_manager import jira_manager
from ..rate_limit import RateLimit, RateLimiter

logger = logging.getLogger(__name__)


async def _safe_answer_empty(update: Update) -> None:
    """Всегда отвечаем на inline query, чтобы Telegram не считал, что бот "не работает"."""
    try:
        if update.inline_query:
            await update.inline_query.answer([], cache_time=1)
    except Exception:
        # В inline лучше не шуметь — просто логируем
        logger.debug("Failed to answer empty inline query", exc_info=True)


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Inline поиск по Jira.

    Важно:
    - Inline-режим Telegram дергает бота очень часто при наборе текста
    - Нельзя делать тяжёлые запросы без ограничений, иначе улетишь в rate-limit Jira/Telegram
    """
    if not update.inline_query or not update.effective_user:
        return

    raw_query = (update.inline_query.query or "").strip().upper()

    # Если коротко — просто ответим пустым, чтобы UI не выглядел сломанным
    if len(raw_query) < 3:
        await _safe_answer_empty(update)
        return

    # Разрешаем только безопасные символы, чтобы не было JQL-инъекций и мусора
    if not re.fullmatch(r"[A-Z0-9._\-\s]{3,64}", raw_query):
        await _safe_answer_empty(update)
        return

    query = raw_query[:64]
    user_id = update.effective_user.id

    # Rate limit
    rate_limiter: RateLimiter = context.application.bot_data.setdefault("rate_limiter", RateLimiter())
    allowed = await rate_limiter.allow(user_id, "inline", RateLimit(limit=8, per_seconds=10.0))
    if not allowed:
        await _safe_answer_empty(update)
        return

    client = jira_manager.get_client(user_id)
    if not client:
        # Пользователь не настроил Jira — inline пустой
        await _safe_answer_empty(update)
        return

    # Небольшой кэш на 5 секунд (иначе при наборе будет долбить Jira каждую букву)
    cache: Dict[Any, Any] = context.application.bot_data.setdefault("inline_cache", {})
    cache_key = (user_id, query)
    now_ts = datetime.utcnow().timestamp()

    cached = cache.get(cache_key)
    if cached and (now_ts - cached[0]) < 5:
        issues = cached[1]
    else:
        jql = f'(text ~ "{query}*" OR key = "{query}") ORDER BY updated DESC'
        try:
            issues_result = client.search(jql, max_results=10)
            issues = await issues_result if inspect.iscoroutine(issues_result) else issues_result
        except Exception:
            logger.exception("Inline Jira search failed")
            await _safe_answer_empty(update)
            return

        cache[cache_key] = (now_ts, issues)

    results: List[InlineQueryResultArticle] = []
    for issue in issues or []:
        key = issue.get("key")
        if not key:
            continue

        fields = issue.get("fields", {}) or {}
        summary = fields.get("summary", "Без названия") or "Без названия"
        status = (fields.get("status", {}) or {}).get("name", "Неизвестно") or "Неизвестно"
        url = f"{client.base_url}/browse/{key}"

        assignee_obj = fields.get("assignee")
        assignee_name = assignee_obj.get("displayName", "Не назначен") if assignee_obj else "Не назначен"

        # Берём последний комментарий (если есть), но аккуратно ограничиваем длину
        comments = (fields.get("comment", {}) or {}).get("comments", []) or []
        last_comment_text = ""
        if comments:
            last_c = comments[-1] or {}
            author = (last_c.get("author", {}) or {}).get("displayName", "Неизвестно") or "Неизвестно"
            body = last_c.get("body", "") or ""
            if len(body) > 150:
                body = body[:150] + "..."
            last_comment_text = (
                f"\n\n💬 <b>{html.escape(author)}</b>:\n<i>{html.escape(body)}</i>"
            )

        message_text = (
            f"📋 <b><a href='{url}'>{html.escape(key)}</a></b>\n"
            f"{html.escape(summary)}\n\n"
            f"📊 <b>Статус:</b> {html.escape(status)}\n"
            f"👤 <b>Исполнитель:</b> {html.escape(assignee_name)}"
            f"{last_comment_text}"
        )

        description_preview = f"👤 {assignee_name} | {summary}"
        results.append(
            InlineQueryResultArticle(
                id=key,
                title=f"{key} — {status}",
                description=description_preview[:250],
                input_message_content=InputTextMessageContent(
                    message_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ),
            )
        )

    # Telegram ждёт answer всегда
    try:
        await update.inline_query.answer(results, cache_time=10)
    except Exception:
        logger.debug("Failed to answer inline query", exc_info=True)