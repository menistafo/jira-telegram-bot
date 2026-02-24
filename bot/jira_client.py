# jira_client.py
import httpx
import logging
import asyncio
import os
import random
from datetime import datetime
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

class JiraAuthError(Exception):
    """Исключение при ошибке аутентификации в Jira (401)"""
    pass

class JiraClient:
    def __init__(self, base_url: str = None, username: str = None, token: str = None, semaphore: asyncio.Semaphore | None = None):
        from .config import JIRA_BASE_URL
        
        self.base_url = (base_url or JIRA_BASE_URL).rstrip('/')
        self.username = username
        self.token = token
        
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}" if self.token else "",
            "Content-Type": "application/json"
        }
        
        self._semaphore = semaphore  # общий семафор JiraManager для ограничения параллелизма

        # TLS verify options:
        #  - JIRA_TLS_VERIFY=true|false (default: true)
        #  - JIRA_CA_BUNDLE=/path/to/ca.pem (optional)
        tls_verify = os.getenv("JIRA_TLS_VERIFY", "true").lower() in ("1", "true", "yes", "on")
        ca_bundle = os.getenv("JIRA_CA_BUNDLE")

        verify_value = True
        if not tls_verify:
            verify_value = False
        elif ca_bundle:
            verify_value = ca_bundle

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            verify=verify_value,
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=20)
        )
        
        logger.debug(f"Async JiraClient created for {username}")
    
    async def close(self):
        await self.client.aclose()
        logger.debug(f"Async JiraClient closed for {self.username}")
    
    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Dict | None = None,
        data: Dict | None = None,
        timeout: int = 30,
    ) -> httpx.Response:
        """HTTP-запрос к Jira с production-retry/backoff.

        Поведение:
        - 401/403 -> JiraAuthError (без ретраев)
        - 429 -> ждём Retry-After (если есть) + jitter, иначе backoff
        - 5xx/сеть/таймаут -> экспоненциальный backoff c full-jitter
        """
        url = endpoint

        max_retries = int(os.getenv("JIRA_HTTP_MAX_RETRIES", "6"))
        base_delay = float(os.getenv("JIRA_HTTP_BASE_DELAY", "0.5"))
        max_delay = float(os.getenv("JIRA_HTTP_MAX_DELAY", "30"))

        def backoff(attempt: int) -> float:
            # Full jitter: random between 0 and min(max_delay, base*2^attempt)
            cap = min(max_delay, base_delay * (2 ** attempt))
            return random.uniform(0.0, cap)

        for attempt in range(max_retries):
            try:
                logger.debug(
                    "Making %s request to: %s%s (attempt %s/%s)",
                    method,
                    self.base_url,
                    url,
                    attempt + 1,
                    max_retries,
                )

                if self._semaphore:
                    async with self._semaphore:
                        response = await self.client.request(
                            method=method,
                            url=url,
                            params=params,
                            json=data,
                            timeout=timeout,
                        )
                else:
                    response = await self.client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=data,
                        timeout=timeout,
                    )

                logger.debug("Response status: %s", response.status_code)

                if response.status_code in (401, 403):
                    logger.error("❌ Jira authentication failed (%s) for user %s", response.status_code, self.username)
                    raise JiraAuthError(f"Authentication failed: {response.status_code}")

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        delay = min(max_delay, float(retry_after)) + random.uniform(0.0, 0.5)
                    else:
                        delay = backoff(attempt)
                    logger.warning("Jira rate-limited (429). Sleeping %.2fs", delay)
                    await asyncio.sleep(delay)
                    continue

                if 500 <= response.status_code < 600:
                    if attempt < max_retries - 1:
                        delay = backoff(attempt)
                        logger.warning("Jira server error %s. Retrying in %.2fs", response.status_code, delay)
                        await asyncio.sleep(delay)
                        continue
                    logger.error("Max retries exceeded for server error %s", response.status_code)
                    return response

                return response

            except (httpx.ConnectError, httpx.ReadError) as e:
                if attempt < max_retries - 1:
                    delay = backoff(attempt)
                    logger.warning("Connection error: %s. Retrying in %.2fs", e, delay)
                    await asyncio.sleep(delay)
                    continue
                logger.error("Max retries exceeded for %s", url)
                raise

            except httpx.TimeoutException as e:
                if attempt < max_retries - 1:
                    delay = backoff(attempt)
                    logger.warning("Timeout: %s. Retrying in %.2fs", e, delay)
                    await asyncio.sleep(delay)
                    continue
                raise

            except Exception:
                logger.exception("Request failed")
                raise

        raise Exception(f"Failed after {max_retries} attempts")

    async def test_connection(self) -> bool:
        temp_client = None
        try:
            temp_client = JiraClient(
                base_url=self.base_url,
                username=self.username,
                token=self.token
            )
            
            response = await temp_client._make_request("/rest/api/2/myself", method="GET")
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"✅ Jira connection successful for user: {data.get('displayName', 'Unknown')}")
                return True
            else:
                logger.error(f"❌ Jira connection failed: {response.status_code} - {response.text[:200]}")
                return False
        except JiraAuthError:
            logger.error("❌ Jira authentication failed during test")
            return False
        except Exception as e:
            logger.error(f"❌ Jira connection error: {e}")
            return False
        finally:
            if temp_client:
                await temp_client.close()
    
    async def search(self, jql: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Быстрый поиск (без расширенных полей)."""
        try:
            params = {
                "jql": jql,
                "maxResults": max_results,
                "fields": "id,key,summary,assignee,status,priority,updated,issuelinks,comment,reporter",
            }

            response = await self._make_request("/rest/api/2/search", params=params)

            if response.status_code == 200:
                data = response.json()
                issues = data.get("issues", [])
                logger.info("✅ Found %s issues for JQL: %s...", len(issues), jql[:50])
                return issues

            logger.error("❌ Search failed: %s - %s", response.status_code, response.text[:200])
            return []

        except JiraAuthError:
            raise
        except Exception as e:
            logger.error("❌ Search error: %s", e)
            return []

    async def search_with_details_page(
        self,
        jql: str,
        *,
        start_at: int = 0,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        """Постраничный поиск задач с полями.

        Возвращает dict:
          {"issues": [...], "total": int, "startAt": int, "maxResults": int}
        """
        fields = (
            "id,key,summary,assignee,status,priority,updated,issuelinks,"
            "comment,reporter,description,creator,labels,duedate,components,"
            "customfield_24525"
        )
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": fields,
        }

        response = await self._make_request("/rest/api/2/search", params=params)
        if response.status_code == 200:
            data = response.json()
            return {
                "issues": data.get("issues", []) or [],
                "total": int(data.get("total", 0) or 0),
                "startAt": int(data.get("startAt", start_at) or start_at),
                "maxResults": int(data.get("maxResults", max_results) or max_results),
            }

        logger.error("❌ Jira search_with_details_page failed: %s %s", response.status_code, response.text[:200])
        return {"issues": [], "total": 0, "startAt": start_at, "maxResults": max_results}

    async def search_with_details(self, jql: str, max_results: int = 50) -> List[Dict[str, Any]]:
        """Совместимость со старым кодом: возвращает только issues списком."""
        page = await self.search_with_details_page(jql, start_at=0, max_results=max_results)
        issues = page.get("issues", [])
        logger.info("✅ Found %s issues with details for JQL: %s..", len(issues), jql[:50])
        return issues
    
    async def get_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Получить задачу Jira по ключу."""
        try:
            params = {
                "fields": "id,key,summary,assignee,status,priority,updated,issuelinks,comment,reporter,description,creator"
            }

            response = await self._make_request(f"/rest/api/2/issue/{issue_key}", params=params)

            if response.status_code == 200:
                return response.json()

            logger.error("❌ Get issue failed: %s - %s", response.status_code, response.text[:200])
            return None

        except JiraAuthError:
            raise
        except Exception as e:
            logger.error("❌ Get issue error: %s", e)
            return None

    def is_resolved_status(self, status_name: str) -> bool:
        if not status_name:
            return False
            
        resolved_statuses = [
            'closed', 'закрыт', 'закрыто', 'done', 'выполнено', 'resolved',
            'решена', 'готово', 'готов', 'отклонено', 'rejected', 'cancelled',
            'отменено', 'отменен', 'решен', 'завершено', 'завершен','исправлен','ответ получен','доступ выдан'
        ]
        
        status_lower = status_name.lower().strip()
        return any(resolved_status in status_lower for resolved_status in resolved_statuses)
    
    async def search_mentions_batch(self, username: str, last_check_time: str = None, 
                             batch_size: int = 10, exclude_resolved: bool = True) -> List[Dict]:
        try:
            jql = f'text ~ "@{username}"'
            
            if last_check_time:
                try:
                    clean_time = last_check_time.replace('Z', '+00:00').replace('T', ' ')
                    
                    formats_to_try = [
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d %H:%M",
                        "%Y-%m-%d"
                    ]
                    
                    parsed_dt = None
                    for fmt in formats_to_try:
                        try:
                            parsed_dt = datetime.strptime(clean_time.split('+')[0].strip(), fmt)
                            break
                        except ValueError:
                            continue
                    
                    if parsed_dt:
                        jira_time_format = parsed_dt.strftime("%Y-%m-%d %H:%M")
                        jql += f' AND updated >= "{jira_time_format}"'
                        logger.info(f"📅 Using Jira time format: {jira_time_format} (from {last_check_time})")
                    else:
                        logger.warning(f"⚠️ Could not parse time: {last_check_time}, skipping time filter")
                        
                except Exception as time_error:
                    logger.error(f"❌ Error parsing time {last_check_time}: {time_error}")
                    pass
            
            if exclude_resolved:
                jql += f' AND statusCategory != Done'
            
            logger.info(f"🔍 Searching mentions for @{username} with batch size {batch_size}")
            logger.info(f"📝 JQL: {jql}")
            
            all_issues = []
            start_at = 0
            total_issues = 0
            
            while True:
                params = {
                    "jql": jql,
                    "maxResults": batch_size,
                    "startAt": start_at,
                    "fields": "id,key,summary,assignee,status,priority,updated,comment,reporter,creator,description",
                    "expand": "comment"
                }
                
                response = await self._make_request("/rest/api/2/search", params=params)
                
                if response.status_code != 200:
                    logger.error(f"Search mentions failed: {response.status_code} - {response.text[:200]}")
                    break
                
                data = response.json()
                issues = data.get("issues", [])
                total_issues = data.get("total", 0)
                
                if exclude_resolved:
                    filtered_issues = []
                    for issue in issues:
                        status_name = issue.get("fields", {}).get("status", {}).get("name", "")
                        if not self.is_resolved_status(status_name):
                            filtered_issues.append(issue)
                        else:
                            logger.debug(f"Filtered out resolved issue: {issue.get('key')} with status: {status_name}")
                    issues = filtered_issues
                
                all_issues.extend(issues)
                
                logger.info(f"Batch {start_at//batch_size + 1}: loaded {len(issues)} active issues, total {len(all_issues)}/{total_issues}")
                
                if start_at + batch_size >= total_issues or len(issues) == 0:
                    break
                    
                start_at += batch_size
                
                await asyncio.sleep(0.5)
            
            logger.info(f"✅ Found {len(all_issues)} active issues mentioning @{username} (excluding resolved)")
            return all_issues
            
        except JiraAuthError:
            raise
        except Exception as e:
            logger.error(f"Search mentions error: {e}")
            return []