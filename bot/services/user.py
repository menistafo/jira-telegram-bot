from __future__ import annotations

from typing import Any, Dict

from telegram import Update

from ..db import db


async def get_user_data(update: Update) -> Dict[str, Any]:
    """Получить (или создать) запись пользователя в БД.

    Исторически эта функция была в `bot.py`. После рефакторинга мы держим её
    в `services`, чтобы:
      - её могли использовать и handlers, и UI,
      - избежать циклических импортов между слоями.
    """
    user = update.effective_user
    chat = update.effective_chat

    # Важно: chat_id может отличаться от user_id (например, если бот в группе),
    # поэтому сохраняем его отдельно.
    return await db.get_or_create_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        chat_id=chat.id if chat else None,
    )
