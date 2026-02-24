# digest.py
import asyncio
import logging
import inspect
import html
from datetime import datetime, timedelta
import pytz
from telegram.constants import ParseMode
from .db import db
from .jira_manager import jira_manager

logger = logging.getLogger(__name__)

def get_priority_weight(priority_name: str) -> int:
    p = (priority_name or "").lower()
    
    if any(x in p for x in ["highest", "высш", "критич", "critical", "blocker", "блок"]): 
        return 5
    if any(x in p for x in ["high", "высок", "major", "значител"]): 
        return 4
    if any(x in p for x in ["medium", "средн", "normal", "нормал", "minor"]): 
        return 3
    if any(x in p for x in ["low", "низк", "trivial", "тривиал"]): 
        return 2
    if any(x in p for x in ["lowest", "наименьш", "минимал"]): 
        return 1
    return 0

async def generate_user_digest(user_id: int) -> str:
    client = jira_manager.get_client(user_id)
    if not client:
        return ""

    # было: user_info = db.get_user(user_id)
    user_info = await db.get_user(user_id)
    username = user_info.get("jira_user", "друг")
    
    jql_mentions = f'text ~ "@{username}" AND updated >= -24h AND statusCategory != Done'
    mentions_result = client.search(jql_mentions, max_results=5)
    mentions = await mentions_result if inspect.iscoroutine(mentions_result) else mentions_result

    # было: stats = db.get_user_stats(user_id)
    stats = await db.get_user_stats(user_id)
    active_blockers = stats.get('active_blockers', 0)

    text = f"☀️ <b>Доброе утро! Твой план на день:</b>\n\n"
    text += "\n📋 <b>Сводка по твоим фильтрам:</b>\n"
    
    # было: user_filters = db.get_user_filters(...)
    user_filters = await db.get_user_filters(user_id, active_only=True)
    
    if not user_filters:
        text += "\n<i>У тебя нет активных фильтров. Добавь их через /addfilter</i>\n"
    else:
        for f in user_filters:
            filter_name = f["filter_name"]
            jql = f["jql"]
            
            issues_result = client.search_with_details(jql, max_results=30)
            issues = await issues_result if inspect.iscoroutine(issues_result) else issues_result
            
            if not issues:
                text += f"\n📁 <b>{html.escape(filter_name)}</b>: 0 задач\n"
                continue
            
            sorted_issues = sorted(
                issues, 
                key=lambda x: get_priority_weight(x.get("fields", {}).get("priority", {}).get("name", "")), 
                reverse=True
            )
            
            text += f"\n📁 <b>{html.escape(filter_name)}</b> (Всего в фильтре: {len(issues)}):\n"
            
            for task in sorted_issues[:3]:
                key = task.get("key")
                summary = task.get("fields", {}).get("summary", "")[:35]
                status = task.get("fields", {}).get("status", {}).get("name", "")
                priority = task.get("fields", {}).get("priority", {}).get("name", "Без приоритета")
                
                weight = get_priority_weight(priority)
                p_emoji = "🔴" if weight >= 4 else "🟡" if weight == 3 else "🔵" if weight > 0 else "⚪️"
                
                text += f" {p_emoji} <a href='{client.base_url}/browse/{key}'>{key}</a> [{priority}]\n"
                text += f"   └ {html.escape(summary)}... ({status})\n"

    text += "\nПродуктивного дня! ☕️"
    return text

async def daily_digest_scheduler(bot_app):
    msk_tz = pytz.timezone('Europe/Moscow')
    logger.info("⏰ Daily digest scheduler started")

    while True:
        now_msk = datetime.now(pytz.utc).astimezone(msk_tz)

        target = now_msk.replace(hour=9, minute=0, second=0, microsecond=0)
        if now_msk >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now_msk).total_seconds()
        logger.info(f"⏳ Next digest run at {target.strftime('%Y-%m-%d %H:%M:%S %Z')} (in {int(sleep_seconds)}s)")
        await asyncio.sleep(sleep_seconds)

        logger.info("☀️ Sending morning digests...")
        active_users = await db.get_all_active_users()

        for user in active_users:
            user_id = user["user_id"]
            chat_id = user.get("chat_id")

            from .bot import user_muted
            if user_muted.get(user_id, False) or not chat_id:
                continue

            digest_text = await generate_user_digest(user_id)
            if digest_text:
                try:
                    await bot_app.bot.send_message(
                        chat_id=chat_id,
                        text=digest_text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    logger.error(f"Failed to send digest to {user_id}: {e}")

            await asyncio.sleep(1)

        # safety pause to avoid double-send on quick restarts around 09:00
        await asyncio.sleep(5)
