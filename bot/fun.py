import os
from collections import OrderedDict
import socket
import httpx
import logging
import random
import asyncio
from typing import Optional, Tuple, List
from datetime import date

logger = logging.getLogger(__name__)

# -------------------- HTTP helper with retry --------------------
async def _fetch_json(url: str, params: Optional[dict] = None, timeout: float = 15.0, retries: int = 3) -> Optional[dict]:
    """Единая точка для вызовов внешних API.

    В проде внешние сервисы часто флапают: 5xx, таймауты, DNS.
    Поэтому:
      - ограничиваем таймаут,
      - делаем несколько попыток с экспоненциальной задержкой,
      - не падаем исключением наружу (возвращаем None и используем fallback).
    """
    for attempt in range(retries):
        try:
            resp = await http_client.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = (2 ** attempt) + (0.1 * attempt)
            logger.warning("External API call failed (%s), retry %s/%s in %.1fs: %s", url, attempt + 1, retries, wait, e)
            if attempt < retries - 1:
                await asyncio.sleep(wait)
            else:
                return None


_http_client: Optional[httpx.AsyncClient] = None

# ===================== SETTINGS =====================
TRANSLATE_ENABLED = os.getenv("TRANSLATE_ENABLED", "true").lower() in ("1", "true", "yes", "on")

# Yandex Cloud Translate
# Prefer API key for service account:
#   YC_API_KEY=...
#   YC_FOLDER_ID=...
#
# Or IAM token:
#   YC_IAM_TOKEN=...
#   YC_FOLDER_ID=...  (optional; for some auth modes folderId is required; keep it set to be safe)
YC_API_KEY = (os.getenv("YC_API_KEY") or "").strip()
YC_IAM_TOKEN = (os.getenv("YC_IAM_TOKEN") or "").strip()
YC_FOLDER_ID = (os.getenv("YC_FOLDER_ID") or "").strip()

YC_TRANSLATE_URL = os.getenv(
    "YC_TRANSLATE_URL",
    "https://translate.api.cloud.yandex.net/translate/v2/translate"
).strip()

# Max total length of all strings per request is 10000 chars (YC docs).
YC_MAX_CHARS_PER_REQUEST = int(os.getenv("YC_TRANSLATE_MAX_CHARS", "9000"))  # keep some headroom

# ===================== HTTP CLIENT =====================
async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


# ===================== ZODIAC MAP =====================
ZODIAC_MAP = {
    "овен": "aries",
    "телец": "taurus",
    "близнецы": "gemini",
    "рак": "cancer",
    "лев": "leo",
    "дева": "virgo",
    "весы": "libra",
    "скорпион": "scorpio",
    "стрелец": "sagittarius",
    "козерог": "capricorn",
    "водолей": "aquarius",
    "рыбы": "pisces",
}

# cache: zodiac_en -> (date_iso, translated_text)
_horoscope_cache: dict[str, tuple[str, str]] = {}

# Optional cache for translations to reduce costs/latency
# key = (source_lang, target_lang, text) -> translated_text
_translate_cache: "OrderedDict[tuple[str, str, str], str]" = OrderedDict()
TRANSLATE_CACHE_ENABLED = os.getenv("TRANSLATE_CACHE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
TRANSLATE_CACHE_MAX_ITEMS = int(os.getenv("TRANSLATE_CACHE_MAX_ITEMS", "2000"))


# ===================== HELPERS =====================
def _get_yc_auth_headers() -> dict:
    """
    Yandex Cloud auth:
      - Api-Key <API_key>  (service account API key)
      - Bearer <IAM_token> (IAM token)
    """
    if YC_API_KEY:
        return {"Authorization": f"Api-Key {YC_API_KEY}"}
    if YC_IAM_TOKEN:
        return {"Authorization": f"Bearer {YC_IAM_TOKEN}"}
    return {}


def _yc_ready() -> bool:
    if not TRANSLATE_ENABLED:
        return False
    if not (YC_API_KEY or YC_IAM_TOKEN):
        return False
    # For Api-Key examples, folderId is required in body; enforce it to avoid silent failures.
    # If you use IAM token and it works without folderId in your setup, you can keep YC_FOLDER_ID empty,
    # but it's safer to set it.
    if YC_API_KEY and not YC_FOLDER_ID:
        return False
    return True


def _cache_get(src: str, tgt: str, text: str) -> Optional[str]:
    if not TRANSLATE_CACHE_ENABLED:
        return None
    return _translate_cache.get((src, tgt, text))


def _cache_put(src: str, tgt: str, text: str, translated: str) -> None:
    if not TRANSLATE_CACHE_ENABLED:
        return
    if len(_translate_cache) >= TRANSLATE_CACHE_MAX_ITEMS:
        # simple eviction: drop random 10%
        for _ in range(max(1, TRANSLATE_CACHE_MAX_ITEMS // 10)):
            try:
                _translate_cache.pop(next(iter(_translate_cache)))
            except Exception:
                break
    _translate_cache[(src, tgt, text)] = translated


def _chunk_texts(texts: List[str], max_chars: int) -> List[List[str]]:
    """
    Split list of texts into chunks where sum(len(text)) <= max_chars.
    """
    chunks: List[List[str]] = []
    current: List[str] = []
    total = 0

    for t in texts:
        t = t or ""
        if not t:
            current.append(t)
            continue

        if len(t) > max_chars:
            # If single text is too long, hard-split it
            start = 0
            while start < len(t):
                part = t[start:start + max_chars]
                if current:
                    chunks.append(current)
                    current = []
                    total = 0
                chunks.append([part])
                start += max_chars
            continue

        if total + len(t) > max_chars and current:
            chunks.append(current)
            current = [t]
            total = len(t)
        else:
            current.append(t)
            total += len(t)

    if current:
        chunks.append(current)

    return chunks


# ===================== TRANSLATE (Yandex Cloud) =====================
async def translate_to_ru(text: str, source_lang: str = "en") -> str:
    """
    Translate text to Russian using Yandex Cloud Translate API v2.
    If YC isn't configured or request fails, returns original text.
    """
    if not text:
        return text

    cached = _cache_get(source_lang, "ru", text)
    if cached is not None:
        return cached

    if not _yc_ready():
        # If translate not configured, just return original text without noise.
        return text

    client = await get_http_client()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_get_yc_auth_headers(),
    }

    # For best compatibility: include folderId if provided.
    body = {
        "sourceLanguageCode": source_lang,
        "targetLanguageCode": "ru",
        "texts": [text],
    }
    if YC_FOLDER_ID:
        body["folderId"] = YC_FOLDER_ID

    try:
        resp = await client.post(YC_TRANSLATE_URL, headers=headers, json=body)
        if resp.status_code == 200:
            data = resp.json()
            translations = data.get("translations") or []
            if translations:
                translated = translations[0].get("text", text)
                _cache_put(source_lang, "ru", text, translated)
                return translated
            return text

        # Non-200
        try:
            err = resp.json()
        except Exception:
            err = resp.text[:300]
        logger.warning(f"YC Translate status={resp.status_code}: {err}")
        return text

    except (socket.gaierror, OSError, httpx.ConnectError, httpx.TimeoutException) as e:
        logger.warning(f"YC Translate unavailable (network/DNS): {e}")
        return text
    except Exception as e:
        logger.error(f"YC Translate error: {e}")
        return text


async def translate_texts_to_ru(texts: List[str], source_lang: str = "en") -> List[str]:
    """
    Batch translate list of texts to Russian using Yandex Cloud Translate.
    Preserves order. Uses chunking to satisfy 10000 chars/request limit.
    """
    if not texts:
        return texts

    if not _yc_ready():
        return texts

    # Try cache first
    results: List[Optional[str]] = [None] * len(texts)
    to_translate: List[tuple[int, str]] = []
    for i, t in enumerate(texts):
        if not t:
            results[i] = t
            continue
        cached = _cache_get(source_lang, "ru", t)
        if cached is not None:
            results[i] = cached
        else:
            to_translate.append((i, t))

    if not to_translate:
        return [r if r is not None else "" for r in results]

    client = await get_http_client()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_get_yc_auth_headers(),
    }

    # Chunk by total chars
    chunks = _chunk_texts([t for _, t in to_translate], YC_MAX_CHARS_PER_REQUEST)

    cursor = 0
    try:
        for chunk in chunks:
            indices = [to_translate[cursor + j][0] for j in range(len(chunk))]
            cursor += len(chunk)

            body = {
                "sourceLanguageCode": source_lang,
                "targetLanguageCode": "ru",
                "texts": chunk,
            }
            if YC_FOLDER_ID:
                body["folderId"] = YC_FOLDER_ID

            resp = await client.post(YC_TRANSLATE_URL, headers=headers, json=body)

            if resp.status_code != 200:
                try:
                    err = resp.json()
                except Exception:
                    err = resp.text[:300]
                logger.warning(f"YC Translate status={resp.status_code}: {err}")
                # fallback: keep originals for this chunk
                for idx, orig in zip(indices, chunk):
                    results[idx] = orig
                continue

            data = resp.json()
            translations = data.get("translations") or []
            # If mismatch, fallback to originals
            if len(translations) != len(chunk):
                for idx, orig in zip(indices, chunk):
                    results[idx] = orig
                continue

            for idx, orig, tr in zip(indices, chunk, translations):
                translated = tr.get("text", orig)
                results[idx] = translated
                _cache_put(source_lang, "ru", orig, translated)

    except (socket.gaierror, OSError, httpx.ConnectError, httpx.TimeoutException) as e:
        logger.warning(f"YC Translate unavailable (network/DNS): {e}")
        # fallback originals for remaining
        for i, t in to_translate:
            if results[i] is None:
                results[i] = t
    except Exception as e:
        logger.error(f"YC Translate error: {e}")
        for i, t in to_translate:
            if results[i] is None:
                results[i] = t

    return [r if r is not None else "" for r in results]


# ===================== FUN TEXT GENERATORS =====================
ROLE_PREFIX = {
    "developer": [
        "🤖 Я ничего не трогал",
        "💻 У меня работает",
        "🧠 Это фича",
        "🚀 Сейчас быстро поправлю",
        "📦 Просто обновил зависимости",
    ],
    "tester": [
        "🔍 Баг не воспроизводится",
        "🧪 Нашёл ещё один сценарий",
        "📋 Шаги воспроизведения прилагаю",
        "🐞 Оно сломано",
        "⚠️ Проверил на проде",
    ],
    "support": [
        "📩 Передал разработчикам",
        "🙏 Уже разбираемся",
        "📞 Пользователь очень просит",
        "⏳ Исправим в следующем релизе",
        "🙂 Спасибо за обращение",
    ],
    "common": [
        "😐 Ну началось",
        "🫠 Опять это",
        "😂 Классика",
        "🙃 Всё по плану",
        "🔥 Стабильно",
    ],
}

PUNCHLINES = [
    "— это заняло 5 минут",
    "— и никто не заметит",
    "— в пятницу вечером",
    "— после одного маленького изменения",
    "— зато теперь быстрее",
    "— само починилось",
    "— а я предупреждал",
    "— не трогай, работает",
]

WORK_CONTEXT = [
    "Jira в проде",
    "после деплоя",
    "после рефакторинга",
    "когда горит дедлайн",
    "когда менеджер онлайн",
    "когда ушёл на обед",
    "когда закрыл задачу",
]


def generate_caption(role: str, meme_title: str) -> str:
    role = role if role in ROLE_PREFIX else "common"
    prefix = random.choice(ROLE_PREFIX[role])
    context = random.choice(WORK_CONTEXT)
    punch = random.choice(PUNCHLINES)
    # meme_title currently not used, but kept for future personalization
    _ = meme_title
    return f"{prefix}\n\n{context}\n{punch}"


# ===================== MEME =====================
async def get_random_meme(role: str = "common") -> Tuple[str, Optional[str]]:
    client = await get_http_client()

    try:
        resp = await client.get("https://api.imgflip.com/get_memes")
        resp.raise_for_status()

        memes = resp.json().get("data", {}).get("memes", [])
        if not memes:
            return "Сегодня без мемов — всё слишком стабильно.", None

        meme = random.choice(memes)
        caption = generate_caption(role, meme.get("name", ""))
        return caption, meme.get("url")

    except Exception as e:
        logger.error(f"Ошибка получения мема: {e}")
        return "Мемы закончились. Возможно, их зарефакторили.", None


# ===================== HOROSCOPE =====================
async def get_random_horoscope(zodiac: str) -> str:
    if not zodiac:
        return "Сначала укажи знак зодиака командой /setzodiac"

    zodiac_en = ZODIAC_MAP.get(zodiac.lower())
    if not zodiac_en:
        return "Неизвестный знак зодиака 🤔"

    today = date.today().isoformat()

    cached = _horoscope_cache.get(zodiac_en)
    if cached and cached[0] == today:
        return f"🔮 {zodiac.capitalize()}\n\n{cached[1]}"

    client = await get_http_client()

    try:
        resp = await client.get(f"https://ohmanda.com/api/horoscope/{zodiac_en}/")
        resp.raise_for_status()

        data = resp.json()
        desc_en = data.get("horoscope", "Stars are silent today.")
        desc = await translate_to_ru(desc_en, source_lang="en")

        _horoscope_cache[zodiac_en] = (today, desc)
        return f"🔮 {zodiac.capitalize()}\n\n{desc}"

    except Exception as e:
        logger.error(f"Ошибка гороскопа: {e}")
        return f"🔮 {zodiac.capitalize()}\n\nСегодня звёзды недоступны, но дедлайны — нет."


# ===================== FACT =====================
async def get_random_fact() -> str:
    client = await get_http_client()

    try:
        resp = await client.get("https://catfact.ninja/fact")
        resp.raise_for_status()

        fact_en = resp.json().get("fact", "")
        if not fact_en:
            return "Сегодня фактов нет, только баги."
        return await translate_to_ru(fact_en, source_lang="en")

    except Exception as e:
        logger.error(f"Ошибка факта: {e}")
        return "Сегодня только один факт: всё ломается внезапно."