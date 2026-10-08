"""Rich-message transport and premium custom-emoji support for TGA Store | API.

Every normal outgoing text message is upgraded to Bot API 10.3 ``sendRichMessage``.
If Telegram rejects rich messages (for example, an outdated self-hosted Bot API), the
original classic message is sent automatically.  This keeps every existing bot flow
working while giving supported clients native rich cards and Premium emoji.
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
    EditMessageText, SendAnimation, SendAudio, SendDocument, SendMessage,
    SendPhoto, SendRichMessage, SendVideo, SendVoice,
)
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation,
    InputMediaAudio, InputMediaDocument, InputMediaPhoto, InputMediaVideo,
    InputRichBlockAnimation, InputRichBlockAudio, InputRichBlockDocument,
    InputRichBlockPhoto, InputRichBlockVideo, InputRichMessage, RichBlockCaption,
    RichTextCustomEmoji,
)

log = logging.getLogger("tga.rich")

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
            emoji_id = str(item.get("custom_emoji_id") or item.get("id") or "")
            if emoji and emoji_id:
                # First pack wins, so the icon packs keep priority.
                result.setdefault(emoji, emoji_id)
    return result


EMOJI_IDS = _load_emoji_ids()
_EMOJI_PATTERN = re.compile(
    "|".join(re.escape(item) for item in sorted(EMOJI_IDS, key=len, reverse=True))
) if EMOJI_IDS else re.compile(r"(?!x)x")
_TAG_SPLIT = re.compile(r"(<[^>]+>)")
_INR_AMOUNT = re.compile(r"₹\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def convert_currency(text: str, currency: str | None) -> str:
    """Convert INR amounts in existing flows to the user's selected display currency."""
    if currency != "USD":
        return text

    def replace(match: re.Match) -> str:
        try:
            inr = float(match.group(1).replace(",", ""))
            return f"${inr / 95.0:,.2f}"
        except ValueError:
            return match.group(0)

    return _INR_AMOUNT.sub(replace, text)


def emoji_id(emoji: str) -> str | None:
    """Return a Premium custom-emoji id for a Unicode emoji, if supplied."""
    return EMOJI_IDS.get(emoji) or EMOJI_IDS.get(emoji.rstrip("\ufe0f"))


def _premium_text(text: str) -> str:
    """Replace visible Unicode emoji with Rich HTML ``tg-emoji`` elements."""
    parts = _TAG_SPLIT.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _EMOJI_PATTERN.sub(
            lambda match: (
                f'<tg-emoji emoji-id="{EMOJI_IDS[match.group(0)]}">'
                f"{match.group(0)}</tg-emoji>"
            ),
            parts[index],
        )
    return "".join(parts)


def to_rich_html(text: str, parse_mode: Any = "HTML") -> str:
    """Convert classic Telegram HTML/plain text into valid Rich HTML.

    Rich HTML follows browser whitespace rules, unlike ``sendMessage``. Therefore,
    line breaks are explicitly converted to ``<br>``. Existing code/pre blocks are
    left untouched so their whitespace remains meaningful.
    """
    source = str(text or "")
    mode = str(parse_mode or "").upper()
    # Aiogram's Default('parse_mode') means the configured HTML default.
    is_html = "HTML" in mode or parse_mode is not None and "DEFAULT" in mode
    if not is_html:
        source = html.escape(source)

    source = re.sub(
        r'<span\s+class=["\']tg-spoiler["\']\s*>',
        "<tg-spoiler>",
        source,
        flags=re.IGNORECASE,
    )
    source = re.sub(r"</span>", "</tg-spoiler>", source, flags=re.IGNORECASE)

    # Preserve literal newlines in preformatted blocks; use explicit breaks elsewhere.
    chunks = re.split(r"(<pre(?:\s[^>]*)?>.*?</pre>)", source, flags=re.I | re.S)
    for idx in range(0, len(chunks), 2):
        chunks[idx] = chunks[idx].replace("\r\n", "\n").replace("\r", "\n")
        chunks[idx] = chunks[idx].replace("\n", "<br>")
    return _premium_text("".join(chunks))


def premium_button(button: InlineKeyboardButton, currency: str | None = None) -> InlineKeyboardButton:
    """Use a Premium icon on a button and deliberately remove color styling."""
    updates: dict[str, Any] = {
        "style": None,
        "text": convert_currency(button.text, currency),
    }
    if not button.icon_custom_emoji_id:
        for symbol in sorted(EMOJI_IDS, key=len, reverse=True):
            if button.text.startswith(symbol):
                updates["icon_custom_emoji_id"] = EMOJI_IDS[symbol]
                cleaned = button.text[len(symbol):].lstrip()
                updates["text"] = convert_currency(cleaned or button.text, currency)
                break
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


_MEDIA_METHODS = (SendPhoto, SendVideo, SendDocument, SendAudio, SendAnimation, SendVoice)


def _plain_caption(text: str | None, currency: str | None) -> str | None:
    if not text:
        return None
    converted = convert_currency(text, currency)
    converted = re.sub(r"<br\s*/?>", "\n", converted, flags=re.I)
    converted = re.sub(r"<[^>]+>", "", converted)
    return html.unescape(converted)


def _premium_rich_text(text: str):
    parts = []
    position = 0
    for match in _EMOJI_PATTERN.finditer(text):
        if match.start() > position:
            parts.append(text[position:match.start()])
        symbol = match.group(0)
        parts.append(RichTextCustomEmoji(
            custom_emoji_id=EMOJI_IDS[symbol], alternative_text=symbol
        ))
        position = match.end()
    if position < len(text):
        parts.append(text[position:])
    return parts or text


def _media_rich_message(method: Any, currency: str | None) -> InputRichMessage | None:
    """Build an explicit native Rich Message media block for media sends."""
    caption_text = _plain_caption(getattr(method, "caption", None), currency)
    caption = RichBlockCaption(text=_premium_rich_text(caption_text)) if caption_text else None
    if isinstance(method, SendPhoto):
        media = InputMediaPhoto(media=method.photo, has_spoiler=method.has_spoiler)
        block = InputRichBlockPhoto(photo=media, caption=caption)
    elif isinstance(method, SendVideo):
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
        # InputRichBlockVoiceNote currently accepts only remote/file-id strings,
        # while SendVoice also accepts uploads. Keep its safe classic fallback.
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
    return method


class RichBot(Bot):
    """Aiogram Bot that transparently upgrades all normal text to Rich Messages."""

    currency_resolver = None

    def set_currency_resolver(self, resolver) -> None:
        """Set a lightweight ``chat_id -> INR|USD`` preference callback."""
        self.currency_resolver = resolver

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        currency = None
        if self.currency_resolver and isinstance(method, (SendMessage, EditMessageText, *_MEDIA_METHODS)):
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
            # Keep the complete old behavior as a compatibility/error fallback.
            log.warning("Rich message rejected; using classic fallback: %s", exc)
            updates = {"reply_markup": premium_markup(method.reply_markup, currency)}
            if isinstance(method, (SendMessage, EditMessageText)):
                updates["text"] = convert_currency(method.text or "", currency)
            elif isinstance(method, _MEDIA_METHODS):
                updates["caption"] = convert_currency(method.caption or "", currency)
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
