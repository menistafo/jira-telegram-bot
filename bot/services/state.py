from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import asyncio


@dataclass
class AppTasks:
    """Храним ссылки на фоновые задачи внутри Application.bot_data.

    Это убирает глобальные переменные и упрощает graceful shutdown.
    """
    watcher: Optional[asyncio.Task] = None
    digest: Optional[asyncio.Task] = None
