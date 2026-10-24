# File Tree Diagram — TeleScrape

> مرجع قوانین: [Instructions for Ai.md](Instructions%20for%20Ai.md)
> منبع: فهرست `git ls-files` از کلون شاخه‌ی `main` (کامیت `6cf416c6004c03767183617abcc9679407f7dc0c`) + فایل‌های مستندات افزوده‌شده در شاخه‌ی `docs/ai-documentation`. ریپازیتوری از طریق کلون سطحی (`--depth 1`) بررسی شد؛ تاریخچه‌ی کامل کامیت‌ها بررسی نشده است.

```
telescrape/
├── .gitignore                 # secret/داده‌ی حساس و فایل‌های موقت را از git خارج می‌کند
├── LICENSE                    # MIT (Copyright (c) 2026 Arsalan)
├── README.md                  # راهنمای فارسی (RTL): نصب، پارامترها، ساختار خروجی، امنیت، عیب‌یابی
├── requirements.txt           # telethon>=1.40
├── telescrape_v3.py           # تمام برنامه (۱۳۵۵ خط)؛ نقطه‌ی ورود: main() → app_main()
├── PROJECT_HANDOFF.txt        # خلاصه‌ی تحویل (فارسی، متن ساده)؛ ارجاع به docs/Handoff_Project.md
└── docs/
    ├── Instructions for Ai.md # قوانین دائمی برای عامل‌های AI
    ├── Update.md              # دفتر فقط-افزودنی تغییرات
    ├── Workflow.md            # جریان اجرایی
    ├── Mindmap.md             # نقشه‌ی ذهنی
    ├── Coding Architecture.md # معماری و بدهی فنی
    ├── File Tree Diagram.md   # همین فایل
    └── Handoff_Project.md     # تحویل کامل پروژه
```

## نقاط مهم

| مسیر | نقش |
|---|---|
| `telescrape_v3.py` | تنها فایل کد؛ پارامترهای فرم Colab در خطوط ۲۱–۲۷ |
| `requirements.txt` | تنها فهرست وابستگی |
| `.gitignore` | شامل `telescrape_data/`, `*.session`, `*.session-journal`, `tg_api_credentials.json`, `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `venv/`, `*.part`, `*.tmp` |

## فایل‌هایی که زمان اجرا (خارج از git) ساخته می‌شوند

(از کد `App` و `scrape` استخراج شده؛ در ریپازیتوری وجود ندارند و به‌خاطر `.gitignore` نباید commit شوند)

```
telescrape_data/                     # (یا /content/drive/MyDrive/TeleScrape با USE_DRIVE در Colab)
├── exports/<نام‌چت>_<آیدی>/
│   ├── <id:06d>_Text.txt
│   ├── <id:06d>_<Kind><ext>
│   ├── <id:06d>_<Kind><ext>.info.txt   # فقط اگر «فایل info» روشن باشد
│   ├── metadata.txt
│   ├── all_texts.md
│   └── LOG.txt
├── zips/<نام‌چت>_<آیدی>_<firstId>_<lastId>.zip
├── session/tg_<api_id>.session          # (همواره در پوشه‌ی محلی مگر USE_DRIVE و SESSION_ON_DRIVE)
├── settings.json
└── dialogs_list.txt                     # فقط با گزینه‌ی «ذخیره‌ی لیست»
```

> یادداشت: README نام فایل جانبی را «`.info`» می‌نویسد، اما کد `<path>.info.txt` می‌سازد (ثبت‌شده در `Handoff_Project.md`).
