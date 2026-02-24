from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, Optional, TypeVar, overload

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..jira_client import JiraAuthError

logger = logging.getLogger(__name__)

T = TypeVar("T")


@overload
def handler_guard(
    handler_name: str,
    *,
    user_message: str = "Произошла ошибка. Попробуйте ещё раз чуть позже.",
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    ...


@overload
def handler_guard(
    handler_name: None = None,
    *,
    user_message: str = "Произошла ошибка. Попробуйте ещё раз чуть позже.",
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    ...


def handler_guard(
    handler_name: Optional[str] = None,
    *,
    user_message: str = "Произошла ошибка. Попробуйте ещё раз чуть позже.",
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """
    Декоратор для хэндлеров python-telegram-bot.

    Зачем:
    - Логирует исключения с контекстом (имя хэндлера)
    - Пытается ответить пользователю (если возможно)
    - JiraAuthError не глушит (его обрабатываем отдельно в сценариях авторизации)

    Совместимость:
    - Поддерживает оба варианта использования:
        @handler_guard()
        @handler_guard("some_handler_name")
        @handler_guard(user_message="...")
        @handler_guard("name", user_message="...")
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        # Если handler_name не задан — используем имя функции (удобно и стабильно)
        _name_for_logs = handler_name or func.__name__

        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any) -> Any:
            try:
                return await func(update, context, *args, **kwargs)

            except JiraAuthError:
                # Важно: не скрываем, чтобы upstream-логика могла попросить токен/логин и т.п.
                raise

            except TelegramError as e:
                # Ошибки Telegram API часто не критичны (например, message is not modified)
                logger.warning("TelegramError in handler %s: %s", _name_for_logs, e)
                return None

            except Exception:
                logger.exception("Unhandled error in handler %s", _name_for_logs)
                # Пытаемся написать пользователю "мягкую" ошибку
                try:
                    if update and update.effective_message:
                        await update.effective_message.reply_text(user_message)
                except Exception:
                    # Не зацикливаемся, если ответ тоже падает
                    pass
                return None

        return wrapper

    return decorator


async def on_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок python-telegram-bot.

    Сюда попадает всё, что не поймали в хэндлерах.
    """
    err = context.error
    logger.exception("Application error: %s", err)

    # Пытаемся пользователю что-то сказать (если update — это Update)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Произошла непредвиденная ошибка. Я уже логирую её, попробуйте позже."
            )
    except Exception:
        # Не усугубляем ошибку обработчика ошибок
        pass