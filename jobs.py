import os
import time
import json
import uuid
import math
import re
import hashlib
import shutil
import subprocess
import threading
from typing import Optional, Dict, List, Tuple, Any

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Route

# ---------------------------------------------------------------------------
# Import پروژه
# ---------------------------------------------------------------------------
from settings import *          # تمام ثابت‌ها و تنظیمات
from utils import *             # توابع کمکی
import storage
import worker

# ---------------------------------------------------------------------------
# متغیرهای سراسری Playwright
# ---------------------------------------------------------------------------
_global_playwright = None       # نمونهٔ Playwright
_global_browser: Optional[Browser] = None
_browser_contexts: Dict[str, Dict] = {}   # کلید -> {"context": ..., "last_used": ...}
_browser_contexts_lock = threading.Lock()

# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------

def _adblock_router(route: Route) -> None:
    """مسدود کردن درخواست‌های تبلیغاتی بر اساس دامنه و کلمات کلیدی."""
    url = route.request.url
    try:
        domain = urlparse(url).hostname or ""
    except Exception:
        route.continue_()
        return

    # بررسی دامنه‌های تبلیغاتی
    for ad_domain in AD_DOMAINS:
        if ad_domain in domain:
            route.abort()
            return

    # بررسی کلمات کلیدی در URL
    url_lower = url.lower()
    for kw in BLOCKED_AD_KEYWORDS:
        if kw in url_lower:
            route.abort()
            return

    route.continue_()


def get_or_create_context(chat_id: int, incognito: bool = False) -> BrowserContext:
    """
    دریافت یا ایجاد یک BrowserContext برای کاربر.
    context ها بعد از ۲۰ دقیقه عدم استفاده بسته می‌شوند.
    """
    global _global_playwright, _global_browser

    key = f"{chat_id}_{'incognito' if incognito else 'default'}"
    now = time.time()

    with _browser_contexts_lock:
        if key in _browser_contexts:
            ctx_data = _browser_contexts[key]
            if now - ctx_data["last_used"] < 1200:  # کمتر از ۲۰ دقیقه
                ctx_data["last_used"] = now
                return ctx_data["context"]
            # منقضی شده – ببند
            try:
                ctx_data["context"].close()
            except Exception:
                pass
            del _browser_contexts[key]

        # راه‌اندازی مرورگر در صورت نیاز
        if _global_browser is None:
            _global_playwright = sync_playwright().start()
            _global_browser = _global_playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )

        # ایجاد context جدید
        width = 390  # می‌توان رندم هم کرد (390-414)
        height = 844
        context = _global_browser.new_context(viewport={"width": width, "height": height})

        if incognito:
            context.clear_cookies()

        # افزودن router تبلیغاتی
        context.route("**/*", _adblock_router)

        _browser_contexts[key] = {"context": context, "last_used": now}
        return context


def close_user_context(chat_id: int, incognito: bool = False) -> None:
    """بستن context اختصاصی یک کاربر."""
    key = f"{chat_id}_{'incognito' if incognito else 'default'}"
    with _browser_contexts_lock:
        if key in _browser_contexts:
            ctx_data = _browser_contexts.pop(key)
            try:
                ctx_data["context"].close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# استخراج لینک و ویدیو
# ---------------------------------------------------------------------------

def extract_links(page: Page, mode: str) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """
    بر اساس *mode* لینک‌ها و ویدیوهای صفحه را استخراج می‌کند.
    خروجی: (لیست (type, text, url), لیست video_urls)
    """
    if mode == "text":
        js = """
        () => {
            const links = [];
            const seen = new Set();
            document.querySelectorAll('a[href]').forEach(a => {
                const text = a.innerText.trim().slice(0, 40);
                const url = a.href;
                if (!seen.has(url)) {
                    seen.add(url);
                    links.push(['link', text, url]);
                }
            });
            return links;
        }
        """
        result = page.evaluate(js)
        return [(t, txt, u) for t, txt, u in result], []

    elif mode == "media":
        js_links = """
        () => {
            const links = [];
            const seen = new Set();
            document.querySelectorAll('a[href]').forEach((a, i) => {
                if (i >= 20) return;
                const text = a.innerText.trim().slice(0, 40);
                const url = a.href;
                if (!seen.has(url)) {
                    seen.add(url);
                    links.push(['link', text, url]);
                }
            });
            return links;
        }
        """
        js_videos = """
        () => {
            const seen = new Set();
            const videos = [];
            document.querySelectorAll('video source, video[src]').forEach(el => {
                const src = el.src || el.getAttribute('src');
                if (src && src.startsWith('http') && !seen.has(src)) {
                    seen.add(src);
                    videos.push(src);
                }
            });
            document.querySelectorAll('iframe[src]').forEach(el => {
                const src = el.src;
                if (src && src.startsWith('http') && !seen.has(src)) {
                    seen.add(src);
                    videos.push(src);
                }
            });
            return videos;
        }
        """
        links = page.evaluate(js_links)
        videos = page.evaluate(js_videos)
        return [(t, txt, u) for t, txt, u in links], videos

    elif mode == "explorer":
        js = """
        () => {
            const items = [];
            const seen = new Set();
            function add(type, text, href) {
                if (href && !seen.has(href)) {
                    seen.add(href);
                    items.push([type, text.slice(0, 40), href]);
                }
            }
            document.querySelectorAll('a[href]').forEach(a => {
                add('link', a.innerText.trim(), a.href);
            });
            document.querySelectorAll('button, input[type="submit"]').forEach(el => {
                const text = el.innerText || el.value || el.getAttribute('aria-label') || '';
                const href = el.getAttribute('formaction') || '';
                if (href) add('button', text.trim(), href);
            });
            document.querySelectorAll('[onclick], [role="button"]').forEach(el => {
                const onclick = el.getAttribute('onclick') || '';
                const match = onclick.match(/(?:location\\.href|window\\.open)\\s*=\\s*['"]([^'"]+)['"]/);
                const href = match ? match[1] : '';
                if (href) add('role', (el.innerText || el.getAttribute('aria-label') || '').trim(), href);
            });
            return items;
        }
        """
        items = page.evaluate(js)
        return [(t, txt, u) for t, txt, u in items], []

    else:
        return [], []


# ---------------------------------------------------------------------------
# نمایش صفحه‌بندی مرورگر
# ---------------------------------------------------------------------------

def send_browser_page(chat_id: int, image_path: Optional[str], url: str, page_num: int) -> None:
    """
    صفحهٔ جاری از لینک‌های مرورگر را با کیبورد اینلاین می‌فرستد.
    """
    session = storage.get_session(chat_id)
    all_links = session.get("browser_links") or []
    per_page = 10
    start = page_num * per_page
    end = min(start + per_page, len(all_links))
    page_links = all_links[start:end]

    # ذخیره موقت callback -> url
    callback_urls: Dict[str, str] = {}
    keyboard_rows = []

    # ردیف‌های لینک‌ها
    row = []
    for idx, link in enumerate(page_links):
        global_idx = start + idx
        if link["type"] == "video":
            cb = f"dlvid_{chat_id}_{global_idx}"
        else:
            cb = f"nav_{chat_id}_{global_idx}"
        href = link.get("href", "")
        if not isinstance(href, str):
            href = str(href)
        callback_urls[cb] = href
        text = (link.get("text") or "")[:20] or href[:20]
        row.append({"text": text, "callback_data": cb})
        if len(row) == 2:
            keyboard_rows.append(row)
            row = []
    if row:
        # اگر تعداد فرد بود، دکمهٔ آخر تنها می‌ماند
        keyboard_rows.append(row)

    # ردیف نویگیشن
    nav_row = []
    if page_num > 0:
        nav_row.append({"text": "◀️", "callback_data": f"bpg_{chat_id}_{page_num - 1}"})
    if end < len(all_links):
        nav_row.append({"text": "▶️", "callback_data": f"bpg_{chat_id}_{page_num + 1}"})
    if nav_row:
        keyboard_rows.append(nav_row)

    # ردیف‌های عملیات ویژه بر اساس حالت و اشتراک
    sub = session.get("subscription", "free")
    bw_mode = session["settings"].get("browser_mode", "text")
    extra_rows: List[List[dict]] = []

    if bw_mode == "media":
        if sub in ("pro", "plus") or chat_id == ADMIN_CHAT_ID:
            extra_rows.append([{"text": "🎬 اسکن ویدیوها", "callback_data": f"scvid_{chat_id}"}])
        # دکمهٔ خاموش/روشن adblock برای دامنهٔ فعلی
        domain = urlparse(session.get("browser_url", "")).hostname or ""
        ad_blocked = session.get("ad_blocked_domains", [])
        if domain in ad_blocked:
            extra_rows.append([{"text": "🛡️ تبلیغات: روشن", "callback_data": f"adblock_{chat_id}"}])
        else:
            extra_rows.append([{"text": "🛡️ تبلیغات: خاموش", "callback_data": f"adblock_{chat_id}"}])

    if bw_mode == "explorer" and (sub in ("pro", "plus") or chat_id == ADMIN_CHAT_ID):
        extra_rows.append([
            {"text": "🔍 تحلیل هوشمند", "callback_data": f"sman_{chat_id}"},
            {"text": "🕵️ تحلیل سورس", "callback_data": f"srcan_{chat_id}"},
        ])
    elif bw_mode == "text" and (sub in ("pro", "plus") or chat_id == ADMIN_CHAT_ID):
        extra_rows.append([{"text": "📦 جستجوی فایل‌ها", "callback_data": f"scdl_{chat_id}"}])

    # دکمه‌های مشترک
    common_buttons = []
    if sub in ("pro", "plus") or chat_id == ADMIN_CHAT_ID:
        common_buttons.append({"text": "📋 فرامین", "callback_data": f"extcmd_{chat_id}"})
        common_buttons.append({"text": "🎬 ضبط", "callback_data": f"recvid_{chat_id}"})
        common_buttons.append({"text": "📸 شات کامل", "callback_data": f"fullshot_{chat_id}"})
        common_buttons.append({"text": "🔎 کاوشگر", "callback_data": f"intscan_{chat_id}"})
    if sub == "pro" or chat_id == ADMIN_CHAT_ID:
        common_buttons.append({"text": "🌐 دانلود سایت", "callback_data": f"dlweb_{chat_id}"})
    common_buttons.append({"text": "🪟 حل کپچا", "callback_data": f"captcha_{chat_id}"})
    common_buttons.append({"text": "❌ بستن", "callback_data": f"closebrowser_{chat_id}"})
    # دو تایی چینش
    for i in range(0, len(common_buttons), 2):
        row = common_buttons[i:i+2]
        extra_rows.append(row)

    keyboard_rows.extend(extra_rows)

    # ذخیره callback_urls در session
    session["_callback_urls"] = callback_urls
    session["browser_page"] = page_num
    storage.set_session(chat_id, session)

    # ارسال تصویر
    if image_path and os.path.isfile(image_path):
        worker.send_document(chat_id, image_path, caption=f"🌐 {url}")

    # ارسال پیام با کیبورد
    total_pages = max(1, math.ceil(len(all_links) / per_page))
    text = f"صفحه {page_num + 1}/{total_pages}"
    markup = {"inline_keyboard": keyboard_rows}
    worker.send_message(chat_id, text, reply_markup=markup)

    # ذخیره لینک‌های اضافی به صورت دستور /a...
    if len(all_links) > per_page:
        # فقط لینک‌های خارج از این صفحه
        remaining = all_links[:start] + all_links[end:]
        cmd_map = {}
        for link in remaining:
            h = hashlib.md5(link["href"].encode()).hexdigest()[:8]
            cmd_map[f"/a{h}"] = link["href"]
        session.setdefault("text_links", {}).update(cmd_map)
        storage.set_session(chat_id, session)


# ---------------------------------------------------------------------------
# محدودیت مصرف (ساده)
# ---------------------------------------------------------------------------

def check_rate_limit(chat_id: int, service: str, file_size_bytes: int = 0) -> Optional[str]:
    """
    بررسی محدودیت مصرف برای یک سرویس مشخص.
    در صورت رد شدن، پیام خطا برمی‌گرداند. در غیر این صورت None.
    """
    session = storage.get_session(chat_id)
    sub = session.get("subscription", "free")
    limits = LIMITS.get(sub, {}).get(service)
    if not limits:
        return None  # سرویس تعریف نشده، مشکلی نیست

    max_count, window_seconds, max_size = limits
    now = time.time()
    usage = session.setdefault("usage", {}).setdefault(service, {"count": 0, "start_time": now, "total_size": 0})

    # بازنشانی در صورت عبور از پنجره
    if now - usage["start_time"] > window_seconds:
        usage["count"] = 0
        usage["total_size"] = 0
        usage["start_time"] = now

    # بررسی تعداد
    if usage["count"] >= max_count and max_count != 999:
        return f"⛔ محدودیت تعداد درخواست‌های `{service}` پر شده است."

    # بررسی حجم
    if max_size is not None and usage["total_size"] + file_size_bytes > max_size:
        return f"⛔ محدودیت حجم دانلود برای `{service}` پر شده است."

    # به‌روزرسانی
    usage["count"] += 1
    usage["total_size"] += file_size_bytes
    storage.set_session(chat_id, session)
    return None


# ---------------------------------------------------------------------------
# توابع کمکی صدا و تصویر برای ضبط
# ---------------------------------------------------------------------------

def setup_pulseaudio() -> bool:
    """راه‌اندازی PulseAudio مجازی برای ضبط صدا."""
    try:
        # اطمینان از اجرای pulseaudio
        subprocess.run(["pulseaudio", "--start"], check=False, capture_output=True)
        # بارگذاری ماژول null sink
        result = subprocess.run(
            ["pactl", "load-module", "module-null-sink", "sink_name=virtual_out"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            os.environ["PULSE_SINK"] = "virtual_out"
            return True
    except Exception:
        pass
    return False


def start_audio_capture(job_dir: str) -> Tuple[Optional[subprocess.Popen], str]:
    """شروع ضبط صدا با ffmpeg."""
    audio_path = os.path.join(job_dir, "audio.mp3")
    try:
        proc = subprocess.Popen([
            "ffmpeg", "-y", "-f", "pulse", "-i", "virtual_out.monitor",
            "-ac", "2", "-ar", "44100", "-acodec", "libmp3lame",
            "-b:a", "128k", audio_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc, audio_path
    except Exception:
        return None, audio_path


def stop_audio_capture(proc: Optional[subprocess.Popen], audio_path: str) -> bool:
    """توقف ضبط و بررسی موفقیت."""
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0


def smooth_scroll_to_video(page: Page) -> None:
    """اسکرول نرم به بزرگ‌ترین ویدیو یا iframe."""
    js = """
    () => {
        let el = null, maxArea = 0;
        document.querySelectorAll('video, iframe').forEach(e => {
            const rect = e.getBoundingClientRect();
            const area = rect.width * rect.height;
            if (area > maxArea) {
                maxArea = area;
                el = e;
            }
        });
        if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
    """
    page.evaluate(js)


def find_video_center(page: Page) -> Tuple[float, float]:
    """مرکز بزرگ‌ترین ویدیو/iframe را برمی‌گرداند."""
    js = """
    () => {
        let el = null, maxArea = 0;
        document.querySelectorAll('video, iframe').forEach(e => {
            const rect = e.getBoundingClientRect();
            const area = rect.width * rect.height;
            if (area > maxArea) {
                maxArea = area;
                el = e;
            }
        });
        if (el) {
            const rect = el.getBoundingClientRect();
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        }
        return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
    }
    """
    res = page.evaluate(js)
    return res["x"], res["y"]


def scan_videos_smart(page: Page) -> List[Dict[str, Any]]:
    """
    جستجوی هوشمند ویدیوها در صفحه: المان‌ها، پاسخ‌های شبکه و اسکریپت‌ها.
    خروج: لیست دیکشنری با کلیدهای url, score, source
    """
    results: List[Dict[str, Any]] = []
    seen_urls = set()

    # 1. المان‌های <video> و <iframe>
    js_elements = """
    () => {
        const list = [];
        document.querySelectorAll('video source, video[src]').forEach(el => {
            const src = el.src || el.getAttribute('src');
            if (src) list.push({url: src, area: 1});
        });
        document.querySelectorAll('iframe[src]').forEach(el => {
            const src = el.src;
            if (src) list.push({url: src, area: 1});
        });
        return list;
    }
    """
    elements = page.evaluate(js_elements)
    for item in elements:
        if item["url"] not in seen_urls and item["url"].startswith("http"):
            seen_urls.add(item["url"])
            results.append({"url": item["url"], "score": item.get("area", 1), "source": "element"})

    # 2. پاسخ‌های شبکه (پویا – نمی‌توان به راحتی بعداً جمع کرد، اما در یک callback ذخیره می‌کنیم)
    #    برای سادگی از page.on("response") استفاده می‌کنیم که نیاز به ذخیره‌سازی جانبی دارد.
    #    در این پیاده‌سازی خلاصه شده است.
    # (در صورت نیاز می‌توان از page.route برای جمع‌آوری استفاده کرد.)

    # 3. اسکریپت‌های درون صفحه
    js_scripts = """
    () => {
        const urls = [];
        const re = /(https?:\\/\\/[^\\s"']+\\.(?:mp4|webm|mkv|avi|mov|flv|wmv|m3u8|mpd))/gi;
        document.querySelectorAll('script').forEach(script => {
            const text = script.textContent || '';
            let m;
            while ((m = re.exec(text)) !== null) {
                urls.push(m[1]);
            }
        });
        return urls;
    }
    """
    script_urls = page.evaluate(js_scripts)
    for u in script_urls:
        if u not in seen_urls:
            seen_urls.add(u)
            results.append({"url": u, "score": 2, "source": "script"})

    # مرتب‌سازی بر اساس امتیاز (بیشتر بهتر)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# توابع پردازش Job
# ---------------------------------------------------------------------------

def _done_job(job: dict, mode_file: str = QUEUE_FILE) -> None:
    """مارک job به عنوان done و ذخیره در صف."""
    job["status"] = "done"
    job["updated_at"] = time.time()
    storage.update_job(mode_file, job)


def process_browser_job(job: dict) -> None:
    chat_id = job["chat_id"]
    url = job["url"]

    if is_direct_file_url(url):
        worker.send_message(chat_id, "📥 این لینک یک فایل مستقیم است. از دستور /download استفاده کنید.")
        _done_job(job)
        return

    session = storage.get_session(chat_id)
    context = get_or_create_context(chat_id, incognito=session["settings"]["incognito_mode"])
    page = None
    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)

    try:
        page = context.new_page()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        spath = os.path.join(job_dir, "browser.png")
        page.screenshot(path=spath, full_page=True)

        mode = session["settings"]["browser_mode"]
        links, video_urls = extract_links(page, mode)

        session["state"] = "browsing"
        session["browser_url"] = url
        session["browser_links"] = (
            [{"type": t, "text": txt, "href": href} for t, txt, href in links]
            + [{"type": "video", "text": "🎬 ویدیو", "href": v} for v in video_urls]
        )
        session["browser_page"] = 0
        session["last_browser_time"] = time.time()
        storage.set_session(chat_id, session)

        send_browser_page(chat_id, spath, url, 0)
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در مرورگر: {e}")
        job["status"] = "failed"
        storage.update_job(QUEUE_FILE, job)
    finally:
        if page:
            page.close()
        shutil.rmtree(job_dir, ignore_errors=True)


def process_screenshot_job(job: dict) -> None:
    chat_id = job["chat_id"]
    url = job["url"]
    mode = job["mode"]

    session = storage.get_session(chat_id)
    context = get_or_create_context(chat_id, incognito=session["settings"]["incognito_mode"])
    page = None
    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)

    try:
        page = context.new_page()

        if mode == "2x_screenshot":
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.evaluate("document.body.style.zoom = '200%'")
            page.wait_for_timeout(500)
        elif mode == "4k_screenshot":
            page.set_viewport_size({"width": 3840, "height": 2160})
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        else:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

        spath = os.path.join(job_dir, "screenshot.png")
        page.screenshot(path=spath, full_page=True)

        worker.send_document(chat_id, spath, caption=f"📸 {url}")

        # دکمه‌های اضافی برای کاربران plus/pro
        sub = session.get("subscription", "free")
        if sub in ("plus", "pro") or chat_id == ADMIN_CHAT_ID:
            markup = {"inline_keyboard": [[
                {"text": "🔍 2x Zoom", "callback_data": f"req2x_{job['job_id']}"},
                {"text": "🖼️ 4K", "callback_data": f"req4k_{job['job_id']}"},
            ]]}
            worker.send_message(chat_id, "گزینه‌های اسکرین‌شات:", reply_markup=markup)

        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در اسکرین‌شات: {e}")
        job["status"] = "failed"
        storage.update_job(QUEUE_FILE, job)
    finally:
        if page:
            page.close()
        shutil.rmtree(job_dir, ignore_errors=True)


def process_download_job(job: dict) -> None:
    if job["mode"] == "download_website":
        download_full_website(job)
        return

    url = job["url"]
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)

    if is_direct_file_url(url):
        direct_link = url
    else:
        direct_link = crawl_for_download_link(url, max_depth=1, max_pages=10)

    if not direct_link:
        process_blind_download(job)
        return

    # دریافت اطلاعات فایل
    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size_bytes = int(head.headers.get("Content-Length", 0))
    except Exception:
        size_bytes = 0

    # بررسی محدودیت
    err = check_rate_limit(chat_id, "download", size_bytes)
    if err:
        worker.send_message(chat_id, err)
        _done_job(job)
        return

    fname = get_filename_from_url(direct_link)
    size_str = f"{size_bytes / 1024 / 1024:.1f} MB" if size_bytes else "نامشخص"

    keyboard = {"inline_keyboard": [[
        {"text": "📦 ZIP", "callback_data": f"dlzip_{job['job_id']}"},
        {"text": "📄 اصلی", "callback_data": f"dlraw_{job['job_id']}"},
        {"text": "❌ لغو", "callback_data": f"canceljob_{job['job_id']}"},
    ]]}
    worker.send_message(chat_id, f"📄 {fname} ({size_str})", reply_markup=keyboard)

    job["status"] = "awaiting_user"
    job["extra"] = {"direct_link": direct_link, "filename": fname}
    storage.update_job(QUEUE_FILE, job)


def process_blind_download(job: dict) -> None:
    url = job["url"]
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)

    fname = get_filename_from_url(url)
    if fname == "downloaded_file":
        fname = f"download_{uuid.uuid4().hex[:8]}"

    try:
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        # حدس پسوند
        if "." not in fname:
            if "video/mp4" in content_type:
                fname += ".mp4"
            elif "video/webm" in content_type:
                fname += ".webm"
            elif "application/pdf" in content_type:
                fname += ".pdf"
            elif "application/zip" in content_type:
                fname += ".zip"
            else:
                fname += ".bin"

        fpath = os.path.join(job_dir, fname)
        size = 0
        with open(fpath, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
                size += len(chunk)

        err = check_rate_limit(chat_id, "download", size)
        if err:
            worker.send_message(chat_id, err)
            _done_job(job)
            return

        keyboard = {"inline_keyboard": [[
            {"text": "📦 ZIP", "callback_data": f"dlblindzip_{job['job_id']}"},
            {"text": "📄 اصلی", "callback_data": f"dlblindra_{job['job_id']}"},
            {"text": "❌ لغو", "callback_data": f"canceljob_{job['job_id']}"},
        ]]}
        worker.send_message(chat_id, f"📄 {fname} ({size / 1024 / 1024:.1f} MB)", reply_markup=keyboard)

        job["status"] = "awaiting_user"
        job["extra"] = {"file_path": fpath, "filename": fname}
        storage.update_job(QUEUE_FILE, job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در دانلود: {e}")
        _done_job(job)
        shutil.rmtree(job_dir, ignore_errors=True)


def _send_file_parts(chat_id: int, file_path: str, use_zip: bool, label: str = "") -> None:
    """
    ارسال فایل به صورت قطعه‌قطعه (با توجه به ZIP_PART_SIZE و نوع تحویل).
    """
    if use_zip:
        base = os.path.splitext(os.path.basename(file_path))[0]
        parts = create_zip_and_split(file_path, base)
    else:
        base, ext = os.path.splitext(os.path.basename(file_path))
        ext = ext if ext else ".bin"
        parts = split_file_binary(file_path, base, ext)

    for part_path in parts:
        worker.send_document(chat_id, part_path, caption=label)

    # ارسال فایل merge.txt برای راهنما
    merge_instructions = f"برای ادغام فایل‌ها:\ncat {' '.join(os.path.basename(p) for p in parts)} > merged"
    worker.send_message(chat_id, merge_instructions)


def process_download_execute(job: dict) -> None:
    extra = job.get("extra", {})
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    mode = session["settings"]["default_download_mode"]  # store, stream, adm
    pack_zip = extra.get("pack_zip", False)
    direct_link = extra.get("direct_link")
    fpath = extra.get("file_path")
    fname = extra.get("filename", "file")

    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)

    if not direct_link and not fpath:
        worker.send_message(chat_id, "❌ اطلاعات دانلود موجود نیست.")
        _done_job(job)
        return

    try:
        if mode == "stream" and direct_link and not pack_zip:
            # دانلود جریانی و ارسال همزمان قطعات
            resp = requests.get(direct_link, stream=True, timeout=30)
            resp.raise_for_status()
            part_size = ZIP_PART_SIZE
            part_idx = 0
            buffer = b""
            save_dir = job_dir
            for chunk in resp.iter_content(chunk_size=8192):
                buffer += chunk
                while len(buffer) >= part_size:
                    part_data = buffer[:part_size]
                    buffer = buffer[part_size:]
                    part_idx += 1
                    part_name = f"{fname}.part{part_idx:03d}"
                    part_path = os.path.join(save_dir, part_name)
                    with open(part_path, "wb") as pf:
                        pf.write(part_data)
                    worker.send_document(chat_id, part_path, caption=f"🎬 {fname} (قطعه {part_idx})")
            # باقی‌مانده
            if buffer:
                part_idx += 1
                part_name = f"{fname}.part{part_idx:03d}"
                part_path = os.path.join(save_dir, part_name)
                with open(part_path, "wb") as pf:
                    pf.write(buffer)
                worker.send_document(chat_id, part_path, caption=f"🎬 {fname} (قطعه {part_idx})")
            worker.send_message(chat_id, f"برای ادغام قطعات از دستور `cat *part* > {fname}` استفاده کنید.")
            _done_job(job)
            return

        # دانلود کامل (برای store و adm)
        if direct_link:
            # دانلود با range برای adm
            size_resp = requests.head(direct_link, timeout=10)
            total_size = int(size_resp.headers.get("Content-Length", 0))
            if mode == "adm" and total_size > 0:
                segment_count = 9
                segment_size = math.ceil(total_size / segment_count)
                downloaded_parts = []
                for i in range(segment_count):
                    start = i * segment_size
                    end = min(start + segment_size - 1, total_size - 1)
                    if start >= total_size:
                        break
                    part_path = _download_segment(direct_link, job_dir, fname, start, end, {})
                    if part_path:
                        downloaded_parts.append(part_path)
                if downloaded_parts:
                    # ترکیب قطعات
                    merged_path = os.path.join(job_dir, fname)
                    with open(merged_path, "wb") as mf:
                        for pp in downloaded_parts:
                            with open(pp, "rb") as pf:
                                mf.write(pf.read())
                    final_file = merged_path
                else:
                    raise Exception("downloading segments failed")
            else:
                # دانلود معمولی
                resp = requests.get(direct_link, timeout=30)
                resp.raise_for_status()
                final_file = os.path.join(job_dir, fname)
                with open(final_file, "wb") as f:
                    f.write(resp.content)
        elif fpath:
            final_file = fpath
        else:
            raise Exception("no file")

        # ارسال با توجه به pack_zip
        if not os.path.isfile(final_file):
            worker.send_message(chat_id, "❌ فایل نهایی یافت نشد.")
            _done_job(job)
            return

        if pack_zip:
            _send_file_parts(chat_id, final_file, use_zip=True, label=fname)
        else:
            if mode in ("store", "adm"):
                _send_file_parts(chat_id, final_file, use_zip=False, label=fname)

        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در اجرای دانلود: {e}")
        job["status"] = "failed"
        storage.update_job(QUEUE_FILE, job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def _download_segment(url: str, job_dir: str, fname: str, start: int, end: int, headers: dict) -> Optional[str]:
    """دانلود یک بخش از فایل با هدر Range."""
    try:
        hdrs = headers.copy()
        hdrs["Range"] = f"bytes={start}-{end}"
        resp = requests.get(url, headers=hdrs, timeout=30)
        if resp.status_code not in (200, 206):
            return None
        part_name = f"{fname}.part{start}-{end}"
        part_path = os.path.join(job_dir, part_name)
        with open(part_path, "wb") as f:
            f.write(resp.content)
        return part_path
    except Exception:
        return None


def download_full_website(job: dict) -> None:
    chat_id = job["chat_id"]
    url = job["url"]
    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)

    try:
        # تلاش با wget
        command = [
            "wget",
            "--adjust-extension",
            "--span-hosts",
            "--convert-links",
            "--page-requisites",
            "--no-directories",
            "--directory-prefix", job_dir,
            "--recursive",
            "--level=1",
            "--accept", "html,css,js,jpg,jpeg,png,gif,svg,mp4,webm,pdf",
            "--user-agent", USER_AGENT,
            "--timeout=30",
            "--tries=2",
            url
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception("wget failed")

        # زیپ کردن پوشه
        zip_base = os.path.join(job_dir, "website")
        zip_path = f"{zip_base}.zip"
        shutil.make_archive(zip_base, 'zip', job_dir)
        _send_file_parts(chat_id, zip_path, use_zip=False, label="Website")
        _done_job(job)

    except Exception:
        # fallback: Playwright
        worker.send_message(chat_id, "wget در دسترس نیست، تلاش با مرورگر...")
        try:
            context = get_or_create_context(chat_id, incognito=False)
            page = context.new_page()
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            html = page.content()
            with open(os.path.join(job_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(html)
            page.screenshot(path=os.path.join(job_dir, "screenshot.png"), full_page=True)

            zip_base = os.path.join(job_dir, "website")
            zip_path = f"{zip_base}.zip"
            shutil.make_archive(zip_base, 'zip', job_dir)
            _send_file_parts(chat_id, zip_path, use_zip=False, label="Website (fallback)")
            _done_job(job)
        except Exception as e:
            worker.send_message(chat_id, f"❌ دانلود سایت ناموفق بود. ممکن است سایت در دسترس نباشد. ({e})")
            job["status"] = "failed"
            storage.update_job(QUEUE_FILE, job)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)


def process_record_job(job: dict) -> None:
    chat_id = job["chat_id"]
    url = job["url"]
    session = storage.get_session(chat_id)
    settings = session["settings"]
    rec_time = settings.get("record_time", 20)
    behavior = settings.get("record_behavior", "click")
    audio_enabled = settings.get("audio_enabled", False)
    video_format = settings.get("video_format", "webm")
    video_delivery = settings.get("video_delivery", "split")
    resolution = settings.get("video_resolution", "720p")

    w, h = ALLOWED_RESOLUTIONS.get(resolution, (1280, 720))
    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)

    audio_proc = None
    audio_path = ""
    audio_ok = False

    if audio_enabled:
        if setup_pulseaudio():
            audio_proc, audio_path = start_audio_capture(job_dir)

    rec_pw = None
    rec_browser = None
    context = None
    page = None

    try:
        rec_pw = sync_playwright().start()
        rec_browser = rec_pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--autoplay-policy=no-user-gesture-required"]
        )
        context = rec_browser.new_context(
            viewport={"width": w, "height": h},
            record_video_dir=job_dir,
            record_video_size={"width": w, "height": h}
        )
        page = context.new_page()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        if behavior == "scroll" or job.get("extra", {}).get("live_scroll"):
            smooth_scroll_to_video(page)

        vx, vy = find_video_center(page)
        page.mouse.click(vx, vy)
        page.wait_for_timeout(rec_time * 1000)

        page.close()
        context.close()

        # توقف ضبط صدا
        audio_ok = stop_audio_capture(audio_proc, audio_path) if audio_enabled else False

        # پیدا کردن فایل ویدیو
        video_files = [f for f in os.listdir(job_dir) if f.endswith(".webm")]
        if not video_files:
            raise Exception("فایل ویدیو ضبط نشد")
        video_path = os.path.join(job_dir, video_files[0])

        # تبدیل فرمت در صورت نیاز
        final_video_path = video_path
        if video_format != "webm":
            converted = os.path.join(job_dir, f"converted.{video_format}")
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", video_path,
                "-c:v", "libx264" if video_format == "mp4" else "copy",
                converted
            ]
            subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
            final_video_path = converted

        # ارسال ویدیو
        video_zip = (video_delivery == "zip")
        if os.path.isfile(final_video_path):
            _send_file_parts(chat_id, final_video_path, use_zip=video_zip, label="🎬 ویدیو")
        else:
            worker.send_message(chat_id, "❌ فایل ویدیو نهایی یافت نشد.")

        # ارسال صدا
        if audio_ok and os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
            _send_file_parts(chat_id, audio_path, use_zip=video_zip, label="🎵 صوت")

        _done_job(job, RECORD_QUEUE_FILE)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در ضبط: {e}")
        job["status"] = "failed"
        storage.update_job(RECORD_QUEUE_FILE, job)
    finally:
        if rec_browser:
            rec_browser.close()
        if rec_pw:
            rec_pw.stop()
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# اسکن و تحلیل
# ---------------------------------------------------------------------------

def _send_found_links_page(chat_id: int, links: List[Dict], page_num: int = 0) -> None:
    """نمایش صفحه‌بندی شدهٔ لینک‌های پیدا شده."""
    per_page = 10
    session = storage.get_session(chat_id)
    session["found_downloads"] = links
    session["found_downloads_page"] = page_num
    storage.set_session(chat_id, session)

    start = page_num * per_page
    page_links = links[start:start + per_page]

    cmds = []
    for idx, link in enumerate(page_links):
        h = hashlib.md5(link["url"].encode()).hexdigest()[:8]
        cmd = f"/d{h}"
        cmds.append(cmd)
        session.setdefault("text_links", {})[cmd] = link["url"]

    storage.set_session(chat_id, session)
    msg = "\n".join(f"{cmd}: {link.get('name', link['url'][:40])}" for cmd, link in zip(cmds, page_links))
    worker.send_message(chat_id, msg)


def handle_scan_downloads(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    url = session.get("browser_url")
    if not url:
        worker.send_message(chat_id, "⛔ ابتدا باید یک صفحه را با مرورگر باز کنید.")
        _done_job(job)
        return

    deep_mode = session["settings"].get("deep_scan_mode", "logical")
    context = get_or_create_context(chat_id, incognito=False)
    page = context.new_page()
    found = []

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        # استخراج لینک‌های مستقیم
        links_js = """
        () => {
            const urls = [];
            document.querySelectorAll('a[href]').forEach(a => {
                urls.push(a.href);
            });
            return urls;
        }
        """
        all_urls = page.evaluate(links_js)
        for u in all_urls:
            if is_direct_file_url(u) and not any(ad_domain in u for ad_domain in AD_DOMAINS):
                found.append({"url": u, "name": get_filename_from_url(u)})

        if not found and deep_mode == "everything":
            # تلاش برای کراول
            for u in all_urls[:5]:
                if not u.startswith("http"):
                    continue
                dl = crawl_for_download_link(u, max_depth=1, max_pages=5)
                if dl and dl not in [f["url"] for f in found]:
                    found.append({"url": dl, "name": get_filename_from_url(dl)})

        if not found:
            worker.send_message(chat_id, "🚫 هیچ فایلی یافت نشد.")
        else:
            _send_found_links_page(chat_id, found)

        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در جستجوی فایل‌ها: {e}")
        _done_job(job)
    finally:
        page.close()


def handle_scan_videos(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    url = session.get("browser_url")
    if not url:
        worker.send_message(chat_id, "⛔ ابتدا باید یک صفحه را با مرورگر باز کنید.")
        _done_job(job)
        return

    context = get_or_create_context(chat_id, incognito=False)
    page = context.new_page()
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        videos = scan_videos_smart(page)
        if not videos:
            worker.send_message(chat_id, "🚫 هیچ ویدیویی یافت نشد.")
        else:
            cmds = []
            for idx, v in enumerate(videos[:20]):
                h = hashlib.md5(v["url"].encode()).hexdigest()[:8]
                cmd = f"/o{h}"
                cmds.append(cmd)
                session.setdefault("text_links", {})[cmd] = v["url"]
            storage.set_session(chat_id, session)
            msg_lines = []
            for v, cmd in zip(videos[:20], cmds):
                url_short = v["url"][:60]
                msg_lines.append(f"{cmd}: {url_short}")
            worker.send_message(chat_id, "🎬 ویدیوهای یافت شده:\n" + "\n".join(msg_lines))
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در اسکن ویدیو: {e}")
        _done_job(job)
    finally:
        page.close()


def handle_extract_commands(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    all_links = session.get("browser_links") or []
    cmds = []
    for link in all_links:
        h = hashlib.md5(link["href"].encode()).hexdigest()[:8]
        cmd = f"/H{h}"
        cmds.append(cmd)
        session.setdefault("text_links", {})[cmd] = link["href"]
    storage.set_session(chat_id, session)

    lines = []
    for link, cmd in zip(all_links, cmds):
        text = (link.get("text") or link["href"])[:40]
        lines.append(f"{cmd}: {text}")
    worker.send_message(chat_id, "دستورات مستقیم:\n" + "\n".join(lines))
    _done_job(job)


def handle_smart_analyze(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    links = session.get("browser_links") or []
    categories = {"video": "🎥 ویدیوها", "file": "📁 فایل‌ها", "page": "🌐 صفحات"}
    grouped: Dict[str, List[Dict]] = {"video": [], "file": [], "page": []}
    for link in links:
        cat = categorize_url(link["href"])
        grouped.setdefault(cat, []).append(link)

    for cat, cat_name in categories.items():
        items = grouped.get(cat, [])[:10]
        if not items:
            continue
        cmds = []
        for item in items:
            h = hashlib.md5(item["href"].encode()).hexdigest()[:8]
            cmd = f"/H{h}"
            cmds.append(cmd)
            session.setdefault("text_links", {})[cmd] = item["href"]
        storage.set_session(chat_id, session)
        lines = []
        for item, cmd in zip(items, cmds):
            text = (item.get("text") or item["href"])[:30]
            lines.append(f"{cmd}: {text}")
        worker.send_message(chat_id, f"{cat_name}:\n" + "\n".join(lines))
    _done_job(job)


def handle_source_analyze(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    url = session.get("browser_url")
    if not url:
        worker.send_message(chat_id, "⛔ ابتدا صفحه‌ای را مرور کنید.")
        _done_job(job)
        return

    try:
        resp = requests.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        urls = set()
        for tag in soup.find_all(["a", "img", "source", "script", "link"]):
            for attr in ["href", "src", "data-url"]:
                val = tag.get(attr)
                if val:
                    urls.add(urljoin(url, val))
        # regex for scripts
        scripts = soup.find_all("script")
        for s in scripts:
            if s.string:
                matches = re.findall(r'(https?://[^\s"\'<>]+)', s.string)
                for m in matches:
                    urls.add(m)
        cmds = []
        url_list = list(urls)
        for u in url_list[:30]:
            h = hashlib.md5(u.encode()).hexdigest()[:8]
            cmd = f"/H{h}"
            cmds.append(cmd)
            session.setdefault("text_links", {})[cmd] = u
        storage.set_session(chat_id, session)
        lines = [f"{cmd}: {u[:40]}" for cmd, u in zip(cmds, url_list[:30])]
        worker.send_message(chat_id, "لینک‌های استخراج شده:\n" + "\n".join(lines))
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در تحلیل سورس: {e}")
        _done_job(job)


def handle_download_all_found(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    found = session.get("found_downloads")
    if not found:
        worker.send_message(chat_id, "⛔ ابتدا فایل‌ها را پیدا کنید.")
        _done_job(job)
        return

    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)
    files = []
    try:
        for item in found:
            url = item["url"]
            try:
                resp = requests.get(url, timeout=30)
                fname = get_filename_from_url(url)
                fpath = os.path.join(job_dir, fname)
                with open(fpath, "wb") as f:
                    f.write(resp.content)
                files.append(fpath)
            except Exception:
                continue
        if not files:
            worker.send_message(chat_id, "⛔ هیچ فایلی دانلود نشد.")
        else:
            zip_path = os.path.join(job_dir, "all_found.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in files:
                    zf.write(f, os.path.basename(f))
            _send_file_parts(chat_id, zip_path, use_zip=False, label="فایل‌های یافت شده")
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا: {e}")
        _done_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


def process_scan_job(job: dict) -> None:
    mode = job["mode"]
    if mode == "scan_downloads":
        handle_scan_downloads(job)
    elif mode == "scan_videos":
        handle_scan_videos(job)
    elif mode == "extract_commands":
        handle_extract_commands(job)
    elif mode == "smart_analyze":
        handle_smart_analyze(job)
    elif mode == "source_analyze":
        handle_source_analyze(job)
    elif mode == "download_all_found":
        handle_download_all_found(job)
    else:
        worker.send_message(job["chat_id"], f"⚠️ حالت اسکن نامعتبر: {mode}")
        _done_job(job)


def process_captcha_job(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    url = session.get("browser_url") or job.get("url")
    if not url:
        worker.send_message(chat_id, "⛔ URL نامعتبر.")
        _done_job(job)
        return

    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)
    context = get_or_create_context(chat_id, incognito=False)
    page = context.new_page()
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        # کلیک روی دکمه‌های احتمالی
        page.evaluate("""
            document.querySelectorAll('button, input[type="submit"], a[href*="download"]').forEach(el => el.click());
        """)
        page.wait_for_timeout(3000)

        spath = os.path.join(job_dir, "captcha_result.png")
        page.screenshot(path=spath, full_page=True)

        mode = session["settings"].get("browser_mode", "text")
        links, video_urls = extract_links(page, mode)

        session["state"] = "browsing"
        session["browser_url"] = page.url
        session["browser_links"] = (
            [{"type": t, "text": txt, "href": href} for t, txt, href in links]
            + [{"type": "video", "text": "🎬 ویدیو", "href": v} for v in video_urls]
        )
        session["browser_page"] = 0
        storage.set_session(chat_id, session)

        send_browser_page(chat_id, spath, session["browser_url"], 0)
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا در حل کپچا: {e}")
        _done_job(job)
    finally:
        page.close()
        shutil.rmtree(job_dir, ignore_errors=True)


def process_fullpage_screenshot(job: dict) -> None:
    chat_id = job["chat_id"]
    url = job.get("url") or storage.get_session(chat_id).get("browser_url")
    if not url:
        worker.send_message(chat_id, "⛔ URL موجود نیست.")
        _done_job(job)
        return

    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)
    context = get_or_create_context(chat_id, incognito=False)
    page = context.new_page()
    spath = os.path.join(job_dir, "screenshot.png")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.screenshot(path=spath, full_page=True)
        worker.send_document(chat_id, spath)
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا: {e}")
        _done_job(job)
    finally:
        page.close()
        shutil.rmtree(job_dir, ignore_errors=True)


def process_interactive_scan(job: dict) -> None:
    chat_id = job["chat_id"]
    session = storage.get_session(chat_id)
    url = session.get("browser_url")
    if not url:
        worker.send_message(chat_id, "⛔ ابتدا صفحه‌ای را باز کنید.")
        _done_job(job)
        return
    context = get_or_create_context(chat_id, incognito=False)
    page = context.new_page()
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        elements = page.evaluate("""
        () => {
            const items = [];
            document.querySelectorAll('input[type="text"], textarea, input:not([type])').forEach((el, i) => {
                const placeholder = el.placeholder || el.name || '';
                const id = el.id || '';
                const selector = id ? '#' + id : (el.name ? '[name="' + el.name + '"]' : '');
                items.push({index: i, placeholder, selector});
            });
            return items;
        }
        """)
        if not elements:
            worker.send_message(chat_id, "🔍 المان تعاملی یافت نشد.")
        else:
            session["interactive_elements"] = elements
            storage.set_session(chat_id, session)
            cmds = []
            for el in elements:
                h = hashlib.md5(f"{el['index']}".encode()).hexdigest()[:8]
                cmd = f"/t{h}"
                cmds.append(f"{cmd}: {el['placeholder'][:30]}")
                session.setdefault("text_links", {})[cmd] = el["index"]
            storage.set_session(chat_id, session)
            worker.send_message(chat_id, "المپان تعاملی:\n" + "\n".join(cmds))
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا: {e}")
        _done_job(job)
    finally:
        page.close()


def process_interactive_execute(job: dict) -> None:
    chat_id = job["chat_id"]
    extra = job.get("extra", {})
    element_index = extra.get("element_index")
    user_text = extra.get("user_text", "")
    session = storage.get_session(chat_id)
    elements = session.get("interactive_elements")
    if not elements or element_index is None:
        worker.send_message(chat_id, "⛔ ابتدا کاوشگر تعاملی را اجرا کنید.")
        _done_job(job)
        return

    target = None
    for el in elements:
        if el["index"] == element_index:
            target = el
            break
    if not target:
        worker.send_message(chat_id, "⛔ المان یافت نشد.")
        _done_job(job)
        return

    url = session.get("browser_url")
    if not url:
        worker.send_message(chat_id, "⛔ URL موجود نیست.")
        _done_job(job)
        return

    job_dir = f"jobs/{job['job_id']}"
    os.makedirs(job_dir, exist_ok=True)
    context = get_or_create_context(chat_id, incognito=False)
    page = context.new_page()
    spath = os.path.join(job_dir, "result.png")
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        selector = target["selector"]
        if not selector:
            worker.send_message(chat_id, "⛔ سلکتور نامعتبر.")
            _done_job(job)
            return
        page.fill(selector, user_text)
        page.evaluate("""
            () => {
                const el = document.querySelector('input[type="submit"], button[type="submit"], form button');
                if (el) el.click();
            }
        """)
        page.wait_for_timeout(2000)
        page.screenshot(path=spath, full_page=True)
        worker.send_document(chat_id, spath, caption="نتیجهٔ تعامل")
        _done_job(job)
    except Exception as e:
        worker.send_message(chat_id, f"❌ خطا: {e}")
        _done_job(job)
    finally:
        page.close()
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# نگاشت mode به تابع
# ---------------------------------------------------------------------------
JOB_HANDLERS = {
    "browser": process_browser_job,
    "browser_click": process_browser_job,
    "screenshot": process_screenshot_job,
    "2x_screenshot": process_screenshot_job,
    "4k_screenshot": process_screenshot_job,
    "download": process_download_job,
    "download_execute": process_download_execute,
    "blind_download": process_blind_download,
    "download_website": download_full_website,
    "scan_downloads": process_scan_job,
    "scan_videos": process_scan_job,
    "extract_commands": process_scan_job,
    "smart_analyze": process_scan_job,
    "source_analyze": process_scan_job,
    "download_all_found": process_scan_job,
    "captcha": process_captcha_job,
    "fullpage_screenshot": process_fullpage_screenshot,
    "interactive_scan": process_interactive_scan,
    "interactive_execute": process_interactive_execute,
    "record_video": process_record_job,
}
