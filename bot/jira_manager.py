# jira_manager.py
import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from .jira_client import JiraClient, JiraAuthError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class JiraManagerConfig:
    """Параметры, влияющие на стабильность и нагрузку на Jira."""
    max_concurrency: int = 5          # общий предел параллельных запросов в Jira на процесс
    max_clients: int = 200            # ограничение количества клиентов в памяти (LRU)
    client_ttl_seconds: int = 3600    # время жизни клиента без активности (опционально)


class JiraManager:
    """Хранилище Jira-клиентов по пользователю + общий контроль нагрузки.

    В исходной версии клиенты лежали в dict без лимитов и без синхронизации.
    Здесь:
    - LRU на клиентов (чтобы не течь памятью),
    - общий семафор на все запросы (чтобы не DDOS'ить Jira),
    - безопасное пересоздание клиента после 401/403.
    """

    def __init__(self, config: Optional[JiraManagerConfig] = None):
        self.config = config or JiraManagerConfig()

        # Важно: max_concurrency и max_clients можно переопределять env'ами, но не тащим config.py сюда.
        import os
        self.config.max_concurrency = int(os.getenv("JIRA_MAX_CONCURRENCY", str(self.config.max_concurrency)))
        self.config.max_clients = int(os.getenv("JIRA_CLIENTS_MAX", str(self.config.max_clients)))

        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)

        # OrderedDict даёт LRU: последний использованный в конце.
        self._user_clients: "OrderedDict[int, JiraClient]" = OrderedDict()
        self._lock = asyncio.Lock()

        logger.info(
            "✅ JiraManager initialized (max_concurrency=%s, max_clients=%s)",
            self.config.max_concurrency,
            self.config.max_clients,
        )

    async def create_client(
        self,
        user_id: int,
        jira_user: str,
        jira_token: str,
        base_url: Optional[str] = None,
    ) -> JiraClient:
        """Создаёт/заменяет клиента для пользователя."""
        async with self._lock:
            client = JiraClient(
                base_url=base_url,
                username=jira_user,
                token=jira_token,
                semaphore=self._semaphore,
            )

            # При замене — закрываем старый клиент, чтобы не утекали соединения
            old = self._user_clients.pop(user_id, None)
            if old:
                try:
                    await old.aclose()
                except Exception:
                    logger.exception("Failed to close old JiraClient for user_id=%s", user_id)

            self._user_clients[user_id] = client
            self._user_clients.move_to_end(user_id)

            await self._evict_if_needed()
            return client

    def get_client(self, user_id: int) -> Optional[JiraClient]:
        """Возвращает клиента, если он есть (sync-метод для удобства вызова из хэндлеров)."""
        client = self._user_clients.get(user_id)
        if client:
            # LRU
            self._user_clients.move_to_end(user_id)
        return client

    async def invalidate_client(self, user_id: int) -> None:
        """Удаляет клиента (например, после 401), корректно закрывая ресурсы."""
        async with self._lock:
            client = self._user_clients.pop(user_id, None)
            if client:
                try:
                    await client.aclose()
                except Exception:
                    logger.exception("Failed to close JiraClient for user_id=%s", user_id)

    async def remove_client(self, user_id: int) -> None:
        """Backward-compatible alias.

        В коде раньше использовался remove_client(), теперь основной метод — invalidate_client().
        Оставляем алиас, чтобы не ловить AttributeError в старых местах.
        """
        await self.invalidate_client(user_id)

    async def get_or_create_client(
        self,
        user_id: int,
        jira_user: str,
        jira_token: str,
        base_url: Optional[str] = None,
    ) -> JiraClient:
        client = self.get_client(user_id)
        if client:
            return client
        return await self.create_client(user_id, jira_user, jira_token, base_url=base_url)

    async def handle_auth_error(self, user_id: int, exc: Exception) -> None:
        """Единая точка для реакции на auth errors."""
        if isinstance(exc, JiraAuthError):
            await self.invalidate_client(user_id)

    async def aclose(self) -> None:
        """Закрыть всех клиентов."""
        async with self._lock:
            clients = list(self._user_clients.values())
            self._user_clients.clear()

        for c in clients:
            try:
                await c.aclose()
            except Exception:
                logger.exception("Failed to close JiraClient")

    async def _evict_if_needed(self) -> None:
        """LRU-эвикшн."""
        while len(self._user_clients) > self.config.max_clients:
            uid, client = self._user_clients.popitem(last=False)
            try:
                await client.aclose()
            except Exception:
                logger.exception("Failed to close evicted JiraClient uid=%s", uid)


jira_manager = JiraManager()
