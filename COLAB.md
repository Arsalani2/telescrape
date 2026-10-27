<div dir="rtl">

# 🚀 اجرای TeleScrape در Colab

فقط یک فایل: **telescrape.py** (موتور + رابط وب). چهار سلول زیر را به ترتیب اجرا کنید.

## ۰) سکرت‌ها (فقط بار اول)
در Colab آیکون 🔑 **Secrets** (ستون چپ) → نام‌های زیر را بسازید، مقدار بدهید و
**Notebook access** هرکدام را روشن کنید:

| نام | مقدار |
|---|---|
| TG_API_ID | api id از my.telegram.org |
| TG_API_HASH | api hash از my.telegram.org |
| TG_PHONE | شماره، مثل +98912… |
| TG_2FA_PASSWORD | رمز دو مرحله‌ای (اگر دارید؛ اختیاری) |
| TS_ENC_KEY | رمز بکاپ سشن (اختیاری) |

> ⚠️ سکرت‌ها فقط در پروسه‌ی کرنلِ نوت‌بوک قابل خواندن‌اند؛ به همین دلیل
> سلول لانچر (۲) آنها را می‌خواند و به سرور پاس می‌دهد — وارد کردن دستی لازم نیست.

## ۱) دانلود + تست خودکار
```
!wget -q https://raw.githubusercontent.com/Personalmo/telescrape/main/telescrape.py
!python telescrape.py --selftest
```

## ۲) لانچر سرور (سکرت‌ها را پاس می‌دهد)
> TG_PHONE **اختیاری** است: اگر نسازید فقط شماره را دستی در فرم وارد می‌کنید؛
> بقیه‌ی سکرت‌ها خودکار خوانده می‌شوند (پر باشد → خودش می‌رود؛ خالی باشد → دستی).
```python
import os, subprocess, time
from google.colab import userdata

def _sec(name):
    try:
        v = userdata.get(name)
        return str(v) if v else None
    except Exception:
        return None

# فیکس NB-20: پروسه‌ی قدیمی (بدون سکرتِ تازه) اول کشته شود تا پورت آزاد شود
subprocess.run(["pkill", "-f", "telescrape.py"], capture_output=True)
time.sleep(2)

env = {**os.environ, "TS_WEB_CHILD": "1"}
found = []
for name in ("TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_2FA_PASSWORD", "TS_ENC_KEY"):
    v = _sec(name)
    if v:
        env[name] = v
        found.append(name)

with open("web.log", "w") as log:
    subprocess.Popen(["python", "telescrape.py", "--port", "8000"], env=env,
                     stdout=log, stderr=log)
print("سکرت‌های پاس‌شده:", found or "هیچ — مقادیر را دستی در فرم وارد می‌کنید")
time.sleep(8)
print(open("web.log").read())   # باید بنویسد: سکرت‌های تشخیص‌داده‌شده: [...]
```

## ۳) باز کردن روی گوشی
```python
from google.colab import output
url = output.eval_js("google.colab.kernel.proxyPort(8000)")
print(url)   # همین لینک را روی گوشی باز کنید
```
> تابع `serve_kernel_port_as_window` منسوخ شده و هشدار امنیتی مرورگر می‌دهد؛ از `proxyPort` استفاده کنید.

## Drive (اختیاری — ماندگاری خروجی و بکاپ سشن)
```python
from google.colab import drive
drive.mount('/content/drive')
```
قبل از لانچر اجرا کنید تا خروجی روی Drive بماند.

## ری‌استارت سرور
سلول لانچر (۲) خودش ابتدای کار پروسه‌ی قدیمی را می‌کشد؛ فقط دوباره اجرایش کنید
(و بعد سلول ۳ برای لینک تازه). اگر دستی لازم شد:
```
!pkill -f telescrape.py
```

## عیب‌یابی
- **صفحه باز نمی‌شود**: خروجی سلول ۲ را چک کنید — در web.log باید
  `TeleScrape Web listening on 0.0.0.0:8000` باشد.
- **سکرت خوانده نمی‌شود**: Notebook access آن سکرت روشن است؟ → ری‌استارت سرور.
- **لیست چت‌ها نمی‌آید**: آخر web.log را بفرستید:
  `print(open('web.log').read()[-2000:])`
- بعد از هر بروزرسانی ریپو: سلول ۱ → ری‌استارت → سلول ۲.

</div>
