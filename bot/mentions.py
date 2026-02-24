# mentions.py
import logging
import re
import hashlib
from collections import defaultdict
from typing import Dict, List, Optional, Any

from .db import db

logger = logging.getLogger(__name__)


class MentionsTracker:
    def __init__(self):
        self.db = db
        logger.info("✅ MentionsTracker initialized")

    def _create_mention_hash(self, issue_key: str, comment_id: str) -> str:
        data_str = f"{issue_key}:{comment_id}"
        return hashlib.md5(data_str.encode()).hexdigest()

    async def check_user_mentions(
        self, user_id: int, jira_client, jira_username: str
    ) -> List[Dict]:
        try:
            last_check = await self.db.get_last_mention_check_time(user_id)

            issues = await jira_client.search_mentions_batch(
                jira_username,
                last_check,
                batch_size=5,
                exclude_resolved=True,
            )

            if not issues:
                await self.db.update_last_mention_check(user_id)
                return []

            all_mentions = []
            max_issues_to_process = 20 if not last_check else 50

            for i, issue in enumerate(issues[:max_issues_to_process]):
                issue_key = issue.get("key")
                fields = issue.get("fields", {})
                status_name = fields.get("status", {}).get("name", "")

                if jira_client.is_resolved_status(status_name):
                    logger.info(f"⚠️ Skipping resolved issue {issue_key} (status: {status_name})")
                    continue

                logger.info(
                    f"Processing active issue {i+1}/{min(len(issues), max_issues_to_process)}: "
                    f"{issue_key} (status: {status_name})"
                )

                try:
                    comments = fields.get("comment", {}).get("comments", [])

                    new_comment_mentions = []
                    for comment in comments:
                        comment_body = comment.get("body", "")
                        comment_id = comment.get("id")

                        if self.has_mention(comment_body, jira_username):
                            mention_hash = self._create_mention_hash(issue_key, comment_id)

                            existing = await self.db.get_mention_by_hash(user_id, mention_hash)
                            if existing:
                                logger.debug(
                                    f"⏩ Skipping already saved mention {mention_hash[:8]} for {issue_key}"
                                )
                                continue

                            new_comment_mentions.append({
                                "comment_id": comment_id,
                                "author": comment.get("author", {}).get("displayName", "Неизвестно"),
                                "body": comment_body[:500],
                                "created": comment.get("created"),
                                "type": "comment",
                                "mention_hash": mention_hash,
                            })

                    if new_comment_mentions:
                        mentions_for_issue = []
                        for mention in new_comment_mentions:
                            saved = await self.db.save_mention(
                                user_id=user_id,
                                issue_key=issue_key,
                                comment_id=mention["comment_id"],
                                mentioned_by=mention["author"],
                                mention_text=mention["body"],
                                mention_type="comment",
                                mention_hash=mention["mention_hash"],
                            )
                            if saved:
                                mentions_for_issue.append({
                                    "mention_type": "comment",
                                    "mention_details": mention,
                                })

                        if mentions_for_issue:
                            all_mentions.append({
                                "issue": issue,
                                "issue_key": issue_key,
                                "mentions": mentions_for_issue,
                                "total_mentions": len(mentions_for_issue),
                                "new_mentions_count": len(mentions_for_issue),
                            })

                        logger.info(f"Found {len(mentions_for_issue)} NEW mentions in issue {issue_key}")

                except Exception as e:
                    logger.error(f"Error processing issue {issue_key}: {e}")
                    continue

            grouped = defaultdict(lambda: {
                "mentions": [],
                "total_mentions": 0,
                "new_mentions_count": 0,
            })
            for item in all_mentions:
                key = item["issue_key"]
                grouped[key]["issue"] = item["issue"]
                grouped[key]["issue_key"] = key
                grouped[key]["mentions"].extend(item["mentions"])
                grouped[key]["total_mentions"] += item["total_mentions"]
                grouped[key]["new_mentions_count"] += item["new_mentions_count"]

            all_mentions = list(grouped.values())

            await self.db.update_last_mention_check(user_id)

            logger.info(f"📨 Found {len(all_mentions)} issues with NEW mentions for user {user_id}")
            return all_mentions

        except Exception as e:
            logger.error(f"❌ Error checking mentions for user {user_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    async def find_mentions_in_comments_async(
        self, comments: List[Dict], username: str
    ) -> List[Dict]:
        user_mentions = []
        for comment in comments:
            comment_body = comment.get("body", "")
            if self.has_mention(comment_body, username):
                user_mentions.append({
                    "comment_id": comment.get("id"),
                    "author": comment.get("author", {}).get("displayName", "Неизвестно"),
                    "body": comment_body[:500],
                    "created": comment.get("created"),
                    "updated": comment.get("updated"),
                    "type": "comment",
                })
        return user_mentions

    def has_mention(self, text: str, username: str) -> bool:
        if not text:
            return False

        text_lower = text.lower()
        username_lower = username.lower()

        patterns = [
            f"@{username_lower}",
            f"[~{username_lower}]",
            f"@({username_lower}[^\\w@-]|{username_lower}$)",
        ]

        for pattern in patterns:
            if re.search(pattern, text_lower):
                return True

        if f"@{username}" in text:
            return True

        return False

    async def get_mention_notification_data(self, user_id: int) -> List[Dict[str, Any]]:
        try:
            conn = await self.db._get_conn()
            query = """
            SELECT 
                issue_key,
                COUNT(*) as mention_count,
                GROUP_CONCAT(DISTINCT mentioned_by) as mentioners,
                MAX(created_at) as last_mention
            FROM user_mentions 
            WHERE user_id = ? AND notification_sent = 0
            GROUP BY issue_key
            ORDER BY last_mention DESC
            """
            cursor = await conn.execute(query, (user_id,))
            rows = await cursor.fetchall()

            issues_data = []
            for row in rows:
                issue_key = row["issue_key"]
                mention_count = row["mention_count"]
                mentioners = row["mentioners"] or ""

                details_query = """
                SELECT comment_id, mentioned_by, mention_text, mention_type, created_at
                FROM user_mentions 
                WHERE user_id = ? AND issue_key = ? AND notification_sent = 0
                ORDER BY created_at DESC
                LIMIT 5
                """
                cursor2 = await conn.execute(details_query, (user_id, issue_key))
                details_rows = await cursor2.fetchall()

                mentions_list = []
                for detail in details_rows:
                    mentions_list.append({
                        "comment_id": detail["comment_id"],
                        "mentioned_by": detail["mentioned_by"],
                        "mention_text": (detail["mention_text"][:200]
                                         if detail["mention_text"] else ""),
                        "mention_type": detail["mention_type"],
                        "created_at": detail["created_at"],
                    })

                issues_data.append({
                    "issue_key": issue_key,
                    "total_mentions": mention_count,
                    "mentioners": mentioners.split(",") if mentioners else [],
                    "last_mention": row["last_mention"],
                    "mentions": mentions_list,
                })

            return issues_data

        except Exception as e:
            logger.error(f"Error getting mention notification data: {e}")
            return []


mentions_tracker = MentionsTracker()