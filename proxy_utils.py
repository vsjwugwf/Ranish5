import subprocess
import os
import time
import requests
import random
from typing import Optional

def _log(msg: str) -> None:
    """ثبت لاگ با تابع safe_log پروژه"""
    from utils import safe_log
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
        if not _which("warp-cli"):
            _log("warp-cli پیدا نشد")
            return False
        try:
            subprocess.run(["warp-cli", "register"], check=False, capture_output=True)
            subprocess.run(["warp-cli", "set-mode", "proxy"], check=False, capture_output=True)
            subprocess.run(["warp-cli", "set-proxy-port", "40000"], check=False, capture_output=True)
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
        if not _which("tor"):
            _log("tor پیدا نشد")
            return False
        try:
            subprocess.Popen(["tor"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            test = subprocess.run(
                ["curl", "--socks5", "localhost:9050", "--connect-timeout", "5", "-s", "https://check.torproject.org/"],
                capture_output=True, text=True
            )
            if test.returncode == 0 and "Congratulations" in test.stdout:
                _log("Tor با موفقیت راه‌اندازی شد")
                return True
            else:
                _log("Tor راه‌اندازی شد اما تست موفق نبود")
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
        if _which("warp-cli"):
            subprocess.run(["warp-cli", "disconnect"], check=False, capture_output=True)
            _log("Warp قطع شد")
    elif mode == "tor":
        for cmd in (["pkill", "tor"], ["killall", "tor"]):
            try:
                subprocess.run(cmd, check=False, capture_output=True)
                _log("Tor متوقف شد")
                break
            except Exception:
                continue


def get_free_proxy() -> Optional[str]:
    """
    دریافت یک پروکسی SOCKS5 رایگان از API عمومی و برگرداندن آن
    به صورت رشتهٔ "socks5://ip:port".
    در صورت بروز خطا یا پیدا نشدن پروکسی، None برمی‌گرداند.
    """
    try:
        url = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all&ssl=all&anonymity=all"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        lines = [line.strip() for line in resp.text.splitlines() if line.strip()]
        if lines:
            proxy = random.choice(lines)
            return f"socks5://{proxy}"
    except Exception as e:
        _log(f"خطا در دریافت پروکسی رایگان: {e}")
    return None


def _which(prog: str) -> bool:
    """بررسی موجود بودن یک برنامه در PATH"""
    try:
        from shutil import which
        return which(prog) is not None
    except ImportError:
        return False
