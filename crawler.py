import asyncio
import threading
import time
import uuid
import os
import json
import csv
import shutil
import hashlib
import itertools
from urllib.parse import urljoin, urlparse
from typing import Optional, Dict, List, Set, Tuple, Any, Callable, Awaitable

import aiohttp
from bs4 import BeautifulSoup

from settings import (
    USER_AGENT,
    DOMAIN_DELAY,
    MAX_CRAWL_SIZE,
    AD_DOMAINS,
    BLOCKED_AD_KEYWORDS,
    CRAWLER_MODES,
    ZIP_PART_SIZE,
)
from utils import is_direct_file_url, categorize_url, get_filename_from_url, is_valid_url, safe_log

# ---------------------------------------------------------------------------
# تابع ورودی همگام – توسط jobs.py صدا زده می‌شود
# ---------------------------------------------------------------------------
def start_crawl(
    chat_id: int,
    url: str,
    settings: Dict[str, Any],
    progress_callback: Callable[..., Awaitable[None]],
    stop_event: threading.Event,
) -> None:
    """
    خزنده را در یک نخ جداگانه با asyncio event loop جدید راه‌اندازی می‌کند.
    """
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        crawler = WildCrawler(chat_id, url, settings, progress_callback, stop_event)
        try:
            loop.run_until_complete(crawler.run())
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# کلاس خزنده
# ---------------------------------------------------------------------------
class WildCrawler:
    """خزندهٔ وب ناهمگام (asyncio در یک نخ جدا)."""

    def __init__(
        self,
        chat_id: int,
        start_url: str,
        settings: Dict[str, Any],
        progress_callback: Callable[..., Awaitable[None]],
        stop_event: threading.Event,
    ):
        self.chat_id = chat_id
        self.start_url = start_url
        self.stop_event = stop_event
        self.progress_callback = progress_callback

        # تنظیمات خزنده
        self.mode = settings.get("crawler_mode", "normal")
        self.layers = settings.get("crawler_layers", 2)
        self.max_pages = settings.get("crawler_limit", 0)  # 0 = خودکار
        self.max_time_minutes = settings.get("crawler_max_time", 20)
        self.file_filters = settings.get("crawler_filters", {
            "image": True, "video": True, "archive": True, "pdf": True, "unknown": True
        })
        self.adblock = settings.get("crawler_adblock", True)
        self.use_sitemap = settings.get("crawler_sitemap", False)
        self.priority = settings.get("crawler_priority", False)
        self.js_mode = settings.get("crawler_js", False)

        # عمق و صفحات از CRAWLER_MODES (در صورت خودکار بودن)
        mode_cfg = CRAWLER_MODES.get(self.mode, CRAWLER_MODES["normal"])
        self.max_depth = mode_cfg["max_depth"]
        if self.max_pages == 0:
            self.max_pages = mode_cfg["default_pages"]

        # آمار و وضعیت
        self.visited: Set[str] = set()
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue() if self.priority else asyncio.Queue()
        self._priority_counter = itertools.count()  # برای شکستن تساوی در PriorityQueue
        self.total_pages = 0
        self.total_files = 0
        self.total_errors = 0
        self.total_size = 0
        self.start_time = time.time()
        self.max_time = self.max_time_minutes * 60

        self.domain_last_request: Dict[str, float] = {}

        # پوشهٔ نتایج
        self.results_dir = f"crawl_results_{uuid.uuid4().hex[:8]}"
        self._prepare_directories()

        # فایل CSV
        self.csv_path = os.path.join(self.results_dir, "crawl_log.csv")
        self.csv_file = None
        self.csv_writer = None

        # فایل ادامهٔ خزنده
        self.resume_file = f"data/crawler_{self.chat_id}.json"
        os.makedirs("data", exist_ok=True)

        self.session: Optional[aiohttp.ClientSession] = None

    # -----------------------------------------------------------------------
    # راه‌اندازی پوشه‌ها
    # -----------------------------------------------------------------------
    def _prepare_directories(self):
        os.makedirs(self.results_dir, exist_ok=True)
        for layer in range(1, self.layers + 1):
            layer_dir = os.path.join(self.results_dir, f"layer_{layer}")
            for sub in ["images", "videos", "files", "unknown", "texts"]:
                os.makedirs(os.path.join(layer_dir, sub), exist_ok=True)

    # -----------------------------------------------------------------------
    # بارگذاری / ذخیرهٔ وضعیت
    # -----------------------------------------------------------------------
    def _load_resume(self):
        if not os.path.exists(self.resume_file):
            return
        try:
            with open(self.resume_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.visited.update(data.get("visited", []))
            self.total_pages = data.get("total_pages", 0)
            self.total_files = data.get("total_files", 0)
            self.total_size = data.get("total_size", 0)
            self.total_errors = data.get("total_errors", 0)
        except Exception:
            pass

    async def _save_resume(self):
        data = {
            "visited": list(self.visited),
            "total_pages": self.total_pages,
            "total_files": self.total_files,
            "total_size": self.total_size,
            "total_errors": self.total_errors,
        }
        try:
            with open(self.resume_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # ارسال پیام به کاربر (از طریق callback)
    # -----------------------------------------------------------------------
    async def _notify(self, text: str):
        try:
            await self.progress_callback(text)
        except Exception:
            pass

    async def _send_progress(self):
        elapsed = time.time() - self.start_time
        msg = (
            f"📊 خزنده: {self.total_pages} صفحه، "
            f"{self.total_files} فایل ({self.total_size / 1024 / 1024:.1f} MB)، "
            f"خطا: {self.total_errors} – زمان: {elapsed:.0f} ثانیه"
        )
        await self._notify(msg)

    # -----------------------------------------------------------------------
    # اجرای اصلی
    # -----------------------------------------------------------------------
    async def run(self):
        await self._notify("🚀 خزنده شروع شد...")

        # باز کردن CSV
        self.csv_file = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["url", "status", "content_type", "type", "layer", "depth", "note"])

        self.session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        )

        try:
            # ادامهٔ از جلسهٔ قبل (در صورت وجود)
            self._load_resume()

            # Sitemap
            if self.use_sitemap:
                await self._fetch_sitemap()

            # افزودن URL شروع
            if self.start_url not in self.visited:
                await self._enqueue(self.start_url, depth=0, layer=1)

            # کارگرهای همزمان
            workers = [self._worker() for _ in range(5)]
            await asyncio.gather(*workers)

        except Exception as e:
            await self._notify(f"❌ خطای مرگبار در خزنده: {e}")
        finally:
            await self._finalize()

    # -----------------------------------------------------------------------
    # کارگر (اصلاح‌شده – task_done در finally)
    # -----------------------------------------------------------------------
    async def _worker(self):
        while not self.stop_event.is_set():
            # توقف در صورت رسیدن به محدودیت‌ها (بدون گرفتن آیتم از صف)
            if self.total_pages >= self.max_pages:
                break
            if (time.time() - self.start_time) > self.max_time:
                self.stop_event.set()
                break

            try:
                # گرفتن آیتم از صف
                if self.priority:
                    item = await asyncio.wait_for(self.queue.get(), timeout=1)
                    _, _, url, depth, layer = item
                else:
                    item = await asyncio.wait_for(self.queue.get(), timeout=1)
                    url, depth, layer = item
            except asyncio.TimeoutError:
                if self.queue.empty():
                    break
                continue

            # از اینجا به بعد باید حتماً task_done صدا زده شود
            try:
                # بررسی تکراری و عمق
                if url in self.visited or depth > self.max_depth:
                    continue

                self.visited.add(url)
                self.total_pages += 1
                await self._log_csv(url, "processing", "", "page", layer, depth, "")

                # مسدود کردن تبلیغات
                if self._is_ad(url):
                    await self._log_csv(url, "blocked", "", "ad", layer, depth, "تبلیغ")
                    continue

                # تأخیر دامنه
                await self._respect_delay(url)

                # پردازش URL
                await self._process_url(url, depth, layer)

                # گزارش دوره‌ای
                if self.total_pages % 10 == 0:
                    await self._send_progress()
                    await self._save_resume()

            finally:
                self.queue.task_done()

    # -----------------------------------------------------------------------
    # افزودن به صف
    # -----------------------------------------------------------------------
    async def _enqueue(self, url: str, depth: int, layer: int):
        if self.priority:
            base_prio = depth * 10
            # فایل‌های مستقیم اولویت بالاتری دارند (عدد کمتر)
            if is_direct_file_url(url):
                base_prio -= 5
            await self.queue.put((base_prio, next(self._priority_counter), url, depth, layer))
        else:
            await self.queue.put((url, depth, layer))

    # -----------------------------------------------------------------------
    # پردازش یک URL
    # -----------------------------------------------------------------------
    async def _process_url(self, url: str, depth: int, layer: int):
        try:
            async with self.session.get(url, timeout=15) as resp:
                status = resp.status
                if status != 200:
                    self.total_errors += 1
                    await self._log_csv(url, status, "", "error", layer, depth, f"HTTP {status}")
                    return

                ct = resp.headers.get("Content-Type", "")
                if "text/html" in ct:
                    html = await resp.text()
                    await self._handle_html(url, html, depth, layer)
                else:
                    # اصلاح: ارسال resp به _handle_file
                    await self._handle_file(url, ct, layer, resp=resp)
        except asyncio.TimeoutError:
            self.total_errors += 1
            await self._log_csv(url, 0, "", "timeout", layer, depth, "Timeout")
        except Exception as e:
            self.total_errors += 1
            await self._log_csv(url, 0, "", "error", layer, depth, str(e))

    # -----------------------------------------------------------------------
    # مدیریت HTML
    # -----------------------------------------------------------------------
    async def _handle_html(self, url: str, html: str, depth: int, layer: int):
        soup = BeautifulSoup(html, "html.parser")

        # ذخیرهٔ متن‌های صفحه
        text_name = hashlib.md5(url.encode()).hexdigest()[:12] + ".html"
        text_path = os.path.join(self.results_dir, f"layer_{layer}", "texts", text_name)
        try:
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        # استخراج لینک‌ها
        tags_attrs = [
            ("a", "href"),
            ("link", "href"),
            ("script", "src"),
            ("img", "src"),
            ("iframe", "src"),
            ("source", "src"),
        ]
        for tag, attr in tags_attrs:
            for element in soup.find_all(tag):
                val = element.get(attr)
                if val:
                    full_url = urljoin(url, val)
                    if not is_valid_url(full_url):
                        continue
                    await self._classify_and_act(full_url, depth, layer)

        # ویژگی data-url
        for element in soup.find_all(attrs={"data-url": True}):
            full_url = urljoin(url, element["data-url"])
            if is_valid_url(full_url):
                await self._classify_and_act(full_url, depth, layer)

    # -----------------------------------------------------------------------
    # دسته‌بندی و تصمیم‌گیری برای یک لینک
    # -----------------------------------------------------------------------
    async def _classify_and_act(self, url: str, depth: int, layer: int):
        if is_direct_file_url(url):
            cat = categorize_url(url)
            # فیلتر: اگر نوع غیرفعال بود، رد کن
            filter_key = cat if cat in self.file_filters else "unknown"
            if not self.file_filters.get(filter_key, True):
                await self._log_csv(url, "filtered", "", cat, layer, depth, "filtered")
                return
            # دانلود
            await self._download_and_save(url, cat, layer)
        else:
            # افزودن به صف در صورت رعایت عمق و لایه
            if depth < self.max_depth:
                cur_domain = urlparse(url).hostname or ""
                start_domain = urlparse(self.start_url).hostname or ""
                new_layer = layer if cur_domain == start_domain else layer + 1
                if new_layer <= self.layers:
                    await self._enqueue(url, depth + 1, new_layer)

    # -----------------------------------------------------------------------
    # مدیریت فایل (مستقیم، غیر HTML) – اصلاح‌شده با پاس دادن resp
    # -----------------------------------------------------------------------
    async def _handle_file(self, url: str, content_type: str, layer: int, resp=None):
        cat = categorize_url(url, content_type)
        filter_key = cat if cat in self.file_filters else "unknown"
        if not self.file_filters.get(filter_key, True):
            await self._log_csv(url, resp.status if resp else 0, content_type, cat, layer, 0, "filtered")
            return
        await self._download_and_save(url, cat, layer, resp=resp)

    # -----------------------------------------------------------------------
    # دانلود و ذخیرهٔ فایل – اصلاح‌شده با exception دقیق‌تر
    # -----------------------------------------------------------------------
    async def _download_and_save(self, url: str, cat: str, layer: int, resp=None):
        fname = get_filename_from_url(url)
        if "." not in fname:
            ext_map = {"image": ".img", "video": ".vid", "pdf": ".pdf", "archive": ".arc", "unknown": ".bin"}
            fname += ext_map.get(cat, ".bin")

        save_dir = os.path.join(self.results_dir, f"layer_{layer}", f"{cat}s")
        file_path = os.path.join(save_dir, fname)
        # جلوگیری از بازنویسی با اضافه کردن شماره
        base, ext = os.path.splitext(fname)
        counter = 1
        while os.path.exists(file_path):
            file_path = os.path.join(save_dir, f"{base}_{counter}{ext}")
            counter += 1

        try:
            if resp is None:
                resp = await self.session.get(url, timeout=30)
                resp.raise_for_status()

            taille = 0
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    taille += len(chunk)
                    if self.total_size + taille > MAX_CRAWL_SIZE:
                        # حذف فایل ناقص و توقف
                        f.close()
                        os.remove(file_path)
                        self.stop_event.set()
                        await self._notify("⛔ حجم کل خزنده به حد مجاز رسید. خزنده متوقف شد.")
                        break
                    f.write(chunk)
                else:
                    self.total_files += 1
                    self.total_size += taille
                    await self._log_csv(url, resp.status, resp.content_type, cat, layer, 0, "")

                    # ثبت در links.txt همان پوشه
                    links_file = os.path.join(save_dir, "links.txt")
                    with open(links_file, "a", encoding="utf-8") as lf:
                        lf.write(url + "\n")
        except (aiohttp.ClientError, asyncio.TimeoutError, IOError) as e:
            self.total_errors += 1
            await self._log_csv(url, 0, "", cat, layer, 0, f"download error: {e}")

    # -----------------------------------------------------------------------
    # مسدودسازی تبلیغات
    # -----------------------------------------------------------------------
    def _is_ad(self, url: str) -> bool:
        if not self.adblock:
            return False
        domain = urlparse(url).hostname or ""
        if any(ad_domain in domain for ad_domain in AD_DOMAINS):
            return True
        url_lower = url.lower()
        if any(kw in url_lower for kw in BLOCKED_AD_KEYWORDS):
            return True
        return False

    # -----------------------------------------------------------------------
    # تأخیر بین درخواست‌ها به یک دامنه
    # -----------------------------------------------------------------------
    async def _respect_delay(self, url: str):
        domain = urlparse(url).hostname
        if not domain:
            return
        now = time.time()
        last = self.domain_last_request.get(domain, 0)
        gap = now - last
        if gap < DOMAIN_DELAY:
            await asyncio.sleep(DOMAIN_DELAY - gap)
        self.domain_last_request[domain] = time.time()

    # -----------------------------------------------------------------------
    # واکشی Sitemap
    # -----------------------------------------------------------------------
    async def _fetch_sitemap(self):
        sitemap_urls = [
            urljoin(self.start_url, "/sitemap.xml"),
            urljoin(self.start_url, "/sitemap_index.xml"),
        ]
        for sm_url in sitemap_urls:
            try:
                async with self.session.get(sm_url, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    xml = await resp.text()
                    soup = BeautifulSoup(xml, "xml")
                    for loc in soup.find_all("loc"):
                        loc_url = loc.text.strip()
                        if loc_url:
                            await self._enqueue(loc_url, depth=0, layer=1)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # CSV
    # -----------------------------------------------------------------------
    async def _log_csv(self, url, status, content_type, typ, layer, depth, note=""):
        if self.csv_writer:
            self.csv_writer.writerow([url, status, content_type, typ, layer, depth, note])
            self.csv_file.flush()

    # -----------------------------------------------------------------------
    # پایان کار – ساخت گزارش و فایل ZIP (اصلاح ارسال فایل نهایی)
    # -----------------------------------------------------------------------
    async def _finalize(self):
        if self.csv_file:
            self.csv_file.flush()
            self.csv_file.close()

        report_path = self._generate_html_report()
        # ساخت ZIP از کل پوشه
        zip_base = os.path.join(os.path.dirname(self.results_dir), os.path.basename(self.results_dir))
        zip_path = shutil.make_archive(zip_base, 'zip', self.results_dir)

        # اطلاع‌رسانی به jobs.py برای ارسال فایل – مستقیماً callback دو آرگومانه
        await self.progress_callback("__FINAL_ZIP__", file_path=zip_path)

        # پاک‌سازی
        shutil.rmtree(self.results_dir, ignore_errors=True)

        if self.session:
            await self.session.close()

    # -----------------------------------------------------------------------
    # گزارش HTML
    # -----------------------------------------------------------------------
    def _generate_html_report(self) -> str:
        counts = {"image": 0, "video": 0, "archive": 0, "pdf": 0, "unknown": 0}
        try:
            with open(self.csv_path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader)
                for row in reader:
                    typ = row[3]
                    if typ in counts:
                        counts[typ] += 1
        except Exception:
            pass

        total_files = sum(counts.values())
        colors = {
            "image": "#ff6384",
            "video": "#36a2eb",
            "archive": "#cc65fe",
            "pdf": "#ffce56",
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

        report_path = os.path.join(self.results_dir, "report.html")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)
        return report_path
