# bot/jira_client.py
from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class JiraAuthError(Exception):
    """Исключение при ошибке аутентификации в Jira (401/403)."""


def _build_auth_headers(username: Optional[str], token: Optional[str]) -> Dict[str, str]:
    """
    Поддерживаем 3 сценария:
    1) token начинается с 'Bearer ' -> используем как есть
    2) token начинается с 'Basic '  -> используем как есть
    3) иначе -> по умолчанию Basic(username:token)
       (можно переопределить JIRA_AUTH_MODE=bearer|basic|auto)
    """
    headers: Dict[str, str] = {}

    if not token:
        return headers

    mode = os.getenv("JIRA_AUTH_MODE", "auto").strip().lower()

    tok = token.strip()
    low = tok.lower()

    # если токен уже с префиксом — не трогаем
    if low.startswith("bearer "):
        headers["Authorization"] = tok
        return headers
    if low.startswith("basic "):
        headers["Authorization"] = tok
        return headers

    # принудительный режим
    if mode == "bearer":
        headers["Authorization"] = f"Bearer {tok}"
        return headers

    # basic / auto -> Basic
    # для Basic обязательно нужен username
    if not username:
        # оставим как Bearer (на всякий случай), но залогируем, чтобы было видно
        logger.warning("Jira auth: username is empty, falling back to Bearer token")
        headers["Authorization"] = f"Bearer {tok}"
        return headers

    raw = f"{username}:{tok}".encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    headers["Authorization"] = f"Basic {b64}"
    return headers


class JiraClient:
    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        token: str | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ):
        from .config import JIRA_BASE_URL

        self.base_url = (base_url or JIRA_BASE_URL).rstrip("/")
        self.username = username
        self.token = token
        self._semaphore = semaphore  # общий семафор JiraManager для ограничения параллелизма

        # TLS verify options:
        # - JIRA_TLS_VERIFY=true|false (default: true)
        # - JIRA_CA_BUNDLE=/path/to/ca.pem (optional)
        tls_verify = os.getenv("JIRA_TLS_VERIFY", "true").lower() in ("1", "true", "yes", "on")
        ca_bundle = os.getenv("JIRA_CA_BUNDLE")
        verify_value: bool | str = True
        if not tls_verify:
            verify_value = False
        elif ca_bundle:
            verify_value = ca_bundle

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(_build_auth_headers(self.username, self.token))

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            verify=verify_value,
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=20),
        )

        # Важно: логируем режим, но не токен
        auth_mode = os.getenv("JIRA_AUTH_MODE", "auto")
        logger.debug("Async JiraClient created for %s (auth_mode=%s)", username, auth_mode)

    async def close(self):
        await self.client.aclose()
        logger.debug("Async JiraClient closed for %s", self.username)

    async def aclose(self):
        """Алиас для совместимости (JiraManager вызывает aclose())."""
        await self.close()

    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Dict | None = None,
        data: Dict | None = None,
        timeout: int = 30,
    ) -> httpx.Response:
        """
        HTTP-запрос к Jira с production-retry/backoff.

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
            cap = min(max_delay, base_delay * (2**attempt))
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
                    logger.error(
                        "❌ Jira authentication failed (%s) for user %s",
                        response.status_code,
                        self.username,
                    )
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
        temp_client: JiraClient | None = None
        try:
            temp_client = JiraClient(base_url=self.base_url, username=self.username, token=self.token)
            response = await temp_client._make_request("/rest/api/2/myself", method="GET")
            if response.status_code == 200:
                data = response.json()
                logger.info("✅ Jira connection successful for user: %s", data.get("displayName", "Unknown"))
                return True
            logger.error("❌ Jira connection failed: %s - %s", response.status_code, response.text[:200])
            return False
        except JiraAuthError:
            logger.error("❌ Jira authentication failed during test")
            return False
        except Exception as e:
            logger.error("❌ Jira connection error: %s", e)
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
                issues = data.get("issues", []) or []
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
        """Постраничный поиск задач с полями."""
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
        issues = page.get("issues", []) or []
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
            "closed", "закрыт", "закрыто", "done", "выполнено", "resolved", "решена",
            "готово", "готов", "отклонено", "rejected", "cancelled", "отменено",
            "отменен", "решен", "завершено", "завершен", "исправлен", "ответ получен", "доступ выдан",
        ]
        status_lower = status_name.lower().strip()
        return any(x in status_lower for x in resolved_statuses)

    async def search_mentions_batch(
        self,
        username: str,
        last_check_time: str = None,
        batch_size: int = 10,
        exclude_resolved: bool = True,
    ) -> List[Dict]:
        try:
            jql = f'text ~ "@{username}"'

            if last_check_time:
                try:
                    clean_time = last_check_time.replace("Z", "+00:00").replace("T", " ")
                    formats_to_try = [
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d %H:%M",
                        "%Y-%m-%d",
                    ]
                    parsed_dt = None
                    for fmt in formats_to_try:
                        try:
                            parsed_dt = datetime.strptime(clean_time.split("+")[0].strip(), fmt)
                            break
                        except ValueError:
                            continue

                    if parsed_dt:
                        jira_time_format = parsed_dt.strftime("%Y-%m-%d %H:%M")
                        jql += f' AND updated >= "{jira_time_format}"'
                        logger.info("Using Jira time format: %s (from %s)", jira_time_format, last_check_time)
                    else:
                        logger.warning("⚠️ Could not parse time: %s, skipping time filter", last_check_time)
                except Exception as time_error:
                    logger.error("❌ Error parsing time %s: %s", last_check_time, time_error)

            if exclude_resolved:
                jql += " AND statusCategory != Done"

            logger.info("Searching mentions for @%s with batch size %s", username, batch_size)
            logger.info("JQL: %s", jql)

            all_issues: List[Dict] = []
            start_at = 0

            while True:
                params = {
                    "jql": jql,
                    "maxResults": batch_size,
                    "startAt": start_at,
                    "fields": "id,key,summary,assignee,status,priority,updated,comment,reporter,creator,description",
                    "expand": "comment",
                }
                response = await self._make_request("/rest/api/2/search", params=params)
                if response.status_code != 200:
                    logger.error("Search mentions failed: %s - %s", response.status_code, response.text[:200])
                    break

                data = response.json()
                issues = data.get("issues", []) or []
                total_issues = int(data.get("total", 0) or 0)

                if exclude_resolved:
                    filtered = []
                    for issue in issues:
                        status_name = issue.get("fields", {}).get("status", {}).get("name", "") or ""
                        if not self.is_resolved_status(status_name):
                            filtered.append(issue)
                        else:
                            logger.debug("Filtered out resolved issue: %s (%s)", issue.get("key"), status_name)
                    issues = filtered

                all_issues.extend(issues)
                logger.info(
                    "Batch %s: loaded %s active issues, total %s/%s",
                    start_at // batch_size + 1,
                    len(issues),
                    len(all_issues),
                    total_issues,
                )

                if start_at + batch_size >= total_issues or len(issues) == 0:
                    break

                start_at += batch_size
                await asyncio.sleep(0.5)

            logger.info("✅ Found %s active issues mentioning @%s (excluding resolved)", len(all_issues), username)
            return all_issues

        except JiraAuthError:
            raise
        except Exception as e:
            logger.error("Search mentions error: %s", e)
            return []
