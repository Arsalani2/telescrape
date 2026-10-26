#@title 📡 TeleScrape v3 — اسکرپر تلگرام (Colab / Terminal)
# -*- coding: utf-8 -*-
"""
TeleScrape v3 — Telegram channel / group / private-chat exporter

* Credentials come from Colab Secrets (key icon on the left):
      TG_API_ID, TG_API_HASH, TG_PHONE   (optional: TG_2FA_PASSWORD)
  Turn on "Notebook access" for each. Environment variables with the same names also work.
* Phone-friendly narrow UI (WIDTH), Persian/Arabic digits are accepted everywhere.
* Pick a chat -> Enter -> full export from the start. No sub-menus.
* Built-in cooldown (random pauses, periodic long rests, adaptive FloodWait backoff)
  so Telegram doesn't see bot-like request bursts.
* Watchdogs: every network call has a timeout, stalled downloads are retried, the
  Telegram client runs in its own thread so the notebook UI can never freeze it.

Get API ID / API Hash from https://my.telegram.org -> API Development Tools.
Keep them (and the session file) secret.
"""

# ───────────────────────── Colab form (dropdowns on phone) ─────────────────────────
LANG = "fa"               #@param ["fa", "en"]
SPEED = "normal"          #@param ["gentle", "normal", "fast"]
WIDTH = 34                #@param {type:"slider", min:24, max:60, step:1}
PER_PAGE = 10             #@param {type:"integer"}
USE_DRIVE = False         #@param {type:"boolean"}
SESSION_ON_DRIVE = False  #@param {type:"boolean"}
CLEAR_SCREEN = True       #@param {type:"boolean"}

import os, sys, re, json, time, zipfile, asyncio, subprocess, getpass, random, shutil
import textwrap, threading, collections, errno
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
        "old_session": "⚠️ session قدیمی روی Drive هست. اگر لازم ندارید پاکش کنید:\n{p}",
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
        "logout_q": "از حساب خارج و session حذف شود؟ (y/N): ",
        "logged_out": "خارج شدید و session حذف شد.",
        "enter": "Enter برای ادامه...",
        "bye": "خداحافظ 👋",
        "none": "ندارد",
    },
    "en": {
        "title": "📡 TeleScrape",
        "hint": "The input box is at the bottom of the output; scroll down if hidden.",
        "secrets_missing": "⚠️ Secret not found: {names}\n🔑 Create it in Colab and enable 'Notebook access'. Enter manually for now.",
        "legacy_removed": "🧹 Old saved API Hash file removed (no longer needed).",
        "old_session": "⚠️ An old session is on Drive. Delete it if unneeded:\n{p}",
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
        "logout_q": "Log out and delete the session? (y/N): ",
        "logged_out": "Logged out, session deleted.",
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
PROG = {"txt": ""}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def hr():
    return "─" * max(10, min(int(WIDTH), 40))


def say(text=""):
    w = max(20, int(WIDTH))
    for line in str(text).split("\n"):
        print(textwrap.fill(line, w, break_long_words=False, break_on_hyphens=False) if line else "")


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
        out.write("\r" + str(line)[:width].ljust(width))
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


DEFAULT_EXT = {"Photo": ".jpg", "Voice": ".ogg", "Music": ".mp3", "Video": ".mp4",
               "VideoMessage": ".mp4", "GIF": ".mp4", "Sticker": ".webp", "File": ""}


def safe_ext(ext, kind):
    """The extension comes from remote data -> never trust it inside a path."""
    ext = str(ext or "")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        ext = DEFAULT_EXT.get(kind, "")
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
                if f.endswith(".part"):
                    continue
                full = os.path.join(root, f)
                if os.path.islink(full):
                    continue
                comp = zipfile.ZIP_DEFLATED if f.lower().endswith(TEXT_EXT) else zipfile.ZIP_STORED
                zf.write(full, os.path.relpath(full, base), compress_type=comp)
    os.replace(tmp, zip_path)


# ───────────────────────────── secrets / credentials ─────────────────────────────
def get_secret(key):
    """Colab Secret first, then environment variable. Returns (value or None)."""
    for name in SECRET_NAMES[key]:
        if IN_COLAB:
            try:
                from google.colab import userdata
                v = userdata.get(name)
                if v:
                    return str(v).strip()
            except Exception:
                pass
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
    if p and not p.startswith("+"):
        p = "+" + p
    return p


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
    missing = [SECRET_NAMES[k][0] for k, v in (("api_id", api_id), ("api_hash", api_hash),
                                               ("phone", phone)) if not v]
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
    phone = norm_phone(phone) if phone else norm_phone(ask(t("phone")))
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
        # The session file equals your Telegram login -> keep it off Drive unless asked.
        sess_root = self.base if (USE_DRIVE and SESSION_ON_DRIVE) else local
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
        if USE_DRIVE and not SESSION_ON_DRIVE:
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
    for attempt in range(4):
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
                status(f"{tag} ⬇ {st['cur'] // MB}/{size // MB}MB")
            r = task.result()
            if r and os.path.exists(r) and os.path.getsize(r) > 0:
                os.replace(r, path)
                th.ok()
                return True
            return False
        except FloodWaitError as e:
            last = e
            w = th.flood(e.seconds)
            notify(t("flood", s=int(w)))
            await nap(w)
        except RETRY_ERRORS as e:
            last = e
            if attempt < 3:
                await nap(min(BACKOFF_BASE * 2 ** attempt, 40) + random.random())
        except OSError as e:
            if is_disk_full(e):
                raise DiskLow()
            last = e
            if attempt < 3:
                await nap(min(BACKOFF_BASE * 2 ** attempt, 40) + random.random())
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


async def scrape(app, client, item, speed):
    """Full export of one chat from its first message. Returns a result dict (never prints)."""
    entity, pid, name = item["entity"], item["id"], item["name"]
    cfg = app.settings
    th = Throttle(speed)
    folder = os.path.join(app.exports, f"{sanitize(name)}_{pid}")
    os.makedirs(folder, exist_ok=True)
    meta_path = os.path.join(folder, "metadata.txt")
    md_path = os.path.join(folder, "all_texts.md")
    log_path = os.path.join(folder, "LOG.txt")
    max_bytes = int(cfg.get("max_mb") or 0) * MB

    stats = {"msgs": 0, "files": 0, "skipped": 0, "errors": 0}
    res = {"folder": folder, "zip": None, "interrupted": False, "reason": "", "empty": False, **stats}
    cache, err_lines = {}, []
    first_id = last_done = None
    t0 = time.time()
    PROG["txt"] = ""

    try:
        total = (await tg_call(lambda: client.get_messages(entity, limit=0), th, 60)).total or 0
    except Stopped:
        res.update(interrupted=True, reason="stop")
        return res
    if total == 0:
        res["empty"] = True
        return res

    async def author_of(m):
        sid = m.sender_id
        if sid is None:
            return name
        if sid not in cache:
            s = getattr(m, "sender", None)
            if s is None:
                try:
                    s = await asyncio.wait_for(m.get_sender(), 20)
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
            elif os.path.exists(path) and os.path.getsize(path) > 0:
                status_txt = "already downloaded"
            if status_txt:
                stats["skipped"] += 1
                mf.write(f"File: {fname} -> {status_txt}\n")
            else:
                tag = f"{int(stats['msgs'] * 100 / total):3d}% #{mid}"
                ok = await download(client, m, path, size, th, tag)
                if ok:
                    stats["files"] += 1
                    mf.write(f"File: {fname}\n")
                    if cfg.get("sidecar"):
                        with open(path + ".info.txt", "w", encoding="utf-8") as inf:
                            inf.write(f"Author: {author}\nDate: {dt}\nType: {kind}\n"
                                      f"Caption: {text or 'None'}\n")
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
    last_id = 0
    try:
        with open(meta_path, "w", encoding="utf-8") as mf, open(md_path, "w", encoding="utf-8") as md:
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
                    pct = min(99, int(stats["msgs"] * 100 / total))
                    PROG["txt"] = f"{pct}% {stats['msgs']}/{total}"
                    status(f"{pct:3d}% {stats['msgs']}/{total} f{stats['files']} e{stats['errors']}")
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

    zip_path = None
    if not interrupted and cfg.get("zip", True) and first_id is not None:
        zip_path = os.path.join(app.zips, f"{sanitize(name)}_{pid}_{first_id}_{last_done}.zip")
        status(t("zipping"))
        try:
            await asyncio.to_thread(make_zip, folder, zip_path)   # off the loop: never blocks
        except Exception as e:
            err_lines.append(f"zip: {e}")
            zip_path = None

    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"Run start: {datetime.fromtimestamp(t0).strftime('%Y-%m-%d %H:%M:%S')}\n")
            lf.write(f"Duration (s): {time.time() - t0:.1f}\n")
            lf.write(f"Chat: {name} ({pid}) | Interrupted: {interrupted} {reason}\n")
            lf.write(f"Messages: {stats['msgs']} | Files: {stats['files']} | "
                     f"Skipped: {stats['skipped']} | Errors: {stats['errors']}\n")
            lf.write(f"First id: {first_id} | Last id: {last_done} | Rests: {th.rests}\n")
            for line in err_lines[:200]:
                lf.write(f"ERR {line}\n")
            lf.write("-" * 80 + "\n")
    except Exception:
        pass

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
    total = run(count_msgs(client, item["entity"]))
    print(hr())
    say(t("chat_head", name=short(item["name"], 60), id=item["id"], total="?" if total is None else total))
    print(hr())
    if ask(t("start_q")) == "0":
        return
    say(t("starting"))
    try:
        res = run(scrape(app, client, item, app.speed), stoppable=True)
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
                try:
                    run(client.log_out())
                except Exception:
                    pass
                app.delete_session(api_id)
                say(t("logged_out"))
                return "logout"
        elif c == "0":
            app.save_settings()
            return None
        app.save_settings()


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
            time.sleep(e.seconds + 2)
        except Exception as e:
            say(t("login_err", e=short_err(e)))
            raise LoginAbort()
    else:
        raise LoginAbort()
    if run(client.is_user_authorized()):
        return

    def send():
        nonlocal phone
        for _ in range(5):
            try:
                run(client.send_code_request(phone))
                return
            except FloodWaitError as e:
                if e.seconds > 600:
                    say(t("flood_login", s=e.seconds))
                    raise LoginAbort()
                say(t("flood_login", s=e.seconds))
                time.sleep(e.seconds + 2)
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
            time.sleep(e.seconds + 2)
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
            time.sleep(e.seconds + 2)
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
    client = run(make_client(app.session_path(api_id), api_id, api_hash))
    sys._telescrape_prev = (client, _BG["loop"])   # survives cell re-runs (kept on sys)
    app.lock_session(api_id)          # 600 from the moment the file exists, before any login step
    try:
        login(client, phone, pwd2fa)
        app.lock_session(api_id)
        me = run(client.get_me())
        who = utils.get_display_name(me) or str(me.id)
        say(t("login_ok", name=who))
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


def main():
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


if __name__ == "__main__":
    main()
