# bot/fun.py
import os
import re
import socket
import httpx
import logging
import random
import asyncio
import html as html_lib
from collections import OrderedDict
from datetime import date, datetime
from typing import Optional, Tuple, List, Dict, Any

logger = logging.getLogger(__name__)

# ===================== HTTP helper with retry =====================

_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            headers={
                "User-Agent": os.getenv(
                    "FUN_UA",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
                )
            },
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


async def _fetch(url: str, params: Optional[dict] = None, timeout: float = 15.0, retries: int = 3) -> Optional[str]:
    """
    Единая точка для вызовов внешних HTTP.
    """
    client = await get_http_client()
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            wait = (2 ** attempt) + (0.1 * attempt)
            logger.warning(
                "External call failed (%s), retry %s/%s in %.1fs: %s",
                url,
                attempt + 1,
                retries,
                wait,
                e,
            )
            if attempt < retries - 1:
                await asyncio.sleep(wait)
            else:
                return None
    return None


async def _fetch_json(url: str, params: Optional[dict] = None, timeout: float = 15.0, retries: int = 3) -> Optional[dict]:
    client = await get_http_client()
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = (2 ** attempt) + (0.1 * attempt)
            logger.warning(
                "External API call failed (%s), retry %s/%s in %.1fs: %s",
                url,
                attempt + 1,
                retries,
                wait,
                e,
            )
            if attempt < retries - 1:
                await asyncio.sleep(wait)
            else:
                return None
    return None


# ===================== SETTINGS =====================
# По умолчанию RU-источники -> без переводчика
FUN_FACT_SOURCE = os.getenv("FUN_FACT_SOURCE", "ru").lower()         # ru | en
FUN_HORO_SOURCE = os.getenv("FUN_HORO_SOURCE", "ru").lower()         # ru | en

# Переводчик (опционально; если включишь EN источники)
TRANSLATE_ENABLED = os.getenv("TRANSLATE_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# Yandex Cloud Translate (опционально)
YC_API_KEY = (os.getenv("YC_API_KEY") or "").strip()
YC_IAM_TOKEN = (os.getenv("YC_IAM_TOKEN") or "").strip()
YC_FOLDER_ID = (os.getenv("YC_FOLDER_ID") or "").strip()
YC_TRANSLATE_URL = os.getenv("YC_TRANSLATE_URL", "https://translate.api.cloud.yandex.net/translate/v2/translate").strip()

_translate_cache: "OrderedDict[tuple[str, str, str], str]" = OrderedDict()
TRANSLATE_CACHE_ENABLED = os.getenv("TRANSLATE_CACHE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
TRANSLATE_CACHE_MAX_ITEMS = int(os.getenv("TRANSLATE_CACHE_MAX_ITEMS", "2000"))

# cache: key -> (date_iso, text)
_horoscope_cache: Dict[str, Tuple[str, str]] = {}

# ===================== ZODIAC MAP =====================
# Под RU-гороскоп Mail.ru нужны EN-ключи (aries, taurus, ...)
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


# ===================== TEXT HELPERS =====================

_RE_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_RE_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\r\f\v]+")


def _html_to_text(s: str) -> str:
    """
    Простой html->text без внешних библиотек:
    - выкидываем script/style
    - превращаем </p>, <br> и т.п. в переводы строк
    - удаляем теги
    - html-unescape
    """
    if not s:
        return ""
    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_STYLE.sub(" ", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</div\s*>", "\n", s)
    s = _RE_TAGS.sub(" ", s)
    s = html_lib.unescape(s)
    s = s.replace("\u00a0", " ")

    lines = []
    for line in s.split("\n"):
        line = _RE_WS.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


# ===================== TRANSLATE (optional) =====================

def _get_yc_auth_headers() -> dict:
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
        for _ in range(max(1, TRANSLATE_CACHE_MAX_ITEMS // 10)):
            try:
                _translate_cache.pop(next(iter(_translate_cache)))
            except Exception:
                break
    _translate_cache[(src, tgt, text)] = translated


async def translate_to_ru(text: str, source_lang: str = "en") -> str:
    if not text:
        return text
    cached = _cache_get(source_lang, "ru", text)
    if cached is not None:
        return cached
    if not _yc_ready():
        return text

    client = await get_http_client()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_get_yc_auth_headers(),
    }
    body: Dict[str, Any] = {
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
    except (socket.gaierror, OSError, httpx.ConnectError, httpx.TimeoutException) as e:
        logger.warning(f"YC Translate unavailable (network/DNS): {e}")
        return text
    except Exception as e:
        logger.error(f"YC Translate error: {e}")
        return text


# ===================== MEME CAPTION (если используешь) =====================

ROLE_PREFIX = {
    "developer": [
        "🤷 Я ничего не трогал",
        "✅ У меня работает",
        "✨ Это фича",
        "🛠 Сейчас быстро поправлю",
        "📦 Просто обновил зависимости",
    ],
    "tester": [
        "🐛 Баг не воспроизводится",
        "🔎 Нашёл ещё один сценарий",
        "🧾 Шаги воспроизведения прилагаю",
        "💥 Оно сломано",
        "⚠️ Проверил на проде",
    ],
    "support": [
        "📨 Передал разработчикам",
        "🧯 Уже разбираемся",
        "🙏 Пользователь очень просит",
        "⏳ Исправим в следующем релизе",
        "💬 Спасибо за обращение",
    ],
    "common": [
        "😑 Ну началось",
        "🙃 Опять это",
        "📌 Классика",
        "🧩 Всё по плану",
        "🧱 Стабильно",
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
    "когда открыл базу в DBeaver",
]


def generate_caption(role: str, meme_title: str) -> str:
    role = role if role in ROLE_PREFIX else "common"
    prefix = random.choice(ROLE_PREFIX[role])
    context = random.choice(WORK_CONTEXT)
    punch = random.choice(PUNCHLINES)
    _ = meme_title
    return f"{prefix}\n\n{context}\n{punch}"


async def get_random_meme(role: str = "common") -> Tuple[str, Optional[str]]:
    try:
        data = await _fetch_json("https://api.imgflip.com/get_memes")
        memes = (data or {}).get("data", {}).get("memes", []) if data else []
        if not memes:
            return "Сегодня без мемов — всё слишком стабильно.", None
        meme = random.choice(memes)
        caption = generate_caption(role, meme.get("name", ""))
        return caption, meme.get("url")
    except Exception as e:
        logger.error(f"Ошибка получения мема: {e}")
        return "Мемы закончились. Возможно, их зарефакторили.", None


# ===================== HOROSCOPE =====================

_DATE_RANGE_RE = re.compile(r"\b\d{1,2}\s+[а-яё]+\s*-\s*\d{1,2}\s+[а-яё]+\b", re.IGNORECASE)


def _parse_mailru_horoscope(text: str, sign_ru: str) -> str:
    """
    Достаём прогноз именно для sign_ru (например "Дева"),
    а не список всех знаков (меню).
    """
    if not text:
        return ""

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # 1) Ищем строку "Дева" и СЛЕДОМ диапазон дат
    sign_idx = -1
    for i, line in enumerate(lines):
        if line.lower() == sign_ru.lower():
            if i + 1 < len(lines) and _DATE_RANGE_RE.search(lines[i + 1]):
                sign_idx = i
                break

    # fallback: иногда знак может встретиться как часть заголовка
    if sign_idx == -1:
        for i, line in enumerate(lines):
            if sign_ru.lower() in line.lower() and "гороскоп" in line.lower():
                sign_idx = i
                break

    if sign_idx == -1:
        return ""

    # 2) Старт прогноза: первая “длинная” строка после блока с датами/меню
    start = None
    for i in range(sign_idx + 1, len(lines)):
        l = lines[i]

        # пропускаем диапазон дат
        if _DATE_RANGE_RE.search(l):
            continue

        # пропускаем названия знаков (меню)
        if l.lower() in ZODIAC_MAP.keys():
            continue

        # иногда попадаются короткие подписи "Сегодня", "Общий" и т.п.
        if len(l) < 40:
            continue

        # прогноз обычно похож на текст с точками/запятыми
        if "." in l or "!" in l or "?" in l:
            start = i
            break

    if start is None:
        return ""

    # 3) Конец — перед "Финансы/Здоровье/Любовь" (встречается почти всегда)
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().lower() in ("финансы", "здоровье", "любовь"):
            end = i
            break

    forecast_lines = lines[start:end]

    # выкинем случайные мусорные короткие строки
    cleaned = [x for x in forecast_lines if len(x) >= 25]

    # обычно прогноз — 1–3 абзаца; ограничим, чтобы не простыня
    cleaned = cleaned[:3]
    return "\n\n".join(cleaned).strip()


async def get_random_horoscope(zodiac: str) -> str:
    if not zodiac:
        return "Сначала укажи знак зодиака командой /setzodiac"

    zodiac_ru = zodiac.lower().strip()
    zodiac_en = ZODIAC_MAP.get(zodiac_ru)
    if not zodiac_en:
        return "Неизвестный знак зодиака"

    today = date.today().isoformat()
    cache_key = f"{FUN_HORO_SOURCE}:{zodiac_en}:today"
    cached = _horoscope_cache.get(cache_key)
    if cached and cached[0] == today:
        return f"✨ {zodiac.capitalize()}\n\n{cached[1]}"

    try:
        # RU источник (без перевода)
        if FUN_HORO_SOURCE == "ru":
            url = f"https://horo.mail.ru/prediction/{zodiac_en}/today/"
            raw = await _fetch(url)
            if not raw:
                raise RuntimeError("empty response")
            txt = _html_to_text(raw)
            desc = _parse_mailru_horoscope(txt, zodiac.capitalize())
            if not desc:
                raise RuntimeError("parse failed")
            _horoscope_cache[cache_key] = (today, desc)
            return f"✨ {zodiac.capitalize()}\n\n{desc}"

        # EN источник + опциональный перевод
        data = await _fetch_json(f"https://ohmanda.com/api/horoscope/{zodiac_en}/")
        desc_en = (data or {}).get("horoscope", "Stars are silent today.")
        desc = await translate_to_ru(desc_en, source_lang="en") if TRANSLATE_ENABLED else desc_en
        _horoscope_cache[cache_key] = (today, desc)
        return f"✨ {zodiac.capitalize()}\n\n{desc}"

    except Exception as e:
        logger.error(f"Ошибка гороскопа: {e}")
        return f"✨ {zodiac.capitalize()}\n\nСегодня звёзды недоступны, но дедлайны — нет."


# ===================== FACT =====================

def _parse_randstuff_fact(text: str) -> str:
    if not text:
        return ""

    m = re.search(r"(?:^|\n)Факт:\s*(.+)", text, flags=re.IGNORECASE)
    if m:
        tail = m.group(1).strip()
        if tail:
            return tail

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if line.lower() in ("факт:", "# факт:", "факт"):
            if i + 1 < len(lines):
                return lines[i + 1]
    return ""


async def get_random_fact() -> str:
    try:
        # RU источник (без перевода)
        if FUN_FACT_SOURCE == "ru":
            raw = await _fetch("https://randstuff.ru/fact/")
            if not raw:
                raise RuntimeError("empty response")
            txt = _html_to_text(raw)
            fact = _parse_randstuff_fact(txt)
            if not fact:
                raise RuntimeError("parse failed")
            return fact

        # EN источник + опциональный перевод
        data = await _fetch_json("https://catfact.ninja/fact")
        fact_en = (data or {}).get("fact", "")
        if not fact_en:
            return "Сегодня фактов нет, только баги."
        return await translate_to_ru(fact_en, source_lang="en") if TRANSLATE_ENABLED else fact_en

    except Exception as e:
        logger.error(f"Ошибка факта: {e}")
        return "Сегодня только один факт: всё ломается внезапно."
