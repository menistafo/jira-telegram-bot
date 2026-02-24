from __future__ import annotations

import html
import logging
import re
from typing import Optional, Any

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application

logger = logging.getLogger(__name__)

# Управляющие символы (кроме \n и \t), которые иногда ломают форматирование/логирование
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def clean_text(value: Any, *, max_len: int | None = None) -> str:
    """
    Приводит произвольное значение к "безопасному" тексту для Telegram (parse_mode=HTML).

    Что делает:
    - None -> пустая строка
    - убирает управляющие символы (кроме таба/переноса строки)
    - нормализует переводы строк
    - экранирует HTML: <, >, &, кавычки (чтобы не ломать parse_mode=HTML)
    - опционально обрезает по max_len

    Важно:
    - Функция именно "экранирует", а не "удаляет" символы разметки.
      Это безопасно для сообщений, где parse_mode="HTML".
    """
    if value is None:
        text = ""
    else:
        text = str(value)

    # Нормализуем переносы строк
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Убираем мусорные управляющие символы
    text = _CONTROL_CHARS_RE.sub("", text)

    # Экранируем HTML, чтобы пользовательские данные не ломали разметку
    text = html.escape(text, quote=True)

    if max_len is not None and max_len > 0:
        text = text[:max_len]

    return text


async def send_message_safe(
    application: Application,
    user_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: str = "HTML",
    message_id: Optional[int] = None,
) -> int:
    """
    Безопасная отправка/редактирование сообщения.

    Если message_id указан:
      - пытаемся отредактировать сообщение
      - если редактирование невозможно, отправляем новое

    Возвращает message_id результирующего сообщения (старого или нового).
    """
    try:
        if message_id:
            try:
                msg = await application.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                return msg.message_id
            except BadRequest as e:
                msg_txt = str(e)
                if "Message is not modified" in msg_txt:
                    logger.debug("Telegram BadRequest (not modified) for user %s", user_id)
                    return message_id
                # иначе попробуем отправить новое сообщение

        msg = await application.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return msg.message_id

    except BadRequest as e:
        msg_txt = str(e)
        if "Message is not modified" in msg_txt:
            logger.debug("Telegram BadRequest (not modified) for user %s", user_id)
            return message_id or 0
        logger.warning("Telegram BadRequest for user %s: %s", user_id, e)
        return message_id or 0
    except Exception:
        logger.exception("send_message_safe failed for user %s", user_id)
        return message_id or 0