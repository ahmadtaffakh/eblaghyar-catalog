# Eblaghyar Catalog

کاتالوگ مستقل اینترانتی برای نمایه‌سازی و جست‌وجوی بسته‌های ابلاغیار است. این
پروژه منبع حقیقت پرونده، دریافت‌کنندهٔ ثنا یا مالک گردش‌کار حقوقی نیست.

## قرارداد فایل

هر ابلاغیه به‌صورت یک جفت فایل هم‌نام تحویل می‌شود:

```text
<package-name>.zip
<package-name>.cmf
```

فایل CMF یک سند UTF-8 نسخه‌دار است. کاتالوگ فقط وقتی ZIP را وارد می‌کند که
نام، اندازه، SHA-256، مالکیت و ترتیب همهٔ فایل‌ها با CMF تطبیق داشته باشد.
فایل ناقص یا CMF بدون ZIP نادیده گرفته می‌شود و قابل retry باقی می‌ماند.

درخواست و پاسخ در فضای انتقال تخت هستند:

```text
CAT-REQ--<request-id>.req
CAT-RSP--<request-id>.rsp
```

فایل‌های موقت پسوند `.part` دارند و مصرف نمی‌شوند.

بخش `consumers.pargar` در CMF نام ZIP پیوست، موضوع و متن آمادهٔ پیام را در
اختیار FT قرار می‌دهد. FT باید ZIP هم‌نام را پیوست کند و نباید متن را از PDF
یا نام فایل حدس بزند.

## اجرای اولیه

```powershell
python -m pip install -e .
python -m eblaghyar_catalog.cli scan --exchange-dir X:\CatalogExchange --db C:\ProgramData\EblaghyarCatalog\catalog.sqlite
python -m eblaghyar_catalog.cli search --db C:\ProgramData\EblaghyarCatalog\catalog.sqlite --query "شماره یا نام"
python -m eblaghyar_catalog.cli request --exchange-dir X:\CatalogExchange --notice-no 140500123
```

دیتابیس فعال باید روی دیسک محلی باشد و مستقیماً از Share مشترک باز نشود.
