"""Core user-interface translations and currency helpers for TGA Store | API."""

from __future__ import annotations

LANGUAGES = {
    # Flag emojis have no Premium custom-emoji IDs in the bundled packs, so
    # language buttons use the Premium globe icon instead of a normal flag.
    "en": ("🌐", "English"),
    "ru": ("🌐", "Русский"),
    "ar": ("🌐", "العربية (Saudi Arabia)"),
    "zh": ("🌐", "中文"),
    "id": ("🌐", "Bahasa Indonesia"),
}
CURRENCIES = {"INR": ("₹", "Indian Rupee"), "USD": ("$", "US Dollar")}
USD_RATE = 100.0  # $1 = ₹100 everywhere. Balance is stored in INR.

TG_EMOJI = '<tg-emoji emoji-id="5866355487255039002">✈️</tg-emoji>'
WA_EMOJI = '<tg-emoji emoji-id="5935973359480213803">💬</tg-emoji>'
API_EMOJI = '<tg-emoji emoji-id="4994749624662098557">📚</tg-emoji>'

TEXT = {
    "en": {
        "choose_language": (
            "<h2>🌐 Choose Your Language</h2>"
            "<blockquote>✨ <b>Personalize Your Interface</b><br>"
            "Select your preferred language below. You can change this anytime from "
            "<b>More → Settings</b>.</blockquote>"
        ),
        "choose_currency": (
            "<h2>💱 Choose Display Currency</h2>"
            "<blockquote>💰 <b>Wallet &amp; Price Display</b><br>"
            "All product prices and wallet balances will be shown in your chosen currency.</blockquote>"
        ),
        "saved": "✅ Preferences saved successfully.",
        "welcome": (
            f"<h2>{TG_EMOJI} TGA Store | API</h2>"
            "<blockquote>👋 <b>Welcome, {name}!</b></blockquote>"
            "<blockquote>📛 <b>User ID:</b> <code>{user_id}</code><br>"
            "💰 <b>Wallet Balance:</b> <code>{balance}</code></blockquote>"
            "<p>✨ <i>Select a service below to get started:</i></p>"
        ),
        "buy_tg": "Buy Telegram Account",
        "buy_wa": "Buy WhatsApp Account",
        "recharge": "Recharge",
        "account": "Account",
        "smm": "Social Media Services",
        "store": "Store",
        "api_docs": "API Docs",
        "more": "More",
        "support": "Support",
        "settings": "Settings",
        "back": "Back",
        "language": "Language",
        "currency": "Currency",
        "coming_soon": (
            f"<h2>{API_EMOJI} Developer API Documentation</h2>"
            "<blockquote>🚀 <b>Coming Soon</b> 🫠<br>"
            "Soon you will be able to connect directly to the <b>TGA Store API</b> "
            "to power your own automated selling bots, panels, and applications.</blockquote>"
            "<p>🔔 <i>High-speed endpoints, webhook delivery &amp; reseller pricing are on the way.</i></p>"
        ),
        "settings_title": (
            "<h2>⚙️ Preferences &amp; Settings</h2>"
            "<blockquote>🌐 <b>Language:</b> <code>{language}</code><br>"
            "💱 <b>Display Currency:</b> <code>{currency}</code></blockquote>"
            "<p>✨ <i>Tap an option below to customize your TGA Store | API experience:</i></p>"
        ),
        "more_title": (
            "<h2>➕ Wallet, Activity &amp; Tools</h2>"
            "<blockquote>💼 <b>Quick Access Center</b><br>"
            "Manage balance transfers, redeem promo codes, view purchase logs, "
            "and earn referral rewards from one dashboard.</blockquote>"
            "<p>👇 <i>Choose an option below:</i></p>"
        ),
        "balance": "Balance",
        "transfer": "Transfer Balance",
        "redeem": "Redeem",
        "history": "History",
        "sales_log": "Sales Log",
        "about": "About Account",
        "refer": "Refer & Earn",
        "how_buy": "How to Buy",
        "how_sell": "How to Sell",
        "how_recharge": "How to Recharge",
    },
    "ru": {
        "choose_language": (
            "<h2>🌐 Выберите язык</h2>"
            "<blockquote>✨ <b>Настройка интерфейса</b><br>"
            "Его можно изменить в любое время в разделе <b>Ещё → Настройки</b>.</blockquote>"
        ),
        "choose_currency": (
            "<h2>💱 Выберите валюту</h2>"
            "<blockquote>💰 <b>Отображение цен и баланса</b><br>"
            "Все цены и баланс кошелька будут показаны в выбранной валюте.</blockquote>"
        ),
        "saved": "✅ Настройки сохранены.",
        "welcome": (
            f"<h2>{TG_EMOJI} TGA Store | API</h2>"
            "<blockquote>👋 <b>Добро пожаловать, {name}!</b></blockquote>"
            "<blockquote>📛 <b>Ваш ID:</b> <code>{user_id}</code><br>"
            "💰 <b>Баланс:</b> <code>{balance}</code></blockquote>"
            "<p>✨ <i>Выберите нужную услугу ниже:</i></p>"
        ),
        "buy_tg": "Купить Telegram",
        "buy_wa": "Купить WhatsApp",
        "recharge": "Пополнить",
        "account": "Аккаунт",
        "smm": "SMM услуги",
        "store": "Магазин",
        "api_docs": "API Docs",
        "more": "Ещё",
        "support": "Поддержка",
        "settings": "Настройки",
        "back": "Назад",
        "language": "Язык",
        "currency": "Валюта",
        "coming_soon": (
            f"<h2>{API_EMOJI} Документация API</h2>"
            "<blockquote>🚀 <b>Скоро</b> 🫠<br>"
            "Вы сможете использовать <b>TGA Store API</b> для создания собственного бота и приложений.</blockquote>"
        ),
        "settings_title": (
            "<h2>⚙️ Настройки</h2>"
            "<blockquote>🌐 <b>Язык:</b> <code>{language}</code><br>"
            "💱 <b>Валюта:</b> <code>{currency}</code></blockquote>"
            "<p>✨ <i>Персонализируйте интерфейс TGA Store | API:</i></p>"
        ),
        "more_title": (
            "<h2>➕ Дополнительно</h2>"
            "<blockquote>💼 <b>Кошелёк, история и бонусы</b><br>"
            "Переводы баланса, промокоды, история покупок и реферальная программа.</blockquote>"
        ),
        "balance": "Баланс",
        "transfer": "Перевод",
        "redeem": "Промокод",
        "history": "История",
        "sales_log": "Продажи",
        "about": "Об аккаунте",
        "refer": "Пригласить",
        "how_buy": "Как купить",
        "how_sell": "Как продать",
        "how_recharge": "Как пополнить",
    },
    "ar": {
        "choose_language": (
            "<h2>🌐 اختر لغتك</h2>"
            "<blockquote>✨ <b>تخصيص الواجهة</b><br>"
            "يمكنك تغيير اللغة لاحقًا من <b>المزيد ← الإعدادات</b>.</blockquote>"
        ),
        "choose_currency": (
            "<h2>💱 اختر العملة</h2>"
            "<blockquote>💰 <b>عرض الأسعار والرصيد</b><br>"
            "ستظهر جميع الأسعار ورصيد المحفظة بهذه العملة.</blockquote>"
        ),
        "saved": "✅ تم حفظ الإعدادات.",
        "welcome": (
            f"<h2>{TG_EMOJI} TGA Store | API</h2>"
            "<blockquote>👋 <b>مرحبًا، {name}!</b></blockquote>"
            "<blockquote>📛 <b>معرفك:</b> <code>{user_id}</code><br>"
            "💰 <b>الرصيد:</b> <code>{balance}</code></blockquote>"
            "<p>✨ <i>اختر خدمة من القائمة أدناه:</i></p>"
        ),
        "buy_tg": "شراء حساب Telegram",
        "buy_wa": "شراء حساب WhatsApp",
        "recharge": "شحن الرصيد",
        "account": "الحساب",
        "smm": "خدمات التواصل",
        "store": "المتجر",
        "api_docs": "توثيق API",
        "more": "المزيد",
        "support": "الدعم",
        "settings": "الإعدادات",
        "back": "رجوع",
        "language": "اللغة",
        "currency": "العملة",
        "coming_soon": (
            f"<h2>{API_EMOJI} توثيق API</h2>"
            "<blockquote>🚀 <b>قريبًا</b> 🫠<br>"
            "ستتمكن قريبًا من استخدام <b>TGA Store API</b> لإنشاء بوت البيع الخاص بك.</blockquote>"
        ),
        "settings_title": (
            "<h2>⚙️ الإعدادات</h2>"
            "<blockquote>🌐 <b>اللغة:</b> <code>{language}</code><br>"
            "💱 <b>العملة:</b> <code>{currency}</code></blockquote>"
            "<p>✨ <i>خصص تجربتك في TGA Store | API:</i></p>"
        ),
        "more_title": (
            "<h2>➕ المزيد من الأدوات</h2>"
            "<blockquote>💼 <b>المحفظة والسجل والمكافآت</b><br>"
            "تحويل الرصيد، الأكواد الترويجية، سجل الطلبات، وبرنامج الإحالة.</blockquote>"
        ),
        "balance": "الرصيد",
        "transfer": "تحويل الرصيد",
        "redeem": "استرداد",
        "history": "السجل",
        "sales_log": "سجل المبيعات",
        "about": "عن الحساب",
        "refer": "ادعُ واربح",
        "how_buy": "طريقة الشراء",
        "how_sell": "طريقة البيع",
        "how_recharge": "طريقة الشحن",
    },
    "zh": {
        "choose_language": (
            "<h2>🌐 选择语言</h2>"
            "<blockquote>✨ <b>界面语言设置</b><br>"
            "以后随时可在 <b>更多 → 设置</b> 中更改。</blockquote>"
        ),
        "choose_currency": (
            "<h2>💱 选择显示货币</h2>"
            "<blockquote>💰 <b>价格与余额显示</b><br>"
            "所有商品价格和钱包余额将以此货币显示。</blockquote>"
        ),
        "saved": "✅ 设置已保存。",
        "welcome": (
            f"<h2>{TG_EMOJI} TGA Store | API</h2>"
            "<blockquote>👋 <b>欢迎，{name}！</b></blockquote>"
            "<blockquote>📛 <b>用户 ID：</b><code>{user_id}</code><br>"
            "💰 <b>钱包余额：</b><code>{balance}</code></blockquote>"
            "<p>✨ <i>请在下方选择需要的服务：</i></p>"
        ),
        "buy_tg": "购买 Telegram 账户",
        "buy_wa": "购买 WhatsApp 账户",
        "recharge": "充值",
        "account": "账户",
        "smm": "社交媒体服务",
        "store": "商店",
        "api_docs": "API 文档",
        "more": "更多",
        "support": "客服",
        "settings": "设置",
        "back": "返回",
        "language": "语言",
        "currency": "货币",
        "coming_soon": (
            f"<h2>{API_EMOJI} API 开发者文档</h2>"
            "<blockquote>🚀 <b>即将推出</b> 🫠<br>"
            "您很快即可使用 <b>TGA Store API</b> 构建自己的自动销售机器人与应用。</blockquote>"
        ),
        "settings_title": (
            "<h2>⚙️ 偏好设置</h2>"
            "<blockquote>🌐 <b>语言：</b><code>{language}</code><br>"
            "💱 <b>货币：</b><code>{currency}</code></blockquote>"
            "<p>✨ <i>个性化您的 TGA Store | API 体验：</i></p>"
        ),
        "more_title": (
            "<h2>➕ 更多功能</h2>"
            "<blockquote>💼 <b>钱包、记录与奖励中心</b><br>"
            "余额转账、兑换码、购买历史及推广返利一站式管理。</blockquote>"
        ),
        "balance": "余额",
        "transfer": "转账",
        "redeem": "兑换",
        "history": "历史",
        "sales_log": "销售记录",
        "about": "账户详情",
        "refer": "邀请赚钱",
        "how_buy": "购买指南",
        "how_sell": "出售指南",
        "how_recharge": "充值指南",
    },
    "id": {
        "choose_language": (
            "<h2>🌐 Pilih Bahasa</h2>"
            "<blockquote>✨ <b>Pengaturan Bahasa</b><br>"
            "Anda dapat mengubahnya kapan saja di <b>Lainnya → Pengaturan</b>.</blockquote>"
        ),
        "choose_currency": (
            "<h2>💱 Pilih Mata Uang</h2>"
            "<blockquote>💰 <b>Tampilan Harga &amp; Saldo</b><br>"
            "Semua harga layanan dan saldo dompet akan ditampilkan dalam mata uang ini.</blockquote>"
        ),
        "saved": "✅ Preferensi berhasil disimpan.",
        "welcome": (
            f"<h2>{TG_EMOJI} TGA Store | API</h2>"
            "<blockquote>👋 <b>Selamat datang, {name}!</b></blockquote>"
            "<blockquote>📛 <b>ID Anda:</b> <code>{user_id}</code><br>"
            "💰 <b>Saldo Dompet:</b> <code>{balance}</code></blockquote>"
            "<p>✨ <i>Pilih layanan di bawah untuk memulai:</i></p>"
        ),
        "buy_tg": "Beli Akun Telegram",
        "buy_wa": "Beli Akun WhatsApp",
        "recharge": "Isi Saldo",
        "account": "Akun",
        "smm": "Layanan Media Sosial",
        "store": "Toko",
        "api_docs": "Dokumentasi API",
        "more": "Lainnya",
        "support": "Dukungan",
        "settings": "Pengaturan",
        "back": "Kembali",
        "language": "Bahasa",
        "currency": "Mata Uang",
        "coming_soon": (
            f"<h2>{API_EMOJI} Dokumentasi Developer API</h2>"
            "<blockquote>🚀 <b>Segera Hadir</b> 🫠<br>"
            "Anda segera dapat menggunakan <b>TGA Store API</b> untuk membangun bot penjualan dan aplikasi sendiri.</blockquote>"
        ),
        "settings_title": (
            "<h2>⚙️ Pengaturan</h2>"
            "<blockquote>🌐 <b>Bahasa:</b> <code>{language}</code><br>"
            "💱 <b>Mata Uang:</b> <code>{currency}</code></blockquote>"
            "<p>✨ <i>Sesuaikan pengalaman TGA Store | API Anda:</i></p>"
        ),
        "more_title": (
            "<h2>➕ Menu Lainnya</h2>"
            "<blockquote>💼 <b>Dompet, Riwayat &amp; Hadiah</b><br>"
            "Kelola transfer saldo, kode promo, riwayat pesanan, dan komisi referral.</blockquote>"
        ),
        "balance": "Saldo",
        "transfer": "Transfer Saldo",
        "redeem": "Tukarkan",
        "history": "Riwayat",
        "sales_log": "Log Penjualan",
        "about": "Tentang Akun",
        "refer": "Undang & Dapatkan",
        "how_buy": "Cara Membeli",
        "how_sell": "Cara Menjual",
        "how_recharge": "Cara Isi Saldo",
    },
}


def tr(user: dict | None, key: str) -> str:
    lang = (user or {}).get("language", "en")
    return TEXT.get(lang, TEXT["en"]).get(key, TEXT["en"].get(key, _EXTRA_DEFAULTS.get(key, key)))


_EXTRA_DEFAULTS = {
    "wp_maint": "WhatsApp service is currently under maintenance. Please try again later!",
    "wp_unavailable": "WhatsApp is not available right now.",
    "wp_opening": "Fetching live WhatsApp rates…",
    "wp_rates_busy": "WhatsApp live rates are temporarily busy. Please tap Try again.",
}


def t(key: str, user: dict | None = None) -> str:
    """Compatibility helper used by allapps.py and server4.py."""
    return tr(user, key)


def format_balance(user: dict | None, amount_inr: float | None = None) -> str:
    user = user or {}
    amount = float(user.get("balance", 0.0) if amount_inr is None else amount_inr)
    if user.get("currency", "INR") == "USD":
        return f"${amount / USD_RATE:,.2f}"
    return f"₹{amount:,.2f}"


def language_name(code: str) -> str:
    return LANGUAGES.get(code, LANGUAGES["en"])[1]
