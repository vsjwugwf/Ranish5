import threading
import time
import requests
import json
import os
from typing import List, Dict, Optional

from settings import (
    WORKER_COUNT,
    RECORD_WORKER_ID,
    QUEUE_FILE,
    RECORD_QUEUE_FILE,
    API_BASE,
    REQUEST_TIMEOUT,
)
from utils import safe_log
import storage

# ---------------------------------------------------------------------------
# نگاشت mode به تابع پردازش‌گر (توسط main.py مقداردهی می‌شود)
# ---------------------------------------------------------------------------
_job_handlers: Dict[str, callable] = {}


def register_job_handlers(handlers: Dict[str, callable]) -> None:
    """
    بعد از بارگذاری jobs.py، main.py با صدا زدن این تابع نگاشت نهایی را
    ثبت می‌کند. کلیدها نام mode و مقدارها تابع همگام پردازش‌گر هستند.
    مثال:
        {
            "browser": process_browser_job,
            "screenshot": process_screenshot_job,
            "record_video": process_record_job,
            ...
        }
    """
    _job_handlers.clear()
    _job_handlers.update(handlers)


# ---------------------------------------------------------------------------
# تابع لاگ محلی
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """پیام را با قالب استاندارد لاگ می‌کند."""
    safe_log(f"[Worker] {msg}")


# ---------------------------------------------------------------------------
# توابع کمکی ارتباط با API بله
# ---------------------------------------------------------------------------
def send_message(chat_id: int, text: str, reply_markup=None) -> Optional[dict]:
    """ارسال پیام متنی به کاربر."""
    url = f"{API_BASE}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log(f"sendMessage failed: {e}")
        return None


def send_document(chat_id: int, file_path: str, caption: str = "") -> Optional[dict]:
    """ارسال فایل (سند) به کاربر."""
    url = f"{API_BASE}/sendDocument"
    if not os.path.isfile(file_path):
        log(f"sendDocument: file not found {file_path}")
        return None
    try:
        with open(file_path, "rb") as f:
            file_content = f.read()
        filename = os.path.basename(file_path)
        files = {"document": (filename, file_content)}
        data = {"chat_id": chat_id, "caption": caption}
        resp = requests.post(url, data=data, files=files, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log(f"sendDocument failed: {e}")
        return None


def answer_callback_query(cq_id: str, text: str = "", show_alert: bool = False) -> Optional[dict]:
    """پاسخ به callback query (دکمه‌های inline)."""
    url = f"{API_BASE}/answerCallbackQuery"
    payload = {
        "callback_query_id": cq_id,
        "text": text,
        "show_alert": show_alert,
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log(f"answerCallbackQuery failed: {e}")
        return None


# ---------------------------------------------------------------------------
# حلقهٔ اصلی کارگر
# ---------------------------------------------------------------------------
def worker_loop(worker_id: int, stop_event: threading.Event) -> None:
    """
    نخ کارگر: به‌طور مداوم صف را بررسی می‌کند و کارهای موجود را به
    تابع مناسب (از _job_handlers) می‌سپارد.
    """
    log(f"Worker {worker_id} started.")

    while not stop_event.is_set():
        time.sleep(1)  # جلوگیری از مصرف بی‌رویهٔ CPU

        # انتخاب صف بر اساس نوع کارگر
        if worker_id == RECORD_WORKER_ID:
            queue_file = RECORD_QUEUE_FILE
            # در صف ضبط فقط کارهای "record_video" وجود دارند
            default_mode = "record_video"
        else:
            queue_file = QUEUE_FILE
            default_mode = None  # باید از خود job.mode خوانده شود

        job = storage.pop_queued(queue_file)
        if job is None:
            continue

        # تشخیص تابع پردازش‌گر
        mode = job.get("mode", default_mode)
        if mode is None:
            log(f"Worker {worker_id}: job بدون 'mode' رد شد (id={job.get('job_id')})")
            continue

        handler = _job_handlers.get(mode)
        if handler is None:
            log(f"Worker {worker_id}: هیچ handler‌ای برای mode '{mode}' ثبت نشده است.")
            continue

        # اجرای کار
        try:
            handler(job)
        except Exception as e:
            log(f"Worker {worker_id}: خطا در پردازش job {job.get('job_id')}: {e}")

    log(f"Worker {worker_id} stopped.")


# ---------------------------------------------------------------------------
# راه‌اندازی همهٔ کارگرها
# ---------------------------------------------------------------------------
def start_workers(stop_event: threading.Event) -> List[threading.Thread]:
    """
    همهٔ نخ‌های کارگر را ساخته و شروع می‌کند.
    نخ‌ها به صورت daemon تنظیم می‌شوند تا با خاتمهٔ برنامهٔ اصلی،
    خودکار متوقف شوند.
    """
    threads = []
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker_loop, args=(i, stop_event), daemon=True)
        t.start()
        threads.append(t)
    return threads
