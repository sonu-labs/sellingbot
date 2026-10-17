"""Mandatory-channel membership gate and dynamic channel manager for TGA Store | API."""

from __future__ import annotations

import logging
import os
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import MUST_JOIN_CHANNEL, SUPPORT_USERNAME, UPDATES_CHANNEL
from db import settings_col
from rich_messages import premium_button, tg_icon

log = logging.getLogger("tga.mustjoin")

PRIVATE_CHANNEL_ID = int(MUST_JOIN_CHANNEL)
SECOND_CHANNEL_ID = int(os.getenv("MUST_JOIN_CHANNEL_2", "-1004437680505"))
PRIVATE_CHANNEL_LINK = os.getenv("MUST_JOIN_LINK", "https://t.me/twalllogs")

DEFAULT_FORCE_SUB_CHANNELS = [
    {
        "name": "Updates",
        "id": SECOND_CHANNEL_ID,
        "link": f"https://t.me/{str(UPDATES_CHANNEL).lstrip('@')}",
    },
    {
        "name": "Community",
        "id": PRIVATE_CHANNEL_ID,
        "link": PRIVATE_CHANNEL_LINK,
    },
]


def _get_force_sub_doc() -> dict:
    try:
        doc = settings_col.find_one({"_id": "force_sub_config"})
    except Exception:
        doc = None
    if not doc:
        doc = {
            "_id": "force_sub_config",
            "enabled": True,
            "channels": list(DEFAULT_FORCE_SUB_CHANNELS),
        }
        try:
            settings_col.update_one(
                {"_id": "force_sub_config"},
                {"$setOnInsert": doc},
                upsert=True,
            )
        except Exception:
            pass
    return doc


def is_force_sub_enabled() -> bool:
    doc = _get_force_sub_doc()
    return bool(doc.get("enabled", True))


def toggle_force_sub() -> bool:
    doc = _get_force_sub_doc()
    new_state = not bool(doc.get("enabled", True))
    settings_col.update_one(
        {"_id": "force_sub_config"},
        {"$set": {"enabled": new_state}},
        upsert=True,
    )
    return new_state


def get_force_sub_channels() -> list[dict]:
    doc = _get_force_sub_doc()
    channels = doc.get("channels")
    if isinstance(channels, list):
        return channels
    return list(DEFAULT_FORCE_SUB_CHANNELS)


def add_force_sub_channel(name: str, chat_id: int | str, link: str) -> list[dict]:
    channels = get_force_sub_channels()
    raw_id = str(chat_id).strip()
    if raw_id.lstrip("-").isdigit():
        norm_id: int | str = int(raw_id)
    else:
        norm_id = raw_id if raw_id.startswith("@") else f"@{raw_id}"
    clean_link = link.strip()
    if clean_link.startswith("t.me/"):
        clean_link = f"https://{clean_link}"
    elif clean_link.startswith("@"):
        clean_link = f"https://t.me/{clean_link.lstrip('@')}"

    updated = [c for c in channels if str(c.get("id")) != str(norm_id)]
    updated.append({
        "name": name.strip() or "Channel",
        "id": norm_id,
        "link": clean_link,
    })
    settings_col.update_one(
        {"_id": "force_sub_config"},
        {"$set": {"channels": updated, "enabled": True}},
        upsert=True,
    )
    return updated


def remove_force_sub_channel(index: int) -> tuple[bool, dict | None]:
    channels = get_force_sub_channels()
    if index < 0 or index >= len(channels):
        return False, None
    removed = channels.pop(index)
    settings_col.update_one(
        {"_id": "force_sub_config"},
        {"$set": {"channels": channels}},
        upsert=True,
    )
    return True, removed


async def check_join(client, event) -> bool:
    """Check all configured force-join channels for a Message, CallbackQuery, or user_id.

    Membership is only enforced when the bot can actually verify it. If a channel
    cannot be checked (bot is not an admin there, chat removed, network hiccup…),
    that channel is skipped instead of permanently locking every user out — which
    would make buttons such as "Back" / "Verify Join" loop forever.
    """
    if not is_force_sub_enabled():
        return True
    channels = get_force_sub_channels()
    if not channels:
        return True

    user_id = event if isinstance(event, int) else event.from_user.id
    all_joined = True
    for ch in channels:
        try:
            member = await client.get_chat_member(ch["id"], user_id)
        except Exception as exc:
            desc = str(exc).lower()
            if any(
                marker in desc
                for marker in ("user not found", "user not participant", "user_not_participant")
            ):
                # Telegram explicitly says this user is not a member.
                all_joined = False
                break
            # The bot cannot verify this channel (missing admin rights, unknown
            # chat, transient failure) — do not punish the user for it.
            log.warning("force-sub: cannot verify membership for %s: %s", ch.get("id"), exc)
            continue
        status = getattr(member, "status", None)
        if status in {"left", "kicked"}:
            all_joined = False
            break
        if status == "restricted" and not getattr(member, "is_member", True):
            all_joined = False
            break

    if all_joined:
        return True

    if not isinstance(event, int):
        target_msg = event.message if isinstance(event, types.CallbackQuery) else event
        if target_msg is not None:
            try:
                await send_join_message(target_msg)
            except Exception as exc:
                log.warning("force-sub: could not send join prompt: %s", exc)
    return False


async def send_join_message(message: types.Message):
    channels = get_force_sub_channels()
    kb = InlineKeyboardBuilder()

    row_btns = []
    for idx, ch in enumerate(channels):
        icon = "📢" if idx % 2 == 0 else "💌"
        row_btns.append(
            premium_button(
                text=f"{icon} {ch.get('name', 'Channel')}",
                url=ch.get("link") or f"https://t.me/{str(UPDATES_CHANNEL).lstrip('@')}",
            )
        )
        if len(row_btns) == 2:
            kb.row(*row_btns)
            row_btns = []
    if row_btns:
        kb.row(*row_btns)

    if SUPPORT_USERNAME:
        kb.row(
            premium_button(
                text="👨‍💻 Support",
                url=f"https://t.me/{str(SUPPORT_USERNAME).lstrip('@')}",
            )
        )
    kb.row(
        premium_button(
            text="✅ Verify Join",
            callback_data="back_main",
        )
    )

    await message.answer(
        f"<h2>{tg_icon()} TGA Store | API — Membership Required</h2>"
        "<blockquote>🔒 <b>Channel Verification</b><br>"
        "Please join our official channels below to access the bot and receive live stock &amp; gateway updates, "
        "then tap <b>Verify Join</b>.</blockquote>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )
