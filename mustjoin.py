"""Mandatory-channel membership gate for TGA Store | API."""

import os
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import MUST_JOIN_CHANNEL, SUPPORT_USERNAME, UPDATES_CHANNEL

PRIVATE_CHANNEL_ID = int(MUST_JOIN_CHANNEL)
SECOND_CHANNEL_ID = int(os.getenv("MUST_JOIN_CHANNEL_2", "-1004437680505"))
PRIVATE_CHANNEL_LINK = os.getenv("MUST_JOIN_LINK", "https://t.me/twalllogs")

WELCOME_TEXT = (
    "<h2>TGA Store | API</h2>"
    "<blockquote>Membership required</blockquote>"
    "<p>Please join our official channels to use the bot, then press "
    "<b>Verify Join</b>.</p>"
)


async def check_join(client, event) -> bool:
    """Check both required channels for a Message or CallbackQuery."""
    user_id = event.from_user.id
    try:
        member1 = await client.get_chat_member(SECOND_CHANNEL_ID, user_id)
        member2 = await client.get_chat_member(PRIVATE_CHANNEL_ID, user_id)
        if member1.status not in {"left", "kicked"} and member2.status not in {"left", "kicked"}:
            return True
    except Exception:
        # Missing bot permissions and unavailable chats are treated as not joined.
        pass
    await send_join_message(event.message if isinstance(event, types.CallbackQuery) else event)
    return False


async def send_join_message(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.row(
        types.InlineKeyboardButton(text="📢 Updates", url=f"https://t.me/{UPDATES_CHANNEL}"),
        types.InlineKeyboardButton(text="💌 Community", url=PRIVATE_CHANNEL_LINK),
    )
    kb.row(
        types.InlineKeyboardButton(text="👨‍💻 Support", url=f"https://t.me/{SUPPORT_USERNAME}")
    )
    kb.row(types.InlineKeyboardButton(text="✅ Verify Join", callback_data="back_main"))
    await message.answer(WELCOME_TEXT, reply_markup=kb.as_markup(), parse_mode="HTML")
