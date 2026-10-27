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

# ────────────────────── پیام‌های دوزبانه‌ی لایه‌ی وب (قاعده ۴) ──────────────────────
W = {
    "fa": {
        "bad_api_id": "API ID نامعتبر است.",
        "bad_hash": "API Hash باید ۳۲ کاراکتر hex باشد.",
        "need_phone": "شماره لازم است (مثل +98912...).",
        "pwd_mismatch": "رمزهای بکاپ یکی نیستند.",
        "login_err": "ورود ناموفق: {e}",
        "internal": "خطای داخلی: {e}",
        "wrong_phase": "این گام الان معتبر نیست.",
        "invalid_code": "کد نامعتبر است.",
        "code_expired_resent": "کد منقضی شده بود؛ کد جدید ارسال شد.",
        "resent": "کد جدید ارسال شد.",
        "need_password": "رمز دو مرحله‌ای لازم است.",
        "wrong_password": "رمز اشتباه است.",
        "not_ready": "اول وارد حساب شوید.",
        "client_dead": "اتصال تلگرام قطع شد؛ دوباره وارد شوید.",
        "load_failed": "دریافت لیست ناموفق بود؛ دوباره تلاش کنید.",
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
        "login_err": "Login failed: {e}",
        "internal": "Internal error: {e}",
        "wrong_phase": "This step is not valid right now.",
        "invalid_code": "Invalid code.",
        "code_expired_resent": "The code had expired; a new one was sent.",
        "resent": "New code sent.",
        "need_password": "The 2FA password is required.",
        "wrong_password": "Wrong password.",
        "not_ready": "Log in first.",
        "client_dead": "Telegram connection lost; log in again.",
        "load_failed": "Could not load the list; try again.",
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
    path = os.path.realpath(os.path.join(zdir, name))
    return path if os.path.dirname(path) == zdir else None


def _check_token():
    # قابلیت WEB-06: نسخه‌ی سخت‌گیرانه در پایین همین فایل جایگزین این تعریف می‌شود
    return True


def _bad_token():
    if _check_token():
        return None
    return jsonify({"error": wt("csrf_bad")}), 403


# ────────────────────── صفحه‌ی تک‌فایلی (قابلیت WEB-05 فرانت کامل را جایگزین می‌کند) ──────────────────────
PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>اسکرپر تلگرام</title>
</head>
<body style="font-family:system-ui; padding:24px; text-align:center">
<p style="font-size:18px">TeleScrape Web — اسکلت آماده است (WEB-01).</p>
<p>فرانت کامل در WEB-05 اضافه می‌شود؛ API فعلی: <code>/api/state</code></p>
</body>
</html>"""


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
    if not res:
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


@web.route("/api/progress")
def api_progress():
    """پیشرفت زنده: PROG (فیلدهای ساختاریافته WEB-02) + درینِ _msgs + نتیجه‌ی job."""
    msgs = []
    while True:
        try:
            msgs.append(ts._msgs.popleft())
        except IndexError:
            break
    with LOCK:
        return jsonify({
            "phase": STATE["phase"],
            "prog": dict(ts.PROG),
            "msgs": msgs,
            "job": _job_payload(),
            "result": _res_payload(STATE["last_result"]),
        })


@web.route("/api/zip")
def api_zip():
    """دانلود زیپ — فقط فایل داخل app.zips با گارد realpath (قابلیت WEB-04)."""
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
    cfg = app.settings
    if "media" in data:
        cfg["media"] = bool(data["media"])
    if "zip" in data:
        cfg["zip"] = bool(data["zip"])
    if "sidecar" in data:
        cfg["sidecar"] = bool(data["sidecar"])
    if "max_mb" in data:
        try:
            cfg["max_mb"] = max(0, int(str(data["max_mb"]).translate(ts._DIGITS)))
        except (TypeError, ValueError):
            return jsonify({"error": wt("invalid_input"), "field": "max_mb"}), 400
    if "speed" in data:
        if data["speed"] not in ts.PROFILES:
            return jsonify({"error": wt("invalid_input"), "field": "speed"}), 400
        app.speed = data["speed"]
    app.save_settings()
    return jsonify({"ok": True, "settings": cfg, "speed": app.speed})


@web.route("/api/logout", methods=["POST"])
def api_logout():
    """خروج کامل: نتیجه‌ی log_out چک می‌شود (NB-11) + حذف بکاپ Drive (NB-13)."""
    bad = _bad_token()
    if bad:
        return bad
    if STATE["client"] is None:
        return jsonify({"error": wt("not_ready"), "phase": STATE["phase"]}), 409
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


if __name__ == "__main__":
    main()
