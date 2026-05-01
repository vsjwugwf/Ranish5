#!/usr/bin/env python3
# main.py – ورودی اصلی ربات بله (Runsh)
# تمام مکالمات و تعاملات اینجا مدیریت می‌شود

import os
import time
import json
import threading
import uuid
import hashlib
import math
import subprocess
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse, urljoin

import requests

# پروژه
from settings import *          # تمام ثابت‌ها
from utils import *              # توابع کمکی (safe_log, split_file_binary, is_valid_url, …)
import storage
import worker
import jobs
import crawler

# ---------------------------------------------------------------------------
# راه‌اندازی اولیه
# ---------------------------------------------------------------------------
stop_event = threading.Event()
os.makedirs("jobs", exist_ok=True)
os.makedirs("data", exist_ok=True)

# ---------------------------------------------------------------------------
# ابزار ویرایش پیام (چون worker.py ندارد)
# ---------------------------------------------------------------------------
def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup=None) -> Optional[dict]:
    url = f"{API_BASE}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        return resp.json()
    except Exception as e:
        safe_log(f"editMessageText failed: {e}")
        return None

# ---------------------------------------------------------------------------
# توابع کمکی برای ترجمهٔ مقادیر به فارسی
# ---------------------------------------------------------------------------
def translate_dlmode(mode: str) -> str:
    m = {"store": "ذخیره", "stream": "جریانی", "adm": "چندبخشی"}
    return m.get(mode, mode)

def translate_browser_mode(mode: str) -> str:
    m = {"text": "متنی", "media": "مدیا", "explorer": "کاوشگر"}
    return m.get(mode, mode)

def translate_record_behavior(beh: str) -> str:
    m = {"click": "کلیک", "scroll": "اسکرول", "live": "زنده"}
    return m.get(beh, beh)

# ---------------------------------------------------------------------------
# ساخت منوها (inline_keyboard)
# ---------------------------------------------------------------------------
def main_menu_keyboard(is_admin: bool, subscription: str) -> dict:
    kb = [
        [{"text": "🧭 مرورگر", "callback_data": "menu_browser"},
         {"text": "📸 شات", "callback_data": "menu_screenshot"}],
        [{"text": "📥 دانلود", "callback_data": "menu_download"},
         {"text": "🎬 ضبط", "callback_data": "menu_record"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "menu_settings"},
         {"text": "❓ راهنما", "callback_data": "menu_help"}],
    ]
    if is_admin:
        kb.append([{"text": "🛠️ پنل ادمین", "callback_data": "menu_admin"}])
    # ★ Torrent حذف شد
    kb.append([{"text": "🕸️ خزنده وحشی", "callback_data": "menu_crawler"}])
    return {"inline_keyboard": kb}

def settings_keyboard(session: dict) -> dict:
    s = session.setdefault("settings", {})
    sub = session.get("subscription", "free")
    is_admin = session.get("is_admin", False)

    # مقادیر فعلی
    rec_time = s.get("record_time", 20)
    dl_mode = translate_dlmode(s.get("default_download_mode", "store"))
    brw_mode = translate_browser_mode(s.get("browser_mode", "text"))
    deep_mode = s.get("deep_scan_mode", "logical")
    rec_beh = translate_record_behavior(s.get("record_behavior", "click"))
    audio = "🔊" if s.get("audio_enabled", False) else "🔇"
    incognito = "🕶️ روشن" if s.get("incognito_mode", False) else "🕶️ خاموش"
    vfmt = s.get("video_format", "webm").upper()
    vid_del = s.get("video_delivery", "split")
    resolution = s.get("video_resolution", "720p")
    comp = s.get("compression_level", "normal")
    comp_text = "فشرده" if comp == "high" else "عادی"

    kb = [
        [{"text": f"⏱️ زمان ضبط: {rec_time}s", "callback_data": "set_rec"}],
        [{"text": f"📥 دانلود: {dl_mode}", "callback_data": "set_dlmode"},
         {"text": f"🌐 مرورگر: {brw_mode}", "callback_data": "set_brwmode"}],
        [{"text": f"🔍 جستجو: {deep_mode}", "callback_data": "set_deep"}],
        [{"text": f"🎬 رفتار ضبط: {rec_beh}", "callback_data": "set_recbeh"}],
        [{"text": f"{audio} صدا", "callback_data": "set_audio"},
         {"text": incognito, "callback_data": "set_incognito"}],
        [{"text": f"🎞️ فرمت ویدیو: {vfmt}", "callback_data": "set_vfmt"},
         {"text": f"📦 ارسال: {vid_del}", "callback_data": "set_viddel"}],
        [{"text": f"📺 کیفیت: {resolution}", "callback_data": "set_resolution"}],
        [{"text": f"📦 فشرده‌سازی: {comp_text}", "callback_data": "set_compression"}],
        [{"text": "🔙 بازگشت", "callback_data": "back_main"}],
    ]
    return {"inline_keyboard": kb}

def crawler_settings_keyboard(session: dict) -> dict:
    s = session.setdefault("settings", {})
    mode = s.get("crawler_mode", "normal")
    layers = s.get("crawler_layers", 2)
    limit = s.get("crawler_limit", 0)
    max_time = s.get("crawler_max_time", 20)
    filters = s.get("crawler_filters", {})
    adblock = s.get("crawler_adblock", True)
    sitemap = s.get("crawler_sitemap", False)
    priority = s.get("crawler_priority", False)
    js = s.get("crawler_js", False)

    rows = [
        [{"text": f"📊 حالت: {mode}", "callback_data": "crawler_mode"}],
        [{"text": f"🔢 لایه‌ها: {layers}", "callback_data": "crawler_layers"}],
        [{"text": f"📄 صفحات: {'خودکار' if limit==0 else limit}", "callback_data": "crawler_limit"},
         {"text": f"⏱️ زمان: {max_time}m", "callback_data": "crawler_time"}],
    ]
    filter_names = [("image", "🖼️ تصاویر"), ("video", "🎬 ویدیوها"), ("archive", "📦 آرشیو"),
                    ("pdf", "📄 PDF"), ("unknown", "❓ ناشناخته")]
    for key, label in filter_names:
        status = "✅" if filters.get(key, True) else "❌"
        rows.append([{"text": f"{label}: {status}", "callback_data": f"crawler_filter_{key}"}])
    rows.append([
        {"text": f"🛡️ ضد تبلیغ: {'✅' if adblock else '❌'}", "callback_data": "crawler_adblock"},
        {"text": f"📋 Sitemap: {'✅' if sitemap else '❌'}", "callback_data": "crawler_sitemap"},
    ])
    rows.append([
        {"text": f"⚡ اولویت‌بندی: {'✅' if priority else '❌'}", "callback_data": "crawler_priority"},
        {"text": f"🌐 JS: {'✅' if js else '❌'}", "callback_data": "crawler_js"},
    ])
    rows.append([{"text": "▶️ شروع خزنده", "callback_data": "crawler_start"}])
    rows.append([{"text": "🔙 بازگشت", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

# ---------------------------------------------------------------------------
# تابع کمکی برای ویرایش کیبورد پیام (برای تنظیمات)
# ---------------------------------------------------------------------------
def edit_reply_markup(chat_id: int, message_id: int, reply_markup: dict) -> Optional[dict]:
    url = f"{API_BASE}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": json.dumps(reply_markup),
    }
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        return resp.json()
    except Exception as e:
        safe_log(f"editReplyMarkup failed: {e}")
        return None

# ---------------------------------------------------------------------------
#  اطلاعات سرور (برای ادمین)
# ---------------------------------------------------------------------------
def get_server_info() -> str:
    try:
        mem = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        uptime_proc = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        parts = [
            "📊 اطلاعات سرور:",
            "حافظه:",
            mem.stdout.strip() if mem.returncode == 0 else "ناموفق",
            "دیسک:",
            disk.stdout.strip() if disk.returncode == 0 else "ناموفق",
            "آپ‌تایم:",
            uptime_proc.stdout.strip() if uptime_proc.returncode == 0 else "ناموفق",
        ]
        return "\n".join(parts)
    except Exception as e:
        return f"خطا در دریافت اطلاعات: {e}"

# ---------------------------------------------------------------------------
#  مدیریت خزنده (Job Handler اضافی)
# ---------------------------------------------------------------------------
def _crawler_job_handler(job: dict):
    chat_id = job["chat_id"]
    url = job["url"]
    extra = job.get("extra", {})
    crawl_settings = extra.get("settings", {})
    stop_ev = threading.Event()

    async def progress_callback(msg: str, file_path: str = None):
        if msg == "__FINAL_ZIP__" and file_path:
            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            if file_size <= ZIP_PART_SIZE:
                worker.send_document(chat_id, file_path, caption="📦 بستهٔ خزنده")
            else:
                parts = split_file_binary(file_path, "crawler_result", ".zip")
                for p in parts:
                    worker.send_document(chat_id, p)
        else:
            worker.send_message(chat_id, msg)

    try:
        crawler.start_crawl(chat_id, url, crawl_settings, progress_callback, stop_ev)
    finally:
        job["status"] = "done"
        job["updated_at"] = time.time()
        storage.update_job(QUEUE_FILE, job)

# ---------------------------------------------------------------------------
# ثبت Job Handlers (همهٔ توابع پردازش)
# ---------------------------------------------------------------------------
handlers = dict(jobs.JOB_HANDLERS)
handlers["wild_crawler"] = _crawler_job_handler
worker.register_job_handlers(handlers)

# ---------------------------------------------------------------------------
#  مدیریت پیام‌های متنی
# ---------------------------------------------------------------------------
def handle_message(chat_id: int, text: str):
    session = storage.get_session(chat_id)
    if storage.is_banned(chat_id):
        worker.send_message(chat_id, "⛔ شما مسدود شده‌اید.")
        return

    is_admin = session.get("is_admin", False)
    service_disabled = os.path.exists(SERVICE_DISABLED_FLAG)

    if service_disabled and not is_admin:
        if text not in ("/start", "/cancel"):
            worker.send_message(chat_id, "⛔ سرویس موقتاً غیرفعال است.")
            return

    # ---------- فرمان‌های عمومی ----------
    if text == "/start":
        session["state"] = "idle"
        session["click_counter"] = 0
        session.pop("browser_links", None)
        session.pop("browser_url", None)
        session.pop("interactive_elements", None)
        session.pop("crawler_pending_url", None)
        session.pop("status_message_id", None)
        storage.set_session(chat_id, session)

        sub = session.get("subscription", "free")
        if is_admin or sub in ("pro",):
            worker.send_message(chat_id, "🤖 ربات آمادهٔ سرویس.", reply_markup=main_menu_keyboard(is_admin, sub))
        else:
            session["state"] = "waiting_subscription"
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "🔑 کد اشتراک خود را وارد کنید:")
        return

    if text == "/cancel":
        session["state"] = "idle"
        session["cancel_requested"] = True
        session.pop("browser_links", None)
        session.pop("browser_url", None)
        session.pop("interactive_elements", None)
        session.pop("crawler_pending_url", None)
        session.pop("status_message_id", None)
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "⏹️ عملیات لغو شد.", reply_markup=main_menu_keyboard(is_admin, session.get("subscription", "free")))
        return

    # ---------- فرمان‌های ادمین (فقط همین دو) ----------
    if is_admin:
        if text == "/toggleservice":
            if service_disabled:
                os.remove(SERVICE_DISABLED_FLAG)
                worker.send_message(chat_id, "🟢 سرویس فعال شد.")
            else:
                with open(SERVICE_DISABLED_FLAG, "w") as f:
                    f.write("disabled")
                worker.send_message(chat_id, "🔴 سرویس غیرفعال شد.")
            return
        if text == "/serverinfo":
            info = get_server_info()
            worker.send_message(chat_id, info)
            return

    # ---------- فعال‌سازی کد اشتراک (کاربران عادی) ----------
    if session.get("state") == "waiting_subscription":
        result = storage.activate_code(chat_id, text)
        if result:
            session["state"] = "idle"
            session["subscription"] = result
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, f"🎉 اشتراک شما به {result} ارتقا یافت!",
                                reply_markup=main_menu_keyboard(is_admin, result))
        else:
            worker.send_message(chat_id, "❌ کد نامعتبر است. دوباره تلاش کنید یا /cancel")
        return

    # ★ قفل اشتراک برای کاربران free (غیر از /start و /cancel و فعال‌سازی)
    if not is_admin and session.get("subscription", "free") == "free":
        worker.send_message(chat_id, "⛔ ابتدا باید اشتراک خود را فعال کنید. /start را بزنید و کد را وارد کنید.")
        return

    # ---------- پردازش بر اساس وضعیت ----------
    state = session.get("state", "idle")

    # ★ حالت‌های دریافت لینک (بدون waiting_url_torrent)
    if state in ("waiting_url_screenshot", "waiting_url_download", "waiting_url_browser", "waiting_url_record"):
        if not is_valid_url(text):
            worker.send_message(chat_id, "⛔ لینک نامعتبر است.")
            return

        job = {
            "job_id": uuid.uuid4().hex,
            "chat_id": chat_id,
            "url": text,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        status_text = ""
        target_file = QUEUE_FILE
        if state == "waiting_url_screenshot":
            job["mode"] = "screenshot"
            status_text = f"📸 درحال اسکرین‌شات از {text}"
        elif state == "waiting_url_download":
            job["mode"] = "download"
            status_text = f"📥 درحال بررسی لینک و آماده‌سازی دانلود..."
        elif state == "waiting_url_browser":
            job["mode"] = "browser"
            status_text = f"🔄 درحال مرورگر: باز کردن {text}"
        else:  # record
            job["mode"] = "record_video"
            target_file = RECORD_QUEUE_FILE
            job["extra"] = {"live_scroll": session["settings"]["record_behavior"] == "live"}
            rec_time = session["settings"].get("record_time", 20)
            res = session["settings"].get("video_resolution", "720p")
            status_text = f"🎬 درحال ضبط ویدیو با کیفیت {res} و زمان {rec_time} ثانیه..."

        status_msg = worker.send_message(chat_id, status_text)
        if status_msg and status_msg.get("ok") and "result" in status_msg:
            session["status_message_id"] = status_msg["result"]["message_id"]

        storage.enqueue_job(target_file, job)
        session["state"] = "idle"
        storage.set_session(chat_id, session)
        # ★ بدون منوی اصلی
        worker.send_message(chat_id, "✅ کار در صف قرار گرفت.")
        return

    elif state == "waiting_record_time":
        try:
            t = int(text)
            if not 1 <= t <= 1800:
                raise ValueError
            session["settings"]["record_time"] = t
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, f"⏱️ زمان ضبط روی {t} ثانیه تنظیم شد.")
            if "last_settings_msg_id" in session:
                kb = settings_keyboard(session)
                edit_reply_markup(chat_id, session["last_settings_msg_id"], kb)
            session["state"] = "idle"
            storage.set_session(chat_id, session)
        except ValueError:
            worker.send_message(chat_id, "⛔ عدد بین ۱ تا ۱۸۰۰ وارد کنید.")
        return

    elif state == "waiting_crawler_limit":
        try:
            n = int(text)
            if n < 0:
                raise ValueError
            session["settings"]["crawler_limit"] = n
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, f"📄 حد صفحات: {'خودکار' if n==0 else n}")
            session["state"] = "idle"
        except ValueError:
            worker.send_message(chat_id, "⛔ یک عدد صحیح وارد کنید (0=خودکار).")
        return

    elif state == "waiting_crawler_time":
        try:
            m = int(text)
            if not 5 <= m <= 30:
                raise ValueError
            session["settings"]["crawler_max_time"] = m
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, f"⏱️ حداکثر زمان خزنده: {m} دقیقه")
            session["state"] = "idle"
        except ValueError:
            worker.send_message(chat_id, "⛔ عدد بین ۵ تا ۳۰ وارد کنید.")
        return

    elif state == "waiting_crawler_url":
        if not is_valid_url(text):
            worker.send_message(chat_id, "⛔ لینک نامعتبر.")
            return
        session["crawler_pending_url"] = text
        session["state"] = "awaiting_crawler_confirmation"      # قفل کردن state
        storage.set_session(chat_id, session)
        summary = f"🌐 شروع خزنده روی:\n{text}\nتنظیمات: {session['settings']['crawler_mode']}"
        kb = {"inline_keyboard": [[
            {"text": "✅ شروع", "callback_data": "crawler_confirm_yes"},
            {"text": "❌ لغو", "callback_data": "crawler_confirm_no"}
        ]]}
        worker.send_message(chat_id, summary, reply_markup=kb)
        return

    elif state == "waiting_interactive_text":
        job_id = session.pop("pending_interactive_job_id", None)
        if not job_id:
            worker.send_message(chat_id, "⛔ درخواست کاوشگر منقضی شده است.")
            session["state"] = "idle"
            storage.set_session(chat_id, session)
            return

        job = storage.find_job(QUEUE_FILE, job_id)
        if not job:
            worker.send_message(chat_id, "⛔ کار کاوشگر یافت نشد.")
            session["state"] = "idle"
            storage.set_session(chat_id, session)
            return

        job["extra"]["user_text"] = text
        job["status"] = "queued"
        storage.update_job(QUEUE_FILE, job)
        worker.send_message(chat_id, "✅ متن دریافت شد. در حال اجرای کاوشگر...")
        session["state"] = "idle"
        storage.set_session(chat_id, session)
        return

    elif state == "browsing":
        cmd_map = session.get("text_links", {})

        if text.startswith("/t") and text in cmd_map:
            element_index = cmd_map[text]
            job = {
                "job_id": uuid.uuid4().hex,
                "chat_id": chat_id,
                "url": session.get("browser_url", ""),
                "mode": "interactive_execute",
                "status": "queued",
                "created_at": time.time(),
                "extra": {
                    "element_index": int(element_index),
                    "user_text": ""
                }
            }
            storage.enqueue_job(QUEUE_FILE, job)
            session["pending_interactive_job_id"] = job["job_id"]
            session["state"] = "waiting_interactive_text"
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "📝 متن مورد نظر برای این فیلد را وارد کنید:")
            return

        if text.startswith("/api_") and text in cmd_map:
            url = cmd_map[text]
            job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "download", "status": "queued", "created_at": time.time()}
            storage.enqueue_job(QUEUE_FILE, job)
            worker.send_message(chat_id, "🔗 دانلود API در صف قرار گرفت.")
            session["state"] = "idle"
            storage.set_session(chat_id, session)
            return

        if text in cmd_map:
            url = cmd_map[text]
            if text.startswith("/d") or text.startswith("/o") or text.startswith("/api_"):
                mode = "download"
            else:
                mode = "browser"

            job = {
                "job_id": uuid.uuid4().hex,
                "chat_id": chat_id,
                "url": url,
                "mode": mode,
                "status": "queued",
                "created_at": time.time(),
            }
            storage.enqueue_job(QUEUE_FILE, job)
            worker.send_message(chat_id, "🔗 در حال پردازش...")
            session["state"] = "idle"
            storage.set_session(chat_id, session)
            return

        worker.send_message(chat_id, "⛔ از دکمه‌ها استفاده کنید یا /cancel")
        return

    elif state == "waiting_live_command":
        if text.startswith("/Live_"):
            h = text[len("/Live_"):]
            cmd = "/" + h
            url = session.get("text_links", {}).get(cmd)
            if url:
                job = {
                    "job_id": uuid.uuid4().hex,
                    "chat_id": chat_id,
                    "url": url,
                    "mode": "record_video",
                    "status": "queued",
                    "created_at": time.time(),
                    "extra": {"live_scroll": True},
                }
                storage.enqueue_job(RECORD_QUEUE_FILE, job)
                worker.send_message(chat_id, "✅ ضبط زنده افزوده شد.")
                session["state"] = "idle"
                storage.set_session(chat_id, session)
                return
        worker.send_message(chat_id, "⛔ دستور معتبر نیست. /cancel")
        return

    # ---------- وضعیت پیش‌فرض ----------
    worker.send_message(chat_id, "لطفاً از منو استفاده کنید.",
                        reply_markup=main_menu_keyboard(is_admin, session.get("subscription", "free")))

# ---------------------------------------------------------------------------
#  مدیریت callback‌های اینلاین
# ---------------------------------------------------------------------------
def handle_callback(cq: dict):
    chat_id = cq["message"]["chat"]["id"]
    data = cq["data"]
    cq_id = cq["id"]

    session = storage.get_session(chat_id)
    if storage.is_banned(chat_id):
        worker.answer_callback_query(cq_id, "⛔ مسدود شده‌اید", show_alert=True)
        return

    is_admin = session.get("is_admin", False)
    service_disabled = os.path.exists(SERVICE_DISABLED_FLAG)
    if service_disabled and not is_admin:
        worker.answer_callback_query(cq_id, "سرویس موقتاً غیرفعال است", show_alert=True)
        return

    # ★ قفل اشتراک
    if not is_admin and session.get("subscription", "free") == "free":
        worker.answer_callback_query(cq_id, "⛔ ابتدا باید اشتراک خود را فعال کنید. /start را بزنید و کد را وارد کنید.", show_alert=True)
        return

    sub = session.get("subscription", "free")

    # محدودیت کلیک (غیر ادمین)
    if not is_admin:
        session["click_counter"] = session.get("click_counter", 0) + 1
        if session["click_counter"] > 5:
            worker.answer_callback_query(cq_id, "⛔ حداکثر کلیک مجاز", show_alert=True)
            return
    storage.set_session(chat_id, session)

    # ---------- منوهای اصلی ----------
    if data == "menu_browser":
        session["state"] = "waiting_url_browser"
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "🌐 لینک وب‌سایت را بفرستید:")
        return
    elif data == "menu_screenshot":
        session["state"] = "waiting_url_screenshot"
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "📸 لینک صفحه را بفرستید:")
        return
    elif data == "menu_download":
        session["state"] = "waiting_url_download"
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "📥 لینک فایل/صفحه را بفرستید:")
        return
    elif data == "menu_record":
        session["state"] = "waiting_url_record"
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "🎬 لینک صفحهٔ دارای ویدیو را بفرستید:")
        return
    elif data == "menu_settings":
        kb = settings_keyboard(session)
        res = worker.send_message(chat_id, "⚙️ تنظیمات:", reply_markup=kb)
        if res and res.get("ok") and "result" in res:
            session["last_settings_msg_id"] = res["result"]["message_id"]
            storage.set_session(chat_id, session)
        return
    elif data == "menu_help":
        help_text = (
            "🤖 راهنما:\n"
            "/start - شروع\n"
            "/cancel - لغو عملیات جاری\n"
            "با منو کارهای مختلف را انجام دهید."
        )
        worker.send_message(chat_id, help_text)
        return
    elif data == "menu_admin":
        if not is_admin:
            worker.answer_callback_query(cq_id, "دسترسی ندارید", show_alert=True)
            return
        admin_kb = {"inline_keyboard": [
            [{"text": "📊 منابع سرور", "callback_data": "admin_server_info"}],
            [{"text": "🔄 تغییر وضعیت سرویس", "callback_data": "admin_toggleservice"}],
            [{"text": "🔙 بازگشت", "callback_data": "back_main"}],
        ]}
        worker.send_message(chat_id, "🛠️ پنل ادمین:", reply_markup=admin_kb)
        return
    elif data == "menu_crawler":
        kb = crawler_settings_keyboard(session)
        res = worker.send_message(chat_id, "🕸️ تنظیمات خزنده:", reply_markup=kb)
        if res and res.get("ok") and "result" in res:
            session["last_crawler_msg_id"] = res["result"]["message_id"]
            storage.set_session(chat_id, session)
        return

    # ---------- بازگشت ----------
    elif data == "back_main":
        worker.send_message(chat_id, "منوی اصلی", reply_markup=main_menu_keyboard(is_admin, sub))
        return

    # ---------- تنظیمات کاربر ----------
    elif data.startswith("set_"):
        s = session.setdefault("settings", {})
        key = data[4:]
        if key == "rec":
            session["state"] = "waiting_record_time"
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "⏱️ زمان ضبط (۱-۱۸۰۰ ثانیه):")
            return

        # toggle مخصوص فشرده‌سازی
        if key == "compression":
            current = s.get("compression_level", "normal")
            s["compression_level"] = "high" if current == "normal" else "normal"
            storage.set_session(chat_id, session)
            worker.answer_callback_query(cq_id, f"فشرده‌سازی: {'فشرده' if s['compression_level']=='high' else 'عادی'}")
            if "last_settings_msg_id" in session:
                kb = settings_keyboard(session)
                edit_reply_markup(chat_id, session["last_settings_msg_id"], kb)
            return

        # سایر تنظیمات چرخشی
        choices_map = {
            "dlmode": ["store", "stream", "adm"],
            "brwmode": ["text", "media", "explorer"],
            "deep": ["logical", "everything"],
            "recbeh": ["click", "scroll", "live"],
            "vfmt": ["webm", "mkv", "mp4"],
            "viddel": ["split", "zip"],
            "resolution": ["480p", "720p", "1080p", "4k"],
        }
        toggle_map = {
            "audio": ("audio_enabled", "🔊 صدا روشن", "🔇 صدا خاموش"),
            "incognito": ("incognito_mode", "🕶️ ناشناس روشن", "🕶️ ناشناس خاموش"),
        }
        if key in choices_map:
            current = s.get(f"default_download_mode" if key=="dlmode" else (
                "browser_mode" if key=="brwmode" else (
                "deep_scan_mode" if key=="deep" else (
                "record_behavior" if key=="recbeh" else (
                "video_format" if key=="vfmt" else (
                "video_delivery" if key=="viddel" else "video_resolution"))))),
                choices_map[key][0])
            idx = choices_map[key].index(current) if current in choices_map[key] else 0
            next_idx = (idx + 1) % len(choices_map[key])
            new_val = choices_map[key][next_idx]
            mapping = {
                "dlmode": "default_download_mode",
                "brwmode": "browser_mode",
                "deep": "deep_scan_mode",
                "recbeh": "record_behavior",
                "vfmt": "video_format",
                "viddel": "video_delivery",
                "resolution": "video_resolution",
            }
            s[mapping[key]] = new_val
            storage.set_session(chat_id, session)
            worker.answer_callback_query(cq_id, f"تغییر به {new_val}")
            if "last_settings_msg_id" in session:
                kb = settings_keyboard(session)
                edit_reply_markup(chat_id, session["last_settings_msg_id"], kb)
            return
        elif key in toggle_map:
            attr, on_text, off_text = toggle_map[key]
            current = s.get(attr, False)
            s[attr] = not current
            storage.set_session(chat_id, session)
            worker.answer_callback_query(cq_id, on_text if not current else off_text)
            if "last_settings_msg_id" in session:
                kb = settings_keyboard(session)
                edit_reply_markup(chat_id, session["last_settings_msg_id"], kb)
            return
        else:
            worker.answer_callback_query(cq_id, "نامشخص")
            return

    # ---------- تنظیمات خزنده ----------
    elif data.startswith("crawler_"):
        s = session.setdefault("settings", {})
        if data == "crawler_mode":
            current = s.get("crawler_mode", "normal")
            modes = ["normal", "medium", "deep"]
            idx = modes.index(current)
            s["crawler_mode"] = modes[(idx + 1) % 3]
        elif data == "crawler_layers":
            current = s.get("crawler_layers", 2)
            s["crawler_layers"] = (current % 3) + 1
        elif data == "crawler_limit":
            session["state"] = "waiting_crawler_limit"
            worker.send_message(chat_id, "حداکثر صفحات (0=خودکار):")
            storage.set_session(chat_id, session)
            return
        elif data == "crawler_time":
            session["state"] = "waiting_crawler_time"
            worker.send_message(chat_id, "حداکثر زمان (۵-۳۰ دقیقه):")
            storage.set_session(chat_id, session)
            return
        elif data.startswith("crawler_filter_"):
            ftype = data[len("crawler_filter_"):]
            filters = s.setdefault("crawler_filters", {})
            filters[ftype] = not filters.get(ftype, True)
        elif data in ("crawler_adblock", "crawler_sitemap", "crawler_priority", "crawler_js"):
            key = data[8:]  # adblock, sitemap, priority, js
            attr_map = {
                "adblock": "crawler_adblock",
                "sitemap": "crawler_sitemap",
                "priority": "crawler_priority",
                "js": "crawler_js",
            }
            attr = attr_map[key]
            s[attr] = not s.get(attr, False)
        elif data == "crawler_start":
            session["state"] = "waiting_crawler_url"
            worker.send_message(chat_id, "🌐 لینک شروع خزنده:")
            storage.set_session(chat_id, session)
            return
        elif data == "crawler_confirm_yes":
            url = session.get("crawler_pending_url")
            if not url:
                worker.answer_callback_query(cq_id, "لینکی یافت نشد", show_alert=True)
                return
            status_msg = worker.send_message(chat_id, f"🕸️ درحال خزنده روی {url} ...")
            if status_msg and status_msg.get("ok") and "result" in status_msg:
                session["status_message_id"] = status_msg["result"]["message_id"]
            job = {
                "job_id": uuid.uuid4().hex,
                "chat_id": chat_id,
                "url": url,
                "mode": "wild_crawler",
                "status": "queued",
                "created_at": time.time(),
                "extra": {"settings": s.copy()},
            }
            storage.enqueue_job(QUEUE_FILE, job)
            session["state"] = "idle"
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "✅ خزنده شروع شد.")
            return
        elif data == "crawler_confirm_no":
            session["state"] = "idle"
            session.pop("crawler_pending_url", None)
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "خزنده لغو شد.")
            return
        storage.set_session(chat_id, session)
        if "last_crawler_msg_id" in session:
            kb = crawler_settings_keyboard(session)
            edit_reply_markup(chat_id, session["last_crawler_msg_id"], kb)
        return

    # ---------- عملیات مرورگر ----------
    elif data.startswith("nav_") or data.startswith("dlvid_"):
        url = session.get("_callback_urls", {}).get(data)
        if url:
            mode = "browser" if data.startswith("nav_") else "download"
            job = {
                "job_id": uuid.uuid4().hex,
                "chat_id": chat_id,
                "url": url,
                "mode": mode,
                "status": "queued",
                "created_at": time.time(),
            }
            storage.enqueue_job(QUEUE_FILE, job)
            worker.answer_callback_query(cq_id, "در صف قرار گرفت")
        else:
            worker.answer_callback_query(cq_id, "لینک یافت نشد", show_alert=True)
        return
    elif data.startswith("bpg_"):
        parts = data.split("_")
        new_page = int(parts[2])
        session["browser_page"] = new_page
        storage.set_session(chat_id, session)
        from jobs import send_browser_page
        send_browser_page(chat_id, None, session.get("browser_url", ""), new_page)
        return
    elif data.startswith("closebrowser_"):
        session["state"] = "idle"
        session.pop("browser_links", None)
        session.pop("browser_url", None)
        session.pop("_callback_urls", None)
        session.pop("text_links", None)
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "مرورگر بسته شد.", reply_markup=main_menu_keyboard(is_admin, sub))
        return

    # عملیات ویژه
    elif data.startswith("scvid_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "اسکن ویدیو شروع شد")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "scan_videos", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("scdl_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "جستجوی فایل‌ها شروع شد")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "scan_downloads", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("sman_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "✅ در صف قرار گرفت")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "smart_analyze", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("srcan_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "✅ در صف قرار گرفت")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "source_analyze", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("extcmd_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "✅ در صف قرار گرفت")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "extract_commands", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("recvid_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        if session["settings"]["record_behavior"] == "live":
            session["state"] = "waiting_live_command"
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "لینک زنده را با /Live_... بفرستید")
            return
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "record_video", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(RECORD_QUEUE_FILE, job)
        worker.answer_callback_query(cq_id, "ضبط در صف قرار گرفت")
        return

    elif data.startswith("fullshot_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "✅ در صف قرار گرفت")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "fullpage_screenshot", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("captcha_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "✅ در صف قرار گرفت")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "captcha", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("dlweb_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "دانلود سایت شروع شد")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "download_website", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    elif data.startswith("intscan_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "✅ در صف قرار گرفت")
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "interactive_scan", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    # API Hunter
    elif data.startswith("apihunter_"):
        url = session.get("browser_url")
        if not url:
            worker.answer_callback_query(cq_id, "صفحه‌ای باز نیست", show_alert=True)
            return
        worker.answer_callback_query(cq_id, "🔌 شنود API آغاز شد")
        status_msg = worker.send_message(chat_id, "🔌 درحال شنود APIها...")
        if status_msg and status_msg.get("ok") and "result" in status_msg:
            session["status_message_id"] = status_msg["result"]["message_id"]
            storage.set_session(chat_id, session)
        job = {"job_id": uuid.uuid4().hex, "chat_id": chat_id, "url": url, "mode": "api_hunter", "status": "queued", "created_at": time.time()}
        storage.enqueue_job(QUEUE_FILE, job)
        return

    # دانلود (zip/raw)
    elif data.startswith("dlzip_") or data.startswith("dlraw_") or data.startswith("dlblindzip_") or data.startswith("dlblindra_"):
        job_id = data.split("_", 1)[1]
        original_job = storage.find_job(QUEUE_FILE, job_id)
        if not original_job:
            worker.answer_callback_query(cq_id, "کار یافت نشد", show_alert=True)
            return
        pack_zip = "zip" in data
        original_job["extra"]["pack_zip"] = pack_zip
        new_job = {
            "job_id": uuid.uuid4().hex,
            "chat_id": chat_id,
            "url": original_job["url"],
            "mode": "download_execute",
            "status": "queued",
            "created_at": time.time(),
            "extra": original_job["extra"].copy(),
        }
        storage.enqueue_job(QUEUE_FILE, new_job)
        worker.answer_callback_query(cq_id, "دانلود نهایی شد")
        return
    elif data.startswith("canceljob_"):
        job_id = data.split("_", 1)[1]
        job = storage.find_job(QUEUE_FILE, job_id)
        if job:
            job["status"] = "cancelled"
            storage.update_job(QUEUE_FILE, job)
            worker.answer_callback_query(cq_id, "لغو شد")
        return

    # اسکرین‌شات 2x/4k
    elif data.startswith("req2x_"):
        job_id = data[len("req2x_"):]
        original = storage.find_job(QUEUE_FILE, job_id)
        if original:
            worker.answer_callback_query(cq_id, "درخواست 2x ثبت شد")
            new_job = {
                "job_id": uuid.uuid4().hex,
                "chat_id": chat_id,
                "url": original["url"],
                "mode": "2x_screenshot",
                "status": "queued",
                "created_at": time.time(),
            }
            storage.enqueue_job(QUEUE_FILE, new_job)
        return
    elif data.startswith("req4k_"):
        job_id = data[len("req4k_"):]
        original = storage.find_job(QUEUE_FILE, job_id)
        if original:
            worker.answer_callback_query(cq_id, "درخواست 4K ثبت شد")
            new_job = {
                "job_id": uuid.uuid4().hex,
                "chat_id": chat_id,
                "url": original["url"],
                "mode": "4k_screenshot",
                "status": "queued",
                "created_at": time.time(),
            }
            storage.enqueue_job(QUEUE_FILE, new_job)
        return

    # -------- ادمین ----------
    elif data == "admin_server_info":
        info = get_server_info()
        worker.send_message(chat_id, info)
        worker.answer_callback_query(cq_id, "اطلاعات دریافت شد")
        return
    elif data == "admin_toggleservice":
        if os.path.exists(SERVICE_DISABLED_FLAG):
            os.remove(SERVICE_DISABLED_FLAG)
            worker.answer_callback_query(cq_id, "✅ سرویس فعال شد")
        else:
            with open(SERVICE_DISABLED_FLAG, "w") as f:
                f.write("1")
            worker.answer_callback_query(cq_id, "🔴 سرویس غیرفعال شد")
        return

    # دکمه‌های صفحه‌بندی دانلودهای پیدا شده
    elif data.startswith("dfpg_"):
        parts = data.split("_")
        page = int(parts[2])
        session["found_downloads_page"] = page
        storage.set_session(chat_id, session)
        from jobs import _send_found_links_page
        _send_found_links_page(chat_id, session.get("found_downloads", []), page)
        return
    elif data == "close_downloads":
        session.pop("found_downloads", None)
        session.pop("found_downloads_page", None)
        storage.set_session(chat_id, session)
        worker.send_message(chat_id, "لیست پاک شد.")
        return

    # adblock toggle
    elif data.startswith("adblock_"):
        domain = urlparse(session.get("browser_url", "")).hostname or ""
        if not domain:
            return
        blocked = session.setdefault("ad_blocked_domains", [])
        if domain in blocked:
            blocked.remove(domain)
        else:
            blocked.append(domain)
        session["ad_blocked_domains"] = blocked
        storage.set_session(chat_id, session)
        from jobs import send_browser_page
        send_browser_page(chat_id, None, session.get("browser_url", ""), session.get("browser_page", 0))
        return

    worker.answer_callback_query(cq_id, "ناشناخته")

# ---------------------------------------------------------------------------
#  حلقهٔ اصلی polling
# ---------------------------------------------------------------------------
def main():
    storage.load_subscriptions()
    workers = worker.start_workers(stop_event)
    offset = 0
    print("[Main] ربات اجرا شد. منتظر دریافت پیام...")
    while not stop_event.is_set():
        try:
            resp = requests.post(
                f"{API_BASE}/getUpdates",
                json={"offset": offset, "timeout": LONG_POLL_TIMEOUT},
                timeout=LONG_POLL_TIMEOUT + 10,
            )
            if resp.status_code != 200:
                safe_log(f"getUpdates failed with {resp.status_code}")
                time.sleep(2)
                continue
            data = resp.json()
            if not data.get("ok"):
                safe_log(f"getUpdates error: {data}")
                time.sleep(2)
                continue
            updates = data.get("result", [])
            for upd in updates:
                if "message" in upd and "text" in upd["message"]:
                    chat_id = upd["message"]["chat"]["id"]
                    text = upd["message"]["text"]
                    threading.Thread(target=handle_message, args=(chat_id, text), daemon=True).start()
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                offset = upd["update_id"] + 1
        except requests.exceptions.ReadTimeout:
            pass
        except Exception as e:
            safe_log(f"Polling error: {e}")
            time.sleep(2)

    for t in workers:
        t.join(timeout=2)

if __name__ == "__main__":
    main()
