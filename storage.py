import os
import time
import json
import copy
import threading
from typing import Optional, Dict, List

from settings import (
    SUBSCRIPTIONS_FILE,
    SESSIONS_FILE,
    QUEUE_FILE,
    RECORD_QUEUE_FILE,
    DEFAULT_CODES,
    default_settings,
    ADMIN_CHAT_ID,
)

# ---------------------------------------------------------------------------
# حافظهٔ داخلی (cache) برای جلوگیری از خواندن مکرر دیسک
# ---------------------------------------------------------------------------
subscriptions_cache: Dict = {}
sessions_cache: Dict = {}

# قفل سراسری برای همگام‌سازی دسترسی به فایل‌ها
_storage_lock = threading.Lock()


# ---------------------------------------------------------------------------
# ۱. ابزارهای عمومی خواندن / نوشتن JSON
# ---------------------------------------------------------------------------
def load_json(file_path: str, default: dict = None) -> dict:
    """
    محتوای فایل JSON را می‌خواند. در صورت نبود فایل یا خرابی داده،
    *default* (یا {} در صورت None بودن) برگردانده می‌شود.
    """
    if default is None:
        default = {}
    with _storage_lock:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default


def save_json(file_path: str, data: dict) -> None:
    """ابتدا در یک فایل موقت می‌نویسد، سپس با os.replace جایگزین می‌کند."""
    with _storage_lock:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, file_path)


# ---------------------------------------------------------------------------
# ۲. مدیریت اشتراک‌ها
# ---------------------------------------------------------------------------
def load_subscriptions() -> dict:
    """فایل اشتراک‌ها را خوانده و در cache ذخیره می‌کند."""
    global subscriptions_cache
    default_data = {
        "valid_codes": DEFAULT_CODES,
        "user_levels": {},
        "bans": {},
    }
    subscriptions_cache = load_json(SUBSCRIPTIONS_FILE, default_data)
    return subscriptions_cache


def save_subscriptions(data: dict = None) -> None:
    """داده‌های اشتراک را روی دیسک می‌نویسد و cache را به‌روز می‌کند."""
    global subscriptions_cache
    if data is None:
        data = subscriptions_cache
    else:
        subscriptions_cache = data
    save_json(SUBSCRIPTIONS_FILE, data)


def get_user_level(chat_id: int) -> str:
    """سطح اشتراک کاربر را (پیش‌فرض "free") برمی‌گرداند."""
    load_subscriptions()
    return subscriptions_cache["user_levels"].get(str(chat_id), "free")


def set_user_level(chat_id: int, level: str) -> None:
    """سطح اشتراک کاربر را تنظیم کرده و ذخیره می‌کند."""
    subscriptions_cache["user_levels"][str(chat_id)] = level
    save_subscriptions()


def activate_code(chat_id: int, code: str) -> Optional[str]:
    """
    کد فعال‌سازی را بررسی می‌کند. در صورت معتبر بودن، آن را از لیست حذف،
    سطح کاربر را ارتقا داده و نام سطح را برمی‌گرداند.
    """
    load_subscriptions()
    valid_codes = subscriptions_cache["valid_codes"]
    for level, codes in valid_codes.items():
        if code in codes:
            codes.remove(code)
            save_subscriptions()
            set_user_level(chat_id, level)
            return level
    return None


def ban_user(chat_id: int, duration_minutes: Optional[int] = None) -> None:
    """
    کاربر را بن می‌کند. اگر *duration_minutes* None باشد، بن دائمی.
    در غیر این صورت زمان پایان بن محاسبه می‌شود.
    """
    load_subscriptions()
    key = str(chat_id)
    if duration_minutes is None:
        subscriptions_cache["bans"][key] = "forever"
    else:
        subscriptions_cache["bans"][key] = time.time() + duration_minutes * 60
    save_subscriptions()


def unban_user(chat_id: int) -> bool:
    """کاربر را از لیست بن حذف می‌کند. در صورت وجود قبلی True، وگرنه False."""
    load_subscriptions()
    key = str(chat_id)
    if key in subscriptions_cache["bans"]:
        del subscriptions_cache["bans"][key]
        save_subscriptions()
        return True
    return False


def is_banned(chat_id: int) -> bool:
    """
    بررسی می‌کند که آیا کاربر در حال حاضر بن شده است یا خیر.
    بن‌های زمان‌دار منقضی شده را به صورت خودکار پاک می‌کند.
    """
    load_subscriptions()
    key = str(chat_id)
    ban_info = subscriptions_cache["bans"].get(key)
    if ban_info is None:
        return False
    if ban_info == "forever":
        return True
    if time.time() > ban_info:
        del subscriptions_cache["bans"][key]
        save_subscriptions()
        return False
    return True


# ---------------------------------------------------------------------------
# ۳. مدیریت نشست‌ها
# ---------------------------------------------------------------------------
def load_sessions() -> dict:
    """فایل نشست‌ها را خوانده و در cache ذخیره می‌کند."""
    global sessions_cache
    sessions_cache = load_json(SESSIONS_FILE, {})
    return sessions_cache


def save_sessions(data: dict = None) -> None:
    """نشست‌ها را روی دیسک ذخیره و cache را به‌روز می‌کند."""
    global sessions_cache
    if data is None:
        data = sessions_cache
    else:
        sessions_cache = data
    save_json(SESSIONS_FILE, data)


def get_session(chat_id: int) -> dict:
    """
    نشست کاربر را برمی‌گرداند. اگر وجود نداشت، یک نشست پیش‌فرض
    با تنظیمات اولیه ساخته و ذخیره می‌کند.
    """
    load_sessions()
    key = str(chat_id)
    if key in sessions_cache:
        return sessions_cache[key]

    # ساختن نشست جدید
    is_admin = (chat_id == ADMIN_CHAT_ID)
    subscription = "pro" if is_admin else get_user_level(chat_id)
    new_session = {
        "chat_id": chat_id,
        "state": "idle",
        "is_admin": is_admin,
        "subscription": subscription,
        "settings": copy.deepcopy(default_settings),
        "click_counter": 0,
        "browser_links": None,
        "browser_url": None,
        "browser_page": 0,
        "text_links": {},
        "ad_blocked_domains": [],
        "found_downloads": None,
        "found_downloads_page": 0,
        "interactive_elements": None,
        "usage": {},
    }
    sessions_cache[key] = new_session
    save_sessions()
    return new_session


def set_session(chat_id: int, session_data: dict) -> None:
    """نشست کاربر را جایگزین (یا اضافه) کرده و ذخیره می‌کند."""
    sessions_cache[str(chat_id)] = session_data
    save_sessions()


# ---------------------------------------------------------------------------
# ۴. مدیریت صف‌ها (عمومی و ضبط)
# ---------------------------------------------------------------------------
def load_queue(file_path: str) -> List[dict]:
    """صف (لیست jobs) را از فایل JSON می‌خواند."""
    data = load_json(file_path, {"items": []})
    return data.get("items", [])


def save_queue(file_path: str, data: List[dict]) -> None:
    """لیست jobs را در قالب یک دیکشنری ذخیره می‌کند."""
    save_json(file_path, {"items": data})


def enqueue_job(file_path: str, job: dict) -> None:
    """یک job جدید به انتهای صف اضافه می‌کند."""
    queue = load_queue(file_path)
    queue.append(job)
    save_queue(file_path, queue)


def pop_queued(file_path: str) -> Optional[dict]:
    """
    اولین job با وضعیت "queued" را یافته، وضعیت آن را به "running" تغییر
    داده، timestamp ها را به‌روز کرده و برمی‌گرداند. در صورت نبود، None.
    """
    queue = load_queue(file_path)
    now = time.time()
    for job in queue:
        if job.get("status") == "queued":
            job["status"] = "running"
            job["started_at"] = now
            job["updated_at"] = now
            save_queue(file_path, queue)
            return job
    return None


def update_job(file_path: str, job: dict) -> None:
    """
    job با job_id یکسان را جایگزین کرده، یا در صورت نبود به انتها اضافه می‌کند.
    """
    queue = load_queue(file_path)
    job_id = job.get("job_id")
    for i, existing in enumerate(queue):
        if existing.get("job_id") == job_id:
            queue[i] = job
            save_queue(file_path, queue)
            return
    # نبود
    queue.append(job)
    save_queue(file_path, queue)


def find_job(file_path: str, job_id: str) -> Optional[dict]:
    """اولین job با job_id داده شده را برمی‌گرداند."""
    queue = load_queue(file_path)
    for job in queue:
        if job.get("job_id") == job_id:
            return job
    return None
