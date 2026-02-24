from __future__ import annotations

import functools
import os
from typing import Awaitable, Callable, Any

from telegram import Update
from telegram.ext import ContextTypes

from ..rate_limit import RateLimiter, RateLimit


def rate_limit(action: str, *, limit: int | None = None, per_seconds: float | None = None) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Rate limit decorator for handlers (per user + action).

    По умолчанию берём настройки из env:
      CMD_RATE_LIMIT_LIMIT (default 6)
      CMD_RATE_LIMIT_PERIOD (default 10)
    """
    lim = limit if limit is not None else int(os.getenv("CMD_RATE_LIMIT_LIMIT", "6"))
    per = per_seconds if per_seconds is not None else float(os.getenv("CMD_RATE_LIMIT_PERIOD", "10"))

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any) -> Any:
            user_id = getattr(update.effective_user, "id", None)
            if not user_id:
                return await func(update, context, *args, **kwargs)

            limiter: RateLimiter = context.application.bot_data.setdefault("rate_limiter", RateLimiter())
            allowed = await limiter.allow(user_id, action, RateLimit(limit=lim, per_seconds=per))
            if not allowed:
                # мягкое сообщение без спама
                if update.effective_message:
                    await update.effective_message.reply_text("Слишком часто. Попробуй чуть позже.")
                return None
            return await func(update, context, *args, **kwargs)

        return wrapper

    return decorator
