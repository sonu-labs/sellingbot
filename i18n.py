"""Core user-interface translations and currency helpers for TGA Store | API."""

from __future__ import annotations

from html import escape

LANGUAGES = {
    "en": ("🇬🇧", "English"),
    "ru": ("🇷🇺", "Русский"),
    "ar": ("🇸🇦", "العربية (Saudi Arabia)"),
    "zh": ("🇨🇳", "中文"),
    "id": ("🇮🇩", "Bahasa Indonesia"),
}
CURRENCIES = {"INR": ("₹", "Indian Rupee"), "USD": ("$", "US Dollar")}
USD_RATE = 95.0  # Existing bot accounting rate: balance is stored in INR.

TEXT = {
    "en": {
        "choose_language": "<h2>Choose your language</h2><p>You can change this later from <b>More → Settings</b>.</p>",
        "choose_currency": "<h2>Choose your currency</h2><p>Prices and balances will be displayed in this currency.</p>",
        "saved": "✅ Preferences saved successfully.",
        "welcome": "<h2>TGA Store | API</h2><blockquote>Welcome, {name}!</blockquote><p>Fast, secure digital account and service marketplace.</p><p>🆔 <b>Your ID:</b> <code>{user_id}</code><br>💰 <b>Balance:</b> <code>{balance}</code></p><p>Choose a service below.</p>",
        "buy_tg": "Buy Telegram Account", "buy_wa": "Buy WhatsApp Account",
        "recharge": "Recharge", "account": "Account", "smm": "Social Media Services",
        "store": "Store", "api_docs": "API Docs", "more": "More", "support": "Support",
        "settings": "Settings", "back": "Back", "language": "Language", "currency": "Currency",
        "coming_soon": "<h2>API Docs</h2><blockquote>Coming soon 🫠</blockquote><p>Soon you will be able to use the TGA Store API to build your own selling bot and applications.</p>",
        "settings_title": "<h2>Settings</h2><p>Personalize your TGA Store | API experience.</p><p>🌐 <b>Language:</b> {language}<br>💱 <b>Currency:</b> {currency}</p>",
        "more_title": "<h2>More</h2><p>Wallet, activity, rewards and account tools in one place.</p>",
        "balance": "Balance", "transfer": "Transfer Balance", "redeem": "Redeem",
        "history": "History", "sales_log": "Sales Log", "about": "About Account",
        "refer": "Refer & Earn", "how_buy": "How to Buy", "how_sell": "How to Sell",
        "how_recharge": "How to Recharge",
    },
    "ru": {
        "choose_language": "<h2>Выберите язык</h2><p>Его можно изменить в <b>Ещё → Настройки</b>.</p>",
        "choose_currency": "<h2>Выберите валюту</h2><p>Цены и баланс будут показаны в этой валюте.</p>",
        "saved": "✅ Настройки сохранены.",
        "welcome": "<h2>TGA Store | API</h2><blockquote>Добро пожаловать, {name}!</blockquote><p>Быстрый и безопасный магазин цифровых аккаунтов и услуг.</p><p>🆔 <b>Ваш ID:</b> <code>{user_id}</code><br>💰 <b>Баланс:</b> <code>{balance}</code></p><p>Выберите услугу.</p>",
        "buy_tg": "Купить Telegram", "buy_wa": "Купить WhatsApp", "recharge": "Пополнить", "account": "Аккаунт", "smm": "SMM услуги", "store": "Магазин", "api_docs": "API Docs", "more": "Ещё", "support": "Поддержка", "settings": "Настройки", "back": "Назад", "language": "Язык", "currency": "Валюта", "coming_soon": "<h2>API Docs</h2><blockquote>Скоро 🫠</blockquote><p>Вы сможете использовать TGA Store API для своего бота и приложений.</p>", "settings_title": "<h2>Настройки</h2><p>Настройте TGA Store | API.</p><p>🌐 <b>Язык:</b> {language}<br>💱 <b>Валюта:</b> {currency}</p>", "more_title": "<h2>Ещё</h2><p>Кошелёк, история, награды и инструменты аккаунта.</p>", "balance": "Баланс", "transfer": "Перевод", "redeem": "Промокод", "history": "История", "sales_log": "Продажи", "about": "Об аккаунте", "refer": "Пригласить", "how_buy": "Как купить", "how_sell": "Как продать", "how_recharge": "Как пополнить",
    },
    "ar": {
        "choose_language": "<h2>اختر لغتك</h2><p>يمكنك تغييرها لاحقًا من <b>المزيد ← الإعدادات</b>.</p>", "choose_currency": "<h2>اختر العملة</h2><p>ستظهر الأسعار والرصيد بهذه العملة.</p>", "saved": "✅ تم حفظ الإعدادات.", "welcome": "<h2>TGA Store | API</h2><blockquote>مرحبًا، {name}!</blockquote><p>سوق سريع وآمن للحسابات والخدمات الرقمية.</p><p>🆔 <b>معرفك:</b> <code>{user_id}</code><br>💰 <b>الرصيد:</b> <code>{balance}</code></p><p>اختر خدمة أدناه.</p>", "buy_tg": "شراء حساب Telegram", "buy_wa": "شراء حساب WhatsApp", "recharge": "شحن الرصيد", "account": "الحساب", "smm": "خدمات التواصل", "store": "المتجر", "api_docs": "توثيق API", "more": "المزيد", "support": "الدعم", "settings": "الإعدادات", "back": "رجوع", "language": "اللغة", "currency": "العملة", "coming_soon": "<h2>توثيق API</h2><blockquote>قريبًا 🫠</blockquote><p>ستتمكن من استخدام TGA Store API لإنشاء بوت البيع الخاص بك.</p>", "settings_title": "<h2>الإعدادات</h2><p>خصص تجربتك.</p><p>🌐 <b>اللغة:</b> {language}<br>💱 <b>العملة:</b> {currency}</p>", "more_title": "<h2>المزيد</h2><p>المحفظة والسجل والمكافآت وأدوات الحساب.</p>", "balance": "الرصيد", "transfer": "تحويل الرصيد", "redeem": "استرداد", "history": "السجل", "sales_log": "سجل المبيعات", "about": "عن الحساب", "refer": "ادعُ واربح", "how_buy": "طريقة الشراء", "how_sell": "طريقة البيع", "how_recharge": "طريقة الشحن",
    },
    "zh": {
        "choose_language": "<h2>选择语言</h2><p>以后可在<b>更多 → 设置</b>中更改。</p>", "choose_currency": "<h2>选择货币</h2><p>价格和余额将以此货币显示。</p>", "saved": "✅ 设置已保存。", "welcome": "<h2>TGA Store | API</h2><blockquote>欢迎，{name}！</blockquote><p>快速、安全的数字账户与服务市场。</p><p>🆔 <b>用户 ID：</b><code>{user_id}</code><br>💰 <b>余额：</b><code>{balance}</code></p><p>请选择服务。</p>", "buy_tg": "购买 Telegram 账户", "buy_wa": "购买 WhatsApp 账户", "recharge": "充值", "account": "账户", "smm": "社交媒体服务", "store": "商店", "api_docs": "API 文档", "more": "更多", "support": "客服", "settings": "设置", "back": "返回", "language": "语言", "currency": "货币", "coming_soon": "<h2>API 文档</h2><blockquote>即将推出 🫠</blockquote><p>您很快可使用 TGA Store API 创建自己的销售机器人和应用。</p>", "settings_title": "<h2>设置</h2><p>个性化您的体验。</p><p>🌐 <b>语言：</b>{language}<br>💱 <b>货币：</b>{currency}</p>", "more_title": "<h2>更多</h2><p>钱包、记录、奖励与账户工具。</p>", "balance": "余额", "transfer": "转账", "redeem": "兑换", "history": "历史", "sales_log": "销售记录", "about": "账户详情", "refer": "邀请赚钱", "how_buy": "购买指南", "how_sell": "出售指南", "how_recharge": "充值指南",
    },
    "id": {
        "choose_language": "<h2>Pilih bahasa</h2><p>Dapat diubah di <b>Lainnya → Pengaturan</b>.</p>", "choose_currency": "<h2>Pilih mata uang</h2><p>Harga dan saldo ditampilkan dalam mata uang ini.</p>", "saved": "✅ Preferensi berhasil disimpan.", "welcome": "<h2>TGA Store | API</h2><blockquote>Selamat datang, {name}!</blockquote><p>Marketplace akun dan layanan digital yang cepat dan aman.</p><p>🆔 <b>ID Anda:</b> <code>{user_id}</code><br>💰 <b>Saldo:</b> <code>{balance}</code></p><p>Pilih layanan di bawah.</p>", "buy_tg": "Beli Akun Telegram", "buy_wa": "Beli Akun WhatsApp", "recharge": "Isi Saldo", "account": "Akun", "smm": "Layanan Media Sosial", "store": "Toko", "api_docs": "Dokumentasi API", "more": "Lainnya", "support": "Dukungan", "settings": "Pengaturan", "back": "Kembali", "language": "Bahasa", "currency": "Mata Uang", "coming_soon": "<h2>Dokumentasi API</h2><blockquote>Segera hadir 🫠</blockquote><p>Anda akan dapat memakai TGA Store API untuk membuat bot penjualan sendiri.</p>", "settings_title": "<h2>Pengaturan</h2><p>Sesuaikan pengalaman Anda.</p><p>🌐 <b>Bahasa:</b> {language}<br>💱 <b>Mata uang:</b> {currency}</p>", "more_title": "<h2>Lainnya</h2><p>Dompet, aktivitas, hadiah, dan alat akun.</p>", "balance": "Saldo", "transfer": "Transfer Saldo", "redeem": "Tukarkan", "history": "Riwayat", "sales_log": "Log Penjualan", "about": "Tentang Akun", "refer": "Undang & Dapatkan", "how_buy": "Cara Membeli", "how_sell": "Cara Menjual", "how_recharge": "Cara Isi Saldo",
    },
}


def tr(user: dict | None, key: str) -> str:
    lang = (user or {}).get("language", "en")
    return TEXT.get(lang, TEXT["en"]).get(key, TEXT["en"].get(key, key))


def format_balance(user: dict | None, amount_inr: float | None = None) -> str:
    user = user or {}
    amount = float(user.get("balance", 0.0) if amount_inr is None else amount_inr)
    if user.get("currency", "INR") == "USD":
        return f"${amount / USD_RATE:,.2f}"
    return f"₹{amount:,.2f}"


def language_name(code: str) -> str:
    return LANGUAGES.get(code, LANGUAGES["en"])[1]
