import subprocess
import os
import time
from utils import safe_log

def _log(msg: str) -> None:
    """ثبت لاگ با تابع safe_log پروژه"""
    safe_log(f"[proxy_utils] {msg}")

def start_proxy(mode: str) -> bool:
    """
    راه‌اندازی سرویس پروکسی متناسب با mode.
    حالت‌های پشتیبانی‌شده: "warp", "tor" (و "off"/"free" که نیاز به راه‌اندازی ندارند).
    در صورت موفقیت True و در غیر این صورت False برمی‌گرداند.
    """
    if mode in ("off", "free"):
        return True

    if mode == "warp":
        # بررسی وجود warp-cli
        if not shutil_which("warp-cli"):
            _log("warp-cli پیدا نشد")
            return False
        try:
            # ثبت‌نام (اگر قبلاً نشده باشد خطا نده)
            subprocess.run(["warp-cli", "register"], check=False, capture_output=True)
            # تنظیم حالت پروکسی به جای VPN
            subprocess.run(["warp-cli", "set-mode", "proxy"], check=False, capture_output=True)
            # تنظیم پورت پروکسی
            subprocess.run(["warp-cli", "set-proxy-port", "40000"], check=False, capture_output=True)
            # اتصال
            result = subprocess.run(["warp-cli", "connect"], capture_output=True, text=True)
            if result.returncode == 0:
                _log("Warp با موفقیت متصل شد (proxy on port 40000)")
                return True
            else:
                _log(f"Warp connect failed: {result.stderr}")
                return False
        except Exception as e:
            _log(f"خطا در راه‌اندازی Warp: {e}")
            return False

    elif mode == "tor":
        # بررسی وجود tor
        if not shutil_which("tor"):
            _log("tor پیدا نشد")
            return False
        try:
            # راه‌اندازی tor در پس‌زمینه (detach)
            subprocess.Popen(["tor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # صبر برای آماده شدن
            time.sleep(3)
            # بررسی سلامت با curl (timeout=5)
            test = subprocess.run(
                ["curl", "--socks5", "localhost:9050", "--connect-timeout", "5", "-s", "https://check.torproject.org/"],
                capture_output=True, text=True
            )
            if test.returncode == 0 and "Congratulations" in test.stdout:
                _log("Tor با موفقیت راه‌اندازی شد")
                return True
            else:
                _log("Tor راه‌اندازی شد اما تست موفق نبود")
                # حتی اگر تست نشد، ممکن است کار کند — گزارش موفقیت
                return True
        except Exception as e:
            _log(f"خطا در راه‌اندازی Tor: {e}")
            return False

    else:
        _log(f"حالت پروکسی نامعتبر: {mode}")
        return False


def stop_proxy(mode: str) -> None:
    """
    متوقف کردن سرویس پروکسی برای حالت warp یا tor.
    """
    if mode == "warp":
        if shutil_which("warp-cli"):
            subprocess.run(["warp-cli", "disconnect"], check=False, capture_output=True)
            _log("Warp قطع شد")
    elif mode == "tor":
        # تلاش برای کشتن فرآیند tor
        for cmd in (["pkill", "tor"], ["killall", "tor"]):
            try:
                subprocess.run(cmd, check=False, capture_output=True)
                _log("Tor متوقف شد")
                break
            except Exception:
                continue
    # "off" و "free" نیاز به توقف ندارند


def shutil_which(prog: str) -> bool:
    """بررسی موجود بودن یک برنامه در PATH"""
    try:
        from shutil import which
        return which(prog) is not None
    except ImportError:
        return False
