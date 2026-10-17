"""Rich-message transport and premium custom-emoji support for TGA Store | API.

Every normal outgoing text message is upgraded to Bot API 10.3 ``sendRichMessage``.
If Telegram rejects rich messages (for example, an outdated self-hosted Bot API), the
original classic message is sent automatically with converted HTML and Premium emoji.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery, EditMessageCaption, EditMessageText, SendAnimation,
    SendAudio, SendDocument, SendMessage, SendPhoto, SendVideo, SendVoice,
)
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation,
    InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo,
)

try:
    from aiogram.methods import SendRichMessage
except ImportError:  # pragma: no cover
    SendRichMessage = None

try:
    from aiogram.types import (
        InputRichBlockAnimation, InputRichBlockAudio, InputRichBlockDocument,
        InputRichBlockPhoto, InputRichBlockVideo, InputRichMessage, RichBlockCaption,
        RichTextCustomEmoji,
    )
except ImportError:  # pragma: no cover
    InputRichBlockAnimation = InputRichBlockAudio = InputRichBlockDocument = None
    InputRichBlockPhoto = InputRichBlockVideo = InputRichMessage = None
    RichBlockCaption = RichTextCustomEmoji = None

try:
    from aiogram.types import InputRichMessageMedia
except ImportError:  # pragma: no cover
    InputRichMessageMedia = None

log = logging.getLogger("tga.rich")

# Explicit brand/feature custom emoji IDs requested by owner
TELEGRAM_EMOJI_ID = "5866355487255039002"
WHATSAPP_EMOJI_ID = "5935973359480213803"
API_DOCS_EMOJI_ID = "4994749624662098557"
UPI_EMOJI_ID = "6152069549442208798"

# One global money rate for the whole bot: $1 = ₹100.
# Every USD/USDT -> INR conversion (prices, balances, display currency)
# must use 100, never an older 90/95 rate.
USD_TO_INR = 100.0


def tg_icon(alt: str = "✈️") -> str:
    """Return a Rich/HTML Telegram custom emoji tag."""
    return f'<tg-emoji emoji-id="{TELEGRAM_EMOJI_ID}">{alt}</tg-emoji>'


def wa_icon(alt: str = "💬") -> str:
    """Return a Rich/HTML WhatsApp custom emoji tag."""
    return f'<tg-emoji emoji-id="{WHATSAPP_EMOJI_ID}">{alt}</tg-emoji>'


def api_icon(alt: str = "📚") -> str:
    """Return a Rich/HTML API Docs custom emoji tag."""
    return f'<tg-emoji emoji-id="{API_DOCS_EMOJI_ID}">{alt}</tg-emoji>'


def upi_icon(alt: str = "💳") -> str:
    """Return a Rich/HTML UPI custom emoji tag."""
    return f'<tg-emoji emoji-id="{UPI_EMOJI_ID}">{alt}</tg-emoji>'


# Prefer icon-oriented packs, then use the larger decorative packs as fallback.
_PACK_ORDER = (
    "Unigram_Icons.txt",
    "tgmacicons.txt",
    "ToastEmoji.txt",
    "AdaptiveStatus.txt",
    "StatusPackNew.txt",
    "AdReach.txt",
    "Superior_Emoji.txt",
    "KripanshEmojis_by_fStikBot.txt",
    "NewsEmoji.txt",
)
_EMOJI_DIR = Path(__file__).with_name("EMOJI_IDS")

# Owner-supplied custom emoji IDs for the 47 UI emojis + API Docs
_OWNER_CUSTOM_EMOJI_IDS: dict[str, str] = {
    "🛒": "5201850941925531200",   # Buy badge
    "🏪": "5202058053838483782",   # Shop badge
    "🏦": "5264895611517300926",   # Bank building
    "🏢": "5264733042710181045",   # Office / Panel building
    "🅱️": "5217811903685865303",   # Binance logo
    "🅱": "5217811903685865303",
    "🆔": "5202109872618908003",   # iD badge
    "🧾": "5204242830687494041",   # Receipt icon
    "📋": "5348178055338671586",   # Clipboard checkmark
    "📑": "6021547434641987535",   # Document search
    "📜": "6021435576513730578",   # History list
    "🎟️": "5249061621875100582",   # Coupon tag
    "🎟": "5249061621875100582",
    "📢": "6008220984346152956",   # Megaphone
    "📩": "5256100953014152571",   # Incoming OTP / Bookmark
    "📨": "5406631276042002796",   # Incoming envelope
    "📭": "5287533898803211359",   # Mailbox
    "📲": "5819062970998590994",   # Devices / Mobile app
    "💌": "6215361789538866270",   # Invite letter
    "🔑": "5330115548900501467",   # Golden key
    "🛠️": "5462921117423384478",   # FIX / Maintenance tools
    "🛠": "5462921117423384478",
    "🎛️": "5194904833051632910",   # Controls / Red gear
    "🎛": "5194904833051632910",
    "📡": "5202020730572682019",   # Connect badge
    "🛰️": "5321304062715517873",   # Satellite
    "🛰": "5321304062715517873",
    "👮": "6269458311381258421",   # ADMIN badge
    "🐛": "5397991236361527676",   # Bug / Debug
    "⏳": "5451732530048802485",   # Hourglass
    "🕒": "5408910404732595664",   # Clock
    "📅": "5274055917766202507",   # Calendar
    "🔁": "5346269127059196142",   # Repeat / Refresh arrows
    "♻️": "5803057229909202251",   # Spinner / Refresh
    "♻": "5803057229909202251",
    "🚧": "6334528491844798409",   # Construction / Under work
    "🌍": "5399898266265475100",   # Globe 1
    "🌏": "5397753673130463064",   # Globe 2
    "🎯": "5350460637182993292",   # Target / Pin
    "🏁": "5411520005386806155",   # Checkered flag
    "🎣": "5343609421316521960",   # Fishing pole
    "🔤": "5201805788934351199",   # LOG badge
    "🛟": "5780419462314331907",   # Lifebuoy
    "🥂": "5803393311100113792",   # Champagne / Celebration
    "🦁": "6082571661423414884",   # Golden Lion
    "👽": "6114094221705091516",   # Alien
    "😔": "6224160773074061577",   # Sad character
    "👇": "6023566962624306038",   # Pointing down
    "👉": "6021623107670776267",   # Pointing right
    "👈": "6021510515103111203",   # Pointing left/right
    "➖": "5346176879751612829",   # Divider line
    "▪️": "5875091588174059190",   # Red dot
    "▪": "5875091588174059190",
    "🔹": "5980787993139481991",   # Blue spark
    "📘": API_DOCS_EMOJI_ID,       # API Docs icon
    "📚": API_DOCS_EMOJI_ID,
    "✈️": TELEGRAM_EMOJI_ID,
    "✈": TELEGRAM_EMOJI_ID,
}

# Symbols that must remain plain Unicode characters (per owner request: "◍➔ ➜ → ← Ye sa aisa he rendo")
_PLAIN_UNICODE_SYMBOLS = {"◍", "➔", "➜", "→", "←"}


def _register_emoji(mapping: dict[str, str], emoji: str, custom_id: str, overwrite: bool = False) -> None:
    if not emoji or not custom_id or emoji in _PLAIN_UNICODE_SYMBOLS:
        return
    base = emoji.rstrip("\ufe0f")
    if base in _PLAIN_UNICODE_SYMBOLS:
        return
    variants = (emoji, base, f"{base}\ufe0f")
    for variant in variants:
        if not variant:
            continue
        if overwrite or variant not in mapping:
            mapping[variant] = custom_id


def _load_emoji_ids() -> dict[str, str]:
    result: dict[str, str] = {}
    for filename in _PACK_ORDER:
        try:
            records = json.loads((_EMOJI_DIR / filename).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Could not load emoji pack %s: %s", filename, exc)
            continue
        for item in records:
            if not isinstance(item, dict):
                continue
            emoji = str(item.get("emoji") or "")
            custom_id = str(item.get("custom_emoji_id") or item.get("id") or "")
            if emoji and custom_id:
                # First pack wins, so the icon packs keep priority.
                _register_emoji(result, emoji, custom_id, overwrite=False)

    # Apply explicit owner custom emoji IDs with highest priority.
    for emoji, custom_id in _OWNER_CUSTOM_EMOJI_IDS.items():
        _register_emoji(result, emoji, custom_id, overwrite=True)

    # Ensure plain symbols are never replaced.
    for sym in _PLAIN_UNICODE_SYMBOLS:
        result.pop(sym, None)
        result.pop(f"{sym}\ufe0f", None)
    return result


EMOJI_IDS = _load_emoji_ids()
_SORTED_EMOJIS = sorted(EMOJI_IDS, key=len, reverse=True)
_EMOJI_PATTERN = (
    re.compile("|".join(re.escape(item) for item in _SORTED_EMOJIS))
    if _SORTED_EMOJIS
    else re.compile(r"(?!x)x")
)
# Split by complete <tg-emoji>...</tg-emoji> blocks first so they are never double-wrapped.
_TAG_SPLIT = re.compile(r"(<tg-emoji\b[^>]*>.*?</tg-emoji>|<[^>]+>)", flags=re.I | re.S)
_INR_AMOUNT = re.compile(r"₹\s*([0-9][0-9,]*(?:\.[0-9]+)?)")

_BLOCK_OPEN_RE = re.compile(
    r"(\s*<(?:h[1-6]|p|blockquote|ul|ol|li|details|summary|hr)\b[^>]*>\s*)",
    flags=re.I,
)
_BLOCK_CLOSE_RE = re.compile(
    r"(\s*</(?:h[1-6]|p|blockquote|ul|ol|li|details|summary)>\s*)",
    flags=re.I,
)


def convert_currency(text: str, currency: str | None) -> str:
    """Convert INR amounts in existing flows to the user's selected display currency."""
    if currency != "USD":
        return text

    def replace(match: re.Match) -> str:
        try:
            inr = float(match.group(1).replace(",", ""))
            return f"${inr / USD_TO_INR:,.2f}"
        except ValueError:
            return match.group(0)

    return _INR_AMOUNT.sub(replace, text)


# ---------------------------------------------------------------------------
# No-normal-emoji guarantee
# ---------------------------------------------------------------------------
# The bundled packs do not contain a Premium custom emoji for every Unicode
# emoji (most country flags, 🎲, …).  A normal emoji must never be shown next
# to Premium ones, so any emoji that has no Premium ID is replaced by a
# Premium fallback (a globe for flags, a blue spark for everything else)
# instead of leaking through as a plain emoji.  Plain typographic symbols
# the owner explicitly wants kept (◍ ➔ ➜ → ←) and name symbols such as ☻
# are not emojis and are left untouched.
_GLOBE_PREMIUM_ID = "5399898266265475100"  # 🌍 in _OWNER_CUSTOM_EMOJI_IDS
_FALLBACK_PREMIUM_ID = "5980787993139481991"  # 🔹 in _OWNER_CUSTOM_EMOJI_IDS
_KEEP_PLAIN_SYMBOLS = _PLAIN_UNICODE_SYMBOLS | {"☻", "☺", "♥", "♡", "★", "☆", "•", "·"}
_FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]")
_UNMAPPED_EMOJI_RE = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{1,2}"
    r"|[\U0001F000-\U0001FAFF\u2600-\u27BF](?:\ufe0f)?"
    r"(?:\u200d[\U0001F000-\U0001FAFF\u2600-\u27BF](?:\ufe0f)?)*"
    r"|[\ufe0f\u200d\u20e3]"
)


def _is_flag_token(token: str) -> bool:
    return bool(_FLAG_RE.search(token))


def _fallback_premium_tag(token: str) -> str:
    """Premium replacement tag for an emoji that has no Premium ID of its own."""
    if not token.strip("\ufe0f\u200d\u20e3"):
        return ""
    if _is_flag_token(token):
        return f'<tg-emoji emoji-id="{_GLOBE_PREMIUM_ID}">🌍</tg-emoji>'
    return f'<tg-emoji emoji-id="{_FALLBACK_PREMIUM_ID}">🔹</tg-emoji>'


def _replace_unmapped_emoji(text: str) -> str:
    """Replace every remaining (unmapped) emoji with a Premium fallback."""

    def repl(match: re.Match) -> str:
        token = match.group(0)
        if token in _KEEP_PLAIN_SYMBOLS:
            return token
        return _fallback_premium_tag(token)

    return _UNMAPPED_EMOJI_RE.sub(repl, text)


def strip_plain_emoji(text: str) -> str:
    """Remove emoji from plain-text surfaces that cannot show Premium emoji.

    Used for callback-query toasts/alerts (``answerCallbackQuery`` has no
    custom-emoji support) and for button labels, where the Premium icon is
    carried separately by ``icon_custom_emoji_id``.
    """

    def repl(match: re.Match) -> str:
        token = match.group(0)
        return token if token in _KEEP_PLAIN_SYMBOLS else ""

    cleaned = _EMOJI_PATTERN.sub(repl, str(text or ""))
    cleaned = _UNMAPPED_EMOJI_RE.sub(repl, cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def emoji_id(emoji: str) -> str | None:
    """Return a Premium custom-emoji id for a Unicode emoji, if supplied."""
    return (
        EMOJI_IDS.get(emoji)
        or EMOJI_IDS.get(emoji.rstrip("\ufe0f"))
        or EMOJI_IDS.get(f"{emoji.rstrip(chr(0xfe0f))}\ufe0f")
    )


def _premium_text(text: str) -> str:
    """Replace visible Unicode emoji with Rich HTML ``tg-emoji`` elements.

    Emojis with a Premium ID become that Premium emoji.  Emojis without one
    (flags, 🎲, dynamic/API/user-supplied emoji, …) become a Premium globe /
    spark fallback, so a normal emoji is never displayed next to Premium ones.
    """
    parts = _TAG_SPLIT.split(text)
    for index in range(0, len(parts), 2):
        replaced = _EMOJI_PATTERN.sub(
            lambda match: (
                f'<tg-emoji emoji-id="{EMOJI_IDS[match.group(0)]}">'
                f"{match.group(0)}</tg-emoji>"
            ),
            parts[index],
        )
        # Replace unmapped emoji only outside the tg-emoji tags just inserted.
        subparts = re.split(
            r"(<tg-emoji\b[^>]*>.*?</tg-emoji>)", replaced, flags=re.I | re.S
        )
        for sub_index in range(0, len(subparts), 2):
            subparts[sub_index] = _replace_unmapped_emoji(subparts[sub_index])
        parts[index] = "".join(subparts)
    return "".join(parts)


def to_rich_html(text: str, parse_mode: Any = "HTML") -> str:
    """Convert classic Telegram HTML/plain text into valid Rich HTML.

    Rich HTML follows browser whitespace rules, unlike ``sendMessage``. Therefore,
    line breaks inside text are converted to ``<br>`` while extra whitespace around
    block tags (``<h2>``, ``<blockquote>``, ``<p>``, ``<hr>``) is normalized so no
    unwanted blank gaps appear. Existing code/pre blocks are left untouched.
    """
    source = str(text or "")
    mode = str(parse_mode or "").upper()
    is_html = "HTML" in mode or (parse_mode is not None and "DEFAULT" in mode)
    if not is_html:
        source = html.escape(source)

    source = re.sub(
        r'<span\s+class=["\']tg-spoiler["\']\s*>(.*?)</span>',
        r"<tg-spoiler>\1</tg-spoiler>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Preserve literal newlines in preformatted blocks; normalize breaks elsewhere.
    chunks = re.split(r"(<pre(?:\s[^>]*)?>.*?</pre>)", source, flags=re.I | re.S)
    for idx in range(0, len(chunks), 2):
        seg = chunks[idx].replace("\r\n", "\n").replace("\r", "\n")
        # Remove newlines immediately surrounding block tags so they don't turn into <br>
        seg = re.sub(
            r"\n+\s*(<(?:h[1-6]|p|blockquote|ul|ol|li|details|summary|hr)\b[^>]*>)",
            r"\1",
            seg,
            flags=re.I,
        )
        seg = re.sub(
            r"(<(?:h[1-6]|p|blockquote|ul|ol|li|details|summary|hr)\b[^>]*>)\s*\n+",
            r"\1",
            seg,
            flags=re.I,
        )
        seg = re.sub(
            r"\n+\s*(</(?:h[1-6]|p|blockquote|ul|ol|li|details|summary)>)",
            r"\1",
            seg,
            flags=re.I,
        )
        seg = re.sub(
            r"(</(?:h[1-6]|p|blockquote|ul|ol|li|details|summary)>)\s*\n+",
            r"\1",
            seg,
            flags=re.I,
        )
        seg = seg.replace("\n", "<br>")
        chunks[idx] = seg
    return _premium_text("".join(chunks))


def to_classic_html(text: str, parse_mode: Any = "HTML") -> str:
    """Convert Rich HTML into classic Telegram HTML (for SendPhoto/SendMessage fallback)."""
    source = str(text or "")
    mode = str(parse_mode or "").upper()
    is_html = "HTML" in mode or (parse_mode is not None and "DEFAULT" in mode)
    if not is_html:
        return html.escape(source)

    source = re.sub(
        r'<span\s+class=["\']tg-spoiler["\']\s*>(.*?)</span>',
        r"<tg-spoiler>\1</tg-spoiler>",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    source = re.sub(r"<img\b[^>]*/?>", "", source, flags=re.I)
    source = re.sub(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", r"<b>\1</b>\n\n", source, flags=re.I | re.S)
    source = re.sub(r"<p\b[^>]*>(.*?)</p>", r"\1\n\n", source, flags=re.I | re.S)
    source = re.sub(r"<hr\s*/?>", "\n", source, flags=re.I)
    source = re.sub(r"<br\s*/?>", "\n", source, flags=re.I)
    source = re.sub(r"(</blockquote>)\s*(?!\n)", r"\1\n\n", source, flags=re.I)
    source = re.sub(r"\n{3,}", "\n\n", source).strip()
    return _premium_text(source)


def _clean_leading_symbol(text: str, symbol: str) -> str:
    cleaned = text[len(symbol):]
    cleaned = cleaned.lstrip("\ufe0f\u200d \t")
    return cleaned or text


def premium_button(
    button: InlineKeyboardButton | str | None = None,
    currency: str | None = None,
    *,
    text: str | None = None,
    custom_emoji_id: str | None = None,
    icon_custom_emoji_id: str | None = None,
    **kwargs: Any,
) -> InlineKeyboardButton:
    """Use a Premium icon on every button and keep clean text."""
    if not isinstance(button, InlineKeyboardButton):
        btn_text = button if isinstance(button, str) else (text or "")
        chosen_id = custom_emoji_id or icon_custom_emoji_id
        button = InlineKeyboardButton(
            text=btn_text,
            icon_custom_emoji_id=chosen_id,
            **kwargs,
        )
    raw_text = button.text or ""
    cb = button.callback_data or ""
    updates: dict[str, Any] = {
        "style": None,
        "text": convert_currency(raw_text, currency),
    }

    custom_id = custom_emoji_id or icon_custom_emoji_id or button.icon_custom_emoji_id

    # Explicit overrides requested by owner
    if not custom_id:
        if cb == "api_docs":
            custom_id = API_DOCS_EMOJI_ID
        elif cb in {"buy", "s3tg_open", "s3_toggle:lzt", "admin_toggle:telegram"}:
            custom_id = TELEGRAM_EMOJI_ID
        elif cb in {"buy_wa", "buy_server3:wa", "admin_toggle:whatsapp", "s3_toggle:wa"}:
            custom_id = WHATSAPP_EMOJI_ID
        elif cb in {
            "recharge_upi_auto",
            "recharge_upi_manual",
            "pset_inr:upi",
            "pset_toggle:manual_upi",
            "pset_edit:manual_upi_id",
        } or "upi" in raw_text.lower():
            custom_id = UPI_EMOJI_ID

    # Strip leading emoji (and variation selector) and detect icon if not already set
    stripped_text = raw_text.lstrip()
    if stripped_text.startswith("• ") and stripped_text.endswith(" •"):
        stripped_text = stripped_text[2:-2].strip()
    elif stripped_text.startswith("• "):
        stripped_text = stripped_text[2:].strip()

    for symbol in _SORTED_EMOJIS:
        if stripped_text.startswith(symbol):
            if not custom_id:
                custom_id = EMOJI_IDS[symbol]
            stripped_text = _clean_leading_symbol(stripped_text, symbol)
            break

    # Any emoji anywhere in the label (leading, middle or trailing, e.g.
    # "S1: 🟢 ON", "🇮🇳 India", "Next ➡️") becomes the Premium button icon.
    # The emoji character itself is removed from the visible label below, so
    # a normal emoji never sits next to the Premium icon.
    if not custom_id:
        for symbol in _SORTED_EMOJIS:
            if symbol in stripped_text:
                custom_id = EMOJI_IDS[symbol]
                break
    if not custom_id:
        unmapped = _UNMAPPED_EMOJI_RE.search(stripped_text)
        if unmapped and unmapped.group(0) not in _KEEP_PLAIN_SYMBOLS:
            custom_id = (
                _GLOBE_PREMIUM_ID
                if _is_flag_token(unmapped.group(0))
                else _FALLBACK_PREMIUM_ID
            )

    # Secondary detection for buttons without a leading emoji
    if not custom_id:
        lower = stripped_text.lower()
        if button.copy_text is not None or lower.startswith("copy "):
            custom_id = EMOJI_IDS.get("📄")
        elif "get otp" in lower or "get code" in lower or "login code" in lower:
            custom_id = EMOJI_IDS.get("✉️")
        elif "buy now" in lower:
            custom_id = EMOJI_IDS.get("🛍️")
        elif "support" in lower:
            custom_id = EMOJI_IDS.get("👨‍💻")
        elif "remove" in lower or "logout" in lower or "reject" in lower:
            custom_id = EMOJI_IDS.get("🚪")
        elif stripped_text.startswith("₹"):
            custom_id = EMOJI_IDS.get("💱")
        elif stripped_text.startswith("$"):
            custom_id = EMOJI_IDS.get("💵")
    if custom_id:
        updates["icon_custom_emoji_id"] = custom_id
    # Remove every emoji character from the visible label (the Premium icon
    # above carries it now) and collapse the leftover spacing.
    cleaned_text = strip_plain_emoji(stripped_text)
    cleaned_text = re.sub(r"\s+", " ", cleaned_text).strip(" ·-") or "•"
    updates["text"] = convert_currency(cleaned_text, currency)
    return button.model_copy(update=updates)


def premium_markup(markup: Any, currency: str | None = None) -> Any:
    """Add Premium icons to every inline button without colored button styles."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    return markup.model_copy(
        update={
            "inline_keyboard": [
                [premium_button(button, currency) for button in row]
                for row in markup.inline_keyboard
            ]
        }
    )


async def safe_edit_or_send(
    bot: Any,
    message: Any,
    text: str,
    reply_markup: Any = None,
    parse_mode: str = "HTML",
) -> Any:
    """Edit ``message`` when possible, otherwise send a fresh message.

    Keeps navigation buttons (Back / Buy WhatsApp / …) working when the source
    message is an ``InaccessibleMessage`` (too old), was deleted, can no longer be
    edited, or already shows the requested content (``message is not modified``).
    """
    edit_text = getattr(message, "edit_text", None)
    if edit_text is not None:
        try:
            return await edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message
            log.warning("edit failed, sending a new message instead: %s", exc)
    answer = getattr(message, "answer", None)
    if answer is not None:
        return await answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    return await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


_MEDIA_METHODS = (SendPhoto, SendVideo, SendDocument, SendAudio, SendAnimation, SendVoice)


def _plain_caption(text: str | None, currency: str | None) -> str | None:
    if not text:
        return None
    converted = convert_currency(text, currency)
    converted = re.sub(r"<br\s*/?>", "\n", converted, flags=re.I)
    converted = re.sub(r"<[^>]+>", "", converted)
    return html.unescape(converted)


def _premium_rich_text(text: str):
    """Split plain caption text into text + Premium custom-emoji parts.

    Emojis without a Premium ID become a Premium globe/spark fallback, never
    a plain (normal) emoji part.
    """
    if RichTextCustomEmoji is None:
        return text
    combined = re.compile(
        f"(?:{_EMOJI_PATTERN.pattern})|(?:{_UNMAPPED_EMOJI_RE.pattern})"
    )
    parts = []
    position = 0
    for match in combined.finditer(text):
        if match.start() > position:
            parts.append(text[position:match.start()])
        symbol = match.group(0)
        if symbol in _KEEP_PLAIN_SYMBOLS:
            parts.append(symbol)
        elif not symbol.strip("\ufe0f\u200d\u20e3"):
            pass  # stray variation selector / joiner: drop it
        elif symbol in EMOJI_IDS:
            parts.append(RichTextCustomEmoji(
                custom_emoji_id=EMOJI_IDS[symbol], alternative_text=symbol
            ))
        elif _is_flag_token(symbol):
            parts.append(RichTextCustomEmoji(
                custom_emoji_id=_GLOBE_PREMIUM_ID, alternative_text="🌍"
            ))
        else:
            parts.append(RichTextCustomEmoji(
                custom_emoji_id=_FALLBACK_PREMIUM_ID, alternative_text="🔹"
            ))
        position = match.end()
    if position < len(text):
        parts.append(text[position:])
    return parts or text


def _media_rich_message(method: Any, currency: str | None) -> InputRichMessage | None:
    """Build a native Rich Message for media sends, preserving Rich HTML for photos."""
    if isinstance(method, SendPhoto):
        # For string photo references (file_id or URL), embed the photo inside Rich HTML
        # so headings, blockquotes, code tags, spoilers and custom emojis all render.
        if InputRichMessageMedia is not None and isinstance(method.photo, str):
            caption_html = to_rich_html(
                convert_currency(method.caption or "", currency),
                method.parse_mode,
            )
            media_item = InputRichMessageMedia(
                id="photo1",
                media=InputMediaPhoto(media=method.photo, has_spoiler=method.has_spoiler),
            )
            return InputRichMessage(
                html=f'<img src="tg://photo?id=photo1"/>{caption_html}',
                media=[media_item],
                skip_entity_detection=False,
            )
        # For uploaded binary files (e.g. BufferedInputFile), let SendPhoto handle the
        # multipart upload directly with full classic HTML + custom emoji captions.
        return None

    # If a non-photo media method has HTML formatting in its caption, keep classic send
    # so tags like <b> and <code> are not stripped.
    raw_caption = getattr(method, "caption", None)
    if raw_caption and "<" in str(raw_caption) and ">" in str(raw_caption):
        return None

    caption_text = _plain_caption(raw_caption, currency)
    caption = RichBlockCaption(text=_premium_rich_text(caption_text)) if caption_text else None
    if isinstance(method, SendVideo):
        media = InputMediaVideo(
            media=method.video, thumbnail=method.thumbnail, cover=method.cover,
            start_timestamp=method.start_timestamp, width=method.width,
            height=method.height, duration=method.duration,
            supports_streaming=method.supports_streaming, has_spoiler=method.has_spoiler,
        )
        block = InputRichBlockVideo(video=media, caption=caption)
    elif isinstance(method, SendDocument):
        media = InputMediaDocument(
            media=method.document, thumbnail=method.thumbnail,
            disable_content_type_detection=method.disable_content_type_detection,
        )
        block = InputRichBlockDocument(document=media, caption=caption)
    elif isinstance(method, SendAudio):
        media = InputMediaAudio(
            media=method.audio, thumbnail=method.thumbnail, duration=method.duration,
            performer=method.performer, title=method.title,
        )
        block = InputRichBlockAudio(audio=media, caption=caption)
    elif isinstance(method, SendAnimation):
        media = InputMediaAnimation(
            media=method.animation, thumbnail=method.thumbnail, width=method.width,
            height=method.height, duration=method.duration, has_spoiler=method.has_spoiler,
        )
        block = InputRichBlockAnimation(animation=media, caption=caption)
    else:
        return None
    return InputRichMessage(blocks=[block])


def _common_rich_send(method: Any, rich_message: InputRichMessage, currency: str | None):
    return SendRichMessage(
        chat_id=method.chat_id,
        rich_message=rich_message,
        business_connection_id=method.business_connection_id,
        message_thread_id=method.message_thread_id,
        direct_messages_topic_id=method.direct_messages_topic_id,
        disable_notification=method.disable_notification,
        protect_content=method.protect_content,
        allow_paid_broadcast=method.allow_paid_broadcast,
        message_effect_id=method.message_effect_id,
        suggested_post_parameters=method.suggested_post_parameters,
        reply_parameters=method.reply_parameters,
        reply_markup=premium_markup(method.reply_markup, currency),
    )


def _upgrade_method(method: Any, currency: str | None = None) -> Any:
    # Callback toasts/alerts cannot carry Premium custom emoji (the Bot API
    # has no entities/custom-emoji field for answerCallbackQuery), so strip
    # emoji from their text instead of showing normal emoji next to the
    # Premium emojis used in messages and buttons.
    if isinstance(method, AnswerCallbackQuery):
        if method.text:
            cleaned = strip_plain_emoji(method.text)
            cleaned = re.sub(r"\s+", " ", cleaned).strip() or "Done"
            return method.model_copy(update={"text": cleaned})
        return method
    if SendRichMessage is None or InputRichMessage is None:
        updates: dict[str, Any] = {"reply_markup": premium_markup(getattr(method, "reply_markup", None), currency)}
        if isinstance(method, (SendMessage, EditMessageText)):
            updates["text"] = to_classic_html(
                convert_currency(method.text or "", currency),
                method.parse_mode,
            )
            updates["parse_mode"] = "HTML"
            return method.model_copy(update=updates)
        if isinstance(method, (*_MEDIA_METHODS, EditMessageCaption)):
            if getattr(method, "caption", None):
                updates["caption"] = to_classic_html(
                    convert_currency(method.caption or "", currency),
                    getattr(method, "parse_mode", "HTML"),
                )
                updates["parse_mode"] = "HTML"
            return method.model_copy(update=updates)
        return method
    if isinstance(method, SendMessage):
        return SendRichMessage(
            chat_id=method.chat_id,
            rich_message=InputRichMessage(
                html=to_rich_html(convert_currency(method.text, currency), method.parse_mode),
                skip_entity_detection=False,
            ),
            business_connection_id=method.business_connection_id,
            message_thread_id=method.message_thread_id,
            direct_messages_topic_id=method.direct_messages_topic_id,
            disable_notification=method.disable_notification,
            protect_content=method.protect_content,
            allow_paid_broadcast=method.allow_paid_broadcast,
            message_effect_id=method.message_effect_id,
            suggested_post_parameters=method.suggested_post_parameters,
            reply_parameters=method.reply_parameters,
            reply_markup=premium_markup(method.reply_markup, currency),
        )
    if isinstance(method, _MEDIA_METHODS):
        rich_media = _media_rich_message(method, currency)
        if rich_media is not None:
            return _common_rich_send(method, rich_media, currency)
        updates: dict[str, Any] = {"reply_markup": premium_markup(method.reply_markup, currency)}
        if getattr(method, "caption", None):
            updates["caption"] = to_classic_html(
                convert_currency(method.caption or "", currency),
                getattr(method, "parse_mode", "HTML"),
            )
        return method.model_copy(update=updates)
    if isinstance(method, EditMessageText):
        return method.model_copy(
            update={
                "text": None,
                "parse_mode": None,
                "entities": None,
                "rich_message": InputRichMessage(
                    html=to_rich_html(convert_currency(method.text or "", currency), method.parse_mode),
                    skip_entity_detection=False,
                ),
                "reply_markup": premium_markup(method.reply_markup, currency),
            }
        )
    if isinstance(method, EditMessageCaption):
        updates = {"reply_markup": premium_markup(method.reply_markup, currency)}
        if getattr(method, "caption", None):
            updates["caption"] = to_classic_html(
                convert_currency(method.caption or "", currency),
                getattr(method, "parse_mode", "HTML"),
            )
        return method.model_copy(update=updates)
    return method


class RichBot(Bot):
    """Aiogram Bot that transparently upgrades all normal text to Rich Messages."""

    currency_resolver = None

    def set_currency_resolver(self, resolver) -> None:
        """Set a lightweight ``chat_id -> INR|USD`` preference callback."""
        self.currency_resolver = resolver

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        currency = None
        if self.currency_resolver and isinstance(
            method, (SendMessage, EditMessageText, EditMessageCaption, *_MEDIA_METHODS)
        ):
            try:
                currency = self.currency_resolver(method.chat_id)
            except Exception:
                currency = None
        upgraded = _upgrade_method(method, currency)
        try:
            return await super().__call__(upgraded, request_timeout=request_timeout)
        except TelegramBadRequest as exc:
            if upgraded is method:
                raise
            if not isinstance(
                method, (SendMessage, EditMessageText, EditMessageCaption, *_MEDIA_METHODS)
            ):
                # No classic fallback exists for this method type (e.g.
                # answerCallbackQuery toasts); surface the original error.
                raise
            # Keep the complete old behavior as a compatibility/error fallback.
            log.warning("Rich message rejected; using classic fallback: %s", exc)
            updates: dict[str, Any] = {"reply_markup": premium_markup(method.reply_markup, currency)}
            if isinstance(method, (SendMessage, EditMessageText)):
                updates["text"] = to_classic_html(
                    convert_currency(method.text or "", currency),
                    method.parse_mode,
                )
            elif isinstance(method, (*_MEDIA_METHODS, EditMessageCaption)):
                updates["caption"] = to_classic_html(
                    convert_currency(getattr(method, "caption", "") or "", currency),
                    getattr(method, "parse_mode", "HTML"),
                )
            fallback = method.model_copy(update=updates)
            try:
                return await super().__call__(fallback, request_timeout=request_timeout)
            except TelegramBadRequest:
                # Last-resort compatibility for bots without Premium emoji access.
                if isinstance(method.reply_markup, InlineKeyboardMarkup):
                    plain = method.reply_markup.model_copy(update={
                        "inline_keyboard": [[
                            button.model_copy(update={"style": None, "icon_custom_emoji_id": None})
                            for button in row
                        ] for row in method.reply_markup.inline_keyboard]
                    })
                    updates["reply_markup"] = plain
                    fallback = method.model_copy(update=updates)
                return await super().__call__(fallback, request_timeout=request_timeout)
