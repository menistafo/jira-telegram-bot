# utils.py
import pytz
from datetime import datetime, time, timedelta
import logging

logger = logging.getLogger(__name__)

def is_night_time_msk(now_utc: datetime = None) -> bool:
    try:
        if now_utc is None:
            now_utc = datetime.utcnow()
        
        msk_tz = pytz.timezone('Europe/Moscow')
        now_msk = now_utc.replace(tzinfo=pytz.utc).astimezone(msk_tz)
        msk_hour = now_msk.hour
        
        if msk_hour >= 22 or msk_hour < 9:
            logger.debug(f"🌙 Ночное время по МСК: {now_msk.strftime('%H:%M')}")
            return True
        
        logger.debug(f"☀️ Дневное время по МСК: {now_msk.strftime('%H:%M')}")
        return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка при определении времени МСК: {e}")
        return False

def get_msk_time() -> str:
    try:
        msk_tz = pytz.timezone('Europe/Moscow')
        now_msk = datetime.now(pytz.utc).astimezone(msk_tz)
        return now_msk.strftime("%Y-%m-%d %H:%M:%S (MSK)")
    except:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S (UTC)")

def get_msk_time_obj() -> datetime:
    """Возвращает текущее время по МСК как naive datetime (без tzinfo)."""
    msk_tz = pytz.timezone('Europe/Moscow')
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    now_msk = now_utc.astimezone(msk_tz)
    return now_msk.replace(tzinfo=None)

def should_send_reminder_check() -> bool:
    from .config import NIGHT_MODE_ENABLED
    
    if not NIGHT_MODE_ENABLED:
        return True
    
    return not is_night_time_msk()

def format_time_until_morning() -> str:
    try:
        msk_tz = pytz.timezone('Europe/Moscow')
        now_utc = datetime.now(pytz.utc)
        now_msk = now_utc.astimezone(msk_tz)
        
        morning_time = now_msk.replace(hour=9, minute=0, second=0, microsecond=0)
        
        if now_msk.hour < 9:
            morning_time = now_msk.replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            morning_time = (now_msk + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        
        time_diff = morning_time - now_msk
        hours = time_diff.seconds // 3600
        minutes = (time_diff.seconds % 3600) // 60
        
        return f"{hours}ч {minutes}м"
        
    except Exception as e:
        logger.error(f"❌ Ошибка расчета времени до утра: {e}")
        return "неизвестно"

def get_next_reminder_time(last_notification_time: datetime) -> str:
    try:
        next_time = last_notification_time + timedelta(minutes=30)
        msk_tz = pytz.timezone('Europe/Moscow')
        next_time_msk = next_time.replace(tzinfo=pytz.utc).astimezone(msk_tz)
        return next_time_msk.strftime("%H:%M (МСК)")
    except Exception as e:
        logger.error(f"❌ Ошибка расчета времени следующего напоминания: {e}")
        return "неизвестно"