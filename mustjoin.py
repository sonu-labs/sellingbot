from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import MUST_JOIN_CHANNEL

# Private channel details
PRIVATE_CHANNEL_ID =  -1003394213814
PRIVATE_CHANNEL_LINK = "https://t.me/twalllogs"
PRIVATE_CHANNEL_ID2 =  -1004437680505

BOTUSER = "TGAStoreBot"
# Welcome text with HTML formatting
WELCOME_TEXT = ("❌ 𝖸𝗈𝗎 𝗆𝗎𝗌𝗍 𝗌𝗎𝖻𝗌𝖼𝗋𝗂𝖻𝖾 𝗍𝗈 𝗍𝗁𝖾 𝗈𝖿𝖿𝗂𝖼𝗂𝖺𝗅 𝖻𝗈𝗍 𝖼𝗁𝖺𝗇𝗇𝖾𝗅 𝗍𝗈 𝗎𝗌𝖾 𝗍𝗁𝖾 𝖻𝗈𝗍...\n\n<blockquote>- <b>𝖢𝗁𝖺𝗇𝗇𝖾𝗅</b> - @twalllogs</blockquote>\n\n<b>• Press Verify Button below to check</b>")



async def check_join(client, message: types.Message):
    """
    Check if the user has joined both required channels.
    If not, send the join message and return False.
    """
    try:
        # Public channel check
        member1 = await client.get_chat_member(PRIVATE_CHANNEL_ID2, message.from_user.id)

        # Private channel check
        member2 = await client.get_chat_member(PRIVATE_CHANNEL_ID, message.from_user.id)

        if (member1.status in ["left", "kicked"]) or (member2.status in ["left", "kicked"]):
            await send_join_message(message)
            return False

        return True
    except Exception:
        await send_join_message(message)
        return False


async def send_join_message(message: types.Message):
    """
    Send a message asking the user to join the required channels,
    with inline buttons for both channels in one row, a Developer button, and Verify below.
    """
    kb = InlineKeyboardBuilder()

    # First row: Updates & Support channels
    kb.row(
        types.InlineKeyboardButton(text="📢 𝖴𝗉𝖽𝖺𝗍𝖾𝗌", url=f"https://t.me/tgaforu"),
        types.InlineKeyboardButton(text="💌 𝖲𝗎𝗉𝗉𝗈𝗋𝗍", url=PRIVATE_CHANNEL_LINK)
    )
    
    # Second row: Developer channel
    kb.row(
        types.InlineKeyboardButton(text="👨‍💻 𝖣𝖾𝗏𝖾𝗅𝗈𝗉𝖾𝗋", url="https://t.me/TechnoWorldOfficial")
    )
    
    # Third row: Verify Join
    kb.row(
         types.InlineKeyboardButton(text="Verify Join ☑️", callback_data="back_main")
    )

    # Note: Don't forget to send the message with this markup!
    # await message.answer("Please join our channels:", reply_markup=kb.as_markup())

        

    

    await message.answer(
        WELCOME_TEXT,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
