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
from typing import Optional, Tuple, Dict, Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

# ===================== Backward compatibility =====================

# Раньше использовалось для генерации подписей/ролей. Оставляем, чтобы не падали импорты.
ROLE_PREFIX = {
    "common": "",
    "dev": "👨‍💻 ",
    "manager": "📊 ",
    "boss": "👑 ",
}

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


async def _fetch(
    url: str,
    params: Optional[dict] = None,
    timeout: float = 15.0,
    retries: int = 3,
) -> Optional[str]:
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
            wait = (2**attempt) + (0.1 * attempt)
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


async def _fetch_json(
    url: str,
    params: Optional[dict] = None,
    timeout: float = 15.0,
    retries: int = 3,
) -> Optional[dict]:
    client = await get_http_client()
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = (2**attempt) + (0.1 * attempt)
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
FUN_FACT_SOURCE = os.getenv("FUN_FACT_SOURCE", "ru").lower()  # ru | en
FUN_HORO_SOURCE = os.getenv("FUN_HORO_SOURCE", "ru").lower()  # ru | en

# Переводчик (опционально; если включишь EN источники)
TRANSLATE_ENABLED = os.getenv("TRANSLATE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Yandex Cloud Translate (опционально)
YC_API_KEY = (os.getenv("YC_API_KEY") or "").strip()
YC_IAM_TOKEN = (os.getenv("YC_IAM_TOKEN") or "").strip()
YC_FOLDER_ID = (os.getenv("YC_FOLDER_ID") or "").strip()
YC_TRANSLATE_URL = os.getenv(
    "YC_TRANSLATE_URL",
    "https://translate.api.cloud.yandex.net/translate/v2/translate",
).strip()

_translate_cache: "OrderedDict[tuple[str, str, str], str]" = OrderedDict()
TRANSLATE_CACHE_ENABLED = os.getenv("TRANSLATE_CACHE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
TRANSLATE_CACHE_MAX_ITEMS = int(os.getenv("TRANSLATE_CACHE_MAX_ITEMS", "2000"))

# horoscope cache: key -> (date_iso, text)
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

_RE_SCRIPT = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_RE_STYLE = re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\r\f\v]+")


def _html_to_text(s: str) -> str:
    """
    Простой html->text без внешних библиотек:
    - выкидываем script/style
    - превращаем <br>, </p>, </div> и т.п. в переводы строк
    - удаляем теги
    - html-unescape
    """
    if not s:
        return ""

    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_STYLE.sub(" ", s)

    # переносы строк для типовых блочных/разрывных тегов
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?i)</div\s*>", "\n", s)
    s = re.sub(r"(?i)</li\s*>", "\n", s)

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
    # При Api-Key обычно нужен folderId
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


# ===================== MEMES (картинка с РУССКИМ текстом на изображении) =====================

# По умолчанию сначала делаем RU-текст НА КАРТИНКЕ (memegen),
# дальше идут запасные источники (могут давать англ. текст на картинке).
FUN_MEME_PROVIDERS = [
    p.strip().lower()
    for p in os.getenv("FUN_MEME_PROVIDERS", "memegen,apileague,memeapi,imgflip").split(",")
    if p.strip()
]

APILEAGUE_API_KEY = (os.getenv("APILEAGUE_API_KEY") or "").strip()

# Безопасность/качество
FUN_MEME_SAFE_MODE = os.getenv("FUN_MEME_SAFE_MODE", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
FUN_MEME_TITLE_MAX = int(os.getenv("FUN_MEME_TITLE_MAX", "220"))
FUN_MEME_TIMEOUT = float(os.getenv("FUN_MEME_TIMEOUT", "8.0"))

# Чтобы не слать один и тот же мем подряд
_meme_seen: "OrderedDict[str, str]" = OrderedDict()
MEME_SEEN_MAX = int(os.getenv("MEME_SEEN_MAX", "50"))

# Пулы русских мем-фраз (верх/низ). Можно расширять.
RU_MEME_LINES = [
    ("Я: сейчас быстро поправлю", "Прод: а давай ещё одну правку"),
    ("Пишу «маленький фикс»", "Потом 3 часа деплою"),
    ("Всё работает на моей машине", "Значит, проблема у Вселенной"),
    ("Сделал рефакторинг", "Сломал то, что не трогал"),
    ("Дедлайн завтра", "Паника сегодня"),
    ("Стендап через 5 минут", "Я впервые вижу этот тикет"),
    ("Тесты зелёные", "Но почему-то прод горит"),
    ("Баг «не воспроизводится»", "Пользователь: «у меня воспроизводится»"),
    ("Сейчас быстро зарелижу", "CI: держи 17 ошибок"),
    ("ПМ: это на 5 минут", "Я: это на 2 дня"),
]

# Шаблоны memegen.link (обеспечивают текст на картинке).
MEMEGEN_TEMPLATES = [
    "buzz",
    "drake",
    "distractedbf",
    "two_buttons",
    "success",
    "sad-biden",
    "fry",
    "doge",
]


def _clip_caption(s: str, max_len: int = 220) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)].rstrip() + "…"


def _remember_meme(url: str) -> None:
    if not url:
        return
    _meme_seen[url] = datetime.utcnow().isoformat()
    while len(_meme_seen) > MEME_SEEN_MAX:
        try:
            _meme_seen.pop(next(iter(_meme_seen)))
        except Exception:
            break


def _already_seen(url: str) -> bool:
    return bool(url) and url in _meme_seen


def _memegen_escape(text: str) -> str:
    # memegen принимает текст в URL. Кодируем UTF-8.
    # Пустые строки — "_" (у memegen это “пусто”).
    t = (text or "").strip()
    if not t:
        return "_"
    return quote(t, safe="")


async def _url_seems_reachable(url: str) -> bool:
    """
    Быстрая проверка: удаётся ли достучаться до URL (учитывая возможные блокировки/таймауты).
    HEAD иногда режут, поэтому fallback на GET с Range.
    """
    if not url:
        return False

    client = await get_http_client()

    try:
        r = await client.head(url, timeout=FUN_MEME_TIMEOUT)
        if 200 <= r.status_code < 400:
            return True
    except Exception:
        pass

    try:
        r = await client.get(
            url,
            headers={"Range": "bytes=0-0"},
            timeout=FUN_MEME_TIMEOUT,
        )
        if 200 <= r.status_code < 400:
            return True
    except Exception:
        return False

    return False


async def _meme_from_memegen_ru() -> Optional[Tuple[str, str]]:
    """
    Гарантирует РУССКИЙ текст НА КАРТИНКЕ через memegen.link (URL-based rendering).
    """
    top, bottom = random.choice(RU_MEME_LINES)
    template = random.choice(MEMEGEN_TEMPLATES)

    top_e = _memegen_escape(top)
    bottom_e = _memegen_escape(bottom)

    # png — самый совместимый вариант для Telegram sendPhoto
    img_url = f"https://api.memegen.link/images/{template}/{top_e}/{bottom_e}.png"
    caption = "🤣 Мем"
    return caption, img_url


async def _meme_from_apileague() -> Optional[Tuple[str, str]]:
    """
    API League Random Meme API.
    НЕ гарантирует русский текст на картинке (это фоллбек).
    """
    if not APILEAGUE_API_KEY:
        return None

    url = os.getenv("FUN_APILEAGUE_URL", "https://api.apileague.com/retrieve-random-meme")
    params = {"api-key": APILEAGUE_API_KEY}

    data = await _fetch_json(url, params=params, timeout=FUN_MEME_TIMEOUT)
    if not data:
        return None

    img = (data.get("url") or data.get("image") or data.get("imageUrl") or "").strip()
    title = (data.get("title") or data.get("caption") or data.get("name") or "").strip()

    if FUN_MEME_SAFE_MODE:
        nsfw = data.get("nsfw")
        if isinstance(nsfw, bool) and nsfw:
            return None

    if not img:
        return None

    title = _clip_caption(title, FUN_MEME_TITLE_MAX) or "🖼️ Мем дня"
    return title, img


async def _meme_from_memeapi() -> Optional[Tuple[str, str]]:
    """
    meme-api.com (reddit-агрегатор).
    НЕ гарантирует русский текст на картинке (это фоллбек).
    """
    endpoint = os.getenv("FUN_MEMEAPI_URL", "https://meme-api.com/gimme/wholesomememes")
    data = await _fetch_json(endpoint, timeout=FUN_MEME_TIMEOUT)
    if not data:
        return None

    img = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()

    if FUN_MEME_SAFE_MODE:
        if data.get("nsfw") is True or data.get("spoiler") is True:
            return None

    if not img:
        return None

    title = _clip_caption(title, FUN_MEME_TITLE_MAX) or "🖼️ Мем дня"
    return title, img


async def _meme_from_imgflip_templates() -> Optional[Tuple[str, str]]:
    """
    Фоллбек: Imgflip get_memes — это скорее шаблоны, но хотя бы картинка + название.
    НЕ гарантирует русский текст на картинке (это фоллбек).
    """
    data = await _fetch_json("https://api.imgflip.com/get_memes", timeout=FUN_MEME_TIMEOUT)
    memes = (data or {}).get("data", {}).get("memes", []) if data else []
    if not memes:
        return None

    for _ in range(10):
        meme = random.choice(memes)
        img = (meme.get("url") or "").strip()
        title = (meme.get("name") or "🖼️ Мем").strip()
        if not img or _already_seen(img):
            continue
        return _clip_caption(title, FUN_MEME_TITLE_MAX), img

    meme = random.choice(memes)
    img = (meme.get("url") or "").strip()
    title = (meme.get("name") or "🖼️ Мем").strip()
    if not img:
        return None
    return _clip_caption(title, FUN_MEME_TITLE_MAX), img


async def get_random_meme(role: str = "common") -> Tuple[str, Optional[str]]:
    """
    Возвращает (caption, image_url).

    ВАЖНО:
    - Если FUN_MEME_PROVIDERS начинается с "memegen", то мемы будут с РУССКИМ текстом НА КАРТИНКЕ.
    - Остальные провайдеры — фоллбеки, язык на картинке не гарантируют.
    """
    _ = role  # роль оставлена для совместимости
    providers = FUN_MEME_PROVIDERS or ["memegen", "apileague", "memeapi", "imgflip"]

    for prov in providers:
        try:
            if prov == "memegen":
                item = await _meme_from_memegen_ru()
            elif prov == "apileague":
                item = await _meme_from_apileague()
            elif prov == "memeapi":
                item = await _meme_from_memeapi()
            elif prov == "imgflip":
                item = await _meme_from_imgflip_templates()
            else:
                continue

            if not item:
                continue

            caption, img_url = item
            if not img_url or _already_seen(img_url):
                continue

            if not await _url_seems_reachable(img_url):
                logger.warning("Meme image URL not reachable (maybe blocked): %s", img_url)
                continue

            _remember_meme(img_url)
            # caption для телеги оставляем коротким (сам текст — на картинке)
            return _clip_caption(caption, FUN_MEME_TITLE_MAX) or "🤣 Мем", img_url

        except Exception as e:
            logger.warning("Meme provider failed (%s): %s", prov, e)

    return "Сегодня без мемов — всё слишком стабильно.", None


# ===================== HOROSCOPE =====================

_DATE_RANGE_RE = re.compile(
    r"\b\d{1,2}\s+[а-яё]+\s*-\s*\d{1,2}\s+[а-яё]+\b",
    re.IGNORECASE,
)


def _parse_mailru_horoscope(text: str, sign_ru: str) -> str:
    """
    Достаём прогноз именно для sign_ru (например "Дева"), а не список всех знаков (меню).
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

        if _DATE_RANGE_RE.search(l):
            continue
        if l.lower() in ZODIAC_MAP.keys():
            continue
        if len(l) < 40:
            continue

        if "." in l or "!" in l or "?" in l:
            start = i
            break

    if start is None:
        return ""

    # 3) Конец — перед "Финансы/Здоровье/Любовь"
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().lower() in ("финансы", "здоровье", "любовь"):
            end = i
            break

    forecast_lines = lines[start:end]

    cleaned = [x for x in forecast_lines if len(x) >= 25]
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
