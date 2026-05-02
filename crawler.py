import os
import time
import uuid
import json
import csv
import shutil
import re
import hashlib
import threading
import traceback
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Route

from settings import (
    USER_AGENT,
    DOMAIN_DELAY,
    MAX_CRAWL_SIZE,
    AD_DOMAINS,
    BLOCKED_AD_KEYWORDS,
    CRAWLER_MODES,
    ZIP_PART_SIZE,
)
from utils import (
    is_direct_file_url,
    categorize_url,
    get_filename_from_url,
    is_valid_url,
    safe_log,
    split_file_binary,
)

# ---------------------------------------------------------------------------
# تابع ورودی همگام – توسط jobs.py / main.py صدا زده می‌شود
# ---------------------------------------------------------------------------
def start_crawl(
    chat_id: int,
    url: str,
    settings: Dict[str, Any],
    progress_callback,
    stop_event: threading.Event,
) -> None:
    """
    خزنده را در یک نخ جداگانه راه‌اندازی می‌کند.
    """
    def _run():
        crawler = Crawler(chat_id, url, settings, progress_callback, stop_event)
        try:
            crawler.run()
        except Exception as e:
            safe_log(f"خزنده با خطای مرگبار متوقف شد: {e}")
            traceback.print_exc()
            try:
                crawler._invoke_callback(f"❌ خزنده با خطا متوقف شد: {e}")
            except:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# کلاس خزنده همگام
# ---------------------------------------------------------------------------
class Crawler:
    """خزندهٔ وب همگام با Playwright و Requests"""

    def __init__(
        self,
        chat_id: int,
        start_url: str,
        settings: Dict[str, Any],
        progress_callback,
        stop_event: threading.Event,
    ):
        self.chat_id = chat_id
        self.start_url = start_url
        self.stop_event = stop_event
        self.progress_callback = progress_callback

        # تنظیمات خزنده
        self.mode = settings.get("crawler_mode", "normal")
        self.layers = settings.get("crawler_layers", 2)
        self.max_pages = settings.get("crawler_limit", 0)
        self.max_time_minutes = settings.get("crawler_max_time", 20)
        self.max_time = self.max_time_minutes * 60
        self.file_filters = settings.get("crawler_filters", {
            "image": True, "video": True, "archive": True, "pdf": True, "unknown": True
        })
        self.adblock = settings.get("crawler_adblock", True)
        self.use_sitemap = settings.get("crawler_sitemap", False)
        self.priority = settings.get("crawler_priority", False)
        self.js_mode = settings.get("crawler_js", False)

        # ★ تنظیمات پروکسی و فشرده‌سازی
        self.proxy_mode = settings.get("proxy_mode", "off")
        self.compression = settings.get("compression_level", "normal")

        # عمق و صفحات از CRAWLER_MODES
        mode_cfg = CRAWLER_MODES.get(self.mode, CRAWLER_MODES["normal"])
        self.max_depth = mode_cfg["max_depth"]
        if self.max_pages == 0:
            self.max_pages = mode_cfg["default_pages"]

        # آمار و وضعیت
        self.visited: Set[str] = set()
        self.queue: List[Tuple[str, int]] = []   # (url, depth)
        self.total_pages = 0
        self.total_clickables = 0
        self.total_files = 0
        self.total_errors = 0
        self.total_size = 0

        self.successful_ops = 0
        self.failed_ops = 0
        self.images_count = 0
        self.videos_count = 0
        self.files_count = 0
        self.unknown_count = 0

        self.start_time = time.time()
        self.domain_last_request: Dict[str, float] = {}

        # پوشهٔ نتایج
        self.results_dir = f"crawl_results_{uuid.uuid4().hex[:8]}"
        self._prepare_directories()

        # فایل‌های گزارش
        self.errors_log_path = os.path.join(self.results_dir, "full_report", "errors.log")
        self.all_txt_path = os.path.join(self.results_dir, "full_report", "all.txt")
        self.csv_path = os.path.join(self.results_dir, "full_report", "crawl_log.csv")
        self.report_html_path = os.path.join(self.results_dir, "full_report", "report.html")

        self.errors_log = None
        self.csv_file = None
        self.csv_writer = None

        # Playwright و Session (در run ایجاد می‌شوند)
        self.pw = None
        self.browser = None
        self.context = None
        self.session = None

    # -------------------------------------------------------------------
    # کمک‌کننده برای فراخوانی callback (همگام یا async)
    # -------------------------------------------------------------------
    def _invoke_callback(self, msg: str, file_path: str = None):
        try:
            cb = self.progress_callback
            if asyncio.iscoroutinefunction(cb):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(cb(msg, file_path=file_path))
                finally:
                    loop.close()
            else:
                cb(msg, file_path=file_path)
        except Exception as e:
            safe_log(f"crawler callback error: {e}")

    # -------------------------------------------------------------------
    # آماده‌سازی پوشه‌ها
    # -------------------------------------------------------------------
    def _prepare_directories(self):
        os.makedirs(self.results_dir, exist_ok=True)
        os.makedirs(os.path.join(self.results_dir, "full_report"), exist_ok=True)
        for layer in range(1, self.layers + 1):
            layer_dir = os.path.join(self.results_dir, f"layer_{layer}")
            os.makedirs(os.path.join(layer_dir, "clickable"), exist_ok=True)
            os.makedirs(os.path.join(layer_dir, "downloads"), exist_ok=True)
            os.makedirs(os.path.join(layer_dir, "screenshots"), exist_ok=True)

    # -------------------------------------------------------------------
    # ثبت خطا در فایل errors.log
    # -------------------------------------------------------------------
    def _log_error(self, msg: str):
        if self.errors_log:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.errors_log.write(f"[{timestamp}] {msg}\n")
            self.errors_log.flush()

    # -------------------------------------------------------------------
    # احترام به تأخیر دامنه
    # -------------------------------------------------------------------
    def _respect_delay(self, url: str):
        domain = urlparse(url).hostname
        if not domain:
            return
        now = time.time()
        last = self.domain_last_request.get(domain, 0)
        if now - last < DOMAIN_DELAY:
            time.sleep(DOMAIN_DELAY - (now - last))
        self.domain_last_request[domain] = time.time()

    # -------------------------------------------------------------------
    # مسدودساز تبلیغات
    # -------------------------------------------------------------------
    def _adblock_router(self, route: Route):
        url = route.request.url
        try:
            domain = urlparse(url).hostname or ""
        except:
            route.continue_()
            return
        if any(ad_domain in domain for ad_domain in AD_DOMAINS):
            route.abort()
            return
        url_lower = url.lower()
        if any(kw in url_lower for kw in BLOCKED_AD_KEYWORDS):
            route.abort()
            return
        route.continue_()

    # -------------------------------------------------------------------
    # دریافت دیکشنری پروکسی
    # -------------------------------------------------------------------
    def _get_proxy(self) -> Optional[Dict]:
        if self.proxy_mode == "off":
            return None
        elif self.proxy_mode == "warp":
            return {"server": "socks5://127.0.0.1:40000"}
        elif self.proxy_mode == "tor":
            return {"server": "socks5://127.0.0.1:9050"}
        elif self.proxy_mode == "free":
            return {"server": "socks5://127.0.0.1:1080"}
        return None

    # -------------------------------------------------------------------
    # راند ۱: اسکن المان‌های قابل کلیک
    # -------------------------------------------------------------------
    def _scan_clickables(self, url: str, layer: int) -> Optional[List[Dict]]:
        self._respect_delay(url)
        page = None
        try:
            page = self.context.new_page()
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            js = """
            () => {
                const items = [];
                const seen = new Set();
                function add(type, text, selector, href) {
                    const key = selector + href;
                    if (!seen.has(key)) {
                        seen.add(key);
                        items.push({type, text, selector, href});
                    }
                }
                document.querySelectorAll('a[href]').forEach(a => {
                    let href = a.href;
                    if (href && href.startsWith('http')) {
                        const text = (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80);
                        add('link', text, getUniqueSelector(a), href);
                    }
                });
                document.querySelectorAll('button, input[type="submit"]').forEach(el => {
                    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80);
                    let href = el.getAttribute('formaction') || '';
                    try { if (href) href = new URL(href, document.baseURI).href; } catch(e) {}
                    add('button', text, getUniqueSelector(el), href || '');
                });
                const allElements = document.querySelectorAll('*');
                for (const el of allElements) {
                    if (el.tagName === 'A' || el.tagName === 'BUTTON' || el.tagName === 'INPUT') continue;
                    const style = window.getComputedStyle(el);
                    if (style.cursor === 'pointer' || el.getAttribute('role') === 'button') {
                        const text = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80);
                        let href = '';
                        const onclick = el.getAttribute('onclick') || '';
                        const match = onclick.match(/location\\.href=['"]([^'"]+)['"]/) || onclick.match(/window\\.open\\(['"]([^'"]+)['"]\\)/);
                        if (match) href = match[1];
                        add('clickable', text, getUniqueSelector(el), href);
                    }
                }
                function getUniqueSelector(el) {
                    if (el.id) return '#' + el.id;
                    let path = '';
                    while (el && el.nodeType === 1) {
                        let selector = el.tagName.toLowerCase();
                        if (el.className) selector += '.' + Array.from(el.classList).join('.');
                        const parent = el.parentElement;
                        if (parent) {
                            const siblings = Array.from(parent.children).filter(e => e.tagName === el.tagName);
                            if (siblings.length > 1) {
                                const index = siblings.indexOf(el) + 1;
                                selector += `:nth-child(${index})`;
                            }
                        }
                        path = (path ? selector + ' > ' + path : selector);
                        el = el.parentElement;
                        if (el === document.body) break;
                    }
                    return path;
                }
                return items;
            }
            """
            clickables = page.evaluate(js)

            layer_clickable_dir = os.path.join(self.results_dir, f"layer_{layer}", "clickable")
            os.makedirs(layer_clickable_dir, exist_ok=True)          # ★ تضمین وجود پوشه
            json_path = os.path.join(layer_clickable_dir, "clickable.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(clickables, f, ensure_ascii=False, indent=2)

            screenshot_path = os.path.join(self.results_dir, f"layer_{layer}", "screenshots", "page.png")
            os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)  # ★ تضمین وجود پوشه
            page.screenshot(path=screenshot_path, full_page=True)

            self.total_clickables += len(clickables)
            self._invoke_callback(f"🔍 راند ۱ لایه {layer} به پایان رسید. {len(clickables)} المان قابل کلیک یافت شد.")
            return clickables
        except Exception as e:
            self.total_errors += 1
            self.failed_ops += 1
            msg = f"خطا در راند ۱ لایه {layer} ({url}): {e}"
            self._log_error(msg)
            self._invoke_callback(f"❌ {msg}")
            return None
        finally:
            if page:
                page.close()

    # -------------------------------------------------------------------
    # راند ۲: اسکن و دانلود محتوا
    # -------------------------------------------------------------------
    def _scan_downloads(self, url: str, layer: int):
        self._respect_delay(url)
        page = None
        try:
            page = self.context.new_page()
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            downloads_dir = os.path.join(self.results_dir, f"layer_{layer}", "downloads")
            os.makedirs(downloads_dir, exist_ok=True)                # ★ تضمین وجود پوشه
            links_list_path = os.path.join(downloads_dir, "links.txt")
            links_file = open(links_list_path, "w", encoding="utf-8")

            downloaded_count = 0
            proxy = self._get_proxy()
            proxies = {"http": proxy["server"], "https": proxy["server"]} if proxy else None

            # ----- مرحله ۱: لینک‌های مستقیم از DOM -----
            dom_urls = page.evaluate("""() => {
                const urls = [];
                document.querySelectorAll('a[href], img[src], video[src], audio[src], source[src], link[href], script[src], iframe[src]').forEach(el => {
                    const val = el.href || el.src;
                    if (val && val.startsWith('http')) urls.push(val);
                });
                return [...new Set(urls)];
            }""")

            for link in dom_urls:
                if not is_valid_url(link):
                    continue
                if is_direct_file_url(link):
                    cat = categorize_url(link)
                    filter_key = cat if cat in self.file_filters else "unknown"
                    if not self.file_filters.get(filter_key, True):
                        continue
                    if self._download_file(link, downloads_dir, proxies):
                        downloaded_count += 1
                        if cat == "image":
                            self.images_count += 1
                        elif cat == "video":
                            self.videos_count += 1
                        elif cat in ("archive", "pdf"):
                            self.files_count += 1
                        else:
                            self.unknown_count += 1
                        links_file.write(link + "\n")

            # ----- مرحله ۲: شنود APIهای شبکه -----
            api_calls = set()
            def capture(response):
                ct = response.headers.get("content-type", "")
                if any(kw in ct for kw in ["json", "xml", "text/plain", "octet-stream"]) or "api" in response.url.lower():
                    api_calls.add(response.url)

            page.on("response", capture)
            page.wait_for_timeout(5000)
            page.remove_listener("response", capture)

            api_download_dir = os.path.join(downloads_dir, "api")
            os.makedirs(api_download_dir, exist_ok=True)
            for api_url in api_calls:
                if self._download_file(api_url, api_download_dir, proxies, guess_extension=True):
                    downloaded_count += 1
                    self.unknown_count += 1
                    links_file.write(api_url + "\n")

            # ----- مرحله ۳: URLهای مخفی از اسکریپت‌ها -----
            hidden_urls = page.evaluate("""() => {
                const urls = [];
                const regex = /https?:\\/\\/[^\\s"']+\\.(?:mp4|webm|mkv|avi|mov|flv|wmv|m3u8|mpd|zip|apk|pdf|rar|7z|tar|gz)/gi;
                document.querySelectorAll('script').forEach(script => {
                    const text = script.textContent || '';
                    let m;
                    while ((m = regex.exec(text)) !== null) {
                        urls.push(m[0]);
                    }
                });
                return [...new Set(urls)];
            }""")
            for hid_url in hidden_urls:
                if not is_valid_url(hid_url):
                    continue
                cat = categorize_url(hid_url)
                if not self.file_filters.get(cat, True):
                    continue
                if self._download_file(hid_url, downloads_dir, proxies):
                    downloaded_count += 1
                    if cat == "video":
                        self.videos_count += 1
                    elif cat in ("archive", "pdf"):
                        self.files_count += 1
                    else:
                        self.unknown_count += 1
                    links_file.write(hid_url + "\n")

            links_file.close()
            self.total_files += downloaded_count
            self._invoke_callback(f"📥 راند ۲ لایه {layer} به پایان رسید. {downloaded_count} فایل دانلود شد.")
        except Exception as e:
            self.total_errors += 1
            self.failed_ops += 1
            msg = f"خطا در راند ۲ لایه {layer} ({url}): {e}"
            self._log_error(msg)
            self._invoke_callback(f"❌ {msg}")
        finally:
            if page:
                page.close()
            if 'links_file' in locals() and not links_file.closed:
                links_file.close()

    # -------------------------------------------------------------------
    # دانلود یک فایل و ذخیره
    # -------------------------------------------------------------------
    def _download_file(self, file_url: str, save_dir: str, proxies: Optional[Dict] = None, guess_extension: bool = False) -> bool:
        try:
            fname = get_filename_from_url(file_url)
            if '.' not in fname and guess_extension:
                try:
                    head = requests.head(file_url, timeout=10, allow_redirects=True, proxies=proxies)
                    ct = head.headers.get("content-type", "")
                    if "json" in ct:
                        ext = ".json"
                    elif "xml" in ct:
                        ext = ".xml"
                    elif "text/plain" in ct:
                        ext = ".txt"
                    elif "octet-stream" in ct:
                        ext = ".bin"
                    else:
                        ext = ".dat"
                    fname += ext
                except:
                    fname += ".dat"

            fpath = os.path.join(save_dir, fname)
            base, ext = os.path.splitext(fname)
            counter = 1
            while os.path.exists(fpath):
                fpath = os.path.join(save_dir, f"{base}_{counter}{ext}")
                counter += 1

            resp = requests.get(file_url, stream=True, timeout=30, proxies=proxies)
            resp.raise_for_status()
            size = 0
            with open(fpath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if self.total_size + len(chunk) > MAX_CRAWL_SIZE:
                        f.close()
                        os.remove(fpath)
                        self._invoke_callback("⛔ حجم کل خزنده به حد مجاز رسید. دانلود فایل جدید متوقف شد.")
                        return False
                    f.write(chunk)
                    self.total_size += len(chunk)
                    size += len(chunk)
            return True
        except Exception as e:
            self._log_error(f"دانلود ناموفق {file_url}: {e}")
            return False

    # -------------------------------------------------------------------
    # ارسال پیام پیشرفت
    # -------------------------------------------------------------------
    def _send_progress(self, msg: str):
        self._invoke_callback(msg)

    # -------------------------------------------------------------------
    # حلقهٔ اصلی خزنده
    # -------------------------------------------------------------------
    def run(self):
        # راه‌اندازی Playwright و Requests با پروکسی
        proxy = self._get_proxy()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context_kwargs = {"viewport": {"width": 390, "height": 844}}
        if proxy:
            context_kwargs["proxy"] = proxy
        self.context = self.browser.new_context(**context_kwargs)
        if self.adblock:
            self.context.route("**/*", self._adblock_router)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        # باز کردن فایل‌های گزارش
        os.makedirs(os.path.join(self.results_dir, "full_report"), exist_ok=True)
        self.errors_log = open(self.errors_log_path, "w", encoding="utf-8")
        self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["url", "status", "content_type", "type", "layer", "depth", "note"])

        try:
            self._invoke_callback("🚀 خزنده شروع شد...")

            if self.use_sitemap:
                self._fetch_sitemap()

            self.queue.append((self.start_url, 1))

            while self.queue:
                if self.stop_event.is_set():
                    break
                if time.time() - self.start_time > self.max_time:
                    self._invoke_callback("⏰ زمان خزنده به پایان رسید.")
                    break
                if self.total_pages >= self.max_pages:
                    self._invoke_callback("📄 حد مجاز صفحات بازدیدشده رسید.")
                    break

                url, depth = self.queue.pop(0)
                if url in self.visited or depth > self.max_depth:
                    continue

                self.visited.add(url)
                self.total_pages += 1
                self._log_csv(url, "processing", "", "page", depth, "processing")

                clickables = self._scan_clickables(url, depth)
                self._scan_downloads(url, depth)

                if clickables is not None:
                    self.successful_ops += 1
                else:
                    self.failed_ops += 1

                if clickables and depth < self.max_depth:
                    for item in clickables:
                        href = item.get("href", "")
                        if href and href.startswith("http") and href not in self.visited:
                            # تبدیل URL نسبی به مطلق با استفاده از صفحهٔ فعلی
                            absolute_href = urljoin(url, href)
                            self.queue.append((absolute_href, depth + 1))

            self._finalize()
        except Exception as e:
            self._invoke_callback(f"❌ خطای بحرانی: {e}")
            self._log_error(f"FATAL: {e}")
        finally:
            if self.errors_log:
                self.errors_log.close()
            if self.csv_file:
                self.csv_file.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
            if self.session:
                self.session.close()

    # -------------------------------------------------------------------
    # واکشی Sitemap
    # -------------------------------------------------------------------
    def _fetch_sitemap(self):
        sitemap_urls = [
            urljoin(self.start_url, "/sitemap.xml"),
            urljoin(self.start_url, "/sitemap_index.xml"),
        ]
        for sm_url in sitemap_urls:
            try:
                resp = self.session.get(sm_url, timeout=10)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "xml")
                for loc in soup.find_all("loc"):
                    loc_url = loc.text.strip()
                    if loc_url:
                        self.queue.append((loc_url, 1))
            except:
                pass

    # -------------------------------------------------------------------
    # نوشتن یک ردیف در CSV
    # -------------------------------------------------------------------
    def _log_csv(self, url, status, content_type, typ, layer, depth, note=""):
        if self.csv_writer:
            self.csv_writer.writerow([url, status, content_type, typ, layer, depth, note])
            self.csv_file.flush()

    # -------------------------------------------------------------------
    # پایان کار – گزارش‌گیری و ارسال فایل نهایی
    # -------------------------------------------------------------------
    def _finalize(self):
        # نوشتن all.txt
        total_ops = self.successful_ops + self.failed_ops
        with open(self.all_txt_path, "w", encoding="utf-8") as f:
            f.write(f"Total Operations: {total_ops}\n")
            f.write(f"Successful: {self.successful_ops}\n")
            f.write(f"Failed: {self.failed_ops}\n")
            f.write(f"Images: {self.images_count}\n")
            f.write(f"Videos: {self.videos_count}\n")
            f.write(f"Files (archive/pdf): {self.files_count}\n")
            f.write(f"Unknown: {self.unknown_count}\n")
            f.write(f"Total Pages Visited: {self.total_pages}\n")
            f.write(f"Total Download Size: {self.total_size / 1024 / 1024:.2f} MB\n")

        if self.errors_log and not self.errors_log.closed:
            if os.path.getsize(self.errors_log_path) == 0:
                self.errors_log.write("No errors.\n")
            self.errors_log.close()

        self._generate_html_report()

        # ساخت ZIP نهایی با توجه به فشرده‌سازی
        if self.compression == "high":
            # ابتدا یک zip معمولی
            zip_base = os.path.join(os.path.dirname(self.results_dir), os.path.basename(self.results_dir))
            zip_path = shutil.make_archive(zip_base, 'zip', self.results_dir)
            try:
                from utils import compress_7z
                final_path = compress_7z(zip_path)
            except:
                final_path = zip_path
        else:
            zip_base = os.path.join(os.path.dirname(self.results_dir), os.path.basename(self.results_dir))
            final_path = shutil.make_archive(zip_base, 'zip', self.results_dir)

        self._invoke_callback("__FINAL_ZIP__", file_path=final_path)

        # پاک‌سازی
        shutil.rmtree(self.results_dir, ignore_errors=True)

    # -------------------------------------------------------------------
    # گزارش HTML
    # -------------------------------------------------------------------
    def _generate_html_report(self):
        counts = {
            "image": self.images_count,
            "video": self.videos_count,
            "archive/pdf": self.files_count,
            "unknown": self.unknown_count,
        }
        total_files = sum(counts.values())
        colors = {
            "image": "#ff6384",
            "video": "#36a2eb",
            "archive/pdf": "#cc65fe",
            "unknown": "#4bc0c0",
        }

        svg_parts = []
        cumulative = 0
        r = 80
        cx, cy = 100, 100
        for cat, count in counts.items():
            if count == 0:
                continue
            angle = (count / total_files) * 360
            start_angle = cumulative
            end_angle = cumulative + angle
            cumulative = end_angle

            from math import radians, sin, cos
            x1 = cx + r * cos(radians(start_angle - 90))
            y1 = cy + r * sin(radians(start_angle - 90))
            x2 = cx + r * cos(radians(end_angle - 90))
            y2 = cy + r * sin(radians(end_angle - 90))
            large_arc = 1 if angle > 180 else 0

            d = f"M{cx},{cy} L{x1},{y1} A{r},{r} 0 {large_arc},1 {x2},{y2} Z"
            svg_parts.append(f'<path d="{d}" fill="{colors[cat]}" stroke="white" stroke-width="1"/>')

        svg = f"""
        <svg width="300" height="200" viewBox="0 0 200 200">
            <rect width="200" height="200" fill="#f7f7f7" rx="10"/>
            {''.join(svg_parts)}
        </svg>
        """

        stats_rows = "".join(
            f"<tr><td style='color:{colors[cat]}'>{cat}</td><td>{count}</td></tr>"
            for cat, count in counts.items() if count > 0
        )

        html = f"""<!DOCTYPE html>
<html lang="fa">
<head><meta charset="UTF-8"><title>گزارش خزنده</title></head>
<body dir="rtl">
<h1>📊 گزارش خزنده</h1>
<table border="1">
    <tr><th>نوع</th><th>تعداد</th></tr>
    {stats_rows}
</table>
<p><b>مجموع فایل‌ها:</b> {total_files}</p>
{svg}
</body>
</html>"""

        with open(self.report_html_path, "w", encoding="utf-8") as f:
            f.write(html)
