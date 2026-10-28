#@title 📡 TeleScrape — اسکرپر تلگرام (وب + ترمینال)
# -*- coding: utf-8 -*-
"""
TeleScrape — یک فایل، همه‌ی TeleScrape: موتور + لایه‌ی وبِ موبایل‌پسند (هم‌نام ریپو)

اجرا در Colab (جزئیات کامل در COLAB.md):
    !rm -f telescrape.py telescrape.py.*
    !wget -q -O telescrape.py "https://raw.githubusercontent.com/Personalmo/telescrape/main/telescrape.py?t=$(date +%s)"
    !python telescrape.py --selftest
سپس (فیکس NB-21) در یک سلول پایتون:
    import sys; sys.modules.pop("telescrape", None)
    import telescrape; telescrape.colab_launch()
سکرت‌ها را از Notebook secrets در کرنل می‌خواند (وضعیت هر کدام را می‌گوید)، به‌صورت
env (فقط RAM) به سرور پاس می‌دهد، سرور را اجرا می‌کند و از خودِ سرور تایید می‌گیرد.

حالت‌ها:
    python telescrape.py                    → رابط وب (پیش‌فرض — موبایل‌پسند)
    python telescrape.py --cli              → حالت ترمینال کامل
    python telescrape.py --selftest         → تست خودکار: اول موتور، بعد وب
    python telescrape.py --selftest-engine  → فقط تست خودکار موتور

* موتور و لایه‌ی وب در همین یک فایلند؛ ts = همین ماژول است و
  کارهای تلگرام روی loop پس‌زمینه‌ی موتور اجرا می‌شوند (asyncio.run_coroutine_threadsafe).
* credentialها فقط در RAM می‌مانند؛ هرگز در پاسخ API یا لاگ نمی‌آیند.
* Secretها از Colab Secrets خوانده می‌شوند: TG_API_ID / TG_API_HASH / TG_PHONE /
  TG_2FA_PASSWORD / TS_ENC_KEY — اگر Secretی موجود باشد، فیلدش در فرم اصلاً
  نشان داده نمی‌شود و مقدار از همان Secret خوانده می‌شود.
* فیکس NB-15: Secretها فقط در پروسه‌ی کرنلِ نوت‌بوک قابل خواندن‌اند؛ سلول لانچر
  COLAB.md آنها را می‌خواند و به‌صورت env به سرور پاس می‌دهد (TS_WEB_CHILD=1).
"""

# ───────────────────────── Colab form (dropdowns on phone) ─────────────────────────
LANG = "fa"               #@param ["fa", "en"]
SPEED = "normal"          #@param ["gentle", "normal", "fast"]
WIDTH = 34                #@param {type:"slider", min:24, max:60, step:1}
PER_PAGE = 10             #@param {type:"integer"}
USE_DRIVE = False         #@param {type:"boolean"}
# فیکس NB-12: بکاپ رمزنگاری‌شده‌ی session روی Drive — دیگر لازم نیست هر بار لاگین کنید
SESSION_BACKUP = True     #@param {type:"boolean"}
CLEAR_SCREEN = True       #@param {type:"boolean"}

import os, sys, re, json, time, zipfile, asyncio, subprocess, getpass, random, shutil
import textwrap, threading, collections, errno, unicodedata
import sqlite3                            # فیکس NB-12: snapshot سازگار session با API استاندارد backup
import concurrent.futures as cf
from datetime import datetime

if sys.version_info < (3, 9):        # asyncio.to_thread (used in scrape) needs 3.9+
    print("⛔ Python 3.9+ required / پایتون ۳.۹ یا بالاتر لازم است")
    raise SystemExit(1)


def _ensure(pkg, mod=None, required=True):
    """Import a package, pip-installing it if missing. required=False -> best effort only."""
    try:
        __import__(mod or pkg)
    except ImportError:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])
        except Exception as e:
            if required:
                print(f"⛔ pip install failed for {pkg}: {e}")
                print("⛔ نصب بسته ناموفق بود؛ اینترنت و دسترسی pip را بررسی و دوباره اجرا کنید.")
                raise SystemExit(1)


_ensure("telethon>=1.40", mod="telethon")
_ensure("cryptg", required=False)      # optional: fast AES downloads; Telethon uses it if present
_ensure("cryptography", required=False)  # فیکس NB-12: رمزنگاری بکاپ session (AES-256-GCM)
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except Exception:      # فیکس NB-12: نبودِ کتابخانه فقط قابلیت بکاپ را خاموش می‌کند، نه اجرا را
    HAS_CRYPTO = False

from telethon import TelegramClient, utils
from telethon.errors import (FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
                             PhoneCodeExpiredError, PasswordHashInvalidError, ServerError,
                             TimedOutError)
from telethon.tl.types import MessageMediaWebPage, PeerChannel, PeerChat, PeerUser, Channel

IN_COLAB = "google.colab" in sys.modules
TEXT_EXT = (".txt", ".md", ".json")
MB = 1024 * 1024
STALL_SECS = 120          # a download with no progress for this long is cancelled and retried
BACKOFF_BASE = 5.0        # retry backoff: base * 2**attempt seconds (max 60)

# Secret names (first one found wins).
SECRET_NAMES = {
    "api_id": ("TG_API_ID", "API_ID"),
    "api_hash": ("TG_API_HASH", "API_HASH"),
    "phone": ("TG_PHONE", "PHONE"),
    "pwd": ("TG_2FA_PASSWORD",),
    "enc_key": ("TS_ENC_KEY", "ENC_KEY"),  # فیکس NB-12: رمز بکاپ — الزامی نیست، فقط لحظه‌ی بکاپ/ریستور خوانده می‌شود
}

# Cooldown profiles. Seconds are multiplied by an adaptive factor that grows after FloodWait.
#   batch = pause between 100-message fetches, dl = pause after each downloaded file,
#   n / mb = take a long rest after this many files / this many MB, rest = length of that rest.
PROFILES = {
    "gentle": {"batch": (2.0, 4.0), "dl": (1.5, 3.5), "n": 40, "mb": 300, "rest": (45, 90)},
    "normal": {"batch": (1.0, 2.0), "dl": (0.6, 1.5), "n": 80, "mb": 600, "rest": (20, 45)},
    "fast": {"batch": (0.4, 0.8), "dl": (0.2, 0.5), "n": 150, "mb": 1500, "rest": (8, 15)},
}

# ───────────────────────────────── strings ─────────────────────────────────
S = {
    "fa": {
        "title": "📡 اسکرپر تلگرام",
        "hint": "کادر ورودی پایین خروجی است؛ اگر دیده نشد اسکرول کنید.",
        "secrets_missing": "⚠️ Secret پیدا نشد: {names}\n🔑 در Colab بسازید و «Notebook access» را روشن کنید. فعلاً دستی وارد کنید.",
        "legacy_removed": "🧹 فایل قدیمی API Hash پاک شد (دیگر لازم نیست).",
        "old_session": "🚨 سشنِ خامِ قدیمی روی Drive پیدا شد — این فایل = لاگین کاملِ رمزنگاری‌نشده‌ی اکانت شماست. اگر بکاپ رمزنگاری‌شده دارید، همین را دستی پاک کنید:\n{p}",
        "prev_closed": "🧹 کلاینتِ اجرای قبلی بسته شد (جلوگیری از دو اتصال روی یک session).",
        "api_id": "API ID: ",
        "api_hash": "API Hash (نامرئی): ",
        "bad_hash": "API Hash باید ۳۲ کاراکتر hex باشد.",
        "phone": "شماره (+98912...): ",
        "code": "کد تلگرام (r=ارسال دوباره): ",
        "pwd": "رمز دو مرحله‌ای: ",
        "login_err": "ورود ناموفق: {e}",
        "login_abort": "ورود متوقف شد. بعداً دوباره اجرا کنید.",
        "fatal": "⛔ خطای غیرمنتظره: {e}",
        "code_resent": "کد جدید ارسال شد.",
        "flood_login": "تلگرام موقتاً محدود کرده. {s} ثانیه بعد دوباره امتحان کنید.",
        "login_ok": "✅ وارد شدید: {name}",
        "drive_ok": "Drive وصل شد.",
        "drive_fail": "اتصال Drive ناموفق: {e}\nذخیره در حافظه‌ی موقت.",
        "loading": "در حال دریافت لیست چت‌ها...",
        "m_channels": "📢 کانال‌ها",
        "m_groups": "👥 گروه‌ها",
        "m_private": "👤 چت‌های شخصی",
        "m_bots": "🤖 ربات‌ها",
        "m_all": "📋 همه",
        "m_manual": "🔎 آیدی/یوزرنیم/لینک",
        "m_save": "💾 ذخیره‌ی لیست",
        "m_refresh": "🔄 بازخوانی",
        "m_settings": "⚙️ تنظیمات",
        "m_exit": "🚪 خروج",
        "choose": "انتخاب: ",
        "invalid": "ورودی نامعتبر.",
        "list_head": "{title} ({n}) صفحه {p}/{tp}",
        "list_help": "شماره=انتخاب n=بعدی p=قبلی\ns=جست‌وجو r=پاک 0=برگشت",
        "search_q": "جست‌وجو: ",
        "filter": "فیلتر: {q}",
        "empty": "موردی نیست.",
        "manual_q": "آیدی / @username / لینک: ",
        "not_found": "پیدا نشد. آیدی عددی فقط برای چت‌های لیست شما کار می‌کند.",
        "chat_head": "{name}\nآیدی: {id}\nتعداد پیام: {total}",
        "start_q": "Enter = شروع دانلود | 0 = لغو: ",
        "empty_chat": "چت خالی است.",
        "starting": "شروع شد. توقف: دکمه‌ی ■",
        "stopping": "⏹ در حال توقف...",
        "interrupted": "⏹ متوقف شد. (فایل‌های دانلودشده می‌مانند.)",
        "zip_partial": "📦 زیپِ ناقصِ همین‌تا دانلود ساخته شد (برای ادامه دوباره اجرا کنید):\n{z}",
        "disk_low": "⛔ فضای دیسک کم است؛ متوقف شد.",
        "run_err": "⛔ خطا: {e}",
        "flood": "⏳ محدودیت تلگرام؛ {s} ثانیه صبر...",
        "zipping": "📦 ساخت زیپ...",
        "summary": "پیام: {n} | فایل: {f}\nردشده: {s} | خطا: {e}",
        "done": "✅ تمام شد\nپوشه: {d}\nزیپ: {z}",
        "dl_q": "زیپ ({mb}MB) در مرورگر دانلود شود؟ (y/N): ",
        "saved_list": "لیست ذخیره شد:\n{p}",
        "set_head": "⚙️ تنظیمات",
        "set1": "1) دانلود مدیا: {v}",
        "set2": "2) سقف حجم فایل MB (0=نامحدود): {v}",
        "set3": "3) ساخت زیپ: {v}",
        "set4": "4) فایل info کنار مدیا: {v}",
        "set5": "5) خنک‌سازی: {v}",
        "set6": "6) خروج از حساب + حذف session",
        "set0": "0) برگشت",
        "on": "روشن",
        "off": "خاموش",
        "sp_gentle": "آرام",
        "sp_normal": "عادی",
        "sp_fast": "سریع",
        "mb_q": "سقف حجم (MB): ",
        "logout_q": "از حساب خارج و session محلی + بکاپ Drive هر دو حذف شوند؟ (y/N): ",
        "logged_out": "خارج شدید و session حذف شد.",
        "logout_partial": "⚠️ session محلی حذف شد ولی خروج از سرور ناموفق بود؛ از Settings → Devices در خود تلگرام خارج شوید.",
        "backup_pwd_q": "رمزِ بکاپ session: ",
        "backup_pwd_q2": "تکرار همان رمز (تأیید): ",
        "backup_pwd_mismatch": "رمزها یکی نبودند؛ دوباره.",
        "backup_pwd_visible": "⚠️ این محیط ورودی مخفی ندارد؛ رمز شما روی صفحه دیده می‌شود.",
        "backup_ok": "🔐 بکاپ session رمزنگاری‌شده در Drive ذخیره شد:\n{p}",
        "restore_ok": "🔓 session از بکاپ بازگردانی شد — حساب: {name}\n(برای لاگین تازه: گزینه ۶ یا حذف فایل بکاپ)",
        "backup_bad": "⚠️ بازکردن بکاپ نشد (رمز غلط یا فایل خراب — اگر رمز TS_ENC_KEY را عوض کرده‌اید، بکاپ قدیمی دیگر باز نمی‌شود). لاگین تازه انجام می‌شود؛ بکاپ حذف نشد.",
        "backup_skip": "⚠️ بکاپ session ساخته/بازخوانی نشد ({e}) — بدون بکاپ ادامه می‌دهیم.",
        "backup_offline": "ℹ️ بکاپ session فقط در Colab با Drive فعال می‌شود؛ در ترمینال session محلی ماندگار است.",
        "backup_deleted": "🧹 بکاپ Drive هم حذف شد.",
        "backup_left": "⚠️ حذف بکاپ Drive ناموفق بود؛ فایل هنوز آنجاست:\n{p}",
        "legacy_import_q": "یک session قدیمی (خام) روی Drive پیدا شد. همین‌جا import شود تا به بکاپ رمزنگاری‌شده تبدیل شود؟ (Y/n): ",
        "legacy_import_ok": "session قدیمی import شد؛ بعد از ورود، بکاپ رمزنگاری‌شده ساخته می‌شود. بعد خودتان فایل خام را از Drive پاک کنید.",
        "legacy_import_fail": "⚠️ import سشن قدیمی ناموفق بود: {e}",
        "enter": "Enter برای ادامه...",
        "bye": "خداحافظ 👋",
        "none": "ندارد",
    },
    "en": {
        "title": "📡 TeleScrape",
        "hint": "The input box is at the bottom of the output; scroll down if hidden.",
        "secrets_missing": "⚠️ Secret not found: {names}\n🔑 Create it in Colab and enable 'Notebook access'. Enter manually for now.",
        "legacy_removed": "🧹 Old saved API Hash file removed (no longer needed).",
        "old_session": "🚨 A raw old session was found on Drive — this file IS your full, unencrypted account login. If you have an encrypted backup, delete it manually:\n{p}",
        "prev_closed": "🧹 Previous run's client closed (avoids two connections on one session).",
        "api_id": "API ID: ",
        "api_hash": "API Hash (hidden): ",
        "bad_hash": "API Hash must be 32 hex characters.",
        "phone": "Phone (+98912...): ",
        "code": "Telegram code (r=resend): ",
        "pwd": "2FA password: ",
        "login_err": "Login failed: {e}",
        "login_abort": "Login stopped. Run again later.",
        "fatal": "⛔ Unexpected error: {e}",
        "code_resent": "New code sent.",
        "flood_login": "Telegram rate-limited you. Retry in {s}s.",
        "login_ok": "✅ Logged in: {name}",
        "drive_ok": "Drive mounted.",
        "drive_fail": "Drive failed: {e}\nUsing temporary storage.",
        "loading": "Loading chat list...",
        "m_channels": "📢 Channels",
        "m_groups": "👥 Groups",
        "m_private": "👤 Private chats",
        "m_bots": "🤖 Bots",
        "m_all": "📋 All",
        "m_manual": "🔎 ID/username/link",
        "m_save": "💾 Save list",
        "m_refresh": "🔄 Refresh",
        "m_settings": "⚙️ Settings",
        "m_exit": "🚪 Exit",
        "choose": "Choice: ",
        "invalid": "Invalid input.",
        "list_head": "{title} ({n}) page {p}/{tp}",
        "list_help": "number=select n=next p=prev\ns=search r=clear 0=back",
        "search_q": "Search: ",
        "filter": "Filter: {q}",
        "empty": "Nothing here.",
        "manual_q": "ID / @username / link: ",
        "not_found": "Not found. Numeric IDs only work for chats in your list.",
        "chat_head": "{name}\nID: {id}\nMessages: {total}",
        "start_q": "Enter = start | 0 = cancel: ",
        "empty_chat": "Chat is empty.",
        "starting": "Started. Stop with the ■ button.",
        "stopping": "⏹ Stopping...",
        "interrupted": "⏹ Stopped. (Downloaded files are kept.)",
        "zip_partial": "📦 A partial zip of the run so far was created (re-run to continue):\n{z}",
        "disk_low": "⛔ Low disk space; stopped.",
        "run_err": "⛔ Error: {e}",
        "flood": "⏳ Telegram limit; waiting {s}s...",
        "zipping": "📦 Creating zip...",
        "summary": "Messages: {n} | Files: {f}\nSkipped: {s} | Errors: {e}",
        "done": "✅ Done\nFolder: {d}\nZip: {z}",
        "dl_q": "Download zip ({mb}MB) in browser? (y/N): ",
        "saved_list": "List saved:\n{p}",
        "set_head": "⚙️ Settings",
        "set1": "1) Download media: {v}",
        "set2": "2) Max file size MB (0=unlimited): {v}",
        "set3": "3) Create zip: {v}",
        "set4": "4) .info file per media: {v}",
        "set5": "5) Cooldown: {v}",
        "set6": "6) Log out + delete session",
        "set0": "0) Back",
        "on": "ON",
        "off": "OFF",
        "sp_gentle": "gentle",
        "sp_normal": "normal",
        "sp_fast": "fast",
        "mb_q": "Max size (MB): ",
        "logout_q": "Log out and delete the session + its Drive backup? (y/N): ",
        "logged_out": "Logged out, session deleted.",
        "logout_partial": "⚠️ Local session deleted but server-side logout failed; sign out via Settings → Devices in Telegram itself.",
        "backup_pwd_q": "Session backup password: ",
        "backup_pwd_q2": "Repeat the same password (confirm): ",
        "backup_pwd_mismatch": "Passwords did not match; try again.",
        "backup_pwd_visible": "⚠️ No hidden input in this environment; your password will be visible on screen.",
        "backup_ok": "🔐 Encrypted session backup saved to Drive:\n{p}",
        "restore_ok": "🔓 Session restored from backup — account: {name}\n(for a fresh login: option 6 or delete the backup file)",
        "backup_bad": "⚠️ Could not open the backup (wrong password or corrupt file — if you changed TS_ENC_KEY, the old backup no longer opens). Doing a fresh login; the backup was NOT deleted.",
        "backup_skip": "⚠️ Session backup not created/read ({e}) — continuing without backup.",
        "backup_offline": "ℹ️ Session backup only works in Colab with Drive; in a terminal the local session already persists.",
        "backup_deleted": "🧹 Drive backup deleted too.",
        "backup_left": "⚠️ Could not delete the Drive backup; the file is still there:\n{p}",
        "legacy_import_q": "An old raw session was found on Drive. Import it here so it becomes an encrypted backup? (Y/n): ",
        "legacy_import_ok": "Old session imported; after login an encrypted backup will be created. Delete the raw file from Drive yourself afterwards.",
        "legacy_import_fail": "⚠️ Importing the old session failed: {e}",
        "enter": "Press Enter to continue...",
        "bye": "Bye 👋",
        "none": "none",
    },
}


def t(key, **kw):
    s = S.get(LANG, S["en"]).get(key) or S["en"][key]
    return s.format(**kw) if kw else s


# ───────────────────────────────── UI helpers ─────────────────────────────────
class Stopped(Exception):
    """User pressed stop (cooperative cancellation)."""


class DiskLow(Exception):
    pass


class LoginAbort(Exception):
    pass


STOP = threading.Event()
_ui = {"line": None, "shown": None}
_msgs = collections.deque()
PROG = {"txt": "", "phase": ""}          # قابلیت WEB-02: فاز ساختاریافته برای لایه‌ی وب
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def hr():
    return "─" * max(10, min(int(WIDTH), 40))


def say(text=""):
    w = max(20, int(WIDTH))
    for line in str(text).split("\n"):
        if not line:
            print()
            continue
        if len(line) > w:            # hard-wrap runaway tokens (URLs, hashes) fill() won't break
            pieces = []
            for tok in line.split(" "):
                while len(tok) > w:
                    pieces.append(tok[:w])
                    tok = tok[w:]
                pieces.append(tok)
            line = " ".join(pieces)
        print(textwrap.fill(line, w, break_long_words=False, break_on_hyphens=False))


def clear():
    # Immediate clear (wait=False). clear_output(wait=True) right before input() can make
    # Colab hide the input box, so it is deliberately not used.
    if IN_COLAB and CLEAR_SCREEN:
        try:
            from IPython.display import clear_output
            clear_output(wait=False)
        except Exception:
            pass


def ask(prompt):
    """input() that flushes first and converts Persian/Arabic digits (phone keyboards) to ASCII."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    return input(prompt).strip().translate(_DIGITS)


def pause():
    ask(t("enter"))


def short(s, n=26):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def short_err(e):
    return f"{type(e).__name__}: {str(e)[:80]}".strip()


# Worker threads never print: they publish to these helpers and the main thread draws them
# (notebook front-ends attribute output from foreign threads unreliably).
def status(text):
    _ui["line"] = text


def notify(text):
    _msgs.append(text)


def ui_slice(s, width):
    """Cut a status line to width without leaving a dangling combining mark:
    if the cut lands inside a Persian/Arabic cluster (base + diacritic), the orphan
    combining mark is dropped together with the character it decorates."""
    if len(s) <= width:
        return s
    cut = s[:width]
    while cut and unicodedata.combining(cut[-1]):
        cut = cut[:-1]
    return cut


def _ui_tick():
    out = sys.stdout
    width = max(20, int(WIDTH)) - 2
    while _msgs:
        if _ui["shown"] is not None:
            out.write("\r" + " " * width + "\r")
            _ui["shown"] = None
        say(_msgs.popleft())
    line = _ui["line"]
    if line is not None and line != _ui["shown"]:
        out.write("\r" + ui_slice(str(line), width).ljust(width))
        out.flush()
        _ui["shown"] = line


def _ui_end():
    _ui_tick()
    if _ui["shown"] is not None:
        sys.stdout.write("\r" + " " * max(20, int(WIDTH)) + "\r")
        sys.stdout.flush()
    _ui["line"] = None
    _ui["shown"] = None


# ─────────────────── background event loop (no nested loops, no nest_asyncio) ───────────────────
_BG = {"loop": None, "thread": None}


def _bg_loop():
    if _BG["loop"] is None or not _BG["thread"].is_alive():
        loop = asyncio.new_event_loop()

        def _target():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        th = threading.Thread(target=_target, name="telescrape-loop", daemon=True)
        th.start()
        _BG["loop"], _BG["thread"] = loop, th
    return _BG["loop"]


def run(coro, stoppable=False):
    """Run a coroutine on the background loop and wait for it from plain synchronous code.

    The main thread stays responsive: it redraws status, and Ctrl+C / the Colab ■ button is honoured.
    stoppable=True -> the first interrupt only sets STOP (the coroutine winds down and returns its
    partial result); a second interrupt, or 90 s without winding down, cancels it."""
    fut = asyncio.run_coroutine_threadsafe(coro, _bg_loop())
    t_int = None
    try:
        while True:
            try:
                done, _ = cf.wait([fut], timeout=0.3)
                _ui_tick()
                if done:
                    return fut.result()
                if t_int is not None and time.time() - t_int > 90:
                    raise KeyboardInterrupt
            except KeyboardInterrupt:
                if not stoppable or t_int is not None:
                    fut.cancel()
                    raise
                STOP.set()
                t_int = time.time()
                notify(t("stopping"))
    finally:
        _ui_end()
        if stoppable:
            STOP.clear()


async def nap(sec, show=True):
    """Interruptible sleep. Long pauses show a countdown."""
    if sec >= 30:
        notify(f"😴 استراحت {int(sec)} ثانیه‌ای — خنک‌سازی برای جلوگیری از محدودیت تلگرام")  # UIUX-02
    end = time.time() + max(0.0, sec)
    while True:
        left = end - time.time()
        if left <= 0:
            return
        if STOP.is_set():
            raise Stopped()
        if show and sec >= 3:
            status(f"😴 {int(left) + 1}s {PROG['txt']}")
        await asyncio.sleep(min(0.5, left))


# ───────────────────────────────── cooldown ─────────────────────────────────
class Throttle:
    """Human-like pacing: random jitter, periodic long rests, and a multiplier that grows after
    FloodWait and slowly decays back to 1."""

    def __init__(self, profile="normal"):
        self.p = PROFILES.get(profile) or PROFILES["normal"]
        self.mult = 1.0
        self.dl_count = 0
        self.dl_bytes = 0
        self.rests = 0

    def batch_pause(self):
        return random.uniform(*self.p["batch"]) * self.mult

    def after_download(self, nbytes):
        self.dl_count += 1
        self.dl_bytes += max(0, int(nbytes))
        pause_s = random.uniform(*self.p["dl"]) * self.mult
        if self.dl_count >= self.p["n"] or self.dl_bytes >= self.p["mb"] * MB:
            pause_s += random.uniform(*self.p["rest"]) * self.mult
            self.dl_count = 0
            self.dl_bytes = 0
            self.rests += 1
        return pause_s

    def flood(self, seconds):
        self.mult = min(self.mult * 1.5, 4.0)
        return float(seconds) + random.uniform(2, 8)

    def ok(self):
        self.mult = max(1.0, self.mult * 0.95)


RETRY_ERRORS = (asyncio.TimeoutError, ConnectionError, OSError, ServerError, TimedOutError)


def is_disk_full(e):
    """True when e is a local 'no space left' OSError. Never retry those: stop the whole run."""
    return isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT)


async def tg_call(factory, th, timeout=90, tries=6):
    """Await factory() with a timeout; retry network hiccups with backoff; sit out FloodWait."""
    last = None
    for i in range(tries):
        if STOP.is_set():
            raise Stopped()
        try:
            r = await asyncio.wait_for(factory(), timeout)
            th.ok()
            return r
        except FloodWaitError as e:
            last = e
            w = th.flood(e.seconds)
            notify(t("flood", s=int(w)))
            await nap(w)
        except RETRY_ERRORS as e:
            last = e
            if i < tries - 1:
                await nap(min(BACKOFF_BASE * 2 ** i, 60) + random.random())
    raise last


# ───────────────────────────────── file helpers ─────────────────────────────────
def sanitize(name, limit=40):
    s = re.sub(r"[^\w\-]+", "_", str(name or ""))
    s = re.sub(r"_+", "_", s).strip("_")[:limit]
    return s or "chat"


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def build_link(entity, msg_id):
    """t.me link — only valid for public channels/supergroups or private channels (t.me/c/...)."""
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if isinstance(entity, Channel):
        try:
            return f"https://t.me/c/{utils.resolve_id(utils.get_peer_id(entity))[0]}/{msg_id}"
        except Exception:
            return ""
    return ""  # private chats / basic groups have no public link


def fmt_date(m):
    try:
        return m.date.strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    except Exception:
        return "N/A"


def _write_sidecar(path, author, dt, kind, text):
    """Companion .info.txt file next to a media file."""
    with open(path + ".info.txt", "w", encoding="utf-8") as inf:
        inf.write(f"Author: {author}\nDate: {dt}\nType: {kind}\n"
                  f"Caption: {text or 'None'}\n")


DEFAULT_EXT = {"Photo": ".jpg", "Voice": ".ogg", "Music": ".mp3", "Video": ".mp4",
               "VideoMessage": ".mp4", "GIF": ".mp4", "Sticker": ".webp", "File": ""}


# فیکس NB-15: پسوندِ پیام از داده‌ی ریموت می‌آید -> فقط allowlist اعتماد می‌شود؛
# ناشناس‌ها به default همان kind برمی‌گردند (selftest: ext.untrusted)
SAFE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg", ".ico",
             ".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp",
             ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac",
             ".pdf", ".txt", ".csv", ".json", ".html", ".htm", ".xml",
             ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".rtf",
             ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".apk",
             ".ttf", ".otf", ".epub", ".mobi"}


def safe_ext(ext, kind):
    """The extension comes from remote data -> never trust it inside a path."""
    ext = str(ext or "").lower()
    if ext not in SAFE_EXTS:
        return DEFAULT_EXT.get(kind, "")
    return ext


def classify(m):
    if getattr(m, "action", None):
        return "Service"
    if m.photo:
        return "Photo"
    if m.voice:
        return "Voice"
    if m.video_note:
        return "VideoMessage"
    if m.sticker:
        return "Sticker"
    if m.gif:
        return "GIF"
    if m.video:
        return "Video"
    if m.audio:
        return "Music"
    if m.document:
        return "File"
    if m.media and not isinstance(m.media, MessageMediaWebPage):
        return "Other"
    if m.message:
        return "Text"
    return "Other"


def make_zip(folder, zip_path):
    os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)
    base = os.path.dirname(folder)
    tmp = zip_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
        for root, _, files in os.walk(folder):
            for f in files:
                if f.endswith((".part", ".tmp")) or f == "state.json":   # فیکس NB-07: *.tmp هم رد شود
                    continue
                full = os.path.join(root, f)
                if os.path.islink(full):
                    continue
                comp = zipfile.ZIP_DEFLATED if f.lower().endswith(TEXT_EXT) else zipfile.ZIP_STORED
                zf.write(full, os.path.relpath(full, base), compress_type=comp)
    os.replace(tmp, zip_path)


def folder_for(exports, name, pid):
    """Reuse the existing folder for this chat id even if the title changed between runs
    (P4-6): a rename must never trigger a full re-download. The old folder is renamed to
    the current title; if renaming fails (e.g. locked), we keep using the old folder."""
    suffix = f"_{pid}"
    new = os.path.join(exports, f"{sanitize(name)}_{pid}")
    try:
        existing = sorted(d for d in os.listdir(exports)
                          if d.endswith(suffix) and os.path.isdir(os.path.join(exports, d)))
    except OSError:
        existing = []
    if existing:
        old = os.path.join(exports, existing[0])
        if old != new:
            try:
                os.rename(old, new)
            except OSError:
                return old
    return new


# ───────────────────────────── secrets / credentials ─────────────────────────────
# فیکس NB-15: google.colab.userdata فقط در پروسه‌ی کرنلِ نوت‌بوک جواب می‌دهد؛ در
# پروسه‌ی جدا (nohup python ...) همیشه timeout می‌دهد و هر فراخوانی را کند می‌کند.
# بعد از اولین timeout دیگر userdata امتحان نمی‌شود (فقط env).
_USERDATA_DEAD = False


def get_secret(key):
    """Colab Secret first, then environment variable. Returns (value or None)."""
    for name in SECRET_NAMES[key]:
        if IN_COLAB and not _USERDATA_DEAD:          # فیکس NB-15
            try:
                from google.colab import userdata
                v = userdata.get(name)
                if v:
                    return str(v).strip()
            except Exception as e:                   # فیکس NB-15: timeout ⇒ از این پس فقط env
                # فیکس NB-21: در پروسه‌ی جدا خطای userdata AttributeError است نه timeout؛
                # هر خطای غیرقطعی (به‌جز «سکرت نیست/دسترسی خاموش») یعنی userdata در دسترس نیست.
                if type(e).__name__ not in ("SecretNotFoundError", "NotebookAccessError"):
                    globals()["_USERDATA_DEAD"] = True
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    return None


def norm_phone(p):
    p = str(p or "").translate(_DIGITS)
    p = re.sub(r"[\s\-()]", "", p)
    if p.startswith("00"):
        p = "+" + p[2:]
    if re.fullmatch(r"09\d{9}", p):          # Iranian local format
        p = "+98" + p[1:]
    elif re.fullmatch(r"9\d{9}", p):        # same without the leading zero
        p = "+98" + p
    if p and not p.startswith("+"):
        p = "+" + p
    return p


# ───────────────────── session backup: crypto + snapshot (فیکس NB-12) ─────────────────────
BK_MAGIC = b"TSSE1"
BK_HDR = len(BK_MAGIC) + 16 + 12          # magic + salt + nonce (تگ GCM داخل ciphertext است)
BK_ITERS = 240_000


def _bk_key(password, salt):
    """PBKDF2-HMAC-SHA256 → کلید ۳۲ بایتی AES-256 (فیکس NB-12)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                      salt=salt, iterations=BK_ITERS).derive(str(password).encode("utf-8"))


def bk_encrypt(data, password):
    """AES-256-GCM → TSSE1|salt|nonce|ciphertext+tag؛ salt و nonce هر بار تازه‌اند (فیکس NB-12)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_bk_key(password, salt)).encrypt(nonce, data, BK_MAGIC)
    return BK_MAGIC + salt + nonce + ct


def bk_decrypt(blob, password):
    """TSSE1 → plaintext؛ فرمت خراب/رمز غلط = exception (تگ GCM خودش صحت را چک می‌کند) (فیکس NB-12)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < BK_HDR + 16 or not blob.startswith(BK_MAGIC):
        raise ValueError("bad backup format")
    salt, nonce, ct = blob[5:21], blob[21:33], blob[33:]
    return AESGCM(_bk_key(password, salt)).decrypt(nonce, ct, BK_MAGIC)


def _session_ok(path):
    """سشن محلی هست و SQLite سالم است؟ (پوشش سشنِ خراب‌شده وسط نوشتن) (فیکس NB-12)."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "rb") as f:
            if not f.read(16).startswith(b"SQLite format 3\x00"):
                return False
    except OSError:
        return False
    try:
        con = sqlite3.connect(path)
        try:
            con.execute("SELECT name FROM sqlite_master LIMIT 1")
        finally:
            con.close()
        return True
    except Exception:
        return False


def _ask_pwd_hidden(label):
    """ورودی رمزِ مخفی — هرگز از ask() استفاده نمی‌شود (تبدیل ارقامِ فارسیِ ask رمز را خراب می‌کند) (فیکس NB-12)."""
    try:
        return getpass.getpass(t(label)).strip()
    except Exception:
        say(t("backup_pwd_visible"))
        return ask(t(label)).strip()


def get_backup_password(confirm=False):
    """رمزِ بکاپ: اول Secret (TS_ENC_KEY/ENC_KEY) و بعد پرسیدنِ مخفی (فیکس NB-12).
    جزو Secretهای الزامی نیست؛ فقط همین‌جا خوانده می‌شود. رمز خالی/عدم تطابق = None."""
    pw = (get_secret("enc_key") or "").strip()
    if pw:
        return pw
    for _ in range(3):
        pw = _ask_pwd_hidden("backup_pwd_q")
        if not pw:
            return None
        if not confirm or _ask_pwd_hidden("backup_pwd_q2") == pw:
            return pw
        say(t("backup_pwd_mismatch"))
    return None


def make_session_backup(session_file, dest, password):
    """Snapshot سازگار (API استاندارد backup پایتون) → AES-256-GCM → نوشتن اتمیک (فیکس NB-12).
    بکاپ قدیمی تا نوشتنِ کاملِ جدید تمام نشود حذف نمی‌شود؛ snapshot متن‌ساده هرگز جا نمی‌ماند."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp_plain = session_file + ".bksnap"
    tmp_enc = dest + ".tmp"
    try:
        src = sqlite3.connect(session_file)
        try:
            dst = sqlite3.connect(tmp_plain)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        _chmod(tmp_plain, 0o600)
        with open(tmp_plain, "rb") as f:
            plain = f.read()
        with open(tmp_enc, "wb") as f:
            f.write(bk_encrypt(plain, password))
        _chmod(tmp_enc, 0o600)
        os.replace(tmp_enc, dest)
        return True
    finally:
        _rm(tmp_plain)
        _rm(tmp_enc)


def restore_session_backup(backup_file, session_file, password=None):
    """بازگردانی سشن از بکاپ رمزنگاری‌شده (فیکس NB-12): True=بازگردانی، False=نشد (بکاپ حذف نمی‌شود)."""
    try:
        with open(backup_file, "rb") as f:
            blob = f.read()
    except OSError:
        return False
    pw = password if password is not None else get_backup_password()
    if not pw:
        say(t("backup_skip", e="no password"))
        return False
    try:
        plain = bk_decrypt(blob, pw)
    except Exception:
        say(t("backup_bad"))
        return False
    if not plain.startswith(b"SQLite format 3\x00"):
        say(t("backup_bad"))
        return False
    tmp = session_file + ".bkrest"
    try:
        with open(tmp, "wb") as f:
            f.write(plain)
        _chmod(tmp, 0o600)
        os.replace(tmp, session_file)
        return True
    finally:
        _rm(tmp)


def backup_active():
    """قابلیت بکاپ فقط در Colab + Drive وصل‌شده + کتابخانه‌ی cryptography (فیکس NB-12)."""
    if not SESSION_BACKUP:
        return False
    if not IN_COLAB or not USE_DRIVE:
        say(t("backup_offline"))
        return False
    if not HAS_CRYPTO:
        say(t("backup_skip", e="cryptography"))
        return False
    return True


def get_creds():
    """Returns (api_id, api_hash, phone, pwd2fa, all_from_secrets)."""
    api_id_s = get_secret("api_id")
    api_hash = get_secret("api_hash")
    phone = get_secret("phone")
    pwd = get_secret("pwd")
    api_id = None
    if api_id_s:
        try:
            api_id = int(api_id_s.translate(_DIGITS))
        except ValueError:
            api_id = None
    if api_hash and not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        say(t("bad_hash"))
        api_hash = None
    missing = [SECRET_NAMES[k][0] for k, v in (("api_id", api_id), ("api_hash", api_hash)) if not v]  # فیکس NB-12: phone دیگر الزامی نیست
    from_secrets = bool(api_id and api_hash)      # both came from Secrets/env (not typed by hand)
    if missing:
        say(t("secrets_missing", names=", ".join(missing)))
    while api_id is None:
        try:
            api_id = int(ask(t("api_id")))
        except ValueError:
            say(t("invalid"))
    while not api_hash:
        api_hash = getpass.getpass(t("api_hash")).strip().translate(_DIGITS)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            say(t("bad_hash"))
            api_hash = None
    phone = norm_phone(phone) if phone else None   # فیکس NB-12: phone فقط لحظه‌ی لاگین واقعی پرسیده می‌شود
    return api_id, api_hash, phone, pwd, from_secrets


# ───────────────────────────── app state / storage ─────────────────────────────
class App:
    DEFAULTS = {"media": True, "max_mb": 0, "zip": True, "sidecar": False}

    def __init__(self):
        local = os.path.abspath("telescrape_data")
        base = local
        if USE_DRIVE and IN_COLAB:
            try:
                from google.colab import drive
                drive.mount("/content/drive")
                base = "/content/drive/MyDrive/TeleScrape"
                say(t("drive_ok"))
            except Exception as e:
                say(t("drive_fail", e=short_err(e)))
        self.base = os.path.abspath(base)
        self.exports = os.path.join(self.base, "exports")
        self.zips = os.path.join(self.base, "zips")
        self.settings_path = os.path.join(self.base, "settings.json")
        # فیکس NB-14: سشن همیشه محلی است؛ روی Drive فقط بکاپ رمزنگاری‌شده ذخیره می‌شود
        sess_root = local  # فیکس NB-14
        self.session_dir = os.path.join(sess_root, "session")
        for d in (self.base, self.exports, self.zips, self.session_dir):
            os.makedirs(d, exist_ok=True)
        _chmod(self.session_dir, 0o700)
        self.settings = dict(self.DEFAULTS)
        loaded = load_json(self.settings_path) or {}
        for k, dv in self.DEFAULTS.items():
            if k in loaded and type(loaded[k]) is type(dv):
                self.settings[k] = loaded[k]
        # Speed lives next to settings (validated): saved value wins over the form default,
        # so a change made in the settings menu survives the next run.
        sp = loaded.get("speed")
        if sp not in PROFILES:
            sp = SPEED if SPEED in PROFILES else "normal"
        self.speed = sp
        self.local = local

    def save_settings(self):
        try:
            save_json(self.settings_path, {**self.settings, "speed": self.speed})
        except Exception:
            pass

    def session_path(self, api_id):
        return os.path.join(self.session_dir, f"tg_{api_id}")

    def backup_path(self, api_id):
        """مسیر بکاپ رمزنگاری‌شده‌ی session روی Drive (فیکس NB-12)."""
        return os.path.join(self.base, "backup", f"tg_{api_id}.session.enc")

    def lock_session(self, api_id):
        for suffix in (".session", ".session-journal"):
            _chmod(self.session_path(api_id) + suffix, 0o600)

    def delete_session(self, api_id):
        for suffix in (".session", ".session-journal"):
            _rm(self.session_path(api_id) + suffix)

    def cleanup_legacy(self):
        """v2 stored API ID/Hash in plain JSON (possibly on Drive). Secrets replace that."""
        removed = False
        for root in {self.base, self.local}:
            p = os.path.join(root, "tg_api_credentials.json")
            if os.path.exists(p):
                _rm(p)
                removed = True
        if removed:
            say(t("legacy_removed"))
        if USE_DRIVE:   # فیکس NB-14: با حذف SESSION_ON_DRIVE، هشدار سشن خام قدیمی همیشه فعال است
            old = os.path.join(self.base, "session")
            if os.path.isdir(old) and any(f.endswith(".session") for f in os.listdir(old)) \
                    and os.path.abspath(old) != os.path.abspath(self.session_dir):
                say(t("old_session", p=old))


# ───────────────────────────── dialogs / selection ─────────────────────────────
def make_item(entity, cat="other"):
    return {
        "cat": cat,
        "id": utils.get_peer_id(entity),
        "name": " ".join((utils.get_display_name(entity) or "").split()) or "(no name)",
        "username": getattr(entity, "username", None),
        "entity": entity,
    }


async def load_dialogs(client):
    async def _collect():
        items = []
        async for d in client.iter_dialogs():
            e = d.entity
            if d.is_channel and not d.is_group:
                cat = "channel"
            elif d.is_group:
                cat = "group"
            elif d.is_user:
                cat = "bot" if getattr(e, "bot", False) else "private"
            else:
                continue
            it = make_item(e, cat)
            it["id"] = d.id
            if d.name:
                it["name"] = " ".join(d.name.split())
            items.append(it)
        return items

    last = None
    for i in range(3):
        try:
            return await asyncio.wait_for(_collect(), 600)
        except FloodWaitError as e:
            last = e
            await nap(float(e.seconds) + 2)          # sit out the limit, keep STOP responsive
        except RETRY_ERRORS as e:
            last = e
            await asyncio.sleep(3 * (i + 1))
    raise last


def _match(it, q):
    return q in it["name"].lower() or q in str(it["id"]) or q in (it["username"] or "").lower()


def item_row(i, it):
    """'3. Name - -1001234567890', name shortened so the line fits the screen width."""
    pre, suf = f"{i}. ", f" - {it['id']}"
    room = max(6, int(WIDTH) - len(pre) - len(suf))
    return f"{pre}{short(it['name'], room)}‎{suf}"


def pick_from_list(title, items, per):
    q, page, note = "", 0, ""
    while True:
        view = [it for it in items if _match(it, q)] if q else items
        tp = max(1, -(-len(view) // per))
        page = min(page, tp - 1)
        clear()
        print(hr())
        say(t("list_head", title=title, n=len(view), p=page + 1, tp=tp))
        if q:
            say(t("filter", q=q))
        print(hr())
        start = page * per
        for i, it in enumerate(view[start:start + per], start + 1):
            print(item_row(i, it))
        if not view:
            say(t("empty"))
        print(hr())
        say(t("list_help"))
        if note:
            say(note)
            note = ""
        c = ask(t("choose")).lower()
        if c in ("0", "b", "q"):
            return None
        if c == "n":
            page = min(page + 1, tp - 1)
        elif c == "p":
            page = max(page - 1, 0)
        elif c == "s":
            q, page = ask(t("search_q")).lower(), 0
        elif c == "r":
            q, page = "", 0
        elif re.fullmatch(r"-?\d+", c):
            n = int(c)
            if 1 <= n <= len(view):
                return view[n - 1]
            hit = [it for it in items if it["id"] == n]
            if hit:
                return hit[0]
            note = t("invalid")
        else:
            note = t("invalid")


TME_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(.+)", re.I)
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


async def _get_entity(client, peer):
    """get_entity with visible status, a short timeout, and FloodWait retries."""
    status(t("loading"))
    for _ in range(3):
        try:
            return await asyncio.wait_for(client.get_entity(peer), 20)
        except FloodWaitError as e:
            await nap(float(e.seconds) + 2)
        except RETRY_ERRORS:      # فیکس NB-05: خطای گذِرا (شبکه/تایم‌اوت) با backoff کوتاه دوباره
            await asyncio.sleep(2)
        except Exception:
            return None
    return None


async def resolve(client, text, items):
    """Chat from a list ID, numeric ID, @username or t.me link. Returns an item or None."""
    text = str(text or "").strip().translate(_DIGITS)
    if not text:
        return None
    m = TME_RE.match(text)
    forced = None                                  # peers known exactly (t.me/c/<channel id>/...)
    if m:
        parts = m.group(1).split("?")[0].strip("/").split("/")
        if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
            forced = [PeerChannel(int(parts[1]))]
            text = str(utils.get_peer_id(forced[0]))
        elif parts[0].startswith("+") or parts[0].lower() == "joinchat":
            return None            # invite links need joining first — out of scope
        else:
            text = "@" + parts[0]
    try:
        if re.fullmatch(r"-?\d+", text):
            n = int(text)
            for it in items:                       # exact marked ID, then bare ID
                if it["id"] == n:
                    return it
            if forced is None:
                for it in items:
                    if utils.resolve_id(it["id"])[0] == abs(n):
                        return it
            if forced is not None:
                peers = forced
            elif n > 0:
                peers = [PeerUser(n), PeerChannel(n), PeerChat(n)]
            else:
                real, cls = utils.resolve_id(n)
                peers = [cls(real)]
            for peer in peers:
                ent = await _get_entity(client, peer)
                if ent is not None:
                    return make_item(ent)
            return None
        uname = text.lstrip("@")
        if not USERNAME_RE.fullmatch(uname):
            return None
        ent = await _get_entity(client, uname)
        return make_item(ent) if ent is not None else None
    except Exception:
        return None


def export_list(app, items):
    path = os.path.join(app.base, "dialogs_list.txt")
    titles = [("channel", t("m_channels")), ("group", t("m_groups")),
              ("private", t("m_private")), ("bot", t("m_bots"))]
    with open(path, "w", encoding="utf-8") as f:
        for cat, title in titles:
            sub = [it for it in items if it["cat"] == cat]
            f.write(f"== {title} ({len(sub)}) ==\n")
            for i, it in enumerate(sub, 1):
                u = f"  @{it['username']}" if it["username"] else ""
                f.write(f"{i}. {it['name']} - {it['id']}{u}\n")
            f.write("\n")
    _chmod(path, 0o600)
    return path


# ───────────────────────────────────── scrape ─────────────────────────────────────
async def download(client, m, path, size, th, tag=""):
    """Download to <path>.part then rename. Retries on errors, cancels stalled transfers."""
    part = path + ".part"
    last = None
    attempt, floods = 0, 0        # فیکس NB-06: FloodWait سهمیه‌ی تلاش را نمی‌سوزاند
    while attempt < 4:
        if STOP.is_set():
            raise Stopped()
        try:
            free = shutil.disk_usage(os.path.dirname(path) or ".").free
        except Exception:
            free = None
        if free is not None and free < max(int(size * 1.1), 200 * MB):
            raise DiskLow()
        _rm(part)
        st = {"t": time.time(), "cur": 0}

        def cb(cur, tot, st=st):
            st["t"] = time.time()
            st["cur"] = cur

        task = asyncio.ensure_future(client.download_media(m, file=part, progress_callback=cb))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=1.0)
                if done:
                    break
                if STOP.is_set():
                    raise Stopped()
                if time.time() - st["t"] > STALL_SECS:
                    raise asyncio.TimeoutError("stalled")
                PROG["dl_got"] = st["cur"]        # UIUX-02: پیشرفت فایل جاری برای وب
                status(f"{tag} ⬇ {st['cur'] // MB}/{size // MB}MB")
            r = task.result()
            if r and os.path.exists(r):        # a 0-byte file is valid if the transfer finished
                os.replace(r, path)
                th.ok()
                return True
            return False
        except FloodWaitError as e:
            last = e
            floods += 1                     # فیکس NB-06: سقف مستقل برای محدودیت‌ها (نه تلاش)
            if floods >= 6:
                break
            w = th.flood(e.seconds)
            notify(t("flood", s=int(w)))
            await nap(w)
        except RETRY_ERRORS as e:      # NB-02: OSError (incl. disk-full) is caught here first
            if is_disk_full(e):
                raise DiskLow()        # never retry a full disk
            last = e
            if attempt < 3:
                await nap(min(BACKOFF_BASE * 2 ** attempt, 40) + random.random())
            attempt += 1               # فیکس NB-06: فقط خطای واقعی تلاش مصرف می‌کند
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            _rm(part)
    raise last or RuntimeError("download failed")


async def count_msgs(client, entity):
    for _ in range(3):
        try:
            return (await asyncio.wait_for(client.get_messages(entity, limit=0), 30)).total
        except FloodWaitError as e:
            await nap(float(e.seconds) + 2)          # short limits no longer silently eaten
        except Exception:
            return None
    return None


async def scrape(app, client, item, speed, total_hint=None):
    """Full export of one chat from its first message. Returns a result dict (never prints)."""
    entity, pid, name = item["entity"], item["id"], item["name"]
    cfg = app.settings
    th = Throttle(speed)
    folder = folder_for(app.exports, name, pid)   # same chat id -> same folder (P4-6)
    os.makedirs(folder, exist_ok=True)
    meta_path = os.path.join(folder, "metadata.txt")
    md_path = os.path.join(folder, "all_texts.md")
    log_path = os.path.join(folder, "LOG.txt")
    max_bytes = int(cfg.get("max_mb") or 0) * MB

    stats = {"msgs": 0, "files": 0, "skipped": 0, "errors": 0}
    res = {"folder": folder, "zip": None, "interrupted": False, "reason": "", "empty": False, **stats}
    cache, err_lines = {}, []
    t0 = time.time()
    PROG["txt"] = ""

    # True resume (P3-4): state.json remembers the last completed message; a re-run
    # continues from there in append mode instead of re-fetching the whole chat.
    state_path = os.path.join(folder, "state.json")
    st = load_json(state_path) or {}
    try:
        resume_id = int(st.get("last_id") or 0)
    except (TypeError, ValueError):
        resume_id = 0
    first_id = st.get("first_id") if isinstance(st.get("first_id"), int) else None
    try:
        done_before = int(st.get("done") or 0)
    except (TypeError, ValueError):
        done_before = 0
    last_done = resume_id if resume_id else None
    # قابلیت WEB-02: پیشرفت ساختاریافته برای لایه‌ی وب — رفتار ترمینال تغییر نمی‌کند
    PROG.update(phase="scrape", chat=name, chat_id=pid, total=None, pct=0,
                done=done_before, files=0, skipped=0, errors=0, zip=None,
                milestone=0, dl_name=None, dl_got=None)   # UIUX-02: فاز/فایل جاری برای وب

    total = total_hint                     # reuse the count shown in the chat header (P3-6)
    if total is None:
        PROG["phase"] = "count"            # UIUX-02: وب زنده نشان دهد که مشغول شمارش پیام‌هاست
        try:
            total = (await tg_call(lambda: client.get_messages(entity, limit=0), th, 60)).total or 0
            PROG["phase"] = "scrape"       # UIUX-02
        except Stopped:
            res.update(interrupted=True, reason="stop")
            PROG["phase"] = "stopped"          # قابلیت WEB-02
            return res
    if total == 0 and not resume_id:
        res["empty"] = True
        PROG.update(phase="done", empty=True, total=0)   # قابلیت WEB-02
        return res
    total = max(1, total)              # فیکس NB-08: گارد تقسیم — شمارش ۰ ولی resume در جریان
    PROG["total"] = total             # قابلیت WEB-02

    async def author_of(m):
        sid = m.sender_id
        if sid is None:
            return name
        if sid not in cache:
            s = getattr(m, "sender", None)
            if s is None:
                try:
                    s = await tg_call(lambda: m.get_sender(), th, 30, tries=2)  # throttled (P4-8)
                except Stopped:
                    raise
                except Exception:
                    s = None
            cache[sid] = " ".join(((utils.get_display_name(s) if s else "") or str(sid)).split())
        return cache[sid]

    async def handle(m, mf, md):
        mid = m.id
        kind = classify(m)
        author = await author_of(m)
        dt = fmt_date(m)
        try:
            text = m.text or ""
        except Exception:
            text = m.raw_text or ""
        rid = getattr(m, "reply_to_msg_id", None)

        mf.write(f"[{mid}] [{kind}] [Author: {author}] [Date: {dt}]\n")
        if text:
            mf.write(f"Caption: {text}\n")
        if rid:
            link = build_link(entity, rid)
            mf.write(f"Reply to: {rid}" + (f" | {link}" if link else "") + "\n")
        if kind == "Other" and m.media:
            mf.write(f"Media: {type(m.media).__name__}\n")
        header = f"### Message {mid} - {author} - {dt}\n\n"

        if kind == "Text":
            with open(os.path.join(folder, f"{mid:06d}_Text.txt"), "w", encoding="utf-8") as tf:
                tf.write(header + text)
            md.write(header + text + "\n\n")
            stats["files"] += 1
        elif kind not in ("Service", "Other"):
            try:
                ext = safe_ext(m.file.ext if m.file else "", kind)
            except Exception:
                ext = safe_ext("", kind)
            fname = f"{mid:06d}_{kind}{ext}"
            path = os.path.join(folder, fname)
            try:
                size = int(m.file.size or 0) if m.file else 0
            except Exception:
                size = 0
            status_txt = None
            if not cfg.get("media", True):
                status_txt = "skipped (media off)"
            elif max_bytes and size > max_bytes:
                status_txt = f"skipped (too big: {size // MB} MB)"
            elif os.path.exists(path):      # NB-03: even a 0-byte file counts as done (P3-8)
                status_txt = "already downloaded"
            if status_txt:
                stats["skipped"] += 1
                mf.write(f"File: {fname} -> {status_txt}\n")
                if cfg.get("sidecar") and os.path.exists(path) \
                        and not os.path.exists(path + ".info.txt"):
                    _write_sidecar(path, author, dt, kind, text)
            else:
                tag = f"{int((done_before + stats['msgs']) * 100 / total):3d}% #{mid}"
                PROG.update(dl_name=fname, dl_size=size, dl_got=0)   # UIUX-02: فایل جاری در صفحه‌ی پیشرفت
                ok = await download(client, m, path, size, th, tag)
                PROG.update(dl_name=None, dl_got=None)
                if ok:
                    stats["files"] += 1
                    mf.write(f"File: {fname}\n")
                    if cfg.get("sidecar"):
                        _write_sidecar(path, author, dt, kind, text)
                    try:
                        real = os.path.getsize(path)
                    except OSError:
                        real = size
                    await nap(th.after_download(real))       # cooldown after every download
                else:
                    stats["errors"] += 1
                    err_lines.append(f"{mid}: download returned nothing")
            if text:
                md.write(header + f"[{kind}: {fname}]\n\n" + text + "\n\n")
        elif text:
            md.write(header + text + "\n\n")
        mf.write("-" * 80 + "\n")

    interrupted, reason = False, ""
    last_id = resume_id
    mode = "a" if resume_id else "w"
    try:
        with open(meta_path, mode, encoding="utf-8") as mf, open(md_path, mode, encoding="utf-8") as md:
            while True:
                if STOP.is_set():
                    raise Stopped()
                batch = await tg_call(
                    lambda: client.get_messages(entity, limit=100, offset_id=last_id, reverse=True),
                    th, 90)
                batch = sorted((m for m in batch if m.id > last_id), key=lambda m: m.id)
                if not batch:
                    break
                for m in batch:
                    try:
                        await handle(m, mf, md)
                    except (Stopped, DiskLow):
                        raise
                    except Exception as e:
                        if is_disk_full(e):
                            raise DiskLow()       # disk filled mid-write: stop, don't spam Telegram
                        stats["errors"] += 1
                        err_lines.append(f"{m.id}: {type(e).__name__}: {e}")
                    stats["msgs"] += 1
                    if first_id is None:
                        first_id = m.id
                    last_done = last_id = m.id
                    save_json(state_path, {"last_id": last_id, "first_id": first_id,
                                           "done": done_before + stats["msgs"]})
                    done_n = done_before + stats["msgs"]
                    pct = min(99, int(done_n * 100 / total))
                    PROG["txt"] = f"{pct}% {done_n}/{total}"
                    PROG.update(pct=pct, done=done_n, files=stats["files"],   # قابلیت WEB-02
                                skipped=stats["skipped"], errors=stats["errors"])
                    status(f"{pct:3d}% {done_n}/{total} f{stats['files']} e{stats['errors']}")
                    if pct // 25 > PROG.get("milestone", 0) // 25:   # UIUX-02: خبر هر ۲۵٪ برای گزارش زنده‌ی وب
                        PROG["milestone"] = pct
                        notify(f"✅ {pct}% از پیام‌ها دریافت شد")
                    if stats["msgs"] % 50 == 0:
                        mf.flush()
                        md.flush()
                await nap(th.batch_pause(), show=False)       # cooldown between fetches
    except Stopped:
        interrupted, reason = True, "stop"
    except DiskLow:
        interrupted, reason = True, "disk"
    except Exception as e:
        interrupted, reason = True, short_err(e)
        err_lines.append(f"fatal: {reason}")

    # LOG first (P3-5): so the zip of this run contains this run's LOG.txt
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"Run start: {datetime.fromtimestamp(t0).strftime('%Y-%m-%d %H:%M:%S')}\n")
            lf.write(f"Resume from id: {resume_id or 0} (already done: {done_before})\n")
            lf.write(f"Duration (s): {time.time() - t0:.1f}\n")
            lf.write(f"Chat: {name} ({pid}) | Interrupted: {interrupted} {reason}\n")
            lf.write(f"Messages: {stats['msgs']} | Files: {stats['files']} | "
                     f"Skipped: {stats['skipped']} | Errors: {stats['errors']}\n")
            lf.write(f"First id: {first_id} | Last id: {last_done} | Rests: {th.rests}\n")
            for line in err_lines[:500]:
                lf.write(f"ERR {line}\n")
            if len(err_lines) > 500:
                lf.write(f"ERR (+{len(err_lines) - 500} more errors not listed)\n")
            lf.write("-" * 80 + "\n")
    except Exception:
        pass

    zip_path = None
    # P4-7: an interrupted run also gets a zip (marked _partial) when it exported
    # something this run — the resume flow simply continues on the next run.
    # A disk-full stop skips the zip: there is no room to write it anyway.
    if cfg.get("zip", True) and first_id is not None \
            and (not interrupted or (stats["msgs"] and reason != "disk")):
        partial = bool(interrupted)
        zip_path = os.path.join(
            app.zips,
            f"{sanitize(name)}_{pid}_{first_id}_{last_done}{'_partial' if partial else ''}.zip")
        status(t("zipping"))
        PROG["phase"] = "zip"                 # قابلیت WEB-02
        try:
            await asyncio.to_thread(make_zip, folder, zip_path)   # off the loop: never blocks
            try:
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(("Zip (partial): " if partial else "Zip: ") + zip_path + "\n")
            except Exception:
                pass
        except Exception as e:
            try:
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(f"Zip: failed: {short_err(e)}\n")
            except Exception:
                pass
            zip_path = None

    # قابلیت WEB-02: فاز پایانی برای لایه‌ی وب
    if interrupted:
        PROG["phase"] = "stopped" if reason == "stop" else "error"
    else:
        PROG["phase"] = "done"
    PROG["zip"] = zip_path
    res.update(stats)
    res.update(zip=zip_path, interrupted=interrupted, reason=reason, rests=th.rests)
    return res


def report(res):
    if res.get("empty"):
        say(t("empty_chat"))
        return
    if res["interrupted"]:
        r = res["reason"]
        say(t("interrupted") if r == "stop" else t("disk_low") if r == "disk" else t("run_err", e=r))
        if res.get("zip"):
            say(t("zip_partial", z=res["zip"]))
    else:
        say(t("done", d=res["folder"], z=res["zip"] or t("none")))
    say(t("summary", n=res["msgs"], f=res["files"], s=res["skipped"], e=res["errors"]))


def offer_download(zip_path):
    """Plain synchronous prompt (never asked from inside a coroutine)."""
    if zip_path and IN_COLAB:
        try:
            mb = os.path.getsize(zip_path) // MB
        except OSError:
            mb = "?"
        if ask(t("dl_q", mb=mb)).lower() == "y":
            try:
                from google.colab import files
                files.download(zip_path)
            except Exception as e:
                say(short_err(e))


# ───────────────────────────────────── menus ─────────────────────────────────────
def start_chat(app, client, item):
    """Chat header -> Enter -> full export. The only choice is start or cancel."""
    clear()
    try:
        total = run(count_msgs(client, item["entity"]))
    except KeyboardInterrupt:
        return                      # back to the main menu, don't kill the whole app
    print(hr())
    say(t("chat_head", name=short(item["name"], 60), id=item["id"], total="?" if total is None else total))
    print(hr())
    if ask(t("start_q")) == "0":
        return
    say(t("starting"))
    try:
        total = run(count_msgs(client, item["entity"]))
    except KeyboardInterrupt:
        return                      # back to the main menu, don't kill the whole app
    print(hr())
    say(t("chat_head", name=short(item["name"], 60), id=item["id"], total="?" if total is None else total))
    print(hr())
    if ask(t("start_q")) == "0":
        return
    say(t("starting"))
    try:
        res = run(scrape(app, client, item, app.speed, total_hint=total), stoppable=True)
    except KeyboardInterrupt:
        say(t("interrupted"))
        return
    except Exception as e:
        say(t("run_err", e=short_err(e)))
        pause()
        return
    report(res)
    offer_download(res.get("zip"))
    pause()


def settings_screen(app, client, api_id):
    """Returns 'logout' when the session was deleted, else None."""
    cfg = app.settings
    onoff = lambda v: t("on") if v else t("off")
    order = ["gentle", "normal", "fast"]
    while True:
        clear()
        print(hr())
        say(t("set_head"))
        print(hr())
        say(t("set1", v=onoff(cfg["media"])))
        say(t("set2", v=cfg["max_mb"]))
        say(t("set3", v=onoff(cfg["zip"])))
        say(t("set4", v=onoff(cfg["sidecar"])))
        say(t("set5", v=t("sp_" + app.speed)))
        say(t("set6"))
        say(t("set0"))
        c = ask(t("choose"))
        if c == "1":
            cfg["media"] = not cfg["media"]
        elif c == "2":
            raw = ask(t("mb_q"))
            if raw.isdigit():
                cfg["max_mb"] = int(raw)
        elif c == "3":
            cfg["zip"] = not cfg["zip"]
        elif c == "4":
            cfg["sidecar"] = not cfg["sidecar"]
        elif c == "5":
            app.speed = order[(order.index(app.speed) + 1) % 3] if app.speed in order else "normal"
        elif c == "6":
            if ask(t("logout_q")).lower() == "y":
                ok = False      # فیکس NB-11: log_out روی شکست False برمی‌گرداند — نتیجه چک شود
                try:
                    ok = bool(run(client.log_out()))
                except Exception:
                    ok = False
                app.delete_session(api_id)
                # فیکس NB-13: خروجِ کامل = سشن محلی + بکاپ Drive؛ شکست حذف = هشدار، نه پیام موفقیت
                try:
                    if os.path.exists(app.backup_path(api_id)):
                        _rm(app.backup_path(api_id))
                        say(t("backup_deleted"))
                except Exception:
                    say(t("backup_left", p=app.backup_path(api_id)))
                say(t("logged_out") if ok else t("logout_partial"))
                return "logout"
        elif c == "0":
            app.save_settings()
            return None
        app.save_settings()


def _login_wait(sec):       # فیکس NB-04: صبرِ FloodWait لاگین — شمارش معکوس + قابل قطع
    """Blocking FloodWait wait that keeps the UI alive: countdown via status(), Ctrl+C aborts."""
    end = time.time() + max(0.0, sec)
    while True:
        left = end - time.time()
        if left <= 0:
            return
        try:
            time.sleep(min(0.5, left))
        except KeyboardInterrupt:
            raise LoginAbort()
        status(f"⏳ {int(left) + 1}s")


def login(client, phone, pwd2fa):
    """Interactive login, step by step from synchronous code. Raises LoginAbort on give-up."""
    for _ in range(3):
        try:
            run(client.connect())
            break
        except FloodWaitError as e:
            if e.seconds > 600:
                say(t("flood_login", s=e.seconds))
                raise LoginAbort()
            say(t("flood_login", s=e.seconds))
            _login_wait(e.seconds + 2)
        except Exception as e:
            say(t("login_err", e=short_err(e)))
            raise LoginAbort()
    else:
        raise LoginAbort()
    if run(client.is_user_authorized()):
        return

    def send():
        nonlocal phone
        if not phone:              # فیکس NB-12: phone فقط وقتی واقعاً لاگین لازم است پرسیده می‌شود
            phone = norm_phone(ask(t("phone")))
        for _ in range(5):
            try:
                run(client.send_code_request(phone))
                return
            except FloodWaitError as e:
                if e.seconds > 600:
                    say(t("flood_login", s=e.seconds))
                    raise LoginAbort()
                say(t("flood_login", s=e.seconds))
                _login_wait(e.seconds + 2)
            except Exception as e:
                say(t("login_err", e=short_err(e)))
                phone = norm_phone(ask(t("phone")))
        raise LoginAbort()

    send()
    need_pwd = False
    for _ in range(6):
        code = ask(t("code")).replace(" ", "")
        if code.lower() == "r":
            send()
            say(t("code_resent"))
            continue
        try:
            run(client.sign_in(phone, code))
            return
        except SessionPasswordNeededError:
            need_pwd = True
            break
        except PhoneCodeExpiredError:
            send()
            say(t("code_resent"))
        except PhoneCodeInvalidError:
            say(t("login_err", e="invalid code"))
        except FloodWaitError as e:
            if e.seconds > 600:
                say(t("flood_login", s=e.seconds))
                raise LoginAbort()
            say(t("flood_login", s=e.seconds))
            _login_wait(e.seconds + 2)
        except Exception as e:
            say(t("login_err", e=short_err(e)))
    if not need_pwd:
        raise LoginAbort()
    pw = pwd2fa
    for _ in range(5):
        if not pw:
            pw = getpass.getpass(t("pwd"))
        try:
            run(client.sign_in(password=pw))
            return
        except PasswordHashInvalidError:
            say(t("login_err", e="wrong password"))
        except FloodWaitError as e:
            if e.seconds > 600:
                say(t("flood_login", s=e.seconds))
                raise LoginAbort()
            say(t("flood_login", s=e.seconds))
            _login_wait(e.seconds + 2)
        except Exception as e:
            say(t("login_err", e=short_err(e)))
        pw = None
    raise LoginAbort()


async def make_client(session, api_id, api_hash):
    c = TelegramClient(session, api_id, api_hash, connection_retries=10, retry_delay=3,
                       request_retries=5, timeout=30, auto_reconnect=True)
    # False -> Telethon never sleeps through a FloodWait silently: every limit reaches our
    # notify/Throttle/nap path, user messages stay visible and STOP stays responsive.
    c.flood_sleep_threshold = False
    return c


async def _disconnect(client):
    # Telethon's disconnect() looks up the *running* loop, so it must be called from the loop's thread.
    r = client.disconnect()
    if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
        await r


async def _get_me(client):        # فیکس NB-09: get_me با timeout و retry — hiccup بعد از لاگین دیگر fatal نیست
    last = None
    for _ in range(3):
        try:
            return await asyncio.wait_for(client.get_me(), 20)
        except Exception as e:
            last = e
            await asyncio.sleep(2)
    raise last


def _cleanup_previous_session():
    """Re-running the Colab cell re-executes this module, so module globals reset and the
    previous client/loop become unreachable. We therefore keep the reference on the sys
    module (which survives re-execution) and close the old client before making a new one —
    two live clients on one SQLite session file risk 'database is locked'."""
    prev = getattr(sys, "_telescrape_prev", None)
    if not prev:
        return
    sys._telescrape_prev = None
    client, loop = prev
    try:
        asyncio.run_coroutine_threadsafe(_disconnect(client), loop).result(timeout=30)
    except Exception:
        try:
            client.disconnect()
        except Exception:
            pass
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    say(t("prev_closed"))


def app_main():
    app = App()
    print(hr())
    say(t("title"))
    say(t("hint"))
    api_id, api_hash, phone, pwd2fa, from_secrets = get_creds()
    if from_secrets:
        app.cleanup_legacy()
    _cleanup_previous_session()
    sess = app.session_path(api_id) + ".session"
    bk_on = backup_active()             # فیکس NB-12: فقط در Colab + Drive + cryptography
    restored = False
    if bk_on and not _session_ok(sess) and os.path.exists(app.backup_path(api_id)):
        restored = restore_session_backup(app.backup_path(api_id), sess)   # فیکس NB-12: restore قبل از ساخت کلاینت
    if bk_on and not _session_ok(sess) \
            and not os.path.exists(app.backup_path(api_id)):   # فیکس NB-14: مهاجرت اختیاری سشن خام قدیمی Drive
        legacy = os.path.join(app.base, "session", f"tg_{api_id}.session")
        if os.path.exists(legacy):
            try:
                if ask(t("legacy_import_q")).lower() in ("", "y"):
                    shutil.copyfile(legacy, sess)
                    _chmod(sess, 0o600)
                    say(t("legacy_import_ok"))
            except Exception as e:
                say(t("legacy_import_fail", e=short_err(e)))
    client = run(make_client(app.session_path(api_id), api_id, api_hash))
    sys._telescrape_prev = (client, _BG["loop"])   # survives cell re-runs (kept on sys)
    app.lock_session(api_id)          # 600 from the moment the file exists, before any login step
    try:
        login(client, phone, pwd2fa)
        app.lock_session(api_id)
        me = run(_get_me(client))     # فیکس NB-09
        who = utils.get_display_name(me) or str(me.id)
        if restored:                  # فیکس NB-12: هویت حسابِ restore‌شده صریح چاپ شود (دام حساب اشتباه)
            say(t("restore_ok", name=who))
        else:
            say(t("login_ok", name=who))
        if bk_on and not restored:     # فیکس NB-12: بعد از ورود موفق، بکاپ رمزنگاری‌شده روی Drive
            try:
                pw = get_backup_password(confirm=True)
                if pw and make_session_backup(sess, app.backup_path(api_id), pw):
                    say(t("backup_ok", p=app.backup_path(api_id)))
                elif pw is None:
                    say(t("backup_skip", e="no password"))
            except Exception as e:
                say(t("backup_skip", e=short_err(e)))
        say(t("loading"))
        items = run(load_dialogs(client))

        cats = {"1": ("channel", "m_channels"), "2": ("group", "m_groups"),
                "3": ("private", "m_private"), "4": ("bot", "m_bots")}
        while True:
            clear()
            print(hr())
            say(f"{t('title')} | {short(who, 14)}")
            print(hr())
            for k, (cat, key) in cats.items():
                say(f"{k}) {t(key)} ({sum(1 for i in items if i['cat'] == cat)})")
            say(f"5) {t('m_all')} ({len(items)})")
            say(f"6) {t('m_manual')}")
            say(f"7) {t('m_save')}")
            say(f"8) {t('m_refresh')}")
            say(f"9) {t('m_settings')}")
            say(f"0) {t('m_exit')}")
            c = ask(t("choose"))

            if c in cats or c == "5":
                if c == "5":
                    sub, title = items, t("m_all")
                else:
                    sub = [i for i in items if i["cat"] == cats[c][0]]
                    title = t(cats[c][1])
                chosen = pick_from_list(title, sub, max(5, int(PER_PAGE)))
                if chosen:
                    start_chat(app, client, chosen)
            elif c == "6":
                found = run(resolve(client, ask(t("manual_q")), items))
                if found:
                    start_chat(app, client, found)
                else:
                    say(t("not_found"))
                    pause()
            elif c == "7":
                say(t("saved_list", p=export_list(app, items)))
                pause()
            elif c == "8":
                say(t("loading"))
                items = run(load_dialogs(client))
            elif c == "9":
                if settings_screen(app, client, api_id) == "logout":
                    break
            elif c == "0":
                break
    finally:
        try:
            run(_disconnect(client))
        except BaseException:
            pass
    say(t("bye"))


def _engine_selftest():
    """Zero-dependency smoke tests for the pure helpers (no pytest, no Telegram):
    python telescrape.py --selftest-engine"""
    import io
    import contextlib
    import tempfile

    ok = fail = 0

    def eq(got, want, label):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}")

    # sanitize: specials, traversal, empty, limit
    eq(sanitize("a/b\\c:d*e?f<g>h|i"), "a_b_c_d_e_f_g_h_i", "sanitize.specials")
    eq(sanitize("../../etc/passwd"), "etc_passwd", "sanitize.traversal")
    eq(sanitize("   "), "chat", "sanitize.blank")
    eq(sanitize(""), "chat", "sanitize.empty")
    eq(sanitize("x" * 100, 40), "x" * 40, "sanitize.limit")

    # safe_ext: remote extensions are never trusted inside a path
    eq(safe_ext(".jpg", "Photo"), ".jpg", "ext.ok")
    eq(safe_ext(".php", "File"), "", "ext.untrusted")
    eq(safe_ext("../x", "File"), "", "ext.traversal")
    eq(safe_ext(None, "Photo"), ".jpg", "ext.default")
    eq(safe_ext("", "Video"), ".mp4", "ext.video_default")

    # norm_phone: local formats, 00-prefix, Persian digits, spacing junk
    eq(norm_phone("09121234567"), "+989121234567", "phone.local09")
    eq(norm_phone("9121234567"), "+989121234567", "phone.local9")
    eq(norm_phone("+989121234567"), "+989121234567", "phone.plus")
    eq(norm_phone("00989121234567"), "+989121234567", "phone.00")
    eq(norm_phone("۰۹۱۲۱۲۳۴۵۶۷"), "+989121234567", "phone.fa_digits")
    eq(norm_phone("(+44) 20 7946 0958"), "+442079460958", "phone.spaces")

    # Throttle: flood multiplier growth, cap, decay
    th = Throttle("normal")
    eq(th.mult, 1.0, "throttle.initial")
    for _ in range(5):
        th.flood(10)
    eq(th.mult, 4.0, "throttle.cap")
    for _ in range(200):
        th.ok()
    eq(th.mult, 1.0, "throttle.decay")
    eq(0 <= th.batch_pause() <= 32.0, True, "throttle.pause_range")

    # is_disk_full
    eq(is_disk_full(OSError(errno.ENOSPC, "no space")), True, "disk.enospc")
    eq(is_disk_full(OSError(errno.EACCES, "denied")), False, "disk.eacces")
    eq(is_disk_full(ValueError("x")), False, "disk.not_oserror")

    # ui_slice: a cut never leaves a dangling combining mark
    eq(ui_slice("abc", 5), "abc", "slice.short")
    eq(ui_slice("abcdef", 3), "abc", "slice.cut")
    eq(ui_slice("اب" + "ْ" + "cd", 3), "اب", "slice.combining")

    # say(): runaway tokens hard-wrap within WIDTH
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        say("x" * 200)
    eq(max(len(l) for l in buf.getvalue().splitlines()) <= max(20, int(WIDTH)),
       True, "say.wrap")

    # folder_for: a renamed title reuses (renames) the existing pid folder
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "old_title_123"))
        eq(folder_for(tmp, "new title", 123), os.path.join(tmp, "new_title_123"),
           "folder.rename")
        eq(os.path.isdir(os.path.join(tmp, "new_title_123")), True, "folder.moved")
        eq(folder_for(tmp, "another", 456), os.path.join(tmp, "another_456"),
           "folder.fresh")

    # json roundtrip
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "j.json")
        save_json(p, {"a": 1, "b": "فارسی"})
        eq(load_json(p), {"a": 1, "b": "فارسی"}, "json.roundtrip")
        eq(load_json(os.path.join(tmp, "missing.json")), None, "json.missing")

    # فیکس NB-12: بکاپ session — رمزنگاری/درودنگری + سلامت snapshot (بدون شبکه)
    if HAS_CRYPTO:
        blob = bk_encrypt(b"hello session", "p@ss")
        eq(bk_decrypt(blob, "p@ss"), b"hello session", "bk.roundtrip")
        eq(len(blob) > BK_HDR, True, "bk.len")
        badp = False
        try:
            bk_decrypt(blob, "wrong!")
        except Exception:
            badp = True
        eq(badp, True, "bk.wrongpass")
        badp = False
        try:
            bk_decrypt(b"XXXX2" + blob[5:], "p@ss")
        except Exception:
            badp = True
        eq(badp, True, "bk.badmagic")
        badp = False
        try:
            bk_decrypt(BK_MAGIC + b"x" * 10, "p@ss")
        except Exception:
            badp = True
        eq(badp, True, "bk.shortheader")
        with tempfile.TemporaryDirectory() as tmp:
            s = os.path.join(tmp, "tg_1.session")
            con = sqlite3.connect(s)
            con.execute("CREATE TABLE v(x)")
            con.commit()
            con.close()
            eq(_session_ok(s), True, "bk.sessok")
            eq(_session_ok(os.path.join(tmp, "nope.session")), False, "bk.sessmissing")
            with open(os.path.join(tmp, "zero.session"), "w"):
                pass
            eq(_session_ok(os.path.join(tmp, "zero.session")), False, "bk.sesscorrupt")
            dest = os.path.join(tmp, "backup", "tg_1.session.enc")
            eq(make_session_backup(s, dest, "pw"), True, "bk.mksnap")
            eq(os.path.exists(dest), True, "bk.file")
            eq(not os.path.exists(s + ".bksnap"), True, "bk.notmpplain")
            r = os.path.join(tmp, "r.session")
            eq(restore_session_backup(dest, r, password="pw"), True, "bk.restore")
            eq(_session_ok(r), True, "bk.restored")
            eq(restore_session_backup(dest + ".nope", os.path.join(tmp, "r2.session")), False,
               "bk.restore_missing")

    print(f"self-test: {ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)


def cli_main():
    if "--selftest" in sys.argv:
        _engine_selftest()
        return
    try:
        app_main()
    except LoginAbort:
        say(t("login_abort"))
    except (KeyboardInterrupt, EOFError):
        print()
        say(t("bye"))
    except Exception as e:
        say(t("fatal", e=short_err(e)))       # no raw tracebacks — project UX rule
        try:
            pause()
        except Exception:
            pass

# ═════════════════════════════════════════════════════════════════════════════════
# لایه‌ی وب — فایل یکپارچه: ts همان ماژول جاری است
# ═════════════════════════════════════════════════════════════════════════════════

import argparse
import asyncio
import os
import re
import secrets as _secrets
import sys
import threading
import time

# فایل یکپارچه‌ی telescrape.py: لایه‌ی وبِ همین فایل — موتور همان ماژول جاری است
ts = sys.modules[__name__]

ts._ensure("flask")                     # پیش‌نصبِ Colab؛ همان الگوی _ensure موتور (قابلیت WEB-01)
from flask import Flask, jsonify, request, send_file

# قابلیت WEB-01: خروجی وب باید روی Drive بماند — پیش‌فرضِ False موتور override می‌شود
ts.USE_DRIVE = True


# فیکس WB-01: اجرای وب با `!python` پروسه‌ی جدیدی است؛ IN_COLAB موتور
# ("google.colab" in sys.modules) در آن False می‌شود → Secretها/Drive/بکاپ سشن
# خاموش بودند. تشخیص واقعی + override فقط در لایه‌ی وب (موتور دست‌نخورده):
def _is_colab():
    """تشخیص واقعی Colab — حتی داخل پروسه‌ی جدا (nohup python ...)."""
    try:
        from google.colab import userdata  # noqa: F401
        return True
    except Exception:
        return False


if _is_colab():
    ts.IN_COLAB = True

# ────────────────────── پیام‌های دوزبانه‌ی لایه‌ی وب (قاعده ۴) ──────────────────────
W = {
    "fa": {
        "bad_api_id": "API ID نامعتبر است.",
        "bad_hash": "API Hash باید ۳۲ کاراکتر hex باشد.",
        "need_phone": "شماره لازم است (مثل +98912...).",
        "pwd_mismatch": "رمزهای بکاپ یکی نیستند.",
        "internal": "خطای داخلی: {e}",
        "wrong_phase": "این گام الان معتبر نیست.",
        "invalid_code": "کد نامعتبر است.",
        "code_expired_resent": "کد منقضی شده بود؛ کد جدید ارسال شد.",
        "resent": "کد جدید ارسال شد.",
        "need_password": "رمز دو مرحله‌ای لازم است.",
        "wrong_password": "رمز اشتباه است.",
        "not_ready": "اول وارد حساب شوید.",
        "client_dead": "اتصال تلگرام قطع شد؛ دوباره وارد شوید.",
        "not_found": "پیدا نشد.",
        "invalid_input": "ورودی نامعتبر است.",
        "job_running": "یک اسکرپ در جریان است؛ تا پایانش صبر کنید.",
        "job_started": "اسکرپ «{name}» شروع شد.",
        "zip_not_found": "فایل زیپ پیدا نشد.",
        "logged_out": "خارج شدید و سشن حذف شد.",
        "logout_partial": "سشن محلی حذف شد ولی خروج از سرور ناموفق بود؛ از Settings → Devices در خود تلگرام خارج شوید.",
        "backup_deleted": "بکاپ Drive هم حذف شد.",
        "backup_left": "حذف بکاپ Drive ناموفق بود؛ فایل هنوز آنجاست.",
        "csrf_bad": "توکن صفحه نامعتبر است؛ صفحه را رفرش کنید.",
        "not_found_404": "صفحه پیدا نشد.",
        "server_500": "خطای سرور؛ دوباره تلاش کنید.",
        # فیکس NB-21: لانچر درون‌فایلی Colab
        "no_launcher": "⚠️ سرور بدون لانچر اجرا شده؛ سکرت‌های Colab به آن نمی‌رسند. سلول «لانچر» COLAB.md را اجرا کنید.",
        "ln_ok": "✅ سکرت {name} خوانده شد",
        "ln_missing_req": "⚠️ سکرت {name} ساخته نشده (الزامی)",
        "ln_missing_opt": "➖ سکرت {name} ساخته نشده (اختیاری)",
        "ln_noaccess": "⛔ سکرت {name}: «Notebook access» خاموش است — در 🔑 Secrets روشنش کنید",
        "ln_timeout": "⛔ سکرت {name}: Colab پاسخ نداد (timeout) — لانچر را دوباره اجرا کنید",
        "ln_error": "⛔ سکرت {name}: خطا — {e}",
        "ln_port_busy": "⛔ پورت {port} آزاد نشد؛ Runtime → Restart session بزنید یا پورت دیگری بدهید: colab_launch(port=8001)",
        "ln_started": "🚀 سرور اجرا شد (پورت {port}؛ لاگ: web.log)",
        "ln_verified": "✅ سرور این سکرت‌ها را دریافت کرد: {names}",
        "ln_none_passed": "ℹ️ هیچ سکرتی پاس نشد؛ مقادیر را دستی در فرم وارد می‌کنید.",
        "ln_mismatch": "⚠️ سرور این سکرت‌ها را ندید: {names} — احتمالاً پروسه‌ی قدیمی روی پورت است؛ لانچر را دوباره اجرا کنید.",
        "ln_crashed": "⛔ سرور بالا نیامد؛ آخر web.log:",
        "ln_slow": "⛔ سرور تا {s} ثانیه پاسخ نداد؛ آخر web.log:",
        "ln_url": "🔗 این لینک را روی گوشی باز کنید:",
        "ln_nokernel": "⚠️ این تابع باید داخل سلول نوت‌بوک Colab اجرا شود (سکرت‌ها فقط آنجا خوانده می‌شوند).",
        "ln_gate_on": "🔒 قفل دسترسی عمومی روشن شد (APP_TOKEN).",
        "ln_no_app_token": "⛔ سکرت APP_TOKEN پیدا نشد — برای لینک عمومی الزامی است. در 🔑 Secrets مقداری الفبایی-عدمی بسازید، «Notebook access» را روشن کنید و لانچر را دوباره اجرا کنید.",
        "ln_tunnel_url": "🌍 لینک عمومی (روی هر دستگاهی و بدون لاگین گوگل باز می‌شود):",
        "ln_tunnel_fail": "⛔ تونل cloudflared تا {s} ثانیه لینک نداد؛ لانچر را دوباره اجرا کنید. اگر تکرار شد، روش‌های جایگزین (localtunnel/ngrok) در COLAB.md.",
        # UIUX-2: Drive از لانچر + انتقال زیپ به پوشهٔ دلخواه
        "ln_drive_mounting": "🔌 در حال اتصال Google Drive…",
        "ln_drive_ok": "✅ Drive وصل شد — خروجی‌ها و بکاپ سشن روی Drive می‌مانند",
        "ln_drive_fail": "⚠️ اتصال Drive ناموفق: {e} — خروجی روی دیسک موقت می‌ماند",
        "mv_bad_dest": "مسیر مقصد نامعتبر است — مسیر مطلق بدهید (در Colab فقط داخل /content، مثل /content/drive/MyDrive/…).",
        "mv_fail": "انتقال زیپ ناموفق بود: {e}",
    },
    "en": {
        "bad_api_id": "Invalid API ID.",
        "bad_hash": "API Hash must be 32 hex characters.",
        "need_phone": "Phone number is required (e.g. +98912...).",
        "pwd_mismatch": "Backup passwords do not match.",
        "internal": "Internal error: {e}",
        "wrong_phase": "This step is not valid right now.",
        "invalid_code": "Invalid code.",
        "code_expired_resent": "The code had expired; a new one was sent.",
        "resent": "New code sent.",
        "need_password": "The 2FA password is required.",
        "wrong_password": "Wrong password.",
        "not_ready": "Log in first.",
        "client_dead": "Telegram connection lost; log in again.",
        "not_found": "Not found.",
        "invalid_input": "Invalid input.",
        "job_running": "A scrape is already running; wait for it to finish.",
        "job_started": "Scraping \"{name}\" started.",
        "zip_not_found": "Zip file not found.",
        "logged_out": "Logged out; session deleted.",
        "logout_partial": "Local session deleted but server-side logout failed; sign out via Settings → Devices in Telegram.",
        "backup_deleted": "Drive backup deleted too.",
        "backup_left": "Could not delete the Drive backup; the file is still there.",
        "csrf_bad": "Page token mismatch; refresh the page.",
        "not_found_404": "Page not found.",
        "server_500": "Server error; try again.",
        # NB-21: in-file Colab launcher
        "no_launcher": "⚠️ The server was started without the launcher; Colab Secrets cannot reach it. Run the launcher cell from COLAB.md.",
        "ln_ok": "✅ Secret {name} read",
        "ln_missing_req": "⚠️ Secret {name} is not created (required)",
        "ln_missing_opt": "➖ Secret {name} is not created (optional)",
        "ln_noaccess": "⛔ Secret {name}: \"Notebook access\" is off — turn it on in 🔑 Secrets",
        "ln_timeout": "⛔ Secret {name}: Colab did not answer (timeout) — run the launcher again",
        "ln_error": "⛔ Secret {name}: error — {e}",
        "ln_port_busy": "⛔ Port {port} did not free up; use Runtime → Restart session or pick another port: colab_launch(port=8001)",
        "ln_started": "🚀 Server started (port {port}; log: web.log)",
        "ln_verified": "✅ The server received these secrets: {names}",
        "ln_none_passed": "ℹ️ No secrets were passed; you will enter the values in the form.",
        "ln_mismatch": "⚠️ The server did not see these secrets: {names} — an old process probably holds the port; run the launcher again.",
        "ln_crashed": "⛔ The server did not start; tail of web.log:",
        "ln_slow": "⛔ The server did not answer within {s}s; tail of web.log:",
        "ln_url": "🔗 Open this link on your phone:",
        "ln_nokernel": "⚠️ Run this inside a Colab notebook cell (Secrets can only be read there).",
        "ln_gate_on": "🔒 Public access lock enabled (APP_TOKEN).",
        "ln_no_app_token": "⛔ Secret APP_TOKEN not found — required for a public link. Create an alphanumeric value in 🔑 Secrets, enable \"Notebook access\", and run the launcher again.",
        "ln_tunnel_url": "🌍 Public link (opens on any device, no Google sign-in):",
        "ln_tunnel_fail": "⛔ cloudflared gave no URL within {s}s; run the launcher again. If it repeats, use the alternatives (localtunnel/ngrok) in COLAB.md.",
        # UIUX-2: Drive via launcher + zip move to a custom folder
        "ln_drive_mounting": "🔌 Connecting Google Drive…",
        "ln_drive_ok": "✅ Drive connected — outputs and session backup stay on Drive",
        "ln_drive_fail": "⚠️ Drive connection failed: {e} — outputs stay on the temporary disk",
        "mv_bad_dest": "Invalid destination — give an absolute path (in Colab only inside /content, e.g. /content/drive/MyDrive/…).",
        "mv_fail": "Zip move failed: {e}",
    },
}


def wt(key, **kw):
    """رشته‌ی دوزبانه — همان قاعده‌ی ۴ پروژه، برای لایه‌ی وب (قابلیت WEB-03)."""
    s = W.get(ts.LANG, W["en"]).get(key) or W["en"][key]
    return s.format(**kw) if kw else s


# ────────────────────── state کلی — فقط RAM (قابلیت WEB-01) ──────────────────────
# فازها: setup → need_code → need_2fa → ready؛ need_login = کلاینت وسط کار مُرد (WEB-06)
LOCK = threading.RLock()
STATE = {
    "phase": "setup",
    "app": None,                # ts.App (پوشه‌ها/تنظیمات)
    "client": None,             # کلاینت تلگرام
    "api_id": None,
    "api_hash": None,           # هرگز در پاسخ API نمی‌آید (WEB-06)
    "phone": None,
    "enc_pw": None,             # رمز بکاپ از فرم — فقط RAM (WEB-03)
    "restored": False,
    "who": "",
    "items": [],                # کش دیالوگ‌ها (entity هرگز بیرون نمی‌رود — WEB-04)
    "resolve_cache": {},        # آیتم‌های resolve‌شده برای شروع اسکرپ
    "job": None,
    "last_result": None,
    "secrets": {},
    "token": _secrets.token_hex(24),   # قابلیت WEB-06: CSRF تک‌کاربره
    "access_key": "",                  # موج TUN: رمز لینک عمومی (APP_TOKEN) — خالی یعنی بدون قفل
}


def _detect_secrets():
    """کدام Secretها موجودند — فرم فقط برای نبودشان نشان داده می‌شود (قابلیت WEB-01)."""
    STATE["secrets"] = {k: bool(ts.get_secret(k))
                        for k in ("api_id", "api_hash", "phone", "pwd", "enc_key")}


def _client_alive():
    try:
        c = STATE["client"]
        return bool(c and c.is_connected())
    except Exception:
        return False


def _net_error(e):
    """خطای شبکه → اگر کلاینت مُرده، فاز need_login؛ وگرنه خطای داخلی (قابلیت WEB-06)."""
    if not _client_alive():
        STATE["phase"] = "need_login"
        return jsonify({"error": wt("client_dead"), "phase": "need_login"}), 503
    return jsonify({"error": wt("internal", e=ts.short_err(e))}), 500


def _bg_call(coro, timeout=90):
    """اجرای coroutine روی loop موتور + timeout — مسیر وب هرگز سلول Colab را بلاک نمی‌کند."""
    fut = asyncio.run_coroutine_threadsafe(coro, ts._bg_loop())
    return fut.result(timeout)


def _to_int(v, default=None):
    try:
        return int(str(v).translate(ts._DIGITS).strip())
    except (TypeError, ValueError):
        return default


def _valid_api_id(v):
    n = _to_int(v)
    return n if n and n > 0 else None


def _valid_hash(h):
    h = str(h or "").strip().translate(ts._DIGITS)
    return h if re.fullmatch(r"[0-9a-fA-F]{32}", h) else None


def _zip_path(zdir, name):
    """گارد زیپ: فقط نامِ ساده‌ی داخل zdir — بدون path traversal (WEB-04/WEB-06)."""
    name = os.path.basename(str(name or ""))
    if not name:
        return None
    zdir = os.path.realpath(zdir)
    direct = os.path.join(zdir, name)
    path = os.path.realpath(direct)
    if os.path.dirname(path) != zdir:
        return None
    # فیکس WEB-08: symlink داخل zips رد می‌شود — هدفش هرجا باشد، فقط فایل واقعی سرو می‌شود
    if os.path.islink(direct):
        return None
    return path


# فیکس WB-11: اسکلت مرده‌ی PAGE (نسخه‌ی اول WEB-01) حذف شد — فقط PAGE کامل پایین فایل معتبر است


def _safe_dest(raw, colab=None):
    """UIUX-2: مسیر مقصدِ انتقال زیپ — مطلق و امن؛ در Colab فقط زیر /content؛ هرگز داخل session."""
    if colab is None:
        colab = ts.IN_COLAB
    s = os.path.expanduser(str(raw or "").strip())
    if not s.startswith("/") or "\x00" in s:
        return None
    p = os.path.abspath(s)
    if colab and not (p == "/content" or p.startswith("/content/")):
        return None
    app = STATE.get("app")
    if app is not None:
        sd = os.path.abspath(app.session_dir)
        if p == sd or p.startswith(sd + os.sep):
            return None
    return p


def _bad_token():
    if _check_token():
        return None
    return jsonify({"error": wt("csrf_bad")}), 403


# ────────────────────── صفحه‌ی تک‌فایلی — فرانت کامل در پایین فایل تعریف می‌شود (قابلیت WEB-05) ──────────────────────
# فیکس WB-11: اسکلت مرده‌ی PAGE حذف شد؛ PAGE کامل در بخش WEB-05 تعریف می‌شود

web = Flask(__name__)


@web.route("/")
def index():
    """صفحه‌ی تک‌فایلی — inline، بدون پوشه‌ی templates (قابلیت WEB-01/WEB-05)."""
    return PAGE


@web.route("/api/state")
def api_state():
    """فاز فعلی + token CSRF — پایه‌ی بوت فرانت (قابلیت WEB-01)."""
    _detect_secrets()          # فیکس NB-15: تشخیص تازه در هر بوتِ صفحه (مسیر env ارزان است)
    # فیکس NB-21: در Colab اگر سرور بدون لانچر بالا آمده، رابط دلیلِ نرسیدن سکرت را نشان می‌دهد
    _hint = "" if (os.environ.get("TS_WEB_CHILD") or not ts.IN_COLAB) else wt("no_launcher")
    with LOCK:
        return jsonify({
            "hint": _hint,
            "phase": STATE["phase"],
            "token": STATE["token"],
            "who": STATE["who"],
            "restored": STATE["restored"],
            "bk_active": _bk_active(),  # فیکس WB-13: وضعیت بکاپ سشن برای کارت «حساب»
            "phone": STATE["phone"] or "",   # UIUX-2: شمارهٔ حساب برای کارت «حساب»
            "drive": bool(ts.USE_DRIVE and os.path.isdir("/content/drive/MyDrive")),  # UIUX-2
            "base": (STATE["app"].base if STATE["app"] else ""),   # UIUX-2: مسیر خروجی فعلی
            "secrets": dict(STATE["secrets"]),
            "has_job": bool(STATE["job"] and not STATE["job"]["done"]),
        })


def _parse_args():
    """--port پیش‌فرض ۸۰۰۰؛ --no-drive خروجی را در دیسک موقت نگه می‌دارد (قابلیت WEB-01)."""
    p = argparse.ArgumentParser(description="TeleScrape Web")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-drive", action="store_true",
                   help="خروجی در دیسک موقت بماند (پیش‌فرض وب: Drive)")
    return p.parse_args()


# ═══════════ NB-21: لانچر درون‌فایلی Colab (جایگزین سلول بلندِ کپی‌شونده) ═══════════
# ریشه‌ها: (۱) سلول دانلود با `wget -q` روی فایل موجود، نسخه‌ی تازه را بی‌صدا در
# telescrape.py.1 می‌ریخت و نسخه‌ی قدیمی اجرا می‌شد؛ (۲) لانچر دستی هر خطای userdata را
# قورت می‌داد (سکرت نیست؟ Notebook access خاموش؟ timeout؟) و فقط نام‌های اصلی را می‌خواند؛
# (۳) هیچ‌جا چک نمی‌شد که سرور واقعاً سکرت‌ها را گرفته. این تابع در کرنل اجرا می‌شود.

_LN_REQUIRED = ("api_id", "api_hash")
_LN_RANK = {"missing": 0, "error": 1, "timeout": 2, "no_access": 3}   # وضعیتِ آموزنده‌تر برنده است


def _read_kernel_secrets(getter):
    """سکرت‌ها را با getter (در Colab: userdata.get) می‌خواند — فقط داخل کرنل جواب می‌دهد.
    خروجی: (values {نام اصلی سکرت: مقدار}, report {کلید: وضعیت}). نام‌های جایگزین هم دیده
    می‌شوند و زیر نام اصلی پاس داده می‌شوند. وضعیت: ok | missing | no_access | timeout |
    error:<نوع خطا>. هرگز مقدار یا متن خطا در report نمی‌آید (فقط نوع خطا)."""
    values, report = {}, {}
    for key, names in SECRET_NAMES.items():
        status = "missing"
        for name in names:
            try:
                v = getter(name)
            except Exception as e:
                en = type(e).__name__
                if en == "SecretNotFoundError":
                    s = "missing"
                elif en == "NotebookAccessError":
                    s = "no_access"
                elif "timed out" in str(e).lower() or en in ("TimeoutException", "TimeoutError"):
                    s = "timeout"
                else:
                    s = "error:" + en
                if _LN_RANK[s.split(":", 1)[0]] > _LN_RANK[status.split(":", 1)[0]]:
                    status = s
                continue
            if v and str(v).strip():
                values[names[0]] = str(v).strip()
                status = "ok"
                break
        report[key] = status
    return values, report


def _port_free(port):
    """پورت آزاد است؟ با SO_REUSEADDR تا هم‌رفتار با Flask باشد (فیکس NB-21)."""
    import socket
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _print_log_tail(path="web.log", n=1500):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            print(f.read()[-n:])
    except OSError:
        pass


_TUNNEL_WAIT = 60     # موج TUN: حداکثر انتظار (ثانیه) برای لینک trycloudflare


def _start_cloudflared(port, wait=_TUNNEL_WAIT):
    """موج TUN: تونل عمومی cloudflared (بدون اکانت) — فقط همان پورت برنامه expose می‌شود.
    موفق: https://...trycloudflare.com  |  شکست: None. هر اجرا لینک تازه می‌دهد."""
    exe = "cloudflared"
    cf_url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
              "cloudflared-linux-amd64")
    if not (os.path.isfile(exe) and os.path.getsize(exe) > 1_000_000):   # نیمه‌خراب = دوباره بگیر
        subprocess.run(["wget", "-q", cf_url, "-O", exe], check=False)
        if not (os.path.isfile(exe) and os.path.getsize(exe) > 1_000_000):
            return None
        os.chmod(exe, 0o755)
    try:
        subprocess.run(["pkill", "-f", r"[c]loudflared"], capture_output=True)   # تونل قبلی
    except OSError:
        pass
    try:
        proc = subprocess.Popen([os.path.abspath(exe), "tunnel", "--url", f"http://localhost:{port}",
                                 "--no-autoupdate"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    buf = []

    def _pump():
        for line in proc.stderr:
            buf.append(line)

    threading.Thread(target=_pump, daemon=True).start()
    deadline = time.time() + wait
    while time.time() < deadline and proc.poll() is None:
        for line in buf:
            m = re.search(r"https://[-a-zA-Z0-9]+\.trycloudflare\.com", line)
            if m:
                return m.group(0)          # پروسه‌ی تونل زنده می‌ماند و به پورت وصل است
        time.sleep(0.5)
    return None


def colab_launch(port=8000, boot_wait=30, getter=None, show_url=True, tunnel=False, drive=False):
    """لانچر Colab — داخل یک سلولِ نوت‌بوک اجرا کنید (COLAB.md، سلول ۲):
        import sys; sys.modules.pop("telescrape", None)
        import telescrape; telescrape.colab_launch()
    سکرت‌ها را در کرنل می‌خواند (وضعیت هر کدام را می‌گوید)، پروسه‌ی قدیمی را می‌کشد، سرور را
    با سکرت‌ها در env (فقط RAM) اجرا می‌کند و از خودِ سرور تایید می‌گیرد که آنها را دیده.
    True یعنی همه‌ی سکرت‌های خوانده‌شده به سرور رسیده‌اند و سرور بالاست.
    tunnel=True (موج TUN): به‌جای لینک proxyPort، لینک عمومی cloudflared می‌سازد —
    نیازمند سکرت APP_TOKEN؛ بدون آن متوقف می‌شود. درخواست بدون key صحیح → 401.
    drive=True (موج UIUX-2): پیش از اجرای سرور، Google Drive را در همین کرنل ماونت
    می‌کند — خروجی‌ها و بکاپ سشن روی Drive می‌مانند و UI وضعیت را نشان می‌دهد."""
    import urllib.request, urllib.parse
    # UIUX-2: mount تعاملی Drive فقط در کرنل ممکن است؛ سرورِ فرزند پوشهٔ ازپیش‌ماونت
    # را تشخیص می‌دهد (USE_DRIVE در main). drive=True → اتصال همین‌جا، پیش از سرور.
    if drive:
        try:
            from google.colab import drive as _gd
            print(wt("ln_drive_mounting"))
            _gd.mount("/content/drive")
            print(wt("ln_drive_ok"))
        except Exception as e:
            print(wt("ln_drive_fail", e=ts.short_err(e)))
    if getter is None:
        try:
            from google.colab import userdata
            getter = userdata.get
        except Exception:
            print(wt("ln_nokernel"))
            return False
    values, report = _read_kernel_secrets(getter)
    for key, st in report.items():
        name = SECRET_NAMES[key][0]
        kind, _, detail = st.partition(":")
        if kind == "ok":
            print(wt("ln_ok", name=name))
        elif kind == "missing":
            print(wt("ln_missing_req" if key in _LN_REQUIRED else "ln_missing_opt", name=name))
        elif kind == "no_access":
            print(wt("ln_noaccess", name=name))
        elif kind == "timeout":
            print(wt("ln_timeout", name=name))
        else:
            print(wt("ln_error", name=name, e=detail or "?"))

    # موج TUN: رمز دسترسی لینک عمومی — فقط از Secrets؛ هرگز در کد/لاگ/ریپو نمی‌آید
    app_token = ""
    if tunnel:
        try:
            app_token = str(getter("APP_TOKEN") or "").strip()
        except Exception as e:
            print(wt("ln_no_app_token") + " (" + ts.short_err(e) + ")")
            return False
        if not app_token:
            print(wt("ln_no_app_token"))
            return False

    # پروسه‌ی قدیمی (بدون سکرتِ تازه) را بکش و تا آزاد شدن پورت صبر کن (NB-20)
    try:
        subprocess.run(["pkill", "-f", r"[t]elescrape\.py"], capture_output=True)
    except OSError:
        pass
    for _ in range(20):
        if _port_free(port):
            break
        time.sleep(0.5)
    else:
        print(wt("ln_port_busy", port=port))
        return False

    env = {**os.environ, "TS_WEB_CHILD": "1", "PYTHONUNBUFFERED": "1"}
    env.update(values)
    if app_token:
        env["TS_ACCESS_TOKEN"] = app_token   # موج TUN: قفل لینک عمومی — فقط RAM
    script = os.path.abspath(globals().get("__file__") or "telescrape.py")
    with open("web.log", "w") as logf:
        proc = subprocess.Popen([sys.executable, script, "--port", str(port)],
                                env=env, stdout=logf, stderr=logf)
    print(wt("ln_started", port=port))

    state = None
    deadline = time.time() + boot_wait
    _probe = f"http://127.0.0.1:{port}/api/state"
    if app_token:
        _probe += "?key=" + urllib.parse.quote(app_token, safe="")   # موج TUN: رد شدن از قفل
    while time.time() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(_probe, timeout=2) as r:
                state = json.loads(r.read().decode("utf-8"))
            break
        except Exception:
            time.sleep(0.5)
    if state is None:
        print(wt("ln_crashed" if proc.poll() is not None else "ln_slow", s=boot_wait))
        _print_log_tail()
        return False

    # تایید سرتاسری: خودِ سرور باید همان سکرت‌هایی را ببیند که در کرنل خوانده شد
    seen = state.get("secrets") or {}
    sent = [k for k in SECRET_NAMES if SECRET_NAMES[k][0] in values]
    got = [SECRET_NAMES[k][0] for k in sent if seen.get(k)]
    lost = [SECRET_NAMES[k][0] for k in sent if not seen.get(k)]
    if not sent:
        print(wt("ln_none_passed"))
    elif lost:
        print(wt("ln_mismatch", names=", ".join(lost)))
    else:
        print(wt("ln_verified", names=", ".join(got)))
    if tunnel:
        link = _start_cloudflared(port)
        if not link:
            print(wt("ln_tunnel_fail", s=_TUNNEL_WAIT))
            return False
        print(wt("ln_tunnel_url"))
        # لینک آمادهٔ کلیک؛ key فقط در خروجی همین سلول است (RAM) و بعد از اولین باز شدن به کوکی تبدیل می‌شود
        print(link + "/?key=" + urllib.parse.quote(app_token, safe=""))
    elif show_url:
        try:
            from google.colab import output
            link = output.eval_js(f"google.colab.kernel.proxyPort({port})")
            print(wt("ln_url"))
            print(link)
        except Exception:
            pass
    return not lost


def main():
    if "--selftest-engine" in sys.argv:          # سوئیت موتور به‌تنهایی (فایل یکپارچه)
        _engine_selftest()
        return
    if "--selftest" in sys.argv:                 # فایل یکپارچه: اول موتور، بعد وب (WEB-07)
        print("[telescrape] ۱/۲ — سوئیت موتور …")
        rc = subprocess.call([sys.executable, os.path.abspath(__file__),
                              "--selftest-engine"])
        if rc != 0:
            raise SystemExit(rc)
        print("[telescrape] ۲/۲ — سوئیت وب …")
        fn = globals().get("_selftest")
        if fn is None:
            print("selftest: not available in this build")
            raise SystemExit(1)
        fn()
        return
    if "--cli" in sys.argv:                      # حالت ترمینال کامل موتور
        cli_main()
        return
    args = _parse_args()
    # فیکس NB-21: پرچم پروسه‌ی فرزند از همان ابتدا (قبل از ساخت App) تا هیچ get_secretی
    # به userdata نرسد؛ سکرت‌ها فقط از env لانچر می‌آیند.
    if os.environ.get("TS_WEB_CHILD"):
        ts._USERDATA_DEAD = True
    if args.no_drive:
        ts.USE_DRIVE = False
    # فیکس WB-02: App/Drive در استارتاپ آماده می‌شود، نه در thread درخواست —
    # drive.mount تعاملی داخل nohup می‌تواند کل سرور را ببن‌دست کند.
    if ts.IN_COLAB and ts.USE_DRIVE and not os.path.isdir("/content/drive/MyDrive"):
        ts.USE_DRIVE = False
        print("⚠️ Drive ماونت نیست: خروجی روی دیسک موقت می‌ماند (با ریست ران‌تایم پاک می‌شود).")
        print("   برای ماندگاری، در یک سلول اجرا کنید: from google.colab import drive; drive.mount('/content/drive')")
    try:
        STATE["app"] = ts.App()     # چون USE_DRIVE فقط با درایوِ ازپیش‌ماونت True می‌ماند، mount تعاملی در کار نیست
    except Exception as e:
        print(f"⚠️ ساخت App ناموفق بود ({ts.short_err(e)})؛ در اولین درخواست دوباره تلاش می‌شود.")
    # فیکس NB-15: سکرت‌ها را لانچر از کرنل خوانده و به‌صورت env پاس داده است؛
    # (بی‌اثر کردن userdata در فرزند: فیکس NB-21 — بالاتر، پیش از ساخت App)
    _detect_secrets()
    # فیکس NB-15 (پیگیری): web.log باید بگوید کدام سکرت‌ها به سرور رسیده (فقط نام‌ها)
    _got = [k for k, v in STATE["secrets"].items() if v]
    print("سکرت‌های تشخیص‌داده‌شده:", _got or "هیچ — سکرت‌ها پاس نشده‌اند؛ سلول لانچر COLAB.md را اجرا کنید")
    # فیکس NB-20: پورتِ اشغاری یعنی پروسه‌ی قدیمی (بدون سکرتِ تازه) دارد سرو می‌دهد
    import socket as _sock
    _probe = _sock.socket()
    _probe.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)   # فیکس NB-21: هم‌رفتار با Flask
    try:
        _probe.bind(("0.0.0.0", args.port))
        _probe.close()
    except OSError:
        _probe.close()
        print(f"⛔ پورت {args.port} اشغال است — احتمالاً پروسه‌ی قدیمی روی همان پورت سرویس می‌دهد و سکرت‌ها به آن نرسیده‌اند.")
        print("   در یک سلولِ کرنل اجرا کنید: !pkill -f telescrape.py — بعد دوباره سلول لانچر COLAB.md.")
        raise SystemExit(1)
    # موج TUN: قفل دسترسی لینک عمومی — سکرت APP_TOKEN را لانچر به env رسانده است
    STATE["access_key"] = (os.environ.get("TS_ACCESS_TOKEN") or "").strip()
    if STATE["access_key"]:
        print(wt("ln_gate_on"))
    print(f"TeleScrape Web listening on 0.0.0.0:{args.port}")
    print("برای باز کردن روی گوشی، در یک سلولِ کرنل اجرا کنید (proxyPort به‌جای تابع منسوخ‌شده):")
    print("    from google.colab import output")
    print(f"    print(output.eval_js('google.colab.kernel.proxyPort({args.port})'))")
    web.run(host="0.0.0.0", port=args.port, threaded=True)


# ═══════════════ WEB-03: ماشین حالت لاگین گام‌به‌گام (REST) ═══════════════

def _get_app():
    """App موتور — mount Drive در Colab (USE_DRIVE=True)؛ فقط بار اول ساخته می‌شود."""
    if STATE["app"] is None:
        STATE["app"] = ts.App()
    return STATE["app"]


def _bk_active():
    """بکاپ سشن فقط در Colab + Drive + cryptography — همان NB-12، بدون say() (قابلیت WEB-03)."""
    return bool(ts.SESSION_BACKUP and ts.IN_COLAB and ts.USE_DRIVE and ts.HAS_CRYPTO)


def _finish_login():
    """بعد از ورود: _get_me (NB-09) → بکاپ اگر restore نبود (NB-12) → کش دیالوگ‌ها → ready.
    فیکس WB-14: همه‌ی فراخوانی‌های شبکه بیرون از LOCK؛ نوشتهای STATE زیر LOCK اتمیک‌اند."""
    client = STATE["client"]
    try:
        me = _bg_call(ts._get_me(client), 90)
    except Exception as e:
        return _net_error(e)
    who = " ".join((ts.utils.get_display_name(me) or str(me.id)).split())
    backup = "backup_off"
    if _bk_active() and not STATE["restored"]:
        app = STATE["app"]
        pw = ts.get_secret("enc_key") or STATE["enc_pw"]
        if pw and ts.make_session_backup(app.session_path(STATE["api_id"]) + ".session",
                                         app.backup_path(STATE["api_id"]), pw):
            backup = "backup_ok"
    try:
        items = _bg_call(ts.load_dialogs(client), 600) or []
    except Exception:
        items = []
    with LOCK:                                 # فیکس WB-14: who/items/phase با هم
        STATE["who"] = who
        STATE["items"] = items
        STATE["phase"] = "ready"
    return jsonify({"phase": "ready", "who": STATE["who"],
                    "restored": STATE["restored"], "backup": backup})


def _connect_and_flow():
    """connect → authorized؟ ورود کامل؛ وگرنه send_code → فاز need_code (قابلیت WEB-03)."""
    client = STATE["client"]
    try:
        _bg_call(client.connect(), 90)
    except ts.FloodWaitError as e:
        return jsonify({"wait": int(e.seconds) + 2})
    except Exception as e:
        return _net_error(e)
    try:
        ok = _bg_call(client.is_user_authorized(), 60)
    except Exception as e:
        return _net_error(e)
    if ok:
        return _finish_login()
    try:
        _bg_call(client.send_code_request(STATE["phone"]), 90)
    except ts.FloodWaitError as e:
        with LOCK:                              # فیکس WB-14: نوشتن phase زیر LOCK
            STATE["phase"] = "need_code"
        return jsonify({"wait": int(e.seconds) + 2, "phase": "need_code"})
    except Exception as e:
        return _net_error(e)
    with LOCK:                                  # فیکس WB-14
        STATE["phase"] = "need_code"
    return jsonify({"phase": "need_code"})


@web.route("/api/login/start", methods=["POST"])
def api_login_start():
    """creds از Secret یا فرم + شماره → restore قبل از کلاینت (NB-12) → گام اول لاگین."""
    bad = _bad_token()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    with LOCK:
        if STATE["phase"] == "ready":
            return jsonify({"phase": "ready", "who": STATE["who"]})
        if STATE["phase"] in ("need_code", "need_2fa"):
            return jsonify({"phase": STATE["phase"]})
        api_id = _valid_api_id(ts.get_secret("api_id")) or _valid_api_id(data.get("api_id"))
        api_hash = _valid_hash(ts.get_secret("api_hash")) or _valid_hash(data.get("api_hash"))
        if api_id is None:
            return jsonify({"error": wt("bad_api_id"), "field": "api_id"}), 400
        if api_hash is None:
            return jsonify({"error": wt("bad_hash"), "field": "api_hash"}), 400
        phone = ts.norm_phone(data.get("phone") or ts.get_secret("phone") or "")
        if not phone or not phone.startswith("+"):
            return jsonify({"error": wt("need_phone"), "field": "phone"}), 400
        pw1 = str(data.get("enc_pw") or "").strip()
        pw2 = str(data.get("enc_pw2") or "").strip()
        if pw1 or pw2:
            if pw1 != pw2:
                return jsonify({"error": wt("pwd_mismatch"), "field": "enc_pw"}), 400
            STATE["enc_pw"] = pw1
        old = STATE["client"]                 # فاز need_login: کلاینت مرده را ببند (WEB-06)
        if old is not None:
            try:
                _bg_call(ts._disconnect(old), 30)
            except Exception:
                pass
        STATE.update(api_id=api_id, api_hash=api_hash, phone=phone, client=None,
                     restored=False, items=[], resolve_cache={}, who="")
        try:
            app = _get_app()
        except Exception as e:
            return jsonify({"error": wt("internal", e=ts.short_err(e))}), 500
        sess = app.session_path(api_id) + ".session"
        if _bk_active() and not ts._session_ok(sess) \
                and os.path.exists(app.backup_path(api_id)):   # فیکس NB-12: restore قبل از کلاینت
            pw = ts.get_secret("enc_key") or STATE["enc_pw"]
            if pw and ts.restore_session_backup(app.backup_path(api_id), sess, password=pw):
                STATE["restored"] = True
        try:
            STATE["client"] = _bg_call(
                ts.make_client(app.session_path(api_id), api_id, api_hash), 60)
        except Exception as e:
            return jsonify({"error": wt("internal", e=ts.short_err(e))}), 500
        app.lock_session(api_id)
        return _connect_and_flow()


@web.route("/api/login/code", methods=["POST"])
def api_login_code():
    """کد پیامک؛ خطاها نگاشت به پیام‌های W؛ FloodWait → {wait} بدون ریست state (WEB-03)."""
    bad = _bad_token()
    if bad:
        return bad
    if STATE["phase"] != "need_code":
        return jsonify({"error": wt("wrong_phase"), "phase": STATE["phase"]}), 409
    code = str((request.get_json(silent=True) or {}).get("code") or "").replace(" ", "")
    code = code.translate(ts._DIGITS)
    if not re.fullmatch(r"\d{4,8}", code):
        return jsonify({"error": wt("invalid_code"), "field": "code"}), 400
    client = STATE["client"]
    try:
        _bg_call(client.sign_in(STATE["phone"], code), 90)
    except ts.SessionPasswordNeededError:
        STATE["phase"] = "need_2fa"
        return jsonify({"phase": "need_2fa"})
    except ts.PhoneCodeInvalidError:
        return jsonify({"error": wt("invalid_code"), "field": "code"}), 400
    except ts.PhoneCodeExpiredError:
        try:
            _bg_call(client.send_code_request(STATE["phone"]), 90)
            return jsonify({"error": wt("code_expired_resent"), "field": "code"}), 400
        except ts.FloodWaitError as e:
            return jsonify({"wait": int(e.seconds) + 2})
        except Exception:
            return jsonify({"error": wt("invalid_code"), "field": "code"}), 400
    except ts.FloodWaitError as e:
        return jsonify({"wait": int(e.seconds) + 2})
    except Exception as e:
        return _net_error(e)
    return _finish_login()


@web.route("/api/login/resend", methods=["POST"])
def api_login_resend():
    """دکمه‌ی «ارسال دوباره» — همان مسیر تعاملی موتور، بدون ask() (قابلیت WEB-03)."""
    bad = _bad_token()
    if bad:
        return bad
    if STATE["phase"] != "need_code":
        return jsonify({"error": wt("wrong_phase"), "phase": STATE["phase"]}), 409
    try:
        _bg_call(STATE["client"].send_code_request(STATE["phone"]), 90)
    except ts.FloodWaitError as e:
        return jsonify({"wait": int(e.seconds) + 2})
    except Exception as e:
        return _net_error(e)
    return jsonify({"ok": True, "msg": wt("resent")})


@web.route("/api/login/2fa", methods=["POST"])
def api_login_2fa():
    """رمز دو مرحله‌ای — فقط اگر سرور در فاز need_2fa باشد (قابلیت WEB-03)."""
    bad = _bad_token()
    if bad:
        return bad
    if STATE["phase"] != "need_2fa":
        return jsonify({"error": wt("wrong_phase"), "phase": STATE["phase"]}), 409
    pw = str((request.get_json(silent=True) or {}).get("password") or "")
    if not pw:
        # فیکس WB-07: ورودی خالی → رمز از Secret TG_2FA_PASSWORD خوانده می‌شود
        pw = str(ts.get_secret("pwd") or "")
    if not pw:
        return jsonify({"error": wt("need_password"), "field": "password"}), 400
    try:
        _bg_call(STATE["client"].sign_in(password=pw), 90)
    except ts.PasswordHashInvalidError:
        return jsonify({"error": wt("wrong_password"), "field": "password"}), 400
    except ts.FloodWaitError as e:
        return jsonify({"wait": int(e.seconds) + 2})
    except Exception as e:
        return _net_error(e)
    return _finish_login()


# ═══════════════ WEB-04: API چت‌ها، اجرا، پیشرفت، زیپ، تنظیمات، خروج ═══════════════

PER_PAGE_WEB = 20


def _require_ready():
    """گارد فاز: همه‌ی APIهای اصلی فقط در فاز ready (قابلیت WEB-04/WEB-06)."""
    if STATE["phase"] != "ready" or STATE["client"] is None:
        return jsonify({"error": wt("not_ready"), "phase": STATE["phase"]}), 409
    return None


def _find_item(cid):
    for it in STATE["items"]:
        if it["id"] == cid:
            return it
    return STATE["resolve_cache"].get(cid)


def _res_payload(res):
    """نتیجه‌ی scrape برای JSON — فقط فیلدهای امن (قابلیت WEB-04)."""
    # فیکس WEB-08: dict خالی هم payload پیش‌فرض برمی‌گرداند؛ فقط None خودش None است
    if res is None:
        return None
    z = res.get("zip")
    try:
        zsize = os.path.getsize(z) if z else None       # UIUX-03: حجم زیپ در صفحه‌ی نتیجه
    except OSError:
        zsize = None
    return {"folder": res.get("folder"), "zip": z, "zip_size": zsize,
            "zip_name": os.path.basename(z) if z else None,
            "empty": bool(res.get("empty")), "interrupted": bool(res.get("interrupted")),
            "reason": res.get("reason") or "", "msgs": res.get("msgs", 0),
            "files": res.get("files", 0), "skipped": res.get("skipped", 0),
            "errors": res.get("errors", 0)}


def _job_payload():
    with LOCK:                                 # فیکس WB-14: خواندن job زیر LOCK (RLock باز_entry است)
        j = STATE["job"]
        if j is None:
            return None
        return {"chat": j["chat"], "chat_id": j["chat_id"], "started_at": j["started_at"],
                "finished_at": j.get("finished_at"),                       # UIUX-03
                "done": j["done"], "error": j["error"],
                "result": _res_payload(j["result"]) if j["done"] else None}


def _job_done(fut):
    """پایان job روی thread خود future: ذخیره‌ی نتیجه + ریست STOP (قابلیت WEB-04).
    فیکس WB-14: نتیجه/خطا بیرون از LOCK از future خوانده می‌شود؛ همه‌ی
    نوشتهای STATE در یک بلوک اتمیکِ واحد زیر LOCK انجام می‌شوند."""
    res = err = None
    try:
        res = fut.result()
    except Exception as e:
        err = ts.short_err(e)
    with LOCK:
        STATE["job"]["result"] = res
        STATE["job"]["error"] = err
        if res is not None:
            STATE["last_result"] = res
        STATE["job"]["done"] = True
        STATE["job"]["finished_at"] = time.time()      # UIUX-03: مدت برای صفحه‌ی نتیجه
    ts.STOP.clear()


@web.route("/api/dialogs")
def api_dialogs():
    """لیست چت‌ها با فیلتر دسته + جست‌وجو + صفحه‌بندی سمت سرور (قابلیت WEB-04).
    در JSON فقط id/name/cat/username می‌آید — entity هرگز بیرون نمی‌رود."""
    r = _require_ready()
    if r:
        return r
    cat = request.args.get("cat", "all")
    q = (request.args.get("q") or "").strip().lower()
    page = max(1, _to_int(request.args.get("page"), 1) or 1)
    if request.args.get("refresh") == "1" or not STATE["items"]:
        try:
            STATE["items"] = _bg_call(ts.load_dialogs(STATE["client"]), 600) or []
        except Exception as e:
            return _net_error(e)
    items = STATE["items"]
    if cat in ("channel", "group", "private", "bot"):
        items = [it for it in items if it["cat"] == cat]
    if q:
        items = [it for it in items if ts._match(it, q)]
    total = len(items)
    pages = max(1, -(-total // PER_PAGE_WEB))
    page = min(page, pages)
    start = (page - 1) * PER_PAGE_WEB
    return jsonify({
        "total": total, "page": page, "pages": pages, "cat": cat, "q": q,
        "items": [{"id": it["id"], "name": it["name"], "cat": it["cat"],
                   "username": it["username"]} for it in items[start:start + PER_PAGE_WEB]],
    })


@web.route("/api/resolve", methods=["POST"])
def api_resolve():
    """ورودی آیدی/یوزرنیم/لینک — همان resolve موتور؛ نتیجه در resolve_cache (WEB-04)."""
    bad = _bad_token()
    if bad:
        return bad
    r = _require_ready()
    if r:
        return r
    text = str((request.get_json(silent=True) or {}).get("text") or "")
    try:
        it = _bg_call(ts.resolve(STATE["client"], text, STATE["items"]), 90)
    except Exception as e:
        return _net_error(e)
    if it is None:
        return jsonify({"error": wt("not_found")}), 404
    STATE["resolve_cache"][it["id"]] = it
    return jsonify({"item": {"id": it["id"], "name": it["name"], "cat": it["cat"],
                             "username": it["username"]}})


@web.route("/api/chat/<int(signed=True):cid>")
def api_chat(cid):
    """کارت چت: نام + آیدی + تعداد پیام با count_msgs (قابلیت WEB-04).
    فیکس NB-17: آیدی کانال/گروه منفی است؛ مبدل int پیش‌فرض علامت را رد می‌کرد → 404."""
    r = _require_ready()
    if r:
        return r
    it = _find_item(cid)
    if it is None:
        return jsonify({"error": wt("not_found")}), 404
    try:
        total = _bg_call(ts.count_msgs(STATE["client"], it["entity"]), 60)
    except Exception:
        total = None
    return jsonify({"id": it["id"], "name": it["name"], "username": it["username"],
                    "cat": it["cat"], "total": total})


@web.route("/api/start", methods=["POST"])
def api_start():
    """شروع scrape روی bg loop؛ فقط یک job هم‌زمان — وگرنه 409 (قابلیت WEB-04/WEB-06)."""
    bad = _bad_token()
    if bad:
        return bad
    r = _require_ready()
    if r:
        return r
    data = request.get_json(silent=True) or {}
    with LOCK:
        if STATE["job"] is not None and not STATE["job"]["done"]:
            return jsonify({"error": wt("job_running")}), 409
        cid = _to_int(data.get("id"))
        if cid is None:
            return jsonify({"error": wt("invalid_input")}), 400
        it = _find_item(cid)
        if it is None:
            return jsonify({"error": wt("not_found")}), 404
        ts.STOP.clear()                      # توقفِ قبلی روی job تازه اثر نگذارد
        app = STATE["app"]
        fut = asyncio.run_coroutine_threadsafe(
            ts.scrape(app, STATE["client"], it, app.speed), ts._bg_loop())
        ts.PROG.update(phase="scrape", chat=it["name"], chat_id=it["id"], total=None,
                       done=0, pct=0, files=0, skipped=0, errors=0, zip=None,
                       milestone=0, dl_name=None, dl_got=None)   # UIUX-02
        ts.notify(wt("job_started", name=ts.short(it["name"], 40)))
        STATE["job"] = {"chat": it["name"], "chat_id": it["id"],
                        "started_at": time.time(), "result": None, "error": None, "done": False}
        STATE["last_result"] = None
        fut.add_done_callback(_job_done)
    return jsonify({"ok": True, "job": _job_payload()})


@web.route("/api/stop", methods=["POST"])
def api_stop():
    """توقف — ts.STOP.set()؛ idempotent (قابلیت WEB-04/WEB-06)."""
    bad = _bad_token()
    if bad:
        return bad
    ts.STOP.set()
    return jsonify({"ok": True})


# فیکس WB-05: بافر حلقه‌ای seq‌دار سمت سرور — پیام‌ها دیگر دور ریخته نمی‌شوند
_MSGS = []
_MSGS_LOCK = threading.Lock()


def _drain_msgs():
    """انتقال پیام‌های موتور (ts._msgs) به بافر وب با شماره‌ی ترتیبی — بدون گم‌شدن (فیکس WB-05)."""
    moved = []
    while True:
        try:
            moved.append(ts._msgs.popleft())
        except IndexError:
            break
    if moved:
        with _MSGS_LOCK:
            base = _MSGS[-1][0] if _MSGS else 0
            _MSGS.extend((base + i, txt) for i, txt in enumerate(moved, 1))
            del _MSGS[:-200]                 # بافر حلقه‌ای: فقط ۲۰۰ پیام آخر می‌ماند
    return moved


@web.route("/api/progress")
def api_progress():
    """پیشرفت زنده: PROG (فیلدهای ساختاریافته WEB-02) + پیام‌های seq‌دار (WB-05) + نتیجه‌ی job."""
    since = _to_int(request.args.get("since"), 0) or 0
    _drain_msgs()
    with _MSGS_LOCK:
        msgs = [{"seq": s, "text": t} for s, t in _MSGS if s > since]
        last = _MSGS[-1][0] if _MSGS else since
    with LOCK:
        return jsonify({
            "phase": STATE["phase"],
            "now": time.time(),          # UIUX-03: ساعتِ سرور — زمان سپری‌شده بدون اختلاف ساعت گوشی
            "prog": dict(ts.PROG),
            "msgs": msgs,
            "last_seq": last,
            "job": _job_payload(),
            "result": _res_payload(STATE["last_result"]),
        })


@web.route("/api/zip")
def api_zip():
    """دانلود زیپ — فقط فایل داخل app.zips با گارد realpath (قابلیت WEB-04).
    فیکس WB-09: GET حساس است؛ بدون token (هدر یا ?t=) → 403."""
    bad = _bad_token()
    if bad:
        return bad
    r = _require_ready()
    if r:
        return r
    path = _zip_path(STATE["app"].zips, request.args.get("name"))
    if path is None or not os.path.isfile(path):
        return jsonify({"error": wt("zip_not_found")}), 404
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path))


@web.route("/api/zips")
def api_zips():
    """لیست زیپ‌های قبلی داخل app.zips — فیکس WB-10: بعد از ریستارتِ سرور هم
    دانلود از کارت «حساب» ممکن است؛ هر نام با گارد _zip_path بررسی می‌شود."""
    bad = _bad_token()
    if bad:
        return bad
    r = _require_ready()
    if r:
        return r
    out = []
    try:
        names = sorted(os.listdir(STATE["app"].zips))
    except Exception:
        names = []
    for n in names:
        path = _zip_path(STATE["app"].zips, n)
        if path is None or not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": n, "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda z: -z["mtime"])          # جدیدترین زیپ اول
    return jsonify({"zips": out})


@web.route("/api/zip/move", methods=["POST"])
def api_zip_move():
    """UIUX-2: انتقال زیپ به پوشهٔ دلخواه (Colab/Drive) — نام با _zip_path، مقصد با _safe_dest."""
    bad = _bad_token()
    if bad:
        return bad
    r = _require_ready()
    if r:
        return r
    data = request.get_json(silent=True) or {}
    path = _zip_path(STATE["app"].zips, data.get("name"))
    if path is None or not os.path.isfile(path):
        return jsonify({"error": wt("zip_not_found")}), 404
    dest = _safe_dest(data.get("dest"))
    if dest is None:
        return jsonify({"error": wt("mv_bad_dest")}), 400
    try:
        os.makedirs(dest, exist_ok=True)
        final = os.path.join(dest, os.path.basename(path))
        if os.path.exists(final):
            os.remove(final)          # بازنویسی مقصدِ هم‌نام — الگوی پروژه
        shutil.move(path, final)
        st = os.stat(final)
    except Exception as e:
        return jsonify({"error": wt("mv_fail", e=ts.short_err(e))}), 500
    return jsonify({"ok": True, "path": final, "size": st.st_size})


@web.route("/api/settings")
def api_settings_get():
    """همان ۵ گزینه‌ی settings_screen + speed (قابلیت WEB-04)."""
    app = _get_app()
    return jsonify({"settings": app.settings, "speed": app.speed,
                    "speeds": list(ts.PROFILES.keys())})


@web.route("/api/settings", methods=["POST"])
def api_settings_post():
    """ذخیره‌ی تنظیمات با type-check سفت‌لوق + App.save_settings (قابلیت WEB-04)."""
    bad = _bad_token()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    app = _get_app()
    # فیکس WB-04: هیچ mutation قبل از پایانِ موفقِ همه‌ی اعتبارسنجی‌ها —
    # قبلاً فیلد اول در حافظه عوض می‌شد و فیلد نامعتبرِ بعدی 400 می‌داد → واگرایی runtime/دیسک.
    new = dict(app.settings)
    if "media" in data:
        new["media"] = bool(data["media"])
    if "zip" in data:
        new["zip"] = bool(data["zip"])
    if "sidecar" in data:
        new["sidecar"] = bool(data["sidecar"])
    if "max_mb" in data:
        try:
            new["max_mb"] = max(0, int(str(data["max_mb"]).translate(ts._DIGITS)))
        except (TypeError, ValueError):
            return jsonify({"error": wt("invalid_input"), "field": "max_mb"}), 400
    speed = app.speed
    if "speed" in data:
        if data["speed"] not in ts.PROFILES:
            return jsonify({"error": wt("invalid_input"), "field": "speed"}), 400
        speed = data["speed"]
    app.settings.update(new)                    # اعمال اتمیک فقط بعد از موفقیت همه
    app.speed = speed
    app.save_settings()
    return jsonify({"ok": True, "settings": app.settings, "speed": app.speed})


@web.route("/api/logout", methods=["POST"])
def api_logout():
    """خروج کامل: نتیجه‌ی log_out چک می‌شود (NB-11) + حذف بکاپ Drive (NB-13)."""
    bad = _bad_token()
    if bad:
        return bad
    if STATE["client"] is None:
        return jsonify({"error": wt("not_ready"), "phase": STATE["phase"]}), 409
    # فیکس WB-03: job فعال → اول توقف تمیز و تا پایانش صبر (سقف ۳۰ ثانیه)؛ بعد خروج —
    # log_out وسط اسکرپ، کلاینت را زیر پای coroutine فعال می‌کشید.
    job = STATE["job"]
    if job is not None and not job["done"]:
        ts.STOP.set()
        deadline = time.time() + 30
        while not job["done"] and time.time() < deadline:
            time.sleep(0.25)
        if not job["done"]:
            return jsonify({"error": wt("job_running")}), 409
    ok = False                                  # فیکس NB-11: log_out روی شکست False
    try:
        ok = bool(_bg_call(STATE["client"].log_out(), 90))
    except Exception:
        ok = False
    app = _get_app()
    api_id = STATE["api_id"]
    app.delete_session(api_id)
    msg = wt("logged_out") if ok else wt("logout_partial")
    try:                                        # فیکس NB-13: بکاپ Drive هم حذف شود
        bp = app.backup_path(api_id)
        if os.path.exists(bp):
            ts._rm(bp)
            msg += " " + wt("backup_deleted")
    except Exception:
        msg += " " + wt("backup_left")
    try:
        _bg_call(ts._disconnect(STATE["client"]), 30)
    except Exception:
        pass
    with LOCK:
        STATE.update(client=None, who="", items=[], resolve_cache={}, job=None,
                     last_result=None, restored=False, phase="setup", enc_pw=None)
    _detect_secrets()
    return jsonify({"ok": True, "msg": msg})

# ═══════════════ WEB-05: فرانت‌اند تک‌صفحه‌ای فارسی RTL موبایل‌پسند ═══════════════
# فیکس WB-11: مارکر قدیمی جداکننده حذف شد — PAGE از این‌جا تنها تعریفِ صفحه است
PAGE = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f172a">
<title>اسکرپر تلگرام</title>
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link href="https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/Vazirmatn-font-face.css" rel="stylesheet">
<style>
:root{
  --bg:#f4f6fb; --surface:#ffffff; --text:#0f172a; --muted:#556072;
  --primary:#1d4ed8; --primary-ink:#ffffff; --danger:#b91c1c; --ok:#15803d; --warn:#a16207;
  --line:#dbe2ee; --radius:14px; --shadow:0 4px 16px rgba(15,23,42,.08);
}
html.dark{
  --bg:#0b1220; --surface:#141d33; --text:#e8edf7; --muted:#96a1b5;
  --primary:#7aa5f8; --primary-ink:#0b1220; --danger:#f87171; --ok:#4ade80; --warn:#fbbf24;
  --line:#28334e; --shadow:0 4px 16px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  font-family:Vazirmatn, system-ui, -apple-system, "Segoe UI", sans-serif;
  background:var(--bg); color:var(--text); font-size:16px; line-height:1.6;
  min-height:100vh; padding-bottom:calc(env(safe-area-inset-bottom) + 24px);
}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:12px 16px;position:sticky;top:0;background:var(--surface);
  border-bottom:1px solid var(--line);z-index:10}
.topbar h1{font-size:18px;margin:0;display:flex;align-items:center;gap:8px;
  min-width:0;overflow:hidden}
.topbar h1 svg{flex:none}
.wrap{max-width:560px;margin:0 auto;padding:0 16px}
/* UIUX-3: مسیرهای طولانی داخل کارت بشکنند، نه بیرونِ آن */
#resFolder,#curFile,#stBase{overflow-wrap:anywhere;word-break:normal;max-width:100%}
h2{font-size:17px;margin:0 0 8px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px;box-shadow:var(--shadow);margin:16px 0}
.muted{color:var(--muted);font-size:14px;margin:4px 0}
/* UIUX-3: فاصلهٔ خطوط کارت «حساب» */
#accountCard .muted{margin:6px 0;line-height:1.8}
/* UIUX-3: ردیف زیپ — بدون style درون‌خطی؛ لینک کشسان، دکمهٔ 📤 ثابت */
.zipRow{display:flex;gap:8px;align-items:center;margin:8px 0}
.zipRow>a{flex:1;min-width:0;display:inline-flex;flex-wrap:wrap;align-items:center;
  column-gap:8px;row-gap:2px;text-align:start;line-height:1.5;white-space:normal}
.zipRow .zname{overflow-wrap:anywhere;word-break:normal}
.zipRow .badge{flex:none}
.zipRow>[data-mv]{flex:none;width:48px;min-width:48px;padding-inline:0}
.mvDest{margin:6px 0 2px}
.num{font-variant-numeric:tabular-nums}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.btn{min-height:44px;min-width:44px;border:0;border-radius:12px;font:inherit;font-weight:600;
  padding:10px 16px;background:var(--primary);color:var(--primary-ink);cursor:pointer}
.btn:active{transform:scale(.97)}
.btn[disabled]{opacity:.55;cursor:not-allowed}
.btn.ghost{background:transparent;color:var(--primary);border:1px solid var(--line)}
.btn.danger{background:var(--danger);color:#fff}
.btn.full{width:100%}
.btn.sticky{position:sticky;bottom:8px;box-shadow:var(--shadow)}
a{color:var(--primary)}
label{display:block;font-weight:600;margin:12px 0 6px}
input,select{width:100%;min-height:44px;border:1px solid var(--line);border-radius:10px;
  background:var(--bg);color:var(--text);font:inherit;padding:10px 12px}
input:focus,select:focus,button:focus-visible,[tabindex]:focus-visible{
  outline:3px solid var(--primary);outline-offset:1px}
.err{color:var(--danger);font-size:13px;margin-top:6px;display:none}
.err.show{display:block}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
.tab{flex:1 1 19%;min-height:44px;border:1px solid var(--line);background:var(--surface);
  color:var(--text);border-radius:10px;font:inherit;font-size:14px;padding:8px 4px;cursor:pointer}
.tab[aria-selected="true"]{background:var(--primary);color:var(--primary-ink);border-color:var(--primary)}
.list-item{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;
  min-height:56px;border:0;border-bottom:1px solid var(--line);background:none;
  color:var(--text);font:inherit;text-align:right;padding:8px 4px;cursor:pointer}
.list-item b{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.list-item .go{flex:none;font-size:12px;font-weight:700;color:var(--primary);
  border:1px solid var(--primary);border-radius:999px;padding:4px 10px;white-space:nowrap}
.badge{font-size:12px;color:var(--muted);flex:none}
.skeleton{height:56px;border-radius:10px;margin:8px 0;
  background:linear-gradient(90deg,var(--line) 25%,var(--surface) 50%,var(--line) 75%);
  background-size:200% 100%;animation:shimmer 1.2s infinite}
@keyframes shimmer{to{background-position:-200% 0}}
.progress{height:16px;border-radius:999px;background:var(--line);overflow:hidden;margin:12px 0}
.progress>div{height:100%;background:var(--primary);transition:width .4s}
/* UIUX-02: صفحه‌ی پیشرفت — چیپ فاز، درصد درشت، گزارش زنده */
.row h2{margin:0}
.chip{flex:none;font-size:12px;font-weight:700;border-radius:999px;padding:4px 10px;
  border:1px solid var(--line);color:var(--muted);background:var(--bg);white-space:nowrap}
.chip.run{color:var(--primary);border-color:var(--primary)}
.chip.zip{color:var(--warn);border-color:var(--warn)}
.chip.ok{color:var(--ok);border-color:var(--ok)}
.chip.err{color:var(--danger);border-color:var(--danger)}
#pctBig{font-size:26px;font-weight:800;margin:0}
#etaTxt{margin:0}
.log{max-height:180px;overflow-y:auto;background:var(--bg);border:1px solid var(--line);
  border-radius:10px;padding:8px 10px;font-size:12px;line-height:1.7;margin-top:8px;
  font-variant-numeric:tabular-nums}
.log div{white-space:pre-wrap;word-break:break-word;color:var(--muted)}
.log div.imp{color:var(--text);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
  min-width:0;display:flex;flex-direction:column;gap:2px}
.stat .muted{margin:0;font-size:12px}
.stat b{font-size:16px;overflow-wrap:anywhere}
#toasts{position:fixed;inset-inline:12px;bottom:calc(12px + env(safe-area-inset-bottom));
  display:flex;flex-direction:column;gap:8px;z-index:60}
#countdown{position:fixed;inset-inline:12px;top:64px;z-index:55;margin:0;box-shadow:var(--shadow)}
.toast{background:var(--surface);border:1px solid var(--line);border-inline-start:4px solid var(--primary);
  border-radius:10px;padding:10px 14px;box-shadow:var(--shadow);font-size:14px}
.toast.ok{border-inline-start-color:var(--ok)}
.toast.err{border-inline-start-color:var(--danger)}
.toast.warn{border-inline-start-color:var(--warn)}
/* فیکس WB-13: تنظیمات تمام‌صفحه — سوییچ‌ها، کنترل قطعه‌ای سرعت و نوار ذخیره‌ی sticky */
.sw-row{display:flex;align-items:center;justify-content:space-between;gap:12px;
  font-weight:600;padding:12px 0;border-bottom:1px solid var(--line);cursor:pointer;margin:0}
.card .sw-row:last-child{border-bottom:0}
.sw-row>span:first-child{flex:1}
.sw-row .hint{display:block;font-weight:400;color:var(--muted);font-size:13px;
  line-height:1.5;margin-top:2px}
.sw{appearance:none;-webkit-appearance:none;flex:none;width:48px;height:28px;min-height:28px;
  margin:0;border:0;border-radius:999px;background:var(--line);position:relative;
  cursor:pointer;transition:background .2s;padding:0}
.sw::after{content:"";position:absolute;top:3px;inset-inline-start:3px;width:22px;height:22px;
  border-radius:50%;background:#fff;transition:inset-inline-start .2s}
.sw:checked{background:var(--primary)}
.sw:checked::after{inset-inline-start:23px}
.seg{display:flex;gap:6px;margin:12px 0 4px}
.seg-opt{flex:1;display:flex}
.seg-opt input{position:absolute;opacity:0;pointer-events:none;outline:0}
.seg-opt span{flex:1;min-height:44px;display:flex;align-items:center;justify-content:center;
  border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:10px;
  font-size:14px;font-weight:600;padding:8px 4px;cursor:pointer}
.seg-opt input:checked+span{background:var(--primary);color:var(--primary-ink);border-color:var(--primary)}
.seg-opt input:focus-visible+span{outline:3px solid var(--primary);outline-offset:1px}
.savebar{position:sticky;bottom:8px;margin:16px 0}
dialog{border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);
  color:var(--text);max-width:92vw;width:420px;padding:16px}
dialog::backdrop{background:rgba(0,0,0,.5)}
details summary{cursor:pointer;color:var(--muted);font-size:14px;margin-top:8px}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<header class="topbar">
  <h1><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 11a9 9 0 0 1 9 9M4 15a5 5 0 0 1 5 5M5 19h.01"/><circle cx="5.5" cy="8.5" r="2.5"/><path d="M12 12l8-8"/><path d="M17 4h3v3"/></svg>اسکرپر تلگرام</h1>
  <button id="topSetBtn" class="btn ghost" aria-label="تنظیمات">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
  </button>
  <button id="themeBtn" class="btn ghost" aria-label="تغییر روشن/تاریک">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
  </button>
</header>
<main class="wrap">

<section data-view="setup" hidden>
  <div class="card">
    <h2>اتصال به حساب تلگرام</h2>
    <p class="muted" id="secHint"></p>
    <form id="setupForm" novalidate>
      <div id="credFields">
        <!-- قابلیت WB-15: هر فیلد جداگانه مخفی می‌شود اگر Secret همان فیلد موجود باشد -->
        <div id="apiIdFields">
          <label for="api_id">API ID</label>
          <input id="api_id" inputmode="numeric" autocomplete="off" dir="ltr" aria-describedby="err_api_id">
          <p class="err" id="err_api_id" aria-live="polite"></p>
        </div>
        <div id="apiHashFields">
          <label for="api_hash">API Hash</label>
          <input id="api_hash" type="password" autocomplete="off" dir="ltr" aria-describedby="err_api_hash">
          <p class="err" id="err_api_hash" aria-live="polite"></p>
        </div>
      </div>
      <div id="phoneFields">
        <label for="phone">شماره تلگرام</label>
        <input id="phone" type="tel" inputmode="tel" placeholder="+98912..." autocomplete="off" dir="ltr" aria-describedby="err_phone">
        <p class="err" id="err_phone" aria-live="polite"></p>
      </div>
      <details id="encWrap">
        <summary>رمز بکاپ سشن (اختیاری — اگر Secret با نام TS_ENC_KEY ندارید)</summary>
        <label for="enc_pw">رمز بکاپ</label>
        <input id="enc_pw" type="password" autocomplete="off" aria-describedby="err_enc_pw">
        <label for="enc_pw2">تکرار همان رمز</label>
        <input id="enc_pw2" type="password" autocomplete="off">
        <p class="err" id="err_enc_pw" aria-live="polite"></p>
      </details>
      <p class="err" id="err_setup" aria-live="polite"></p>
      <button class="btn full" id="setupBtn" type="submit">شروع ورود</button>
    </form>
  </div>
</section>

<section data-view="login" hidden>
  <div class="card">
    <h2 id="loginTitle">کد تلگرام</h2>
    <p class="muted">کد به تلگرام (نه پیامک) فرستاده می‌شود.</p>
    <form id="codeForm" novalidate>
      <label for="code">کد ورود</label>
      <input id="code" inputmode="numeric" autocomplete="one-time-code" dir="ltr" aria-describedby="err_code">
      <p class="err" id="err_code" aria-live="polite"></p>
      <button class="btn full" id="codeBtn" type="submit">بررسی کد</button>
    </form>
    <form id="twoFaForm" hidden novalidate>
      <div id="pwdFields">
        <label for="password">رمز دو مرحله‌ای</label>
        <input id="password" type="password" autocomplete="off" aria-describedby="err_password">
        <p class="err" id="err_password" aria-live="polite"></p>
      </div>
      <!-- قابلیت WB-15: اگر Secret TG_2FA_PASSWORD موجود باشد فیلد مخفی می‌شود و رمز خودکار از Secret ارسال می‌شود -->
      <p class="muted" id="pwdHint" hidden>رمز از Secret TG_2FA_PASSWORD خوانده می‌شود؛ فقط دکمه‌ی ورود را بزنید.</p>
      <button class="btn full" type="submit">ورود</button>
    </form>
    <button class="btn ghost full" id="resendBtn">ارسال دوباره‌ی کد</button>
  </div>
</section>

<section data-view="main" hidden>
  <!-- UIUX-2: کارت «حساب» — نام/شماره/وضعیت سشن/وضعیت Drive -->
  <div class="card" id="accountCard" hidden>
    <div class="row">
      <h2 id="mAccName">👤 …</h2>
      <button class="btn ghost" id="refreshBtn">بازخوانی</button>
    </div>
    <p class="muted" id="mAccPhone"></p>
    <p class="muted" id="mAccSess"></p>
    <p class="muted" id="mAccDrive"></p>
  </div>
  <div class="tabs" role="tablist" aria-label="دسته‌ی چت‌ها">
    <button class="tab" role="tab" data-cat="all" aria-selected="true">همه</button>
    <button class="tab" role="tab" data-cat="channel" aria-selected="false">کانال</button>
    <button class="tab" role="tab" data-cat="group" aria-selected="false">گروه</button>
    <button class="tab" role="tab" data-cat="private" aria-selected="false">شخصی</button>
    <button class="tab" role="tab" data-cat="bot" aria-selected="false">ربات</button>
  </div>
  <p class="muted">برای شروع اسکرپ، روی هر چت بزنید.</p>
  <label for="q" class="visually-hidden">جست‌وجو</label>
  <input id="q" type="search" placeholder="جست‌وجو…" aria-describedby="listState">
  <form id="resolveForm" novalidate>
    <label for="resolveInput">ورود دستی: آیدی / یوزرنیم / لینک</label>
    <input id="resolveInput" dir="ltr" autocomplete="off" placeholder="@username یا t.me/..." aria-describedby="err_resolve">
    <p class="err" id="err_resolve" aria-live="polite"></p>
    <button class="btn ghost full" type="submit">پیدا کن</button>
  </form>
  <div id="list"></div>
  <p class="muted" id="listState" aria-live="polite"></p>
  <div class="row">
    <button class="btn ghost" id="prevBtn">قبلی</button>
    <span id="pageInfo" class="num muted"></span>
    <button class="btn ghost" id="nextBtn">بعدی</button>
  </div>
  <button class="btn ghost full" id="settingsBtn">تنظیمات و خروج</button>
</section>

<section data-view="progress" hidden>
  <div class="card">
    <div class="row">
      <h2 id="progChat"></h2>
      <span class="chip" id="phaseChip">…</span>
    </div>
    <div class="progress" role="progressbar" id="barWrap" aria-label="پیشرفت" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
      <div id="bar"></div>
    </div>
    <div class="row">
      <b id="pctBig" class="num">۰٪</b>
      <span class="num muted" id="etaTxt"></span>
    </div>
    <p class="muted num" id="curFile" dir="ltr" hidden></p>
    <div class="grid">
      <div class="stat"><p class="muted">پیام</p><b id="stDone" class="num">۰</b></div>
      <div class="stat"><p class="muted">از کل</p><b id="stTotal" class="num">؟</b></div>
      <div class="stat"><p class="muted">فایل</p><b id="stFiles" class="num">۰</b></div>
      <div class="stat"><p class="muted">ردشده</p><b id="stSkip" class="num">۰</b></div>
      <div class="stat"><p class="muted">خطا</p><b id="stErr" class="num">۰</b></div>
      <div class="stat"><p class="muted">زمان</p><b id="stTime" class="num">۰:۰۰</b></div>
    </div>
    <p class="muted" id="phaseNote" aria-live="polite"></p>
    <details id="logWrap" hidden>
      <summary>گزارش زنده</summary>
      <div id="log" class="log"></div>
    </details>
  </div>
  <button class="btn danger full sticky" id="stopBtn">توقف اسکرپ</button>
</section>

<section data-view="result" hidden>
  <div class="card">
    <h2 id="resTitle"></h2>
    <div class="grid" id="resStats"></div>
    <p class="muted" id="resFolder" dir="ltr"></p>
    <a id="zipLink" class="btn full" hidden>دانلود زیپ</a>
    <button class="btn ghost full" id="backBtn">بازگشت به چت‌ها</button>
  </div>
  <!-- فیکس WB-10: زیپ‌های اسکرپ‌های قبلی هم همین‌جا در دسترس‌اند -->
  <div class="card" id="prevZipsCard" hidden>
    <h2>زیپ‌های قبلی</h2>
    <div id="prevZips"></div>
  </div>
</section>

<!-- فیکس WB-13: تنظیمات به‌جای دیالوگ، نمای تمام‌صفحه با کارت‌های گروهی -->
<section data-view="settings" hidden>
  <div class="row">
    <button class="btn ghost" id="setBack">بازگشت به چت‌ها</button>
  </div>
  <form id="setForm" novalidate>
    <div class="card">
      <h2>مدیا</h2>
      <label class="sw-row" for="s_media">
        <span>دانلود مدیا<span class="hint">عکس، ویدیو، صدا، ویس و فایل‌های چت دانلود شوند</span></span>
        <input type="checkbox" role="switch" class="sw" id="s_media">
      </label>
      <label class="sw-row" for="s_limit">
        <span>محدودسازی حجم<span class="hint">فقط فایل‌های کوچک‌تر از سقف دانلود شوند</span></span>
        <input type="checkbox" role="switch" class="sw" id="s_limit">
      </label>
      <div id="mbWrap" hidden>
        <label for="s_max">سقف حجم هر فایل (مگابایت)</label>
        <input id="s_max" inputmode="numeric" dir="ltr" aria-describedby="mbHint">
        <p class="muted" id="mbHint" aria-live="polite"></p>
        <p class="err" id="err_max" aria-live="polite"></p>
      </div>
    </div>
    <div class="card">
      <h2>خروجی</h2>
      <label class="sw-row" for="s_zip">
        <span>ساخت زیپ<span class="hint">در پایان، همه‌ی خروجی‌ها یک فایل zip قابل دانلود می‌شود</span></span>
        <input type="checkbox" role="switch" class="sw" id="s_zip">
      </label>
      <label class="sw-row" for="s_sidecar">
        <span>فایل info کنار مدیا<span class="hint">مشخصات هر فایل در info.txt جداگانه ذخیره می‌شود</span></span>
        <input type="checkbox" role="switch" class="sw" id="s_sidecar">
      </label>
    </div>
    <div class="card">
      <h2>سرعت</h2>
      <p class="muted">سرعت و خنک‌سازی اسکرپ — هرچه تندتر، ریسک محدودیت تلگرام بیشتر</p>
      <div class="seg" role="radiogroup" aria-label="سرعت و خنک‌سازی" id="s_speed"></div>
      <p class="muted" id="speedHint" aria-live="polite"></p>
    </div>
  </form>
  <!-- UIUX-2: وضعیت Drive و مسیر خروجی -->
  <div class="card">
    <h2>Google Drive و خروجی</h2>
    <p id="stDrive"></p>
    <p class="muted" id="stBase" dir="ltr"></p>
    <p class="muted" id="stDriveHint"></p>
  </div>
  <!-- فیکس WB-10: بعد از ریستارت سرور، زیپ‌های قبلی از این کارت قابل دانلود‌اند -->
  <div class="card" id="accZipsCard" hidden>
    <h2>زیپ‌های قبلی</h2>
    <p class="muted">خروجی اسکرپ‌های گذشته — شامل دوره‌های قبل از ریستارت سرور</p>
    <div id="accZips"></div>
  </div>
  <div class="card">
    <h2>حساب</h2>
    <p id="accWho"></p>
    <p class="muted" id="accPhone"></p>
    <p class="muted" id="accBackup"></p>
    <button class="btn danger full" id="logoutBtn">خروج کامل از حساب</button>
  </div>
  <div class="savebar">
    <button class="btn full" type="submit" form="setForm">ذخیره‌ی تنظیمات</button>
  </div>
</section>
</main>

<dialog id="chatDlg" aria-labelledby="dlgName">
  <h2 id="dlgName"></h2>
  <p class="muted">آیدی: <span id="dlgId" dir="ltr" class="num"></span></p>
  <p>تعداد پیام: <span id="dlgTotal" class="num"></span></p>
  <button class="btn full" id="startBtn">شروع اسکرپ</button>
  <button class="btn ghost full" id="dlgClose">انصراف</button>
</dialog>

<!-- فیکس WB-13: دیالوگ قدیمیِ تنظیمات حذف شد — جایگزین: نمای تمام‌صفحه‌ی settings -->

<dialog id="confirmDlg" aria-labelledby="confirmMsg">
  <p id="confirmMsg"></p>
  <div class="row">
    <button class="btn danger" id="confirmYes">بله، خارج شو</button>
    <button class="btn ghost" id="confirmNo">انصراف</button>
  </div>
</dialog>

<div id="countdown" hidden class="card" role="status">
  <p>محدودیت تلگرام؛ <span id="cdSec" class="num"></span> ثانیه صبر…</p>
</div>
<div id="toasts" aria-live="polite"></div>

<script>
"use strict";
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const FD = {"۰":"0","۱":"1","۲":"2","۳":"3","۴":"4","۵":"5","۶":"6","۷":"7","۸":"8","۹":"9",
            "٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9"};
const norm = s => String(s ?? "").replace(/[۰-۹٠-٩]/g, c => FD[c] ?? c);
const FA = "۰۱۲۳۴۵۶۷۸۹";
const fa = n => String(n ?? "").replace(/\d/g, d => FA[+d]);
let TOKEN = "";
let SECRETS = {};                        // فیکس WB-07: وجود Secretها برای راهنمای رمز 2FA
let SEC_HINT = "";                       // فیکس NB-21: دلیل نرسیدن سکرت (سرور بدون لانچر)

async function api(path, body, method) {
  const opt = { method: method || (body ? "POST" : "GET"),
                headers: { "X-TS-Token": TOKEN } };
  if (body) { opt.headers["Content-Type"] = "application/json";
              opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let d = {};
  try { d = await r.json(); } catch (e) {}
  return { status: r.status, data: d };
}

function toast(msg, cls) {
  const t = document.createElement("div");
  t.className = "toast" + (cls ? " " + cls : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

// قابلیت WB-15: فیلدِ مخفی‌شده به‌خاطر Secret، اگر خطا به آن بر بخورد آشکار می‌شود تا کاربر دستی وارد کند
const FIELD_GROUPS = { api_id: "#apiIdFields", api_hash: "#apiHashFields", phone: "#phoneFields",
                       enc_pw: "#encWrap", password: "#pwdFields" };
function setErr(id, msg) {
  const g = FIELD_GROUPS[id];
  if (msg && g && $(g)) {
    $(g).hidden = false;
    const inp = $(g).querySelector("input");
    if (inp) inp.focus();
    if (id === "password") $("#pwdHint").hidden = true;
  }
  const el = $("#err_" + id);
  el.textContent = msg || "";
  el.classList.toggle("show", !!msg);
}
function clearErrs() { $$(".err").forEach(e => { e.textContent = ""; e.classList.remove("show"); }); }

function busy(btn, on, txt) {
  if (on) { btn.dataset.old = btn.textContent; btn.disabled = true; btn.textContent = txt || "لطفا صبر کنید…"; }
  else { btn.disabled = false; if (btn.dataset.old) btn.textContent = btn.dataset.old; }
}

function show(view) {
  $$("section[data-view]").forEach(s => { s.hidden = s.dataset.view !== view; });
  window.scrollTo(0, 0);
}

let cdTimer = null;
function countdown(wait, after, btn) {
  const box = $("#countdown");
  box.hidden = false;
  let left = Math.max(1, parseInt(wait, 10) || 1);
  $("#cdSec").textContent = fa(left);
  if (btn && !btn.dataset.old) btn.dataset.old = btn.textContent;  // فیکس WB-06: شمارش روی خود دکمه
  if (btn) btn.disabled = true;
  if (cdTimer) clearInterval(cdTimer);
  cdTimer = setInterval(() => {
    left -= 1;
    $("#cdSec").textContent = fa(Math.max(0, left));
    if (btn) btn.textContent = (btn.dataset.old || "") + " — " + fa(Math.max(0, left)) + " ثانیه";
    if (left <= 0) { clearInterval(cdTimer); cdTimer = null; box.hidden = true;
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.old || btn.textContent; }
      if (after) after(); }
  }, 1000);
}

function handleAuth(r, retry, btn) {
  const d = r.data || {};
  if (d.wait) { countdown(d.wait, retry, btn); return true; }
  if (d.phase === "ready") { showMain(); return true; }
  if (d.phase === "need_2fa") { showLogin("need_2fa"); return true; }
  if (d.phase === "need_code") { showLogin("need_code"); return true; }
  if (d.error) {
    if (d.field) setErr(d.field, d.error);
    else { setErr("setup", d.error); toast(d.error, "err"); }
    return true;
  }
  return false;
}

function boot() {
  api("/api/state").then(r => {
    TOKEN = (r.data && r.data.token) || "";
    const p = (r.data && r.data.phase) || "setup";
    const sec = (r.data && r.data.secrets) || {};
    SECRETS = sec;                       // فیکس WB-07: بعد از بوت در دسترس showLogin
    SEC_HINT = (r.data && r.data.hint) || "";
    if (p === "ready") showMain();
    else if (p === "need_code" || p === "need_2fa") showLogin(p);
    else showSetup(sec, p === "need_login");
  }).catch(() => toast("ارتباط با سرور برقرار نشد", "err"));
}

function showSetup(sec, dead) {
  clearErrs();
  $("#secHint").textContent =
    (sec.api_id && sec.api_hash ? "API ID و Hash از Secretها خوانده می‌شوند. "
     : "API ID و Hash از my.telegram.org لازم است. ") +
    (sec.phone ? "شماره از Secret خوانده می‌شود." : "شماره را با کد کشور وارد کنید.") +
    (dead ? " اتصال قبلی قطع شده بود؛ دوباره وارد شوید." : "") +
    (SEC_HINT && !(sec.api_id && sec.api_hash) ? " " + SEC_HINT : "");   // فیکس NB-21
  $("#apiIdFields").hidden = !!sec.api_id;    // قابلیت WB-15: Secret هست → فیلد نیاید
  $("#apiHashFields").hidden = !!sec.api_hash;
  $("#phoneFields").hidden = !!sec.phone;
  $("#encWrap").hidden = !!sec.enc_key;
  show("setup");
}

function showLogin(phase) {
  clearErrs();
  const two = phase === "need_2fa";
  $("#loginTitle").textContent = two ? "رمز دو مرحله‌ای" : "کد تلگرام";
  $("#codeForm").hidden = two;
  $("#twoFaForm").hidden = !two;
  const secretPwd = !!(two && SECRETS.pwd);       // قابلیت WB-15: Secret هست → فیلد رمز نیاید
  $("#pwdFields").hidden = secretPwd;
  $("#pwdHint").hidden = !secretPwd;
  $("#resendBtn").hidden = two;
  show("login");
  if (!secretPwd) (two ? $("#password") : $("#code")).focus();
}

function showMain() {
  api("/api/state").then(r => {
    const t = r.data || {};
    if (t.has_job) { showProgress(); return; }
    const card = $("#accountCard");       // UIUX-2: کارت «حساب»
    if (t.who || t.phone) {
      card.hidden = false;
      $("#mAccName").textContent = "👤 " + (t.who || "—");
      $("#mAccPhone").textContent = t.phone ? "📞 \u2066" + t.phone + "\u2069" : "";
      $("#mAccSess").textContent = t.restored
        ? "🔐 سشن از بکاپ Drive بازیابی شده"
        : (t.bk_active ? "🔐 بکاپ سشن روی Drive فعال" : "🔐 بدون بکاپ سشن");
      $("#mAccDrive").textContent = t.drive
        ? "📁 Drive وصل است: \u2066" + (t.base || "") + "\u2069"
        : "📁 Drive وصل نیست — خروجی موقت است؛ لانچر را با \u2066drive=True\u2069 اجرا کنید";
    } else { card.hidden = true; }
  });
  show("main");
  loadList(1);
}

const listQ = { cat: "all", q: "", page: 1 };
function loadList(page, refresh) {
  listQ.page = page || 1;
  $("#list").innerHTML = "";
  for (let i = 0; i < 6; i++) $("#list").insertAdjacentHTML("beforeend", "<div class='skeleton'></div>");
  $("#listState").textContent = "در حال دریافت…";
  const url = "/api/dialogs?cat=" + encodeURIComponent(listQ.cat) +
              "&q=" + encodeURIComponent(listQ.q) + "&page=" + listQ.page +
              (refresh ? "&refresh=1" : "");
  api(url).then(r => {
    const d = r.data || {};
    $("#list").innerHTML = "";
    if (r.status !== 200) {
      $("#listState").textContent = "";
      if (d.phase === "need_login") { showSetup(d.secrets || {}, true); return; }
      errorState("دریافت لیست ناموفق بود.");
      return;
    }
    const items = d.items || [];
    if (!items.length) {
      $("#list").insertAdjacentHTML("beforeend",
        "<div class='card'><p class='muted'>موردی پیدا نشد.</p>" +
        "<button class='btn ghost full' id='retryBtn2'>تلاش دوباره</button></div>");
      const rb = $("#retryBtn2");
      if (rb) rb.onclick = () => loadList(1, true);
    } else {
      items.forEach(it => {
        const b = document.createElement("button");
        b.className = "list-item";
        b.innerHTML = "<b>" + escapeHtml(it.name) + "</b>" +   // فیکس NB-16: نشان اسکرپ روی هر ردیف
          "<span class='go'>اسکرپ ›</span>";
        b.onclick = () => openChat(it.id);
        $("#list").appendChild(b);
      });
    }
    $("#listState").textContent = fa(d.total || 0) + " چت — صفحه‌ی " + fa(d.page) + " از " + fa(d.pages);
    $("#prevBtn").disabled = (d.page || 1) <= 1;
    $("#nextBtn").disabled = (d.page || 1) >= (d.pages || 1);
    $("#pageInfo").textContent = "";
  }).catch(() => { $("#list").innerHTML = ""; errorState("ارتباط با سرور برقرار نشد."); });
}

function errorState(msg) {
  $("#list").insertAdjacentHTML("beforeend",
    "<div class='card'><p>" + escapeHtml(msg) + "</p>" +
    "<button class='btn ghost full' id='retryBtn2'>تلاش دوباره</button></div>");
  const rb = $("#retryBtn2");
  if (rb) rb.onclick = () => loadList(1, true);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function openChat(id) {
  api("/api/chat/" + id).then(r => {
    const d = r.data || {};
    if (r.status !== 200) { toast(d.error || "پیدا نشد", "err"); return; }
    $("#dlgName").textContent = d.name;
    $("#dlgId").textContent = d.id;
    $("#dlgTotal").textContent = (d.total === null || d.total === undefined) ? "؟" : fa(d.total);
    $("#startBtn").dataset.id = d.id;
    const dlg = $("#chatDlg");
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
  });
}

let pollTimer = null;
let lastSeq = 0;                          // فیکس WB-05: فقط پیام‌های جدیدِ هر poll نمایش می‌یابد
let pollErrs = 0;                         // فیکس WB-12: شمارنده‌ی خطاهای متوالی poll
let lastLogFam = "";                       // UIUX-02: خانواده‌ی آخرین خط گزارش (dedupe)
let srvNow = 0, jobStart = 0, lastDone = 0, lastTotal = 0;   // UIUX-03: ساعت سرور/آمار
let tickTimer = null;                      // UIUX-03: تیک هر ثانیه بین pollها
function showProgress() {
  show("progress");
  if (pollTimer) clearInterval(pollTimer);
  $("#log").innerHTML = "";                     // UIUX-02: گزارش هر اسکرپ از نو
  lastLogFam = "";
  $("#logWrap").hidden = true;
  $("#phaseChip").textContent = "…";
  $("#phaseChip").className = "chip";
  $("#curFile").hidden = true;
  $("#stopBtn").disabled = false;
  const stale = $("#pollRetryBtn");             // فیکس WB-12: دکمه‌ی تلاش دوباره‌ی مانده
  if (stale) stale.remove();
  pollErrs = 0;
  if (tickTimer) clearInterval(tickTimer);
  tickTimer = setInterval(() => { if (srvNow) { srvNow += 1; tickUpd(); } }, 1000);
  pollTimer = setInterval(poll, 1500);
  poll();
}

// UIUX-03: مدت به شکل MM:SS یا H:MM:SS با رقم فارسی
function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const two = n => (n < 10 ? "0" : "") + n;
  return fa(h > 0 ? h + ":" + two(m) + ":" + two(sec) : m + ":" + two(sec));
}

// UIUX-02: گزارش زنده — خطِ هم‌خانواده (فقط عدد فرق دارد) همان خط آخر را به‌روز می‌کند
function logLine(txt, important) {
  const box = $("#log");
  const fam = String(txt).replace(/[0-9۰-۹٠-٩]+/g, "#");
  if (fam === lastLogFam && box.lastChild) {
    box.lastChild.textContent = txt;
    if (important) box.lastChild.classList.add("imp");
  } else {
    const el = document.createElement("div");
    if (important) el.className = "imp";
    el.textContent = txt;
    box.appendChild(el);
    lastLogFam = fam;
  }
  while (box.children.length > 40) box.removeChild(box.firstChild);
  $("#logWrap").hidden = false;
  box.scrollTop = box.scrollHeight;
}

// UIUX-02: چیپ فاز رنگی
const PHASE_FA = {count: ["در حال شمردن پیام‌ها", "run"],
                  scrape: ["در حال دریافت", "run"],
                  zip: ["در حال ساخت زیپ", "zip"],
                  stopped: ["متوقف شد", "err"],
                  error: ["خطا", "err"],
                  done: ["تمام شد", "ok"]};
function setPhase(p) {
  const c = PHASE_FA[p] || ["", ""];
  $("#phaseChip").textContent = c[0] || "…";
  $("#phaseChip").className = "chip" + (c[1] ? " " + c[1] : "");
}

// UIUX-03: زمان سپری‌شده + سرعت + تخمین باقی‌مانده — همه با ساعتِ سرور
function tickUpd() {
  if (!jobStart || !srvNow) return;
  const el = Math.max(0, srvNow - jobStart);
  $("#stTime").textContent = fmtDur(el);
  if (lastDone > 0 && el > 3) {
    let txt = fa(Math.round(lastDone * 60 / el)) + " پیام/دقیقه";
    if (lastTotal > 0 && lastTotal > lastDone)
      txt += " — ≈ " + fmtDur((lastTotal - lastDone) * el / lastDone) + " مانده";
    $("#etaTxt").textContent = txt;
  }
}

function poll() {
  api("/api/progress" + (lastSeq ? "?since=" + lastSeq : "")).then(r => {
    pollErrs = 0;                          // فیکس WB-12: پاسخ موفق → شمارنده صفر
    const d = r.data || {};
    const prog = d.prog || {};
    // UIUX-02: پیام‌های موتور → گزارش زنده؛ toast فقط برای رویدادهای مهم
    (d.msgs || []).forEach(m => {
      const txt = String(m.text || m);
      const imp = /محدود|Flood|متوقف|خطا|⛔|🚨|پر شد|کم شد|استراحت|دیسک/.test(txt);
      logLine(txt, imp);
      if (imp) toast(txt, "warn");
    });
    if (typeof d.last_seq === "number") lastSeq = Math.max(lastSeq, d.last_seq);
    if (typeof d.now === "number" && d.now > srvNow) srvNow = d.now;   // UIUX-03
    const job = d.job;
    if (job) {
      $("#progChat").textContent = job.chat;
      if (job.started_at) jobStart = job.started_at;
      lastDone = prog.done || 0;
      lastTotal = prog.total || 0;
      const pct = Math.max(0, Math.min(100, prog.pct || 0));
      $("#bar").style.width = pct + "%";
      $("#barWrap").setAttribute("aria-valuenow", String(pct));
      $("#pctBig").textContent = fa(pct) + "٪";
      setPhase(prog.phase || "");
      $("#stDone").textContent = fa(prog.done || 0);
      $("#stTotal").textContent = prog.total ? fa(prog.total) : "؟";
      $("#stFiles").textContent = fa(prog.files || 0);
      $("#stSkip").textContent = fa(prog.skipped || 0);
      $("#stErr").textContent = fa(prog.errors || 0);
      tickUpd();
      const cf = $("#curFile");                    // UIUX-02: فایل جاری و پیشرفتش
      if (prog.dl_name) {
        cf.hidden = false;
        cf.textContent = prog.dl_name +
          (prog.dl_size ? " — " + fmtSize(prog.dl_size) +
           (prog.dl_got ? " (" + fmtSize(prog.dl_got) + ")" : "") : "");
      } else cf.hidden = true;
      const lc = $("#log").lastChild;              // UIUX-02: نوت استراحت وقتی خط آخر 😴 است
      $("#phaseNote").textContent = (prog.phase === "zip") ? "در حال ساخت زیپ…"
        : (lc && String(lc.textContent || "").indexOf("😴") === 0
           ? "استراحت برنامه‌ریزی‌شده برای جلوگیری از محدودیت تلگرام — به‌زودی ادامه می‌یابد." : "");
      if (job.done) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
        showResult(job.result, job);
      }
    } else if (d.result) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
      showResult(d.result);
    } else if (!$("#progChat").textContent) {
      // فیکس NB-18 (دفاعی): بدون job و نتیجه، صفحه بی‌توضیح خالی نمی‌ماند
      $("#progChat").textContent = "اسکرپی در جریان نیست";
      $("#phaseNote").textContent = "اطلاعاتی از سرور نرسید؛ احتمالاً سرور ری‌استارت شده. به چت‌ها برگردید و دوباره شروع کنید.";
    }
    if (d.phase === "need_login" && (!job || job.done)) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
      showSetup({}, true);
    }
  }).catch(() => {                         // فیکس WB-12: خطای شبکه دیگر بی‌صدا قورت نمی‌رود
    pollErrs += 1;
    if (pollErrs < 3) return;              // تا ۲ خطای پیاپی تحمل می‌شود
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    toast("ارتباط با سرور قطع شد؛ اینترنت یا اجرای Colab را بررسی کنید.", "err");
    $("#phaseNote").textContent = "ارتباط با سرور قطع شد؛ برای ادامه تلاش دوباره کنید.";
    if ($("#pollRetryBtn")) return;
    const btn = document.createElement("button");
    btn.id = "pollRetryBtn";
    btn.className = "btn ghost full";
    btn.textContent = "تلاش دوباره";
    btn.addEventListener("click", () => { btn.remove(); showProgress(); });
    $("#phaseNote").parentNode.appendChild(btn);
  });
}

function showResult(res, job) {
  if (!res) { showMain(); return; }
  const titles = { stop: "اسکرپ متوقف شد — فایل‌های دانلودشده می‌مانند.",
                   disk: "فضای دیسک کم شد؛ متوقف شد.",
                   empty: "چت خالی است." };
  $("#resTitle").textContent = res.empty ? titles.empty :
    (res.interrupted ? (titles[res.reason] || "خطا در اجرا.") : "✅ اسکرپ تمام شد");
  // UIUX-03: مدت اجرا از finished_at سرور + حجم زیپ
  let secs = null;
  if (job && job.started_at && job.finished_at)
    secs = Math.max(0, Math.round(job.finished_at - job.started_at));
  $("#resStats").innerHTML =
    stat("پیام", res.msgs) + stat("فایل", res.files) +
    stat("ردشده", res.skipped) + stat("خطا", res.errors) +
    (secs !== null ? stat("مدت", fmtDur(secs)) : "") +
    (res.zip_size ? stat("حجم زیپ", fmtSize(res.zip_size)) : "");
  $("#resFolder").textContent = res.folder || "";
  const zl = $("#zipLink");
  if (res.zip_name) {
    zl.hidden = false;
    zl.href = "/api/zip?name=" + encodeURIComponent(res.zip_name) +
              "&t=" + encodeURIComponent(TOKEN);   // فیکس WB-09: token در لینک دانلود
    zl.setAttribute("download", res.zip_name);
  } else { zl.hidden = true; }
  show("result");
  loadZips($("#prevZips"), $("#prevZipsCard"));   // فیکس WB-10
}

function stat(label, v) {
  return "<div class='stat'><p class='muted'>" + label + "</p><b class='num'>" +
         fa(v || 0) + "</b></div>";
}

// فیکس WB-10: رندر مشترکِ لیست زیپ‌های قبلی — نمای نتیجه + کارت «حساب» در تنظیمات
function fmtSize(n) {
  n = n || 0;
  if (n >= 1073741824) return fa((n / 1073741824).toFixed(1)) + " گیگابایت";
  if (n >= 1048576) return fa((n / 1048576).toFixed(1)) + " مگابایت";
  if (n >= 1024) return fa((n / 1024).toFixed(1)) + " کیلوبایت";
  return fa(n) + " بایت";
}
function loadZips(box, card) {
  api("/api/zips").then(r => {
    const zs = (r.data && r.data.zips) || [];
    if (r.status !== 200 || !zs.length) { if (card) card.hidden = true; return; }
    // UIUX-2: ورودی مقصد مشترک + دکمهٔ 📤 انتقال برای هر زیپ
    const lastDest = localStorage.getItem("ts_mv_dest") || "/content/drive/MyDrive/TeleScrape/zips";
    box.innerHTML =
      "<p class='muted'>پوشهٔ مقصدِ انتقال (مثلاً <bdi dir='ltr'>/content/drive/MyDrive/…</bdi>):</p>" +
      "<input class='mvDest' dir='ltr' autocomplete='off' aria-label='پوشهٔ مقصد انتقال' value='" + escapeHtml(lastDest) + "'>" +
      zs.map(z => {
        const dl = "/api/zip?name=" + encodeURIComponent(z.name) + "&t=" + encodeURIComponent(TOKEN);
        return "<div class='zipRow'><a class='btn ghost' href='" + dl +
               "' download='" + escapeHtml(z.name) + "'><span class='zname' dir='ltr'>" + escapeHtml(z.name) +
               "</span><span class='badge num' dir='ltr'>" + fmtSize(z.size) + "</span></a>" +
               "<button class='btn ghost' data-mv='" + escapeHtml(z.name) + "' aria-label='انتقال " + escapeHtml(z.name) + " به پوشهٔ مقصد'>📤</button></div>";
      }).join("");
    box.querySelectorAll("[data-mv]").forEach(b =>
      b.addEventListener("click", () => moveZip(b.dataset.mv, box.querySelector(".mvDest").value)));
    if (card) card.hidden = false;
  }).catch(() => { if (card) card.hidden = true; });
}

// UIUX-2: انتقال زیپ به پوشهٔ دلخواه Colab/Drive — همه‌چیز از UI
function moveZip(name, dest) {
  dest = (dest || "").trim();
  if (!dest) { toast("پوشهٔ مقصد را وارد کنید", "err"); return; }
  api("/api/zip/move", { name: name, dest: dest }).then(r => {
    if (r.status === 200) {
      localStorage.setItem("ts_mv_dest", dest);
      toast("✅ انتقال شد: \u2066" + (r.data.path || name) + "\u2069", "ok");
      if (!$("#prevZipsCard").hidden) loadZips($("#prevZips"), $("#prevZipsCard"));
      if (!$("#accZipsCard").hidden) loadZips($("#accZips"), $("#accZipsCard"));
    } else {
      toast((r.data && r.data.error) || "انتقال ناموفق بود", "err");
    }
  });
}

$("#setupForm").addEventListener("submit", e => {
  e.preventDefault();
  clearErrs();
  const btn = $("#setupBtn");
  busy(btn, true);
  const body = {
    api_id: norm($("#api_id").value.trim()),
    api_hash: $("#api_hash").value.trim(),
    phone: norm($("#phone").value.trim()),
    enc_pw: $("#enc_pw").value,
    enc_pw2: $("#enc_pw2").value,
  };
  api("/api/login/start", body).then(r => {
    busy(btn, false);
    if (r.status === 403) { toast((r.data && r.data.error) || "توکن نامعتبر", "err"); return; }
    if (handleAuth(r, () => $("#setupForm").requestSubmit(), btn)) return;
    if (!r.data || (!r.data.phase && !r.data.wait && !r.data.error)) toast("پاسخ نامعتبر", "err");
  }).catch(() => { busy(btn, false); toast("ارتباط برقرار نشد", "err"); });
});

$("#codeForm").addEventListener("submit", e => {
  e.preventDefault();
  clearErrs();
  const btn = $("#codeBtn");
  busy(btn, true);
  api("/api/login/code", { code: norm($("#code").value.trim()) }).then(r => {
    busy(btn, false);
    if (handleAuth(r, () => $("#codeForm").requestSubmit(), btn)) return;
    toast((r.data && r.data.error) || "خطا", "err");
  }).catch(() => { busy(btn, false); toast("ارتباط برقرار نشد", "err"); });
});

$("#twoFaForm").addEventListener("submit", e => {
  e.preventDefault();
  clearErrs();
  api("/api/login/2fa", { password: $("#password").value }).then(r => {
    if (handleAuth(r, () => $("#twoFaForm").requestSubmit(), $("#twoFaForm button"))) return;
    toast((r.data && r.data.error) || "خطا", "err");
  }).catch(() => toast("ارتباط برقرار نشد", "err"));
});

$("#resendBtn").addEventListener("click", () => {
  const btn = $("#resendBtn");
  busy(btn, true, "…");
  api("/api/login/resend").then(r => {
    busy(btn, false);
    if (r.data && r.data.wait) { countdown(r.data.wait); return; }
    if (r.data && r.data.msg) toast(r.data.msg, "ok");
  }).catch(() => busy(btn, false));
});

$$(".tab").forEach(t => t.addEventListener("click", () => {
  $$(".tab").forEach(x => x.setAttribute("aria-selected", "false"));
  t.setAttribute("aria-selected", "true");
  listQ.cat = t.dataset.cat;
  loadList(1);
}));

let qTimer = null;
$("#q").addEventListener("input", () => {
  if (qTimer) clearTimeout(qTimer);
  qTimer = setTimeout(() => { listQ.q = $("#q").value.trim().toLowerCase(); loadList(1); }, 300);
});

$("#prevBtn").addEventListener("click", () => loadList((listQ.page || 1) - 1));
$("#nextBtn").addEventListener("click", () => loadList((listQ.page || 1) + 1));
$("#refreshBtn").addEventListener("click", () => loadList(1, true));

$("#resolveForm").addEventListener("submit", e => {
  e.preventDefault();
  const txt = $("#resolveInput").value.trim();
  if (!txt) return;
  api("/api/resolve", { text: norm(txt) }).then(r => {
    if (r.status === 200 && r.data.item) { openChat(r.data.item.id); $("#resolveInput").value = ""; }
    else setErr("resolve", (r.data && r.data.error) || "پیدا نشد");
  });
});

$("#startBtn").addEventListener("click", () => {
  const id = $("#startBtn").dataset.id;
  if (!id) return;
  const btn = $("#startBtn");
  busy(btn, true);
  api("/api/start", { id: parseInt(norm(id), 10) }).then(r => {
    busy(btn, false);
    if (r.status === 200 && r.data.ok) { $("#chatDlg").close(); showProgress(); return; }
    toast((r.data && r.data.error) || "شروع نشد", "err");
  });
});
$("#dlgClose").addEventListener("click", () => $("#chatDlg").close());

// UIUX-04: توقف با تأیید — ضربه‌ی ناخواسته روی گوشی نصف اسکرپ را نمی‌کشد
$("#stopBtn").addEventListener("click", () => {
  askConfirm("توقف اسکرپ؟ فایل‌های دانلودشده حفظ می‌شوند و دفعه‌ی بعد از همین‌جا ادامه می‌یابد.",
             "بله، متوقف کن", () => {
    api("/api/stop").then(r => {
      if (r.data && r.data.ok) { $("#stopBtn").disabled = true; $("#phaseNote").textContent = "در حال توقف…"; }
    });
  });
});

$("#backBtn").addEventListener("click", () => showMain());

// فیکس WB-13: تنظیمات = نمای تمام‌صفحه با کارت‌های مدیا/خروجی/سرعت/حساب،
// سوییچ‌های استایل‌دار، سرعتِ قطعه‌ای از پاسخ سرور و نوار ذخیره‌ی sticky
const SPEED_LABEL = {gentle: "آرام", normal: "عادی", fast: "سریع"};
const SPEED_HINT = {
  gentle: "امن‌ترین — توقف‌های طولانی‌تر؛ کند اما کم‌ریسک",
  normal: "پیشنهادی — تعادلِ سرعت و امنیت",
  fast: "سریع‌ترین — ریسک محدودیت FloodWait بیشتر",
};
function buildSpeedSeg(speeds, current) {
  const seg = $("#s_speed");
  seg.innerHTML = "";
  (speeds && speeds.length ? speeds : ["gentle", "normal", "fast"]).forEach(sp => {
    const lab = document.createElement("label");
    lab.className = "seg-opt";
    const inp = document.createElement("input");
    inp.type = "radio"; inp.name = "s_speed"; inp.value = sp;
    inp.checked = sp === current;
    inp.addEventListener("change", speedHintUpdate);
    const spn = document.createElement("span");
    spn.textContent = SPEED_LABEL[sp] || sp;
    lab.append(inp, spn);
    seg.appendChild(lab);
  });
}
function speedHintUpdate() {
  const sel = document.querySelector("#s_speed input:checked");
  $("#speedHint").textContent = sel ? (SPEED_HINT[sel.value] || "") : "";
}
function mbHintUpdate() {
  const v = Math.max(0, parseInt(norm($("#s_max").value.trim() || "0"), 10) || 0);
  $("#mbHint").textContent = v > 0
    ? "فایل‌های بزرگ‌تر از " + fa(v) + " مگابایت دانلود نمی‌شوند."
    : "بدون سقف — همه‌ی فایل‌ها با هر حجمی دانلود می‌شوند.";
}
function mbToggle() {
  $("#mbWrap").hidden = !$("#s_limit").checked;
  mbHintUpdate();
}
function showSettings() {
  clearErrs();
  Promise.all([api("/api/settings"), api("/api/state")]).then(([rs, rt]) => {
    const d = rs.data || {};
    const s = d.settings || {};
    $("#s_media").checked = !!s.media;
    $("#s_zip").checked = !!s.zip;
    $("#s_sidecar").checked = !!s.sidecar;
    const mb = Math.max(0, parseInt(norm(s.max_mb ?? 0), 10) || 0);
    $("#s_limit").checked = mb > 0;
    $("#s_max").value = mb > 0 ? String(mb) : "100";
    mbToggle();
    buildSpeedSeg(d.speeds, d.speed || "normal");
    speedHintUpdate();
    const t = rt.data || {};
    $("#accWho").textContent = t.who ? "👤 " + t.who : "";
    $("#accPhone").textContent = t.phone ? "📞 \u2066" + t.phone + "\u2069" : "";
    $("#stDrive").textContent = t.drive ? "✅ Drive وصل است" : "❌ Drive وصل نیست";
    $("#stBase").textContent = t.base || "";
    $("#stDriveHint").textContent = t.drive
      ? "خروجی‌ها و بکاپ سشن روی Drive ذخیره می‌شوند."
      : "برای وصل کردن، سلول لانچر را با \u2066colab_launch(drive=True)\u2069 اجرا کنید. زیپ‌های موجود را با دکمهٔ 📤 کارت «زیپ‌های قبلی» دستی به Drive منتقل کنید.";
    $("#accBackup").textContent = t.bk_active
      ? (t.restored ? "بکاپ سشن روی Drive: فعال — این سشن از بکاپ بازیابی شده بود"
                    : "بکاپ سشن روی Drive: فعال")
      : "بکاپ سشن: غیرفعال (Drive یا کلید رمز TS_ENC_KEY در دسترس نیست)";
    show("settings");
    loadZips($("#accZips"), $("#accZipsCard"));   // فیکس WB-10: بعد از ریستارت هم قابل دانلود
  }).catch(() => toast("ارتباط با سرور برقرار نشد", "err"));
}
$("#settingsBtn").addEventListener("click", showSettings);
$("#topSetBtn").addEventListener("click", showSettings);   // UIUX-2: ⚙️ بالای صفحه
$("#setBack").addEventListener("click", () => boot());   // UIUX-2: ⚙️ از هر فازی باز می‌شود → بازگشت به فاز درست
$("#s_limit").addEventListener("change", mbToggle);
$("#s_max").addEventListener("input", mbHintUpdate);

$("#setForm").addEventListener("submit", e => {
  e.preventDefault();
  const sel = document.querySelector("#s_speed input:checked");
  api("/api/settings", {
    media: $("#s_media").checked, zip: $("#s_zip").checked,
    sidecar: $("#s_sidecar").checked,
    max_mb: $("#s_limit").checked ? norm($("#s_max").value.trim() || "0") : "0",
    speed: sel ? sel.value : "",
  }).then(r => {
    if (r.status === 200) { toast("تنظیمات ذخیره شد", "ok"); showMain(); }
    else if (r.data && r.data.field === "max_mb") setErr("max", r.data.error);
    else toast((r.data && r.data.error) || "ذخیره نشد", "err");
  });
});

// UIUX-04: دیالوگ تأیید عمومی — هم برای خروج، هم برای توقف اسکرپ
let confirmAct = null;
function askConfirm(msg, yesLabel, act) {
  confirmAct = act;
  $("#confirmMsg").textContent = msg;
  $("#confirmYes").textContent = yesLabel || "بله";
  const dlg = $("#confirmDlg");
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
}
$("#logoutBtn").addEventListener("click", () => {
  askConfirm("خروج کامل: سشن محلی و بکاپ Drive هر دو حذف می‌شوند. مطمئنید؟",
             "بله، خارج شو", () => {
    api("/api/logout").then(r => {
      if (r.data && r.data.msg) toast(r.data.msg, "ok");
      boot();
    });
  });
});
$("#confirmNo").addEventListener("click", () => $("#confirmDlg").close());
$("#confirmYes").addEventListener("click", () => {
  $("#confirmDlg").close();
  const act = confirmAct; confirmAct = null;
  if (act) act();
});

$("#themeBtn").addEventListener("click", () => {
  const dark = document.documentElement.classList.toggle("dark");
  try { localStorage.setItem("ts-theme", dark ? "dark" : "light"); } catch (e) {}
});
try { if (localStorage.getItem("ts-theme") === "dark") document.documentElement.classList.add("dark"); }
catch (e) {}
if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches &&
    (function(){ try { return !localStorage.getItem("ts-theme"); } catch(e){ return false; } })()) {
  document.documentElement.classList.add("dark");
}

boot();
</script>
</body>
</html>"""


# ═══════════════ WEB-06: سخت‌افزاری و امنیت ═══════════════

def _check_token():
    """نسخه‌ی سخت‌گیرانه — هر POST باید هدر X-TS-Token با token سرور داشته باشد (قابلیت WEB-06).
    تک‌کاربره و in-memory؛ بدون وابستگی به Flask-WTF. فیکس WB-11: این تنها تعریفِ _check_token است."""
    sent = str(request.headers.get("X-TS-Token", ""))
    # فیکس WB-09: لینک‌های دانلود GETند و هدر نمی‌گذارند → پشتیبان پارامتر ?t=
    if not sent:
        sent = str(request.args.get("t", ""))
    return _secrets.compare_digest(sent.encode("utf-8", "replace")[:128],
                                   str(STATE["token"]).encode("utf-8"))


# ═══════════════ موج TUN: قفل دسترسی لینک عمومی (APP_TOKEN) ═══════════════

PAGE_401 = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>401 — TeleScrape</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;margin:0;min-height:100vh;
display:flex;align-items:center;justify-content:center;background:#0b1220;color:#e8edf7}
p{text-align:center;padding:0 16px;line-height:1.9}</style></head>
<body><p>🔒 لینک را با «?key=رمز» باز کنید<br>(رمز = همان مقدار سکرت APP_TOKEN)</p></body></html>"""


def _access_gate():
    """موج TUN: بدون key صحیح (?key= یا کوکی قبلی) → 401. کلید خالی = بدون قفل."""
    key = str(STATE.get("access_key") or "")
    if not key:
        return None
    sent = str(request.args.get("key", ""))
    if sent and _secrets.compare_digest(sent.encode("utf-8", "replace")[:128], key.encode("utf-8")):
        return None
    cookie = str(request.cookies.get("ts_key", ""))
    if cookie and _secrets.compare_digest(cookie.encode("utf-8", "replace")[:128], key.encode("utf-8")):
        return None
    return PAGE_401, 401


@web.after_request
def _access_remember(response):
    """موج TUN: اولین ورود با ?key= صحیح → کوکی ts_key (HttpOnly، ۳۰ روز) تا دفعات بعد بدون key باز شود."""
    key = str(STATE.get("access_key") or "")
    sent = str(request.args.get("key", ""))
    if key and sent and _secrets.compare_digest(sent.encode("utf-8", "replace")[:128], key.encode("utf-8")):
        try:
            response.set_cookie("ts_key", key, httponly=True, samesite="Lax", max_age=30 * 24 * 3600)
        except Exception:
            pass                    # مقدار غیر‌کوکی‌پسند → هر بار با ?key= وارد شود
    return response


web.before_request(_access_gate)


@web.errorhandler(404)
def _not_found(e):
    """مسیر غایب — پاسخ تمیز و دوستانه، نه 400 خام (قابلیت WEB-06)."""
    if request.path.startswith("/api/"):
        return jsonify({"error": wt("not_found_404")}), 404
    return PAGE, 404


@web.errorhandler(500)
def _server_error(e):
    """خطای سرور — بدون leak جزئیات/secret؛ پیام تمیز (قابلیت WEB-06)."""
    if request.path.startswith("/api/"):
        return jsonify({"error": wt("server_500")}), 500
    return PAGE, 500


# ═══════════════ WEB-07: --selftest (هلپرهای خالص، بدون شبکه) ═══════════════

def _selftest():
    """تست دودِ هلپرهای خالصِ لایه‌ی وب — python telescrape.py --selftest
    پوشش: ارقام فارسی، ولیدیشن creds، گارد زیپ، چک token، گذارهای ماشین حالت لاگین با کلاینت فیک."""
    import tempfile

    # فیکس NB-19: در Colab ساخت App سعی در mount تعاملی Drive می‌کرد و در پروسه‌ی
    # جدا (بدون کرنل) AttributeError می‌داد؛ خودتست Drive لازم ندارد.
    ts.USE_DRIVE = False

    ok = fail = 0

    def eq(got, want, label):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"FAIL {label}: got {got!r}, want {want!r}")

    # _to_int: ارقام فارسی/عربی مثل ورودی موبایل
    eq(_to_int("۱۲۳"), 123, "int.fa")
    eq(_to_int("٤٥"), 45, "int.ar")
    eq(_to_int(" 42 "), 42, "int.space")
    eq(_to_int("x", 7), 7, "int.fallback")
    eq(_to_int(None, 9), 9, "int.none")

    # فیکس WB-01: پرچم IN_COLAB موتور باید با تشخیص واقعی هم‌خوان بماند
    eq(ts.IN_COLAB, _is_colab(), "colab.flagsync")

    # فیکس NB-15: در پروسه‌ی جدا سکرت از env خوانده می‌شود (userdata غیرفعال)
    os.environ["TG_API_ID"] = "424242"
    _dead = ts._USERDATA_DEAD
    ts._USERDATA_DEAD = True
    try:
        eq(ts.get_secret("api_id"), "424242", "sec.env")
        eq(ts.get_secret("phone"), None, "sec.env_missing")
    finally:
        ts._USERDATA_DEAD = _dead
        del os.environ["TG_API_ID"]

    # فیکس NB-21: خواندن سکرت در کرنل — وضعیت هر سکرت، نام‌های جایگزین، بدون نشت مقدار
    _SNF = type("SecretNotFoundError", (Exception,), {})
    _NAE = type("NotebookAccessError", (Exception,), {})

    def _g(name):
        if name == "TG_API_ID":
            return " 424242 "
        if name == "API_HASH":                 # نام جایگزین → زیر نام اصلی پاس داده می‌شود
            return "h" * 32
        if name == "TG_2FA_PASSWORD":
            raise _NAE("off")
        if name == "TS_ENC_KEY":
            raise TimeoutError("timed out")
        raise _SNF(name)

    _vals, _rep = _read_kernel_secrets(_g)
    eq(_vals, {"TG_API_ID": "424242", "TG_API_HASH": "h" * 32}, "ln.values")
    eq([_rep[k] for k in ("api_id", "api_hash", "phone", "pwd", "enc_key")],
       ["ok", "ok", "missing", "no_access", "timeout"], "ln.report")
    eq("424242" in str(_rep), False, "ln.noleak")

    def _bad(name):
        raise ValueError("secret-value-should-not-leak")

    _v2, _r2 = _read_kernel_secrets(_bad)
    eq((_v2, _r2["api_id"]), ({}, "error:ValueError"), "ln.error")
    eq("secret-value" in str(_r2), False, "ln.error_noleak")

    # فیکس NB-21: در پروسه‌ی جدا userdata.get با AttributeError می‌میرد (نه timeout) —
    # باید از همان اولین خطا غیرفعال شود و مقدار از env بیاید؛ خطای «سکرت نیست» غیرفعالش نکند
    import types as _types
    _saved_mods = {k: sys.modules.get(k) for k in ("google", "google.colab", "google.colab.userdata")}
    _g_, _c_, _u_ = (_types.ModuleType(n) for n in ("google", "google.colab", "google.colab.userdata"))
    _c_.userdata, _g_.colab = _u_, _c_
    sys.modules.update({"google": _g_, "google.colab": _c_, "google.colab.userdata": _u_})
    _in_colab, _dead2 = ts.IN_COLAB, ts._USERDATA_DEAD
    os.environ["TG_API_ID"] = "777"
    try:
        ts.IN_COLAB = True
        ts._USERDATA_DEAD = False

        def _snf(name):
            raise _SNF(name)

        _u_.get = _snf
        eq(ts.get_secret("api_id"), "777", "dead.env_after_notfound")
        eq(ts._USERDATA_DEAD, False, "dead.notfound_keeps_userdata")

        def _kernelless(name):
            raise AttributeError("'NoneType' object has no attribute 'kernel'")

        _u_.get = _kernelless
        eq(ts.get_secret("api_id"), "777", "dead.env_after_attr")
        eq(ts._USERDATA_DEAD, True, "dead.attr_disables_userdata")

        # راهنمای /api/state: در Colab بدون TS_WEB_CHILD دلیلِ نرسیدن سکرت گفته می‌شود
        _wc = os.environ.pop("TS_WEB_CHILD", None)
        eq(bool(web.test_client().get("/api/state").get_json()["hint"]), True, "state.hint_no_launcher")
        os.environ["TS_WEB_CHILD"] = "1"
        eq(web.test_client().get("/api/state").get_json()["hint"], "", "state.hint_launcher")
        if _wc is None:
            del os.environ["TS_WEB_CHILD"]
        else:
            os.environ["TS_WEB_CHILD"] = _wc
    finally:
        ts.IN_COLAB, ts._USERDATA_DEAD = _in_colab, _dead2
        os.environ.pop("TG_API_ID", None)
        for _k, _m in _saved_mods.items():
            if _m is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _m
        _detect_secrets()

    # فیکس NB-21: چک پورت — آزاد/اشغال (با سوکت شنونده‌ی واقعی)
    import socket as _s2
    _ls = _s2.socket()
    _ls.bind(("127.0.0.1", 0))
    _ls.listen(1)
    _busy_port = _ls.getsockname()[1]
    eq(_port_free(_busy_port), False, "port.busy")
    _ls.close()
    eq(_port_free(_busy_port), True, "port.free")

    # _valid_api_id / _valid_hash — همان قواعد get_creds موتور (قابلیت WEB-03)
    eq(_valid_api_id("12345"), 12345, "id.ok")
    eq(_valid_api_id("۰"), None, "id.zero")
    eq(_valid_api_id("abc"), None, "id.nan")
    h = "0123456789abcdef0123456789abcdef"
    eq(_valid_hash(h), h, "hash.ok")
    eq(_valid_hash(h.upper()), h.upper(), "hash.upper")
    eq(_valid_hash("short"), None, "hash.short")
    eq(_valid_hash(None), None, "hash.none")
    eq(_valid_hash("  " + h + "  "), h, "hash.strip")

    # _zip_path: گارد زیپ — فقط نامِ ساده داخل zdir (قابلیت WEB-04/WEB-06)
    with tempfile.TemporaryDirectory() as tmp:
        eq(_zip_path(tmp, "chat_1_1_2.zip"),
           os.path.join(os.path.realpath(tmp), "chat_1_1_2.zip"), "zip.ok")
        eq(_zip_path(tmp, ""), None, "zip.empty")
        eq(_zip_path(tmp, None), None, "zip.none")
        eq(_zip_path(tmp, "../etc/passwd"),
           os.path.join(os.path.realpath(tmp), "passwd"), "zip.basename")
        out = os.path.join(tmp, "outside.zip")
        with open(out, "w") as f:
            f.write("x")
        link = os.path.join(tmp, "link.zip")
        os.symlink(out, link)                  # symlink بیرون‌نما → رد با realpath
        eq(_zip_path(tmp, "link.zip"), None, "zip.symlink")

    # _res_payload: شکل امنِ نتیجه برای JSON (قابلیت WEB-04)
    eq(_res_payload(None), None, "res.none")
    r = _res_payload({"folder": "/f", "zip": "/f/x.zip", "empty": False,
                      "interrupted": False, "reason": "", "msgs": 3,
                      "files": 2, "skipped": 1, "errors": 0})
    eq(r["zip_name"], "x.zip", "res.zipname")
    eq(r["msgs"], 3, "res.msgs")
    eq(_res_payload({})["empty"], False, "res.defaults")
    eq(_res_payload({})["msgs"], 0, "res.zero")

    # UIUX-03: now سرور در api_progress + finished_at در job + zip_size در نتیجه
    _pj = web.test_client().get("/api/progress").get_json()
    eq(isinstance(_pj.get("now"), float), True, "prog.now")
    _saved_job = STATE["job"]
    STATE["job"] = {"chat": "x", "chat_id": 1, "started_at": time.time() - 9,
                    "finished_at": time.time() - 1, "done": True, "error": None, "result": None}
    _jp = _job_payload()
    eq(_jp["finished_at"] >= _jp["started_at"], True, "job.finished")
    STATE["job"] = _saved_job
    eq(_res_payload({"zip": "/nope/x.zip"})["zip_size"], None, "res.zsize")

    # فیکس WB-05: بافر seq‌دار — پیام‌ها گم نمی‌شوند و فیلتر since فقط جدیدها را می‌دهد
    saved_msgs = list(_MSGS)
    _MSGS.clear()
    ts._msgs.clear()
    try:
        for m in ("m1", "m2"):
            ts._msgs.append(m)
        _drain_msgs()
        eq([t for _, t in _MSGS], ["m1", "m2"], "msg.drain")
        ts._msgs.append("m3")
        _drain_msgs()
        eq([s for s, _ in _MSGS], [1, 2, 3], "msg.seq")
        eq([t for s, t in _MSGS if s > 2], ["m3"], "msg.since")
        eq(_drain_msgs(), [], "msg.empty_drain")
    finally:
        _MSGS.clear()
        _MSGS.extend(saved_msgs)
        ts._msgs.clear()

    # _check_token (نسخه‌ی سخت WEB-06): هدر درست/غلط/غایب
    with web.test_request_context("/", headers={"X-TS-Token": STATE["token"]}):
        eq(_check_token(), True, "token.ok")
    with web.test_request_context("/", headers={"X-TS-Token": "deadbeef"}):
        eq(_check_token(), False, "token.bad")
    with web.test_request_context("/"):
        eq(_check_token(), False, "token.missing")
    with web.test_request_context("/?t=" + STATE["token"]):   # فیکس WB-09: پشتیبان ?t=
        eq(_check_token(), True, "token.query")
    with web.test_request_context("/?t=deadbeef"):
        eq(_check_token(), False, "token.query_bad")

    # موج TUN: قفل دسترسی عمومی — 401 بدون key، 200 با ?key= و کوکی
    _ak = STATE.get("access_key")
    STATE["access_key"] = "zz-selftest-zz"
    try:
        tc = web.test_client()
        eq(tc.get("/").status_code, 401, "gate.401")
        rv = tc.get("/?key=zz-selftest-zz")
        eq(rv.status_code, 200, "gate.key200")
        eq(any(v.startswith("ts_key=") for v in rv.headers.get_all("Set-Cookie")), True, "gate.cookie")
        eq(tc.get("/", headers={"Cookie": "ts_key=zz-selftest-zz"}).status_code, 200, "gate.cookie200")
        # فیکس NB-22: test_client کوکیِ معتبر را در jar خود نگه می‌دارد؛ این دو تست
        # باید با کلاینت تازه (بدون کوکی) سنجیده شوند — گیت با کوکیِ معتبر به‌درستی 200 می‌دهد.
        tc2 = web.test_client()
        eq(tc2.get("/?key=wrong").status_code, 401, "gate.wrong")
        eq(tc2.get("/api/state").status_code, 401, "gate.api401")
    finally:
        STATE["access_key"] = _ak

    # فیکس NB-17: آیدی منفی (کانال/گروه) باید به api_chat مچ شود
    _m = web.url_map.bind("localhost")
    eq(_m.match("/api/chat/-1004290167321", method="GET")[0], "api_chat", "route.negative")
    eq(_m.match("/api/chat/4290167321", method="GET")[0], "api_chat", "route.positive")

    # UIUX-2: _safe_dest — مسیر مقصدِ انتقال زیپ (خالص)
    eq(_safe_dest("", colab=False), None, "dest.empty")
    eq(_safe_dest("rel/x", colab=False), None, "dest.relative")
    eq(_safe_dest("/content/out", colab=True), "/content/out", "dest.colab_ok")
    eq(_safe_dest("/etc/out", colab=True), None, "dest.colab_denied")

    # UIUX-2: POST /api/zip/move — انتقال واقعی + گاردها (ترتیب: bad → notfound → ok)
    _saved = dict(STATE)
    _oldcwd2 = os.getcwd()
    try:
        os.chdir(tempfile.mkdtemp())
        STATE["app"] = ts.App()
        STATE["phase"] = "ready"
        STATE["client"] = object()
        eq(_safe_dest(STATE["app"].session_dir, colab=False), None, "dest.session")
        _mvdst = "/content/ts_mv_test" if ts.IN_COLAB else tempfile.mkdtemp()
        _zfx = os.path.join(STATE["app"].zips, "t_move.zip")
        with open(_zfx, "w") as f:
            f.write("x" * 100)
        _tc = web.test_client()
        _r1 = _tc.post("/api/zip/move", headers={"X-TS-Token": STATE["token"]},
                       json={"name": "t_move.zip", "dest": "rel"})
        eq(_r1.status_code, 400, "move.bad_dest")
        _r2 = _tc.post("/api/zip/move", headers={"X-TS-Token": STATE["token"]},
                       json={"name": "nope.zip", "dest": _mvdst})
        eq(_r2.status_code, 404, "move.notfound")
        _r3 = _tc.post("/api/zip/move", headers={"X-TS-Token": STATE["token"]},
                       json={"name": "t_move.zip", "dest": _mvdst})
        eq(_r3.status_code, 200, "move.ok")
        eq(_r3.get_json().get("ok"), True, "move.ok_json")
        eq(os.path.isfile(os.path.join(_mvdst, "t_move.zip")), True, "move.file")
        shutil.rmtree(_mvdst, ignore_errors=True)     # پاک‌سازی مقصدِ تست
    finally:
        os.chdir(_oldcwd2)
        STATE.clear()
        STATE.update(_saved)

    # ماشین حالت لاگین با کلاینت فیک — بدون شبکه (قابلیت WEB-03/WEB-07)
    class _FakeMe:
        id = 42

    class _FakeClient:
        def __init__(self, authorized):
            self.authorized = authorized
            self.calls = []

        async def connect(self):
            self.calls.append("connect")

        async def is_user_authorized(self):
            return self.authorized

        async def send_code_request(self, phone):
            self.calls.append(("code", phone))

        async def get_me(self):
            return _FakeMe()

        async def iter_dialogs(self):
            return
            yield

    saved = dict(STATE)
    oldcwd = os.getcwd()
    try:
        os.chdir(tempfile.mkdtemp())            # App() پوشه‌های محلی می‌سازد
        STATE["app"] = ts.App()
        STATE["api_id"] = 1
        STATE["phone"] = "+989121234567"
        with web.app_context():
            fc = _FakeClient(True)             # سشن معتبر → مستقیم ready
            STATE["client"] = fc
            STATE["phase"] = "setup"
            rv = _connect_and_flow()
            eq(rv.get_json()["phase"], "ready", "flow.authorized")
            eq(STATE["phase"], "ready", "flow.phase_ready")
            fc2 = _FakeClient(False)           # لاگین تازه → send_code → need_code
            STATE["client"] = fc2
            STATE["phase"] = "setup"
            rv2 = _connect_and_flow()
            eq(rv2.get_json()["phase"], "need_code", "flow.fresh")
            eq(STATE["phase"], "need_code", "flow.phase_code")
            eq(fc2.calls[-1], ("code", "+989121234567"), "flow.send")
    finally:
        os.chdir(oldcwd)
        STATE.clear()
        STATE.update(saved)

    print(f"self-test: {ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    main()
