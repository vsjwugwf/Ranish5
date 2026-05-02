import os
import time
import queue
import zipfile
import re
import shutil
import subprocess
from urllib.parse import urlparse, urljoin, unquote
from typing import Optional, List, Tuple, Set, Dict

import requests
from bs4 import BeautifulSoup

from settings import ZIP_PART_SIZE, USER_AGENT
import proxy_utils

# ---------------------------------------------------------------------------
# ۱. تشخیص لینک مستقیم فایل
# ---------------------------------------------------------------------------
def is_direct_file_url(url: str) -> bool:
    """
    تعیین می‌کند که آیا *url* مستقیماً به یک فایل قابل دانلود اشاره دارد.
    معیار: پسوند مسیر (بدون کوئری) جزو فهرست شناخته‌شده باشد، یا
    پسوندی با حروف، ارقام، _ و - تا طول ۱۰ کاراکتر داشته باشد.
    """
    known_extensions = {
        "zip", "rar", "7z", "pdf", "mp4", "mkv", "avi", "mp3", "exe", "apk",
        "dmg", "iso", "tar", "gz", "bz2", "xz", "whl", "deb", "rpm", "msi",
        "pkg", "appimage", "jar", "war", "py", "sh", "bat", "run", "bin",
        "img", "mov", "flv", "wmv", "webm", "ogg", "wav", "flac", "csv",
        "docx", "pptx", "m3u8"
    }

    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if "." not in filename:
        return False

    ext = filename.rsplit(".", 1)[-1].lower()

    if ext in known_extensions:
        return True

    if re.fullmatch(r"[a-zA-Z0-9_-]{1,10}", ext):
        return True

    return False


# ---------------------------------------------------------------------------
# ۱-۲. بررسی منطقی بودن دانلود (بر اساس url و اندازه)
# ---------------------------------------------------------------------------
def is_logical_download(url: str, size_bytes: Optional[int] = None) -> bool:
    """
    بررسی می‌کند که آیا URL منطقاً یک فایل قابل دانلود است.
    معیار: یا is_direct_file_url True باشد، یا حجم فایل (اگر مشخص باشد)
    بزرگ‌تر از ۱ مگابایت باشد.
    """
    if is_direct_file_url(url):
        return True
    if size_bytes and size_bytes > 1024 * 1024:
        return True
    return False


# ---------------------------------------------------------------------------
# ۲. استخراج نام فایل از URL
# ---------------------------------------------------------------------------
def get_filename_from_url(url: str) -> str:
    """
    نام فایل را از انتهای مسیر *url* استخراج می‌کند.
    در صورت نبود نام معتبر، مقدار "downloaded_file" برگردانده می‌شود.
    """
    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if not filename or "." not in filename:
        return "downloaded_file"
    return filename


# ---------------------------------------------------------------------------
# ۳. تقسیم فایل به بخش‌های ZIP_PART_SIZE
# ---------------------------------------------------------------------------
def split_file_binary(file_path: str, prefix: str, ext: str) -> List[str]:
    """
    فایل *file_path* را به قطعه‌های با اندازه *ZIP_PART_SIZE* تقسیم می‌کند.
    *prefix* پیشوند نام و *ext* پسوند قطعه‌ها را مشخص می‌کند.
    اگر *ext* برابر ".zip" باشد، شماره‌گذاری به فرمت zip چندبخشی
    (مانند prefix.zip.001) انجام می‌شود.
    """
    if not os.path.isfile(file_path):
        return []

    dir_name = os.path.dirname(file_path)
    part_paths: List[str] = []
    chunk_size = ZIP_PART_SIZE

    with open(file_path, "rb") as f:
        i = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            i += 1
            if ext == ".zip":
                part_name = f"{prefix}.zip.{i:03d}"
            else:
                part_name = f"{prefix}.part{i:03d}{ext}"
            part_full = os.path.join(dir_name, part_name)
            with open(part_full, "wb") as part_file:
                part_file.write(chunk)
            part_paths.append(part_full)

    return part_paths


# ---------------------------------------------------------------------------
# ۴. ایجاد فایل zip و در صورت نیاز تقسیم آن
# ---------------------------------------------------------------------------
def create_zip_and_split(src: str, base: str, compression: str = "normal") -> List[str]:
    """
    فایل *src* را در یک فایل zip قرار می‌دهد. اگر حجم آرشیو از
    ZIP_PART_SIZE بیشتر شد، آن را تقسیم کرده و فایل اصلی zip
    را حذف می‌کند. در غیر این صورت همان تک‌فایل zip برگردانده می‌شود.
    *compression* می‌تواند "normal" یا "high" باشد.
    """
    if not os.path.isfile(src):
        return []

    dir_name = os.path.dirname(src)
    zip_path = os.path.join(dir_name, f"{base}.zip")

    # اگر فایل اصلی خودش ZIP است و مسیر خروجی با مسیر ورودی یکی می‌شود،
    # یک نام موقت برای ZIP انتخاب کن تا فایل اصلی از بین نرود
    if os.path.abspath(zip_path) == os.path.abspath(src):
        zip_path = os.path.join(dir_name, f"{base}_tmp_{int(time.time())}.zip")

    if compression == "high":
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(src, arcname=os.path.basename(src))
    else:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, arcname=os.path.basename(src))

    zip_size = os.path.getsize(zip_path)
    if zip_size <= ZIP_PART_SIZE:
        return [zip_path]

    parts = split_file_binary(zip_path, prefix=base, ext=".zip")
    os.remove(zip_path)
    return parts


# ---------------------------------------------------------------------------
# ۵. خزندهٔ کوچک برای یافتن لینک مستقیم فایل
# ---------------------------------------------------------------------------
def crawl_for_download_link(
    start_url: str,
    max_depth: int = 1,
    max_pages: int = 10,
    timeout_seconds: int = 30,
    proxies: Optional[Dict] = None,
) -> Optional[str]:
    """
    از *start_url* شروع کرده و حداکثر *max_pages* صفحه را تا
    عمق *max_depth* بررسی می‌کند. اولین لینکی که مستقیماً به
    یک فایل اشاره کند (طبق is_direct_file_url) برگردانده می‌شود.
    اگر چیزی پیدا نشد یا زمان *timeout_seconds* گذشت، None.
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    q: queue.Queue[Tuple[str, int]] = queue.Queue()
    q.put((start_url, 0))
    visited: Set[str] = set()
    pages_visited = 0
    start_time = time.time()

    while not q.empty():
        if time.time() - start_time > timeout_seconds:
            break
        if pages_visited >= max_pages:
            break

        cur_url, depth = q.get()
        if cur_url in visited:
            continue
        if depth > max_depth:
            continue

        visited.add(cur_url)
        pages_visited += 1

        try:
            resp = session.get(cur_url, timeout=10, proxies=proxies)
        except Exception:
            continue

        final_url = resp.url
        if is_direct_file_url(final_url):
            return final_url

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            continue

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            continue

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            full_url = urljoin(final_url, href)
            if is_direct_file_url(full_url):
                return full_url
            if depth + 1 <= max_depth:
                q.put((full_url, depth + 1))

    return None


# ---------------------------------------------------------------------------
# ۶. دسته‌بندی نوع URL برای خزنده
# ---------------------------------------------------------------------------
def categorize_url(url: str, content_type: Optional[str] = None) -> str:
    """
    نوع محتوای URL را تشخیص می‌دهد:
    "image" / "video" / "pdf" / "archive" / "page"
    اولویت با *content_type* داده می‌شود؛ در غیر آن پسوند مسیر بررسی می‌شود.
    """
    if content_type:
        ct = content_type.lower()
        if "image" in ct:
            return "image"
        if "video" in ct or "mpegurl" in ct:
            return "video"
        if "pdf" in ct:
            return "pdf"

    path = unquote(urlparse(url).path)
    filename = os.path.basename(path)
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        image_exts = {"jpg", "jpeg", "png", "gif", "svg", "webp", "bmp", "ico"}
        video_exts = {"mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m3u8", "mpd"}
        archive_exts = {"zip", "rar", "7z", "tar", "gz", "exe", "apk", "dmg", "iso", "whl"}
        if ext in image_exts:
            return "image"
        if ext in video_exts:
            return "video"
        if ext == "pdf":
            return "pdf"
        if ext in archive_exts:
            return "archive"

    return "page"


# ---------------------------------------------------------------------------
# ۷. اعتبارسنجی سادهٔ URL
# ---------------------------------------------------------------------------
def is_valid_url(url: str) -> bool:
    """بررسی می‌کند که *url* با http:// یا https:// شروع شود."""
    return url.startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# ۸. لاگ ساده با زمان
# ---------------------------------------------------------------------------
def safe_log(msg: str) -> None:
    """پیام *msg* را همراه با زمان جاری چاپ می‌کند."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# ۹. فشرده‌سازی با 7z (در صورت موجود بودن)
# ---------------------------------------------------------------------------
def compress_7z(file_path: str) -> str:
    """
    فایل ورودی را با 7z با حداکثر فشرده‌سازی فشرده می‌کند.
    خروجی: مسیر فایل 7z. در صورت نبود 7z، فایل zip معمولی با
    compresslevel=9 ساخته می‌شود.
    """
    out_path = file_path + ".7z"
    if shutil.which("7z"):
        try:
            subprocess.run(
                ["7z", "a", "-mx=9", out_path, file_path],
                check=True, capture_output=True, timeout=300
            )
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
        except Exception:
            pass
    # fallback به zip معمولی با فشرده‌سازی بالا
    zp = file_path + ".zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(file_path, os.path.basename(file_path))
    return zp


# ---------------------------------------------------------------------------
# ۱۰. دریافت دیکشنری پروکسی بر اساس حالت
# ---------------------------------------------------------------------------
def get_proxy_dict(proxy_mode: str) -> Optional[Dict[str, str]]:
    """
    بر اساس *proxy_mode*، دیکشنری پروکسی مناسب برای requests
    را برمی‌گرداند.
    *proxy_mode* می‌تواند "off", "warp", "tor", "free" باشد.
    """
    proxy_mode = proxy_mode or "off"
    if proxy_mode == "off":
        return None
    elif proxy_mode == "warp":
        return {"http": "socks5://127.0.0.1:40000", "https": "socks5://127.0.0.1:40000"}
    elif proxy_mode == "tor":
        return {"http": "socks5://127.0.0.1:9050", "https": "socks5://127.0.0.1:9050"}
    elif proxy_mode == "free":
        try:
            free_proxy = proxy_utils.get_free_proxy()
            if free_proxy:
                return {"http": free_proxy, "https": free_proxy}
        except Exception:
            pass
        return None
    return None
