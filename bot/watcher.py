# watcher.py
import logging
import asyncio
import os
from collections import OrderedDict
import hashlib
import json
import time
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from .db import db
from .utils import should_send_reminder_check, get_msk_time

logger = logging.getLogger(__name__)

class SmartWatcher:
    def __init__(self):
        self.db = db
        self._semaphore = asyncio.Semaphore(int(os.getenv("JIRA_MAX_CONCURRENCY", "5")))
        # Кэш для блокирующих задач: ключ = f"{user_id}:{issue_key}", значение = (данные задачи, timestamp)
        self._blocker_cache: "OrderedDict[str, Tuple[Dict, float]]" = OrderedDict()
        self._cache_ttl = 300  # время жизни кэша в секундах (5 минут)
        self._cache_max = int(os.getenv("SMARTWATCHER_CACHE_MAX", "1000"))  # ограничение памяти
        logger.info("✅ SmartWatcher initialized with intelligent change detection and caching")
    
    def _get_cache_key(self, user_id: int, issue_key: str) -> str:
        return f"{user_id}:{issue_key}"
    
    def _get_from_cache(self, user_id: int, issue_key: str) -> Optional[Dict]:
        """Получить данные задачи из кэша, если они не устарели"""
        key = self._get_cache_key(user_id, issue_key)
        if key in self._blocker_cache:
            data, timestamp = self._blocker_cache[key]
            if time.time() - timestamp < self._cache_ttl:
                return data
            else:
                # Удаляем устаревшую запись
                del self._blocker_cache[key]
        return None
    
    def _put_in_cache(self, user_id: int, issue_key: str, data: Dict):
        """Поместить данные задачи в кэш"""
        key = self._get_cache_key(user_id, issue_key)
        self._blocker_cache[key] = (data, time.time())
    
    def clear_cache(self):
        """Очистить кэш (можно вызывать после обработки пользователя)"""
        self._blocker_cache.clear()
        logger.debug("Blocker cache cleared")
    
    async def detect_changes_with_blockers(self, user_id: int, filter_name: str, 
                                         issue: Dict[str, Any], client) -> Optional[Dict[str, Any]]:
        """
        Обнаружение значимых изменений с анализом блокирующих задач
        """
        try:
            issue_key = issue.get("key")
            if not issue_key:
                return None
            
            logger.debug(f"🔍 Checking for significant changes in {issue_key}")
            
            # Получаем последнее сохраненное состояние
            last_state = await self.db.get_last_issue_state(user_id, filter_name, issue_key)
            
            if not last_state:
                # Новая задача в фильтре - это значимое изменение
                change_info = await self._create_change_info(
                    user_id, filter_name, issue, client, "new", {}, 
                    is_new=True, changes={"new_task": True}
                )
                
                # Сохраняем начальное состояние
                if change_info:
                    current_hash = self.db._calculate_issue_hash(issue)
                    await self.db.save_issue_state(user_id, filter_name, issue_key, issue, current_hash)
                
                return change_info
            
            # Сравниваем текущее состояние с предыдущим
            previous_data = last_state["issue_data"]
            current_data = issue
            
            # Обнаруживаем значимые изменения
            significant_changes = await self._detect_significant_changes(
                previous_data, current_data, client
            )
            
            if not significant_changes:
                logger.debug(f"📭 No significant changes detected for {issue_key}")
                return None
            
            # Если есть изменения, создаем информацию об изменениях
            change_info = await self._create_change_info(
                user_id, filter_name, issue, client, "updated", 
                previous_data, is_new=False, changes=significant_changes
            )
            
            # Сохраняем новое состояние
            if change_info:
                current_hash = self.db._calculate_issue_hash(issue)
                await self.db.save_issue_state(user_id, filter_name, issue_key, issue, current_hash)
            
            return change_info
            
        except Exception as e:
            logger.error(f"❌ Error in detect_changes_with_blockers: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def _detect_significant_changes(self, previous: Dict, current: Dict, 
                                        client) -> Dict[str, Any]:
        """
        Обнаружение значимых изменений между двумя состояниями задачи
        """
        prev_fields = previous.get("fields", {})
        curr_fields = current.get("fields", {})
        
        changes = {}
        
        # 1. Изменение статуса
        prev_status = prev_fields.get("status", {}).get("name")
        curr_status = curr_fields.get("status", {}).get("name")
        if prev_status != curr_status:
            changes["status_changed"] = {
                "from": prev_status,
                "to": curr_status,
                "is_resolved": client.is_resolved_status(curr_status) if client else False,
                "is_unresolved": self._is_unresolved_status(curr_status) if client else False
            }
        
        # 2. Изменение комментариев (новые или отредактированные)
        prev_comments = prev_fields.get("comment", {}).get("comments", [])
        curr_comments = curr_fields.get("comment", {}).get("comments", [])
        
        # Находим максимальные даты обновления комментариев
        def max_comment_updated(comments):
            max_date = None
            for c in comments:
                updated = c.get("updated")
                if updated:
                    if max_date is None or updated > max_date:
                        max_date = updated
            return max_date
        
        prev_max_updated = max_comment_updated(prev_comments)
        curr_max_updated = max_comment_updated(curr_comments)
        
        # Если появились новые комментарии или изменилась дата последнего обновления
        if len(curr_comments) > len(prev_comments) or (curr_max_updated and curr_max_updated != prev_max_updated):
            # Определяем, какие комментарии новые или изменённые
            # Для простоты добавим информацию о количестве и факте обновления
            new_comments = []
            updated_comments = []
            
            # Создаём словарь предыдущих комментариев по id
            prev_by_id = {c.get("id"): c for c in prev_comments if c.get("id")}
            
            for comment in curr_comments:
                comment_id = comment.get("id")
                if comment_id not in prev_by_id:
                    # Новый комментарий
                    new_comments.append({
                        "author": comment.get("author", {}).get("displayName", "Неизвестно"),
                        "body": comment.get("body", "")[:200],
                        "created": comment.get("created"),
                        "updated": comment.get("updated")
                    })
                else:
                    # Проверяем, обновлялся ли комментарий
                    prev_updated = prev_by_id[comment_id].get("updated")
                    curr_updated = comment.get("updated")
                    if curr_updated and prev_updated and curr_updated != prev_updated:
                        updated_comments.append({
                            "author": comment.get("author", {}).get("displayName", "Неизвестно"),
                            "body": comment.get("body", "")[:200],
                            "created": comment.get("created"),
                            "updated": curr_updated
                        })
            
            if new_comments:
                changes["new_comments"] = new_comments
            if updated_comments:
                changes["updated_comments"] = updated_comments
        
        # 3. Изменение связанных задач (блокировки, подзадачи)
        prev_links = prev_fields.get("issuelinks", [])
        curr_links = curr_fields.get("issuelinks", [])
        
        # Находим новые связи
        if len(curr_links) != len(prev_links):
            # Получаем ключи связанных задач
            prev_link_keys = set()
            for link in prev_links:
                if link.get("inwardIssue"):
                    prev_link_keys.add(link["inwardIssue"].get("key"))
                if link.get("outwardIssue"):
                    prev_link_keys.add(link["outwardIssue"].get("key"))
            
            new_links = []
            for link in curr_links:
                link_key = None
                link_type = link.get("type", {}).get("name", "")
                
                if link.get("inwardIssue"):
                    link_key = link["inwardIssue"].get("key")
                    direction = "inward"
                elif link.get("outwardIssue"):
                    link_key = link["outwardIssue"].get("key")
                    direction = "outward"
                else:
                    continue
                
                if link_key and link_key not in prev_link_keys:
                    new_links.append({
                        "key": link_key,
                        "type": link_type,
                        "direction": direction
                    })
            
            if new_links:
                changes["new_links"] = new_links
        
        # 4. Изменение исполнителя (важное событие)
        # Jira может отдавать assignee = null. Тогда prev_fields.get("assignee") вернёт None и .get упадёт.
        # Нормализуем значения к dict.
        prev_assignee_obj = prev_fields.get("assignee") or {}
        curr_assignee_obj = curr_fields.get("assignee") or {}
        prev_assignee = prev_assignee_obj.get("displayName") if isinstance(prev_assignee_obj, dict) else None
        curr_assignee = curr_assignee_obj.get("displayName") if isinstance(curr_assignee_obj, dict) else None
        if prev_assignee != curr_assignee:
            changes["assignee_changed"] = {
                "from": prev_assignee,
                "to": curr_assignee,
                "was_unassigned": prev_assignee is None or prev_assignee == "Не назначен",
                "is_unassigned": curr_assignee is None or curr_assignee == "Не назначен"
            }
        
        # 5. Изменение приоритета (особенно если повышается)
        prev_priority = prev_fields.get("priority", {}).get("name")
        curr_priority = curr_fields.get("priority", {}).get("name")
        if prev_priority != curr_priority:
            changes["priority_changed"] = {
                "from": prev_priority,
                "to": curr_priority,
                "priority_increased": self._is_priority_increased(prev_priority, curr_priority)
            }
        
        # 6. Изменение описания (только если существенное)
        prev_desc = prev_fields.get("description", "")
        curr_desc = curr_fields.get("description", "")
        if prev_desc != curr_desc:
            # Считаем изменение существенным если:
            # 1. Добавилось больше 50 символов
            # 2. Изменилось более 30% текста
            if len(curr_desc) - len(prev_desc) > 50:
                changes["description_updated"] = True
            elif prev_desc and curr_desc:
                # Простая проверка на существенное изменение
                prev_words = set(prev_desc.lower().split())
                curr_words = set(curr_desc.lower().split())
                added_words = curr_words - prev_words
                if len(added_words) > 10:
                    changes["description_updated"] = True
        
        # 7. Изменение меток или компонентов
        prev_labels = set(prev_fields.get("labels", []))
        curr_labels = set(curr_fields.get("labels", []))
        if prev_labels != curr_labels:
            added = list(curr_labels - prev_labels)
            removed = list(prev_labels - curr_labels)
            if added or removed:
                changes["labels_changed"] = {
                    "added": added,
                    "removed": removed
                }
        
        # 8. Изменение сроков (если есть поле due date)
        prev_due = prev_fields.get("duedate")
        curr_due = curr_fields.get("duedate")
        if prev_due != curr_due:
            changes["due_date_changed"] = {
                "from": prev_due,
                "to": curr_due
            }
        
        # 9. Изменение компонентов
        prev_components = set(c.get("name", "") for c in prev_fields.get("components", []))
        curr_components = set(c.get("name", "") for c in curr_fields.get("components", []))
        if prev_components != curr_components:
            added = list(curr_components - prev_components)
            removed = list(prev_components - curr_components)
            if added or removed:
                changes["components_changed"] = {
                    "added": added,
                    "removed": removed
                }
        
        return changes
    
    def _is_priority_increased(self, prev_priority: str, curr_priority: str) -> bool:
        """Определить, повысился ли приоритет"""
        if not prev_priority or not curr_priority:
            return False
        
        priority_order = {
            "Highest": 5, "Высший": 5, "Критический": 5,
            "High": 4, "Высокий": 4, "Высокая": 4,
            "Medium": 3, "Средний": 3, "Средняя": 3, "Normal": 3,
            "Low": 2, "Низкий": 2, "Низкая": 2,
            "Lowest": 1, "Наименьший": 1, "Минимальный": 1
        }
        
        prev_level = priority_order.get(prev_priority, 3)
        curr_level = priority_order.get(curr_priority, 3)
        
        return curr_level > prev_level
    
    def _is_unresolved_status(self, status_name: str) -> bool:
        """Проверить, является ли статус проблемным/блокирующим"""
        if not status_name:
            return False
            
        problematic_statuses = [
            'блокировано', 'blocked', 'заблокировано', 'blocked',
            'в ожидании', 'waiting', 'pending', 'ожидание',
            'отклонено', 'rejected', 'отклонен',
            'остановлено', 'stopped', 'приостановлено', 'paused'
        ]
        
        status_lower = status_name.lower().strip()
        return any(problem_status in status_lower for problem_status in problematic_statuses)
    
    async def _create_change_info(self, user_id: int, filter_name: str, issue: Dict,
                                client, change_type: str, previous_data: Dict,
                                is_new: bool, changes: Dict) -> Optional[Dict]:
        """
        Создание информации об изменениях для уведомления
        """
        try:
            issue_key = issue.get("key")
            
            # Анализируем блокирующие задачи с использованием кэша
            blockers = await self.analyze_blockers(user_id, filter_name, issue, client)
            
            # Проверяем отмену блокировок (новые разрешенные блокировки)
            if blockers and previous_data:
                prev_links = previous_data.get("fields", {}).get("issuelinks", [])
                if prev_links:
                    # Находим задачи, которые были блокирующими, а теперь разрешены
                    resolved_blockers = self._find_newly_resolved_blockers(prev_links, blockers)
                    if resolved_blockers:
                        changes["blockers_resolved"] = resolved_blockers
            
            # Вычисляем хэш уведомления
            notification_hash = self._calculate_notification_hash_with_changes(
                issue, change_type, changes, blockers
            )
            
            if not notification_hash:
                return None
            
            # Проверяем, отправляли ли уже такое уведомление
            if await self.db.was_notification_sent(user_id, filter_name, issue_key, notification_hash):
                return None
            
            # Возвращаем полную информацию
            return {
                "change_type": change_type,
            "issue_updated": issue.get("fields", {}).get("updated"),
                "issue_key": issue_key,
                "notification_hash": notification_hash,
                "issue": issue,
                "is_new": is_new,
                "user_id": user_id,
                "filter_name": filter_name,
                "blockers": blockers,
                "changes": changes,
                "previous_state": previous_data if not is_new else {}
            }
            
        except Exception as e:
            logger.error(f"❌ Error creating change info: {e}")
            return None
    
    def _find_newly_resolved_blockers(self, prev_links: List, current_blockers: List) -> List:
        """
        Найти блокирующие задачи, которые были активны, а теперь разрешены
        """
        # Создаем словарь текущих блокировок по ключу
        current_by_key = {b["key"]: b for b in current_blockers if b.get("key")}
        
        # Ищем в предыдущих связях задачи, которые сейчас разрешены
        resolved = []
        
        for link in prev_links:
            blocker_key = None
            if link.get("inwardIssue"):
                blocker_key = link["inwardIssue"].get("key")
            elif link.get("outwardIssue"):
                blocker_key = link["outwardIssue"].get("key")
            
            if blocker_key and blocker_key in current_by_key:
                blocker = current_by_key[blocker_key]
                if blocker.get("is_resolved"):
                    resolved.append(blocker)
        
        return resolved
    
    def _calculate_notification_hash_with_changes(self, issue: Dict, 
                                                change_type: str, 
                                                changes: Dict,
                                                blockers: List) -> str:
        """
        Вычислить хэш уведомления с учетом конкретных изменений
        """
        # Создаем структуру данных для хеширования
        hash_data = {
            "issue_key": issue.get("key"),
            "change_type": change_type,
            # Берём серверное поле updated, чтобы одинаковое событие не давало новый хэш каждую минуту
            "issue_updated": issue.get("fields", {}).get("updated"),
            "changes_summary": {}
        }

        
        # Добавляем только ключевые изменения для хеширования
        if "status_changed" in changes:
            hash_data["changes_summary"]["status"] = changes["status_changed"]["to"]
        
        if "new_comments" in changes:
            hash_data["changes_summary"]["new_comments_count"] = len(changes["new_comments"])
        
        if "updated_comments" in changes:
            hash_data["changes_summary"]["updated_comments_count"] = len(changes["updated_comments"])
        
        if "new_links" in changes:
            hash_data["changes_summary"]["new_links_count"] = len(changes["new_links"])
        
        if "assignee_changed" in changes:
            hash_data["changes_summary"]["assignee"] = changes["assignee_changed"]["to"]
        
        if "priority_changed" in changes:
            hash_data["changes_summary"]["priority"] = changes["priority_changed"]["to"]
        
        if "blockers_resolved" in changes:
            hash_data["changes_summary"]["resolved_blockers"] = len(changes["blockers_resolved"])
        
        # Добавляем информацию о блокировках
        hash_data["blockers_count"] = len(blockers)
        hash_data["blockers_keys"] = [b.get("key") for b in blockers if b.get("key")]
        
        # Преобразуем в строку и вычисляем хэш
        data_str = json.dumps(hash_data, sort_keys=True)
        return hashlib.md5(data_str.encode()).hexdigest()
    
    async def analyze_blockers(self, user_id: int, filter_name: str, 
                             issue: Dict[str, Any], client) -> List[Dict[str, Any]]:
        """
        Анализ блокирующих задач для конкретной задачи с использованием кэша
        """
        try:
            issue_key = issue.get("key")
            
            blockers = []
            
            # Получаем связи задачи
            issue_links = issue.get("fields", {}).get("issuelinks", [])
            
            if not issue_links:
                return []
            
            # Анализируем все связи
            for link in issue_links:
                link_type = link.get("type", {})
                inward_issue = link.get("inwardIssue")
                outward_issue = link.get("outwardIssue")
                
                # Проверяем блокирующие связи
                if inward_issue and link_type.get("inward", "").lower() in ["блокируется", "блокируется посредством", "blocks", "is blocked by"]:
                    blocker_info = await self._get_blocker_info_cached(user_id, client, inward_issue, "inward")
                    if blocker_info:
                        blockers.append(blocker_info)
                
                # Проверяем блокируемые связи
                if outward_issue and link_type.get("outward", "").lower() in ["блокирует", "блокирует посредством", "blocks", "is blocked by"]:
                    blocker_info = await self._get_blocker_info_cached(user_id, client, outward_issue, "outward")
                    if blocker_info:
                        blockers.append(blocker_info)
            
            # Убираем дубликаты
            unique_blockers = []
            seen_keys = set()
            
            for blocker in blockers:
                blocker_key = blocker.get("key")
                if blocker_key and blocker_key not in seen_keys:
                    seen_keys.add(blocker_key)
                    unique_blockers.append(blocker)
                elif not blocker_key:
                    unique_blockers.append(blocker)
            
            return unique_blockers
            
        except Exception as e:
            logger.error(f"❌ Error analyzing blockers: {e}")
            return []
    
    async def _get_blocker_info_cached(self, user_id: int, client, blocker_issue: Dict[str, Any], direction: str) -> Optional[Dict[str, Any]]:
        """
        Получить информацию о блокирующей задаче с использованием кэша
        """
        try:
            issue_key = blocker_issue.get("key")
            if not issue_key:
                return None
            
            # Проверяем кэш
            cached = self._get_from_cache(user_id, issue_key)
            if cached:
                logger.debug(f"Cache hit for {issue_key}")
                # Добавляем направление и URL (кэш не содержит динамических полей)
                cached["direction"] = direction
                cached["url"] = f"{client.base_url}/browse/{issue_key}"
                return cached
            
            # АСИНХРОННЫЙ ВЫЗОВ, если нет в кэше
            async with self._semaphore:
                issue_data = await client.get_issue(issue_key)
            if not issue_data:
                return None
            
            fields = issue_data.get("fields", {})
            resolution = fields.get("resolution")
            is_resolved = resolution is not None
            
            result = {
                "key": issue_key,
                "summary": fields.get("summary", "Без названия"),
                "status": fields.get("status", {}).get("name", "Неизвестно"),
                "assignee": fields.get("assignee", {}).get("displayName", "Не назначен"),
                "is_resolved": is_resolved,
                "resolution": resolution.get("name") if resolution else None,
                "priority": fields.get("priority", {}).get("name", "Не указан"),
                "updated": fields.get("updated"),
                "url": f"{client.base_url}/browse/{issue_key}",
                "direction": direction
            }
            
            # Сохраняем в кэш (без направления и url, так как они могут меняться)
            self._put_in_cache(user_id, issue_key, {
                "key": issue_key,
                "summary": result["summary"],
                "status": result["status"],
                "assignee": result["assignee"],
                "is_resolved": is_resolved,
                "resolution": result["resolution"],
                "priority": result["priority"],
                "updated": result["updated"]
            })
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error getting blocker info: {e}")
            return None
    
    def get_change_description(self, changes: Dict) -> Tuple[str, str]:
        """
        Получить описание изменений и эмодзи для типа изменения
        """
        change_count = sum(1 for k in changes.keys() if not k.startswith('_'))
        
        if not changes:
            return "🔄 Изменения в задаче", "Обновлены детали задачи"
        
        # Определяем основное изменение
        description_parts = []
        emoji = "🔄"
        
        if "status_changed" in changes:
            status_info = changes["status_changed"]
            from_status = status_info["from"] or "нет статуса"
            to_status = status_info["to"] or "неизвестно"
            
            if status_info.get("is_resolved"):
                emoji = "✅"
                description_parts.append(f"Статус: {from_status} → {to_status} (завершено)")
            elif status_info.get("is_unresolved"):
                emoji = "⚠️"
                description_parts.append(f"Статус: {from_status} → {to_status} (проблемный)")
            else:
                emoji = "📊"
                description_parts.append(f"Статус: {from_status} → {to_status}")
        
        if "new_comments" in changes:
            comments = changes["new_comments"]
            emoji = "💬" if emoji == "🔄" else emoji
            if len(comments) == 1:
                description_parts.append("Добавлен новый комментарий")
            else:
                description_parts.append(f"Добавлено {len(comments)} новых комментариев")
        
        if "updated_comments" in changes:
            comments = changes["updated_comments"]
            emoji = "✏️" if emoji == "🔄" else emoji
            if len(comments) == 1:
                description_parts.append("Комментарий отредактирован")
            else:
                description_parts.append(f"Отредактировано {len(comments)} комментариев")
        
        if "new_links" in changes:
            links = changes["new_links"]
            emoji = "🔗" if emoji == "🔄" else emoji
            if len(links) == 1:
                description_parts.append("Добавлена новая связанная задача")
            else:
                description_parts.append(f"Добавлено {len(links)} новых связанных задач")
        
        if "assignee_changed" in changes:
            assignee_info = changes["assignee_changed"]
            emoji = "👤" if emoji == "🔄" else emoji
            
            if assignee_info.get("was_unassigned"):
                description_parts.append(f"Назначен исполнитель: {assignee_info['to']}")
            elif assignee_info.get("is_unassigned"):
                description_parts.append(f"Исполнитель снят: {assignee_info['from']}")
            else:
                description_parts.append(f"Исполнитель: {assignee_info['from']} → {assignee_info['to']}")
        
        if "priority_changed" in changes:
            priority_info = changes["priority_changed"]
            from_priority = priority_info["from"] or "не указан"
            to_priority = priority_info["to"] or "не указан"
            
            if priority_info.get("priority_increased"):
                emoji = "🚨"
                description_parts.append(f"Приоритет повышен: {from_priority} → {to_priority}")
            else:
                emoji = "📋" if emoji == "🔄" else emoji
                description_parts.append(f"Приоритет: {from_priority} → {to_priority}")
        
        if "blockers_resolved" in changes:
            blockers = changes["blockers_resolved"]
            emoji = "🎉"
            if len(blockers) == 1:
                description_parts.append(f"Блокирующая задача {blockers[0].get('key', '')} разрешена")
            else:
                description_parts.append(f"Разрешено {len(blockers)} блокирующих задач")
        
        if "labels_changed" in changes:
            labels_info = changes["labels_changed"]
            if labels_info["added"]:
                emoji = "🏷️" if emoji == "🔄" else emoji
                added_labels = labels_info["added"][:3]
                if len(added_labels) == 1:
                    description_parts.append(f"Добавлена метка: {added_labels[0]}")
                else:
                    description_parts.append(f"Добавлены метки: {', '.join(added_labels)}")
        
        if "due_date_changed" in changes:
            due_info = changes["due_date_changed"]
            emoji = "⏰" if emoji == "🔄" else emoji
            if due_info["from"] and due_info["to"]:
                description_parts.append(f"Срок: {due_info['from']} → {due_info['to']}")
            elif due_info["to"]:
                description_parts.append(f"Установлен срок: {due_info['to']}")
            elif due_info["from"] and not due_info["to"]:
                description_parts.append("Срок снят")
        
        if change_count > 3:
            description = f"Изменено {change_count} параметров"
        elif description_parts:
            description = " | ".join(description_parts)
        else:
            description = "Обновление задачи"
        
        return f"{emoji} {description}", description

# Глобальный экземпляр
smart_watcher = SmartWatcher()
