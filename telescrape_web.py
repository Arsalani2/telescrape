#@title 🌐 TeleScrape Web — رابط موبایل‌پسند (Colab)
# -*- coding: utf-8 -*-
"""
TeleScrape Web — لایه‌ی وبِ موبایل‌پسند برای TeleScrape v3 (قابلیت WEB-01…WEB-07)

اجرا در Colab (سه سلولِ آماده در README):
    !wget -q https://raw.githubusercontent.com/Personalmo/telescrape/main/telescrape_v3.py
    !wget -q https://raw.githubusercontent.com/Personalmo/telescrape/main/telescrape_web.py
    !nohup python telescrape_web.py --port 8000 >/dev/null 2>&1 &
سپس در سلول بعدی:
    from google.colab import output; output.serve_kernel_port_as_window(8000)

* موتور (telescrape_v3.py) دست‌نخورده است و با `import telescrape_v3 as ts` استفاده می‌شود؛
  تنها تغییرِ موتور، hook پیشرفت WEB-02 است.
* کارهای تلگرام فقط روی loop پس‌زمینه‌ی موتور اجرا می‌شوند (asyncio.run_coroutine_threadsafe).
* credentialها فقط در RAM می‌مانند؛ هرگز در پاسخ API یا لاگ نمی‌آیند.
* حالت ترمینال موتور بی‌تغییر می‌ماند — این فایل لایه‌ی جداگانه‌ی وب است.
"""

import argparse
import asyncio
import os
import re
import secrets as _secrets
import sys
import threading
import time

import telescrape_v3 as ts

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
    with LOCK:
        return jsonify({
            "phase": STATE["phase"],
            "token": STATE["token"],
            "who": STATE["who"],
            "restored": STATE["restored"],
            "bk_active": _bk_active(),  # فیکس WB-13: وضعیت بکاپ سشن برای کارت «حساب»
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


def main():
    if "--selftest" in sys.argv:                 # قابلیت WEB-07: تعریف پایین همین فایل
        fn = globals().get("_selftest")
        if fn is None:
            print("selftest: not available in this build")
            raise SystemExit(1)
        fn()
        return
    args = _parse_args()
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
    _detect_secrets()
    print(f"TeleScrape Web listening on 0.0.0.0:{args.port}")
    print("Open it on the phone with: from google.colab import output;")
    print(f"                            output.serve_kernel_port_as_window({args.port})")
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
    """بعد از ورود: _get_me (NB-09) → بکاپ اگر restore نبود (NB-12) → کش دیالوگ‌ها → ready."""
    client = STATE["client"]
    try:
        me = _bg_call(ts._get_me(client), 90)
    except Exception as e:
        return _net_error(e)
    STATE["who"] = " ".join((ts.utils.get_display_name(me) or str(me.id)).split())
    backup = "backup_off"
    if _bk_active() and not STATE["restored"]:
        app = STATE["app"]
        pw = ts.get_secret("enc_key") or STATE["enc_pw"]
        if pw and ts.make_session_backup(app.session_path(STATE["api_id"]) + ".session",
                                         app.backup_path(STATE["api_id"]), pw):
            backup = "backup_ok"
    try:
        STATE["items"] = _bg_call(ts.load_dialogs(client), 600) or []
    except Exception:
        STATE["items"] = []
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
        STATE["phase"] = "need_code"
        return jsonify({"wait": int(e.seconds) + 2, "phase": "need_code"})
    except Exception as e:
        return _net_error(e)
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
    return {"folder": res.get("folder"), "zip": z,
            "zip_name": os.path.basename(z) if z else None,
            "empty": bool(res.get("empty")), "interrupted": bool(res.get("interrupted")),
            "reason": res.get("reason") or "", "msgs": res.get("msgs", 0),
            "files": res.get("files", 0), "skipped": res.get("skipped", 0),
            "errors": res.get("errors", 0)}


def _job_payload():
    j = STATE["job"]
    if j is None:
        return None
    return {"chat": j["chat"], "chat_id": j["chat_id"], "started_at": j["started_at"],
            "done": j["done"], "error": j["error"],
            "result": _res_payload(j["result"]) if j["done"] else None}


def _job_done(fut):
    """پایان job روی thread خود future: ذخیره‌ی نتیجه + ریست STOP (قابلیت WEB-04)."""
    try:
        res = fut.result()
        STATE["job"]["result"] = res
        STATE["last_result"] = res
    except Exception as e:
        STATE["job"]["error"] = ts.short_err(e)
    STATE["job"]["done"] = True
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


@web.route("/api/chat/<int:cid>")
def api_chat(cid):
    """کارت چت: نام + آیدی + تعداد پیام با count_msgs (قابلیت WEB-04)."""
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
                       done=0, pct=0, files=0, skipped=0, errors=0, zip=None)
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
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:12px 16px;position:sticky;top:0;background:var(--surface);
  border-bottom:1px solid var(--line);z-index:10}
.topbar h1{font-size:18px;margin:0;display:flex;align-items:center;gap:8px}
.wrap{max-width:560px;margin:0 auto;padding:0 16px}
h2{font-size:17px;margin:0 0 8px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px;box-shadow:var(--shadow);margin:16px 0}
.muted{color:var(--muted);font-size:14px;margin:4px 0}
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
.badge{font-size:12px;color:var(--muted);flex:none}
.skeleton{height:56px;border-radius:10px;margin:8px 0;
  background:linear-gradient(90deg,var(--line) 25%,var(--surface) 50%,var(--line) 75%);
  background-size:200% 100%;animation:shimmer 1.2s infinite}
@keyframes shimmer{to{background-position:-200% 0}}
.progress{height:14px;border-radius:999px;background:var(--line);overflow:hidden;margin:12px 0}
.progress>div{height:100%;background:var(--primary);transition:width .4s}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.stat .muted{margin:0;font-size:12px}
.stat b{font-size:16px}
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
        <label for="api_id">API ID</label>
        <input id="api_id" inputmode="numeric" autocomplete="off" dir="ltr" aria-describedby="err_api_id">
        <p class="err" id="err_api_id" aria-live="polite"></p>
        <label for="api_hash">API Hash</label>
        <input id="api_hash" type="password" autocomplete="off" dir="ltr" aria-describedby="err_api_hash">
        <p class="err" id="err_api_hash" aria-live="polite"></p>
      </div>
      <label for="phone">شماره تلگرام</label>
      <input id="phone" type="tel" inputmode="tel" placeholder="+98912..." autocomplete="off" dir="ltr" aria-describedby="err_phone">
      <p class="err" id="err_phone" aria-live="polite"></p>
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
      <label for="password">رمز دو مرحله‌ای</label>
      <input id="password" type="password" autocomplete="off" aria-describedby="err_password">
      <p class="err" id="err_password" aria-live="polite"></p>
      <!-- فیکس WB-07: راهنمای فقط-خواندنی وقتی Secret TG_2FA_PASSWORD موجود است -->
      <p class="muted" id="pwdHint" hidden>رمز از Secret TG_2FA_PASSWORD خوانده می‌شود؛ خالی بگذارید تا از Secret استفاده شود</p>
      <button class="btn full" type="submit">ورود</button>
    </form>
    <button class="btn ghost full" id="resendBtn">ارسال دوباره‌ی کد</button>
  </div>
</section>

<section data-view="main" hidden>
  <div class="row">
    <p class="muted" id="who"></p>
    <button class="btn ghost" id="refreshBtn">بازخوانی</button>
  </div>
  <div class="tabs" role="tablist" aria-label="دسته‌ی چت‌ها">
    <button class="tab" role="tab" data-cat="all" aria-selected="true">همه</button>
    <button class="tab" role="tab" data-cat="channel" aria-selected="false">کانال</button>
    <button class="tab" role="tab" data-cat="group" aria-selected="false">گروه</button>
    <button class="tab" role="tab" data-cat="private" aria-selected="false">شخصی</button>
    <button class="tab" role="tab" data-cat="bot" aria-selected="false">ربات</button>
  </div>
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
    <h2 id="progChat"></h2>
    <div class="progress" role="progressbar" id="barWrap" aria-label="پیشرفت" aria-valuemin="0" aria-valuemax="100">
      <div id="bar"></div>
    </div>
    <p class="num" id="progTxt" dir="ltr"></p>
    <div class="grid">
      <div class="stat"><p class="muted">پیام</p><b id="stDone" class="num">۰</b></div>
      <div class="stat"><p class="muted">از کل</p><b id="stTotal" class="num">؟</b></div>
      <div class="stat"><p class="muted">فایل</p><b id="stFiles" class="num">۰</b></div>
      <div class="stat"><p class="muted">خطا</p><b id="stErr" class="num">۰</b></div>
    </div>
    <p class="muted" id="phaseNote"></p>
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
  <div class="card">
    <h2>حساب</h2>
    <p id="accWho"></p>
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

function setErr(id, msg) {
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
    (dead ? " اتصال قبلی قطع شده بود؛ دوباره وارد شوید." : "");
  $("#credFields").hidden = !!(sec.api_id && sec.api_hash);
  $("#encWrap").hidden = !!sec.enc_key;
  show("setup");
}

function showLogin(phase) {
  clearErrs();
  const two = phase === "need_2fa";
  $("#loginTitle").textContent = two ? "رمز دو مرحله‌ای" : "کد تلگرام";
  $("#codeForm").hidden = two;
  $("#twoFaForm").hidden = !two;
  $("#pwdHint").hidden = !(two && SECRETS.pwd);   // فیکس WB-07
  $("#resendBtn").hidden = two;
  show("login");
  (two ? $("#password") : $("#code")).focus();
}

function showMain() {
  api("/api/state").then(r => {
    $("#who").textContent = (r.data && r.data.who) ? "حساب: " + r.data.who : "";
    if (r.data && r.data.has_job) { showProgress(); return; }
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
        b.innerHTML = "<b>" + escapeHtml(it.name) + "</b>" +
          "<span class='badge num' dir='ltr'>" + escapeHtml(String(it.id)) + "</span>";
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
function showProgress() {
  show("progress");
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, 1500);
  poll();
}

function poll() {
  api("/api/progress" + (lastSeq ? "?since=" + lastSeq : "")).then(r => {
    const d = r.data || {};
    const prog = d.prog || {};
    (d.msgs || []).forEach(m => toast(m.text || m, "warn"));
    if (typeof d.last_seq === "number") lastSeq = Math.max(lastSeq, d.last_seq);
    const job = d.job;
    if (job) {
      $("#progChat").textContent = job.chat;
      const pct = Math.max(0, Math.min(100, prog.pct || 0));
      $("#bar").style.width = pct + "%";
      $("#barWrap").setAttribute("aria-valuenow", String(pct));
      $("#progTxt").textContent = prog.txt || "";
      $("#stDone").textContent = fa(prog.done || 0);
      $("#stTotal").textContent = prog.total ? fa(prog.total) : "؟";
      $("#stFiles").textContent = fa(prog.files || 0);
      $("#stErr").textContent = fa(prog.errors || 0);
      $("#phaseNote").textContent = (prog.phase === "zip") ? "در حال ساخت زیپ…" : "";
      if (job.done) {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        showResult(job.result);
      }
    } else if (d.result) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      showResult(d.result);
    }
    if (d.phase === "need_login" && (!job || job.done)) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      showSetup({}, true);
    }
  }).catch(() => {});
}

function showResult(res) {
  if (!res) { showMain(); return; }
  const titles = { stop: "اسکرپ متوقف شد — فایل‌های دانلودشده می‌مانند.",
                   disk: "فضای دیسک کم شد؛ متوقف شد.",
                   empty: "چت خالی است." };
  $("#resTitle").textContent = res.empty ? titles.empty :
    (res.interrupted ? (titles[res.reason] || "خطا در اجرا.") : "✅ اسکرپ تمام شد");
  $("#resStats").innerHTML =
    stat("پیام", res.msgs) + stat("فایل", res.files) +
    stat("ردشده", res.skipped) + stat("خطا", res.errors);
  $("#resFolder").textContent = res.folder || "";
  const zl = $("#zipLink");
  if (res.zip_name) {
    zl.hidden = false;
    zl.href = "/api/zip?name=" + encodeURIComponent(res.zip_name) +
              "&t=" + encodeURIComponent(TOKEN);   // فیکس WB-09: token در لینک دانلود
    zl.setAttribute("download", res.zip_name);
  } else { zl.hidden = true; }
  show("result");
}

function stat(label, v) {
  return "<div class='stat'><p class='muted'>" + label + "</p><b class='num'>" +
         fa(v || 0) + "</b></div>";
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

$("#stopBtn").addEventListener("click", () => {
  api("/api/stop").then(r => {
    if (r.data && r.data.ok) { $("#stopBtn").disabled = true; $("#phaseNote").textContent = "در حال توقف…"; }
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
    $("#accWho").textContent = t.who ? "حساب: " + t.who : "";
    $("#accBackup").textContent = t.bk_active
      ? (t.restored ? "بکاپ سشن روی Drive: فعال — این سشن از بکاپ بازیابی شده بود"
                    : "بکاپ سشن روی Drive: فعال")
      : "بکاپ سشن: غیرفعال (Drive یا کلید رمز TS_ENC_KEY در دسترس نیست)";
    show("settings");
  }).catch(() => toast("ارتباط با سرور برقرار نشد", "err"));
}
$("#settingsBtn").addEventListener("click", showSettings);
$("#setBack").addEventListener("click", () => showMain());
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

$("#logoutBtn").addEventListener("click", () => {
  $("#confirmMsg").textContent = "خروج کامل: سشن محلی و بکاپ Drive هر دو حذف می‌شوند. مطمئنید؟";
  const dlg = $("#confirmDlg");
  if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
});
$("#confirmNo").addEventListener("click", () => $("#confirmDlg").close());
$("#confirmYes").addEventListener("click", () => {
  $("#confirmDlg").close();
  api("/api/logout").then(r => {
    if (r.data && r.data.msg) toast(r.data.msg, "ok");
    boot();
  });
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
    """تست دودِ هلپرهای خالصِ لایه‌ی وب — python telescrape_web.py --selftest
    پوشش: ارقام فارسی، ولیدیشن creds، گارد زیپ، چک token، گذارهای ماشین حالت لاگین با کلاینت فیک."""
    import tempfile

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
