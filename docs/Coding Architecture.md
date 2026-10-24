# Coding Architecture — TeleScrape

> مرجع قوانین: [Instructions for Ai.md](Instructions%20for%20Ai.md)
> بخش‌ها به «تأییدشده با کد»، «پیشنهادی» و «سؤال‌های باز» تفکیک شده‌اند. هیچ بازآرایی پیشنهادی در این سند **پیاده‌سازی‌شده** فرض نشده است.

## 1. Verified implementation

### 1.1 Architectural style and design principles
- **اسکریپت تک‌فایل** (`telescrape_v3.py`) بدون پکیج/ماژول جداگانه؛ برای چسباندن در یک سلول Colab طراحی شده (README).
- **لایه‌بندی منطقی درون یک فایل** (به ترتیب از بالا به پایین): پیکربندی و ثابت‌ها ← متن‌ها (i18n) ← ابزار UI ← پل همزمانی ← خنک‌سازی ← ابزار فایل ← اعتبارنامه ← حالت برنامه ← چت‌ها ← خروجی‌گیری ← منوها ← ورود/`main`.
- **اصول مشاهده‌شده:** هر فراخوانی شبکه timeout/retry دارد؛ کار شبکه در thread جدا از UI؛ توقف تمیز (cooperative cancellation با `STOP`)؛ نوشتن امن فایل (`.part` → `os.replace`، `.tmp` → `os.replace` برای JSON/zip)؛ پاک‌سازی داده‌ی راه دور (`sanitize`, `safe_ext`).

### 1.2 Layer and module responsibilities

| لایه | نماد/توابع | مسئولیت |
|---|---|---|
| پیکربندی | متغیرهای `#@param`, `PROFILES`, `SECRET_NAMES`, ثابت‌ها | پارامترهای ثابت زمان اجرا |
| i18n | `S`, `t()` | متن‌های `fa`/`en` (در `t`، اگر کلید در زبان انتخابی نبود به `en` برمی‌گردد) |
| UI | `say`, `ask`, `clear`, `hr`, `short`, `status`, `notify`, `_ui_tick`, `_ui_end` | ورودی/خروجی، پهنای محدود، ارقام فارسی |
| همزمانی | `_bg_loop`, `run`, `nap`, `STOP` | اجرای coroutine روی loop پس‌زمینه از کد sync |
| خنک‌سازی | `Throttle`, `tg_call`, `RETRY_ERRORS` | pacing، backoff، FloodWait |
| فایل | `sanitize`, `save_json`, `load_json`, `make_zip`, ... | عملیات فایل امن |
| اعتبارنامه | `get_secret`, `get_creds`, `norm_phone` | Secrets/env/ورودی دستی |
| حالت | `App` | مسیرها، `settings.json`، session |
| چت‌ها | `load_dialogs`, `pick_from_list`, `resolve`, `export_list` | فهرست و انتخاب چت |
| خروجی‌گیری | `scrape`, `download`, `classify`, `build_link` | قلب برنامه |
| منو | `start_chat`, `settings_screen`, `app_main`, `login` | جریان تعاملی |

### 1.3 Component boundaries and dependency direction
- جهت وابستگی: منوها (`app_main`, `start_chat`) ← `run()` ← کد async (`scrape`, `download`, `tg_call`) ← Telethon.
- **قرارداد UI/Worker:** کد async فقط `status()`/`notify()` را فراخوانی می‌کند؛ هیچ `print`/`input` در کد async نیست (تأییدشده در `scrape`/`download`/`tg_call`).
- **قرارداد خروجی `scrape`:** دیکشنری با کلیدهای `folder`, `zip`, `interrupted`, `reason`, `empty`, `msgs`, `files`, `skipped`, `errors` (و `rests` پس از اتمام عادی/قطع).

### 1.4 Public interfaces and contracts
- رابط خارجی فقط CLI/Colab تعاملی است؛ هیچ API برنامه‌نویسی پایدار، بسته یا ماژول قابل import مستندسازی نشده (و با side-effect مثل `_ensure("telethon")` هنگام import اجرا می‌شود).
- متغیرهای محیطی/Secrets: `TG_API_ID`/`API_ID`, `TG_API_HASH`/`API_HASH`, `TG_PHONE`/`PHONE`, `TG_2FA_PASSWORD`.

### 1.5 Data models, schemas, persistence
- **item (چت):** دیکشنری `{cat, id, name, username, entity}` (`make_item`)؛ `cat ∈ {channel, group, private, bot, other}`.
- **settings:** `settings.json` با `{media: bool, max_mb: int, zip: bool, sidecar: bool}`؛ فقط مقدارهایی که نوعشان با پیش‌فرض یکی است بارگذاری می‌شوند.
- **ذخیره‌سازی روی دیسک** (`App`): `base` = `./telescrape_data` (یا `/content/drive/MyDrive/TeleScrape` با `USE_DRIVE` در Colab)، شامل `exports/`, `zips/`, `settings.json`, `dialogs_list.txt`؛ پوشه‌ی `session/` در `local` می‌ماند مگر `USE_DRIVE and SESSION_ON_DRIVE`.
- خروجی هر چت: `exports/<sanitize(name)>_<id>/` با `<id:06d>_Text.txt`, `<id:06d>_<Kind><ext>`, `[.info.txt]`, `metadata.txt`, `all_texts.md`, `LOG.txt`. بانک اطلاعاتی ندارد.

### 1.6 State management and data flow
- حالت سراسری ماژول: `STOP`, `_ui`, `_msgs` (deque), `PROG`, `_BG`, و پارامترهای فرم. حالت برنامه در نمونه‌ی `App`. آمار هر اجرا در دیکشنری محلی `stats` داخل `scrape`.
- پیشرفت: `PROG["txt"]` برای نمایش در countdown `nap`.

### 1.7 Error handling and observability
- سلسله‌مراتب: خطای پیام ← شمارش و ثبت در `LOG.txt`؛ `Stopped`/`DiskLow` ← قطع تمیز؛ خطای مرگبار ← `reason` در نتیجه.
- مشاهده‌پذیری: فقط خط وضعیت، `notify` و `LOG.txt`. ماژول `logging` استفاده نشده.

### 1.8 Authentication, authorization, security boundaries
- ورود با شماره + کد (+۲FA) از طریق Telethon؛ session در `session/tg_<api_id>.session` با `chmod 600` (پوشه `700`).
- `api_hash` با `getpass` (نامرئی) گرفته می‌شود و روی دیسک ذخیره نمی‌شود؛ فایل قدیمی `tg_api_credentials.json` (v2) در صورت وجود حذف می‌شود (`cleanup_legacy`، فقط وقتی credentials از Secrets/env بیایند).
- مرز اعتماد: متادیتای پیام/فایل از تلگرام نامطمئن است: نام فایل از `mid` و `kind` ساخته می‌شود و پسوند با `safe_ext` بررسی می‌شود؛ `sanitize` نام چت را برای مسیر پاک می‌کند؛ `make_zip` لینک‌های نمادین را نادیده می‌گیرد.

### 1.9 Performance, scalability, caching
- دانلود **ترتیبی** (یک فایل در هر لحظه)، با تأخیر عمدی. کش نام نویسنده در دیکشنری `cache` (به‌ازای `sender_id`).
- Zip: `ZIP_STORED` برای غیرمتنی، `ZIP_DEFLATED` برای `.txt/.md/.json`، با `allowZip64`.
- ملاحظه: `get_messages` تا ۱۰۰ پیام در هر دسته؛ حجم کل محدود به دیسک است (بررسی فضا قبل از هر دانلود).

### 1.10 Dependency management
- `requirements.txt`: `telethon>=1.40`. بدون lockfile. `_ensure` در زمان اجرا بدون پین `pip install telethon` می‌زند.
- ایمپورت‌های اختیاری: `google.colab` (`userdata`, `drive`, `files`) و `IPython.display.clear_output` فقط در Colab/با try.

### 1.11 Design patterns actually used
- Event-loop-in-thread + Future bridge (`run`)، cooperative cancellation (`STOP`), retry با exponential backoff + jitter، adaptive throttling (`Throttle`)، atomic write (temp + replace)، producer/consumer ساده برای پیام‌های UI (`_msgs`).

### 1.12 Testing strategy and quality requirements
- **هیچ‌کدام در ریپازیتوری موجود نیست** (بدون pytest/CI/linter/type hints سراسری). نگاه کنید به «Known limitations».

### 1.13 Build and deployment architecture
- ندارد؛ اجرای مستقیم. ریلیز: Not documented.

### 1.14 Important technical decisions and rationale (از docstring/کامنت‌های کد)
- loop پس‌زمینه به‌جای `nest_asyncio` تا حلقه‌ی تو در تو ایجاد نشود.
- `clear_output(wait=False)` عمداً؛ `wait=True` قبل از `input()` ممکن است کادر ورودی Colab را پنهان کند (کامنت در `clear()`).
- thread‌های کارگر `print` نمی‌کنند چون front-end نوت‌بوک خروجی thread خارجی را ناپایدار نسبت می‌دهد (کامنت قبل از `status()`).
- `disconnect()` باید از thread خود loop صدا شود (`_disconnect`).
- session پیش‌فرض خارج از Drive نگه داشته می‌شود (کامنت در `App.__init__`).

## 2. Known architectural limitations and technical debt (مشاهده‌شده؛ بدون تغییر در کد)

1. تک‌فایل ۱۳۵۵ خطی با ترکیب UI، شبکه و ذخیره‌سازی؛ بدون تست.
2. `_ensure("telethon")` نسخه را اعمال نمی‌کند (`>=1.40` فقط در `requirements.txt`).
3. `app.speed` ذخیره نمی‌شود؛ هر اجرا از `SPEED` شروع می‌شود.
4. `metadata.txt` و `all_texts.md` هر اجرا از نو نوشته می‌شوند (تاریخچه‌ی اجراهای قبل در آن‌ها حفظ نمی‌شود؛ فقط `LOG.txt` append است).
5. دانلود دوباره‌ی نیمه‌کاره: فقط با وجود فایل نهایی غیرخالی رد می‌شود؛ resume درون فایل وجود ندارد (`.part` پاک می‌شود).
6. `RETRY_ERRORS` شامل `OSError` است که علاوه بر خطای شبکه، خطاهای محلی فایل‌سیستم را هم retry می‌کند (رفتار مشاهده‌شده؛ اثر عملی آزمایش نشده).
7. فایل‌های خروجی رمزگذاری نمی‌شوند؛ حریم خصوصی وابسته به محیط اجرا است.

## 3. Proposed improvements (**پیشنهاد؛ پیاده‌سازی نشده**)

- افزودن تست واحد برای توابع خالص (`sanitize`, `safe_ext`, `norm_phone`, `classify`, `Throttle`, `resolve` با mock).
- پین‌کردن/بررسی نسخه‌ی Telethon در `_ensure`.
- افزودن `docs/` به فرآیند PR و یک CI سبک (مثل `python -m py_compile`).

## 4. Unresolved architectural questions

- آیا هدف بلندمدت حفظ تک‌فایل برای Colab است یا تبدیل به پکیج؟ (تصمیم کاربر؛ Unknown)
- رفتار واقعی `flood_sleep_threshold = 60` در نسخه‌ی نصب‌شده‌ی Telethon بررسی نشده است.
- ارتباط این پروژه با ریپازیتوری‌های دیگر کاربر (در صورت وجود): Unknown.
