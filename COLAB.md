<div dir="rtl">

# 🧪 راهنمای اجرای TeleScrape در Google Colab

چهار سلولِ آمادهٔ زیر را به‌ترتیب در Colab اجرا کنید.
راهنمای کامل (گرفتن API ID/Hash، توضیح Secrets، ساختار خروجی، عیب‌یابی) در [README](README.md)؛ وضعیت باگ‌های باز در [bug.md](bug.md).

## ۰) پیش‌نیاز (یک‌بار، قبل از هر چیز)

در نوار کناری Colab روی آیکون 🔑 (**Secrets**) بزنید و این موارد را بسازید و برای هر کدام **Notebook access** را روشن کنید:

| نام Secret | توضیح | الزامی؟ |
|---|---|---|
| `TG_API_ID` | API ID عددی (از my.telegram.org) | بله |
| `TG_API_HASH` | API Hash (۳۲ کاراکتر hex) | بله |
| `TG_PHONE` | شماره با کد کشور، مثل `+98912xxxxxxx` | بله |
| `TG_2FA_PASSWORD` | رمز تأیید دو مرحله‌ای (در صورت داشتن) | خیر |
| `TS_ENC_KEY` | رمز بکاپ رمزنگاری‌شدهٔ سشن (در صورت تمایل) | خیر |

> 💡 **ماندگاری خروجی روی Drive:** قبل از اجرای سلول ۳، یک‌بار Drive را ماونت کنید — در یک سلول خالی:
>
> ```python
> from google.colab import drive
> drive.mount('/content/drive')
> ```
>
> با این کار پوشه‌های خروجی، زیپ‌ها و بکاپ سشن در `MyDrive/TeleScrape` ذخیره می‌شوند و با قطع ران‌تایم از بین نمی‌روند.

## ۱) دانلود فایل

فایل یکپارچه‌ی `final.py` (موتور + لایه‌ی وب در یک فایل):

```python
!wget -q https://raw.githubusercontent.com/Personalmo/telescrape/main/final.py
```

## ۲) تست سلامت (بدون اتصال به تلگرام)

یک دستور، هر دو سوئیت (اول موتور، بعد وب) — باید هر دو با `0 failed` تمام شوند:

```python
!python final.py --selftest
```

> فقط سوئیت موتور: `!python final.py --selftest-engine`

## ۳) اجرای وب‌سرور در پس‌زمینه

سرور را (ری)استارت می‌کند و لاگ اولیه را نشان می‌دهد — باید پیام `TeleScrape Web listening on 0.0.0.0:8000` را ببینید:

```python
!pkill -f final.py 2>/dev/null; sleep 1
!nohup python final.py --port 8000 > web.log 2>&1 &
import time; time.sleep(5); print(open('web.log').read())
```

> پورت پیش‌فرض ۸۰۰۰ است؛ با `--no-drive` خروجی به‌جای Drive در دیسک موقت می‌ماند.

## ۴) باز کردن رابط روی گوشی

لینک فقط برای حساب Google شما باز می‌شود:

```python
from google.colab import output
from IPython.display import HTML, display

url = output.eval_js("google.colab.kernel.proxyPort(8000)")
display(HTML(f'<a href="{url}" target="_blank">باز کردن صفحه وب</a>'))
```

## ⏹ توقف کامل سرور

```python
!pkill -f final.py
```

## 🔧 عیب‌یابی سریع

| نشانه | راه‌حل |
|---|---|
| `web.log` خالی یا خطای pip | سلول ۱ را دوباره اجرا کنید؛ سپس سلول ۳ |
| لینک باز نمی‌شود | سلول ۴ را دوباره اجرا کنید؛ اگر سرور در حال ری‌استارت بود چند ثانیه صبر کنید |
| پیام `connection refused` | مطمئن شوید سلول ۳ اجرا شده و در `web.log` عبارت `listening` دیده می‌شود |
| بعد از قطع ران‌تایم دوباره کد پیامک می‌خواهد | Drive را ماونت کرده بودید؟ Secret `TS_ENC_KEY` یا رمز بکاپ همان رمز قبلی است؟ |
| تغییر در فایل‌های ریپو | چون `wget` نسخهٔ `main` را می‌گیرد، بعد از هر آپدیت سلول ۱ و ۳ را دوباره اجرا کنید |

</div>
