# Handoff_Project — TeleScrape

> مرجع قوانین: [Instructions for Ai.md](Instructions%20for%20Ai.md) — قبل از هر تغییر بخوانید.
> آخرین به‌روزرسانی: 2026-10-24 (ساعت دقیق: به `Update.md` مراجعه کنید). این سند بر پایه‌ی خواندن کامل `telescrape_v3.py` و `README.md` نوشته شده است.

## 1. Project purpose and current status

- **هدف:** خروجی‌گرفتن (backup) از کانال/گروه/چت شخصی تلگرام با Telethon؛ متن + مدیا + metadata + زیپ اختیاری؛ مناسب Colab (موبایل) و ترمینال.
- **وضعیت فعلی (تأییدشده):** برنامه‌ی کامل و کارکردی در یک فایل (`telescrape_v3.py`) وجود دارد. کار این نشست **فقط مستندسازی** بود؛ هیچ تغییری در کد برنامه داده نشد.
- **اجرای واقعی برنامه در این نشست انجام نشد** (نیاز به حساب تلگرام و API credentials؛ بدون مجوز کاربر انجام نمی‌شود). رفتار زمان اجرا فقط از خواندن کد استنباط شده است. تنها راستی‌آزمایی انجام‌شده: پارس سینتکس (`ast.parse`) موفق بود.

## 2. Repository locations and branches

| مورد | مقدار |
|---|---|
| ریپازیتوری | https://github.com/Arsalani2/telescrape (عمومی؛ مجوز push برای این نشست وجود داشت) |
| شاخه‌ی اصلی | `main` — آخرین کامیت پایه‌ی بررسی‌شده `6cf416c6004c03767183617abcc9679407f7dc0c` («Remove limitations section from README»، 2026-10-23) |
| شاخه‌ی مستندات | `docs/ai-documentation` (ساخته‌شده در این نشست) — وضعیت commit/push را در `Update.md` ببینید |

## 3. Architecture and critical components

جزئیات: [Coding Architecture.md](Coding%20Architecture.md)، [Workflow.md](Workflow.md)، [Mindmap.md](Mindmap.md).
خلاصه: اسکریپت sync (UI در thread اصلی) + loop asyncio پس‌زمینه برای Telethon؛ پل: `run()`. قلب برنامه: `scrape()` + `download()` + `Throttle` + `tg_call()`.

## 4. Development environment and prerequisites

- Python 3 (در نشست تحلیل: 3.13.17 فقط برای parse). کتابخانه: `telethon>=1.40` (`pip install -r requirements.txt`).
- حساب تلگرام و `API ID`/`API Hash` از https://my.telegram.org.
- متغیرهای محیطی یا Colab Secrets: `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, اختیاری `TG_2FA_PASSWORD`.

## 5. Install, configure, build, test

```bash
git clone https://github.com/Arsalani2/telescrape.git
cd telescrape
pip install -r requirements.txt
export TG_API_ID=...     # مقادیر واقعی را هرگز commit نکنید
export TG_API_HASH=...
export TG_PHONE=+98...
python telescrape_v3.py
```

- پارامترهای فرم در ترمینال با ویرایش مستقیم خطوط ۲۱–۲۷ فایل تغییر می‌کنند.
- **Build:** ندارد. **Test/Lint/CI:** ندارد (Not applicable تا زمانی که اضافه شود).
- راستی‌آزمایی سبک: `python3 -I -c "import ast; ast.parse(open('telescrape_v3.py', encoding='utf-8').read())"`.

## 6. Current implementation state (تأییدشده با کد)

- ✅ ورود (کد + 2FA)، دسته‌بندی چت‌ها، جست‌وجو/صفحه‌بندی، ورود دستی (id/@username/لینک t.me؛ لینک دعوت پشتیبانی نمی‌شود).
- ✅ خروجی کامل از اولین پیام، مدیا با `.part`، retry، ضد-گیرکردن (۱۲۰ ثانیه)، بررسی فضای دیسک.
- ✅ خنک‌سازی ۳ پروفایل + افزایش ضریب بعد از FloodWait.
- ✅ زیپ، دانلود زیپ در مرورگر (Colab)، ذخیره روی Drive (اختیاری)، خروج از حساب + حذف session.
- ✅ رابط دو‌زبانه `fa`/`en`، پذیرش ارقام فارسی/عربی.

## 7. Recently completed tasks

- 2026-10-24: تحلیل کامل ریپازیتوری و ایجاد هفت فایل مستندات در `docs/` به‌علاوه‌ی `PROJECT_HANDOFF.txt` (جزئیات: `Update.md`، Update #1).

## 8. Relevant commits and changes

- پایه: `6cf416c` روی `main`.
- کامیت مستندات: به `Update.md` (Update #2) مراجعه کنید؛ hash فقط پس از اجرای واقعی commit ثبت می‌شود.

## 9. Known bugs, blockers, risks, technical debt

| # | مورد | نوع | منبع |
|---|---|---|---|
| 1 | README می‌گوید فایل جانبی «`.info`» است؛ کد `<path>.info.txt` می‌سازد | ناهمخوانی مستندات | `scrape()`/`handle()` |
| 2 | `app.speed` (سطح خنک‌سازی) ذخیره نمی‌شود | محدودیت | `App.DEFAULTS`/`settings_screen` |
| 3 | `_ensure("telethon")` نسخه را اعمال نمی‌کند | محدودیت | `_ensure` |
| 4 | `metadata.txt`/`all_texts.md` هر اجرا بازنویسی می‌شوند | رفتار/بدهی | `scrape()` |
| 5 | هیچ تست/CI/linter ندارد | بدهی فنی | فهرست فایل‌ها |
| 6 | `RETRY_ERRORS` شامل `OSError` (retry خطاهای محلی هم) | مشاهده | `RETRY_ERRORS` |
| 7 | README مسیر پیش‌فرض Colab را `/content/telescrape_data` می‌گوید؛ کد از `os.path.abspath("telescrape_data")` (نسبت به cwd) استفاده می‌کند — تساوی این دو فقط وقتی cwd برابر `/content` باشد | Not verified | `App.__init__` |
| 8 | رفتار `flood_sleep_threshold=60` در Telethon در این نشست اجرا نشد | Not verified | `make_client` |

هیچ باگ قطعی (reproduced) در این نشست تأیید **نشد**؛ موارد بالا مشاهده‌ی ایستا هستند.

## 10. Outstanding work and prioritized next steps

1. **(کاربر)** بازبینی و merge شاخه‌ی `docs/ai-documentation` به `main` (یا اعلام اینکه مستقیم روی `main` قرار بگیرد). *Acceptance:* فایل‌های `docs/*.md` و `PROJECT_HANDOFF.txt` در `main` دیده شوند.
2. رفع ناهمخوانی README (`.info` ↔ `.info.txt`) — یا اصلاح README یا تغییر کد؛ تصمیم با کاربر. *Files:* `README.md` یا `telescrape_v3.py`.
3. افزودن تست‌های واحد برای توابع خالص (`sanitize`, `safe_ext`, `norm_phone`, `classify`, `Throttle`). *Dependencies:* pytest (جدید؛ نیازمند تأیید).
4. بررسی/پین نسخه‌ی Telethon در `_ensure` یا حذف آن به نفع `requirements.txt`.
5. تصمیم درباره‌ی ذخیره‌ی `speed` در `settings.json`.

## 11. Important file paths and entry points

- `telescrape_v3.py` — `main()` (خط ~1344) → `app_main()` (خط ~1274)؛ `scrape()` (خط ~917)؛ `download()` (خط ~856)؛ `Throttle` (خط ~390). (شماره‌خط‌ها تقریبی و مربوط به `6cf416c`.)
- `README.md`، `requirements.txt`، `.gitignore`.

## 12. External service requirements and integration notes

- Telegram API (از طریق Telethon)؛ اختیاری: Google Colab (`google.colab.userdata`, `drive`, `files`) و Google Drive.

## 13. Documentation references

[Instructions for Ai.md](Instructions%20for%20Ai.md) · [Update.md](Update.md) · [Workflow.md](Workflow.md) · [Mindmap.md](Mindmap.md) · [Coding Architecture.md](Coding%20Architecture.md) · [File Tree Diagram.md](File%20Tree%20Diagram.md) · `../PROJECT_HANDOFF.txt`

## 14. Unverified assumptions and missing access permissions

- برنامه روی حساب واقعی اجرا نشد؛ تمام رفتار زمان اجرا از کد خوانده شده.
- Mermaid diagram‌ها render نشدند (فقط نوشته شده‌اند): Not verified.
- تاریخچه‌ی کامل git بررسی نشد (کلون سطحی).
- ارتباط با دیگر ریپازیتوری‌های کاربر: Unknown (بررسی نشدند).
- منطقه‌ی زمانی کاربر در دسترس نبود؛ زمان‌ها به UTC (ساعت کانتینر) ثبت شده‌اند.
- هیچ secret/credential در این مستندات یا ریپازیتوری وارد نشده است.
