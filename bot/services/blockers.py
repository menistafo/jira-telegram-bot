from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

from ..watcher import smart_watcher
from ..jira_client import JiraAuthError

logger = logging.getLogger(__name__)


async def collect_active_blockers_now(
    user_id_db: int,
    client,
    filters_list: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Собрать активные блокировки *сейчас* в Jira для задач, попавших в фильтры пользователя.

    Возвращает: (list_of_blockers, is_truncated)

    list_of_blockers: элементы вида
      {
        blocker_key, blocked_issue, filter_name, status, assignee, summary, updated, url
      }

    Ограничения на объём регулируются env:
      BLOCKERS_MAX_ISSUES_PER_FILTER (default 50)
      BLOCKERS_MAX_TOTAL_ISSUES (default 200)
    """
    all_blockers: List[Dict[str, Any]] = []
    seen = set()  # (filter_name, blocked_issue, blocker_key)

    max_issues_per_filter = int(os.getenv("BLOCKERS_MAX_ISSUES_PER_FILTER", "50"))
    max_total_issues = int(os.getenv("BLOCKERS_MAX_TOTAL_ISSUES", "200"))

    total_issues_processed = 0
    truncated = False

    for f in filters_list:
        if total_issues_processed >= max_total_issues:
            truncated = True
            break

        filter_name = f.get("filter_name") or ""
        jql = (f.get("jql") or "").strip()
        if not jql:
            continue

        try:
            issues = await client.search_with_details(jql, max_results=max_issues_per_filter)
        except JiraAuthError:
            raise
        except Exception as e:
            logger.error(f"❌ collect_active_blockers_now: search failed for filter {filter_name}: {e}")
            continue

        for issue in issues:
            if total_issues_processed >= max_total_issues:
                truncated = True
                break

            total_issues_processed += 1

            try:
                blockers = await smart_watcher.analyze_blockers(user_id_db, filter_name, issue, client)
            except Exception as e:
                logger.error(f"❌ collect_active_blockers_now: analyze_blockers failed: {e}")
                continue

            for b in blockers or []:
                if b and not b.get("is_resolved", True):
                    key = b.get("key") or ""
                    uniq = (filter_name, issue.get("key"), key)
                    if uniq in seen:
                        continue
                    seen.add(uniq)

                    all_blockers.append({
                        "blocker_key": key,
                        "blocked_issue": issue.get("key"),
                        "filter_name": filter_name,
                        "status": b.get("status", "Неизвестно"),
                        "assignee": b.get("assignee", "Не назначен"),
                        "summary": b.get("summary", "Без названия"),
                        "updated": b.get("updated"),
                        "url": b.get("url"),
                    })

    return all_blockers, truncated

