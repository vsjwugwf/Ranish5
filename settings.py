import os
import sys

# ---------------------------------------------------------------------------
# ۱. توکن ربات
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("FATAL: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# ۲. آدرس API بله
# ---------------------------------------------------------------------------
API_BASE = f"https://tapi.bale.ai/bot{BOT_TOKEN}"

# ---------------------------------------------------------------------------
# ۳. تنظیمات زمان و اندازه
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = 30              # ثانیه – زمان انتظار پاسخ API
LONG_POLL_TIMEOUT = 50            # ثانیه – زمان long polling
ZIP_PART_SIZE = 19 * 1024 * 1024  # بایت – حداکثر هر تکه فایل (≈ ۱۹ مگابایت)
DOMAIN_DELAY = 0.5                # ثانیه – تأخیر بین درخواست‌های خزنده به هر دامنه
MAX_CRAWL_SIZE = 2 * 1024 * 1024 * 1024  # بایت – حداکثر حجم کل خزنده (۲ گیگابایت)

# ---------------------------------------------------------------------------
# ۴. شناسه ادمین
# ---------------------------------------------------------------------------
ADMIN_CHAT_ID = 46829437

# ---------------------------------------------------------------------------
# ۵. مسیرهای فایل
# ---------------------------------------------------------------------------
DATA_DIR = "data"
SUBSCRIPTIONS_FILE = os.path.join(DATA_DIR, "subscriptions.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
WORKERS_FILE = os.path.join(DATA_DIR, "workers.json")
RECORD_QUEUE_FILE = os.path.join(DATA_DIR, "record_queue.json")
SERVICE_DISABLED_FLAG = os.path.join(DATA_DIR, "service_disabled.flag")

# ---------------------------------------------------------------------------
# ۶. کدهای اشتراک پیش‌فرض
# ---------------------------------------------------------------------------
DEFAULT_CODES = {
    "bronze": [
        "B826USH", "B83HSIW", "B27627SGSH", "BSUWH8272", "B7272GS6",
        "BHSWG827", "BEJEJEI33", "BI3U37EG", "BEUEYE83HE", "BRONZE10",
    ],
    "plus": [
        "P9282UE", "PEIEUE7", "P3IEUEHD8D", "PEHEIEG883", "P3IEHE7SG",
    ],
    "pro": [
        "PR282UEH", "PR82HEBD8", "PRUEGEI3E", "PRHSU38EGD", "PR83HEDH",
    ],
}

# ---------------------------------------------------------------------------
# ۷. محدودیت‌های مصرف (LIMITS)
#    ساختار: LIMITS[سطح][سرویس] = (حداکثر تعداد, پنجرهٔ زمانی به ثانیه, حداکثر حجم به بایت یا None)
# ---------------------------------------------------------------------------
LIMITS = {
    "free": {
        "browser":            (2, 3600, None),
        "screenshot":         (2, 3600, None),
        "2x_screenshot":      (0, 3600, None),
        "4k_screenshot":      (0, 3600, None),
        "download":           (1, 3600, 10 * 1024 * 1024),
        "record_video":       (0, 3600, None),
        "scan_downloads":     (0, 3600, None),
        "scan_videos":        (0, 3600, None),
        "download_website":   (0, 3600, None),
        "extract_commands":   (0, 3600, None),
    },
    "bronze": {
        "browser":            (5, 3600, None),
        "screenshot":         (2, 3600, None),
        "2x_screenshot":      (1, 3600, None),
        "4k_screenshot":      (1, 3600, None),
        "download":           (2, 3600, 100 * 1024 * 1024),
        "record_video":       (1, 3600, None),
        "scan_downloads":     (1, 3600, None),
        "scan_videos":        (1, 3600, None),
        "download_website":   (0, 3600, None),
        "extract_commands":   (1, 3600, None),
    },
    "plus": {
        "browser":            (10, 3600, None),
        "screenshot":         (10, 3600, None),
        "2x_screenshot":      (5, 3600, None),
        "4k_screenshot":      (3, 3600, None),
        "download":           (5, 3600, 600 * 1024 * 1024),
        "record_video":       (3, 3600, None),
        "scan_downloads":     (2, 3600, None),
        "scan_videos":        (5, 3600, None),
        "download_website":   (1, 3600, None),
        "extract_commands":   (3, 3600, None),
    },
    "pro": {
        "browser":            (999, 3600, None),
        "screenshot":         (999, 3600, None),
        "2x_screenshot":      (999, 3600, None),
        "4k_screenshot":      (999, 3600, None),
        "download":           (999, 3600, None),
        "record_video":       (999, 3600, None),
        "scan_downloads":     (999, 3600, None),
        "scan_videos":        (999, 3600, None),
        "download_website":   (3, 86400, None),
        "extract_commands":   (999, 3600, None),
    },
}

# ---------------------------------------------------------------------------
# ۸. رزولوشن‌های مجاز ویدیو
# ---------------------------------------------------------------------------
ALLOWED_RESOLUTIONS = {
    "480p":  (854, 480),
    "720p":  (1280, 720),
    "1080p": (1920, 1080),
    "4k":    (3840, 2160),
}

# ---------------------------------------------------------------------------
# ۹. سطوح مجاز برای هر رزولوشن
# ---------------------------------------------------------------------------
RES_REQUIREMENTS = {
    "480p":  ["free", "bronze", "plus", "pro"],
    "720p":  ["bronze", "plus", "pro"],
    "1080p": ["plus", "pro"],
    "4k":    ["pro"],
}

# ---------------------------------------------------------------------------
# ۱۰. دامنه‌های تبلیغاتی (حداقل ۱۵ دامنه)
# ---------------------------------------------------------------------------
AD_DOMAINS = [
    "doubleclick.net",
    "googleadservices.com",
    "adservice.google.com",
    "adsrvr.org",
    "outbrain.com",
    "taboola.com",
    "adnxs.com",
    "rubiconproject.com",
    "pubmatic.com",
    "openx.net",
    "criteo.com",
    "casalemedia.com",
    "sovrn.com",
    "indexww.com",
    "advertising.com",
    "zedo.com",
    "revcontent.com",
    "mgid.com",
    "adzerk.net",
    "contextweb.com",
    "amazon-adsystem.com",
]

# ---------------------------------------------------------------------------
# ۱۱. کلمات کلیدی تبلیغاتی
# ---------------------------------------------------------------------------
BLOCKED_AD_KEYWORDS = [
    "ad",
    "banner",
    "popup",
    "sponsor",
    "track",
    "analytics",
    "advert",
    "popunder",
    "taboola",
    "monetize",
]

# ---------------------------------------------------------------------------
# ۱۲. User‑Agent
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
)

# ---------------------------------------------------------------------------
# ۱۳. تنظیمات پیش‌فرض کاربر
# ---------------------------------------------------------------------------
default_settings = {
    "record_time": 20,
    "default_download_mode": "store",       # store, stream, adm
    "browser_mode": "text",                # text, media, explorer
    "deep_scan_mode": "logical",           # logical, everything
    "record_behavior": "click",            # click, scroll, live
    "audio_enabled": False,
    "video_format": "webm",                # webm, mkv, mp4
    "incognito_mode": False,
    "video_delivery": "split",             # split, zip
    "video_resolution": "720p",
    "crawler_mode": "normal",              # normal, medium, deep
    "crawler_layers": 2,
    "crawler_limit": 0,                    # ۰ یعنی خودکار
    "crawler_max_time": 20,                # دقیقه
    "crawler_filters": {
        "image": True,
        "video": True,
        "archive": True,
        "pdf": True,
        "unknown": True,
    },
    "crawler_adblock": True,
    "crawler_sitemap": False,
    "crawler_priority": False,
    "crawler_js": False,
}

# ---------------------------------------------------------------------------
# ۱۴. نام حالت‌های خزنده → max_depth و default_pages
# ---------------------------------------------------------------------------
CRAWLER_MODES = {
    "normal": {"max_depth": 1, "default_pages": 200},
    "medium": {"max_depth": 2, "default_pages": 500},
    "deep":   {"max_depth": 3, "default_pages": 1200},
}

# ---------------------------------------------------------------------------
# ۱۵. تعداد کارگرها
# ---------------------------------------------------------------------------
WORKER_COUNT = 3           # دو نخ عمومی + یک نخ ضبط
RECORD_WORKER_ID = 2       # شناسهٔ کارگر ضبط
