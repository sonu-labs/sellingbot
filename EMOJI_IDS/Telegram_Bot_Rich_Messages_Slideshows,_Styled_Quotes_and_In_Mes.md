# How a Telegram Bot Sends "Rich Messages" (Slideshows, Styled Quotes, In-Message Buttons): Bot API 10.1–10.3 Developer Guide

Yes. Your bot can send the card you described through the Bot API, but it uses a separate method: **`sendRichMessage`** (added in Bot API 10.1 on June 11, 2026), which takes an **`InputRichMessage`** object (Rich HTML, Rich Markdown or explicit blocks). [telegram](https://core.telegram.org/bots/api) [telegram](https://core.telegram.org/bots/api-changelog) It does not use `sendMessage` with `parse_mode`, and it does not use `sendMediaGroup`. The swipeable photo carousel with dots is a **`<tg-slideshow>`** block (`InputRichBlockSlideshow`). [telegram](https://core.telegram.org/bots/api) The buttons inside the card are **`<tg-button>` / `<tg-button-row>`** (`RichMessageButton` / `InputRichBlockButtons`, added in Bot API 10.3 on August 24, 2026). [newreleases +2](https://newreleases.io/project/github/avbor/docker-telegram-bot-api/release/v10.3) In pyTelegramBotAPI (telebot) you need **4.35.0+** for rich messages, **4.36.0+** for input blocks and media, and **4.37.0** for in-message buttons. [github +2](https://github.com/eternnoir/pyTelegramBotAPI/pull/2626)

## TL;DR
- **What you saw is a Rich Message.** Bots send it with `sendRichMessage(chat_id, rich_message=InputRichMessage(html=... | markdown=... | blocks=[...]))` and edit it with `editMessageText(rich_message=...)`. Any regular bot can use it in private chats, groups and channels; no Premium or Business account is needed. The *human* Rich Text Editor in the Telegram app is a separate feature — Telegram CEO Pavel Durov said on X (Jul 16, 2026) it is "Available only to Premium subscribers."
- **The carousel with "1 2" dots is `<tg-slideshow>`**, not an album. `sendMediaGroup` gives a grid album with no dots and no text blocks. Put the slideshow, `<h1>`–`<h6>` headings, `<blockquote>` / `<blockquote expandable>`, custom-emoji "icons" and a `<tg-button-row>` of styled buttons into a single HTML string.
- **Custom emoji (`tg-emoji`) and emoji icons on buttons only work if the bot owner has Telegram Premium** (or the bot has a username bought on Fragment). This rule dates from Bot API 9.4 (Feb 9, 2026). Colored buttons (`style`: `primary` / `success` / `danger`, plus `link` for in-message callback buttons) work for every bot.

## Key Findings

### 1. What "Rich Messages" are, and when they shipped
- **Official definition:** Telegram describes rich messages as being for "highly structured responses: reports, AI-streamed answers, documentation snippets, technical papers and similarly complex content." They support "both **Rich Markdown** and **Rich HTML**", and Rich Markdown "follows GitHub Flavored Markdown where possible." Supported features include headings, tables, media blocks, block quotes, pull quotes, collapsible details, footnotes, LaTeX, maps, collages and slideshows (core.telegram.org/bots/features#rich-messages). [github](https://github.com/NousResearch/hermes-agent/issues/45864) [telegram](https://core.telegram.org/bots/features)
- **Release timeline (from the official Bot API changelog):**

| Bot API | Date | Rich-message additions |
|---|---|---|
| 10.1 | June 11, 2026 | `sendRichMessage`, `sendRichMessageDraft` (streaming), `rich_message` param on `editMessageText`, `Message.rich_message`, `InputRichMessage`, `InputRichMessageContent` (inline/guest/Web App results), all `RichText*` / `RichBlock*` classes incl. `RichBlockSlideshow`, `RichBlockCollage`, `RichBlockBlockQuotation` [telegram +2](https://core.telegram.org/bots/api) |
| 10.2 | July 14, 2026 | `InputRichMessage.media` + `InputRichMessageMedia` (upload your own files referenced as `tg://photo?id=`), `InputRichMessage.blocks` + all `InputRichBlock*` classes (incl. `InputRichBlockSlideshow`, `InputRichBlockPhoto`) [newreleases +2](https://newreleases.io/project/github/avbor/docker-telegram-bot-api/release/v10.3) |
| 10.3 | August 24, 2026 | `RichMessageButton`, `RichTextButton` (button inside text), `RichBlockButtons` / `InputRichBlockButtons` (button row), `RichBlockExpandableBlockQuotation`, `RichBlockDocument`, `is_compact` tables, `tg://document?id=`, `ephemeral_message_parameters` on `sendRichMessage` [newreleases +2](https://newreleases.io/project/github/avbor/docker-telegram-bot-api/release/v10.3) |

- **Is it Bot API or app-only? Both.** Bots got it first in 10.1. Telegram's June 11 blog post "Smartwatch Apps, Rich Text for Bots, AI Guardians for Groups, and Much More" said bots can now send "up to **32,768** characters now, with a merciful Show More button after the first 8,000 or so." On July 14, Telegram released a visual Rich Text Editor so humans can write the same format. [telegram](https://telegram.org/blog/communities-editor-invisible-messages) Telegram's August 25 blog post "Welcome Messages, Buttons in Messages and Signed Gifts" introduced "Buttons in Messages" ("Developers can now put **multiple buttons** right **inside a message**"), listing "Put buttons inside text in groups and channels" and "Build automated flows with ephemeral messages."

### 2. Can a telebot bot send them? Yes. Method, parameters and types
- **Method:** `sendRichMessage`. Its parameters are `chat_id` (required), `rich_message: InputRichMessage` (required), `business_connection_id`, `message_thread_id`, `direct_messages_topic_id`, `disable_notification`, `protect_content`, `allow_paid_broadcast`, `message_effect_id` (private chats only), `suggested_post_parameters`, `reply_parameters`, `reply_markup` (a classic inline keyboard *under* the message is still allowed) and, since 10.3, `ephemeral_message_parameters`. [aiogram](https://docs.aiogram.dev/en/latest/api/methods/send_rich_message.html) [telegram](https://core.telegram.org/bots/api) If the message contains a media block, the bot must have permission to send that media type in the chat. [github](https://github.com/serejaris/telegram-skills/blob/main/reference/rich-messages-spec.md) [telegram](https://core.telegram.org/bots/api)
- **`InputRichMessage`:** "Exactly **one** of the fields *html*, *markdown*, or *blocks* must be used." Optional fields are `is_rtl`, `skip_entity_detection` and `media: Array of InputRichMessageMedia`. [readthedocs](https://pytba.readthedocs.io/en/latest/types.html) [telegram](https://core.telegram.org/bots/api)
- **Editing:** `editMessageText` with `rich_message=` (`text` and `rich_message` are mutually exclusive). [telegram](https://core.telegram.org/bots/api) [telegram](https://core.telegram.org/bots/api-changelog) **Streaming:** `sendRichMessageDraft(chat_id, draft_id, rich_message)` works in private chats only. A draft is a temporary preview, so send the final message with `sendRichMessage`. [github](https://github.com/serejaris/telegram-skills/blob/main/reference/rich-messages-spec.md) [telegram](https://core.telegram.org/bots/features)
- **Reading incoming rich messages:** use `Message.rich_message` (type `RichMessage` with `blocks: Array of RichBlock`). [github +2](https://github.com/serejaris/telegram-skills/blob/main/reference/rich-messages-spec.md) This is *not* `text` + `entities`, so rich messages do **not** use `MessageEntity` types. Formatting lives in `RichText*` objects (e.g., `RichTextBold`, `RichTextUnderline`, `RichTextUrl`, `RichTextCustomEmoji`, `RichTextButton`). [telegram](https://core.telegram.org/bots/api-changelog)
- **telebot versions (from the eternnoir/pyTelegramBotAPI releases page):** 4.35.0 = Bot API 10.1 (Jul 12, 2026), 4.36.0 = 10.2 (Jul 18, 2026), and PR #2626 (Bot API 10.3, bumping to **4.37.0**) adds `RichMessageButton`, `RichTextButton`, `RichBlockButtons`/`InputRichBlockButtons`, `RichBlockExpandableBlockQuotation`, `DisabledButton` and a `stopped_message_generation_handler`. [github +2](https://github.com/eternnoir/pyTelegramBotAPI/pull/2626) The telebot docs list `TeleBot.send_rich_message()`, `send_rich_message_draft()` and `types.InputRichMessage(html=None, markdown=None, is_rtl=None, skip_entity_detection=None, media=None, blocks=None)`. [readthedocs](https://pytba.readthedocs.io/en/latest/types.html) [readthedocs](https://pytba.readthedocs.io/en/latest/)

### 3. The media slider: `<tg-slideshow>`, not `sendMediaGroup`

| Need | Correct API | Notes |
|---|---|---|
| Swipeable photos with dot pager inside one card, alongside text/quotes/buttons | **`sendRichMessage`** with `<tg-slideshow>` (HTML) or `InputRichBlockSlideshow` (`type: "slideshow"`, `blocks: [InputRichBlockPhoto…]`, optional caption) [telegram](https://core.telegram.org/bots/api) [github](https://github.com/jaavid/social-media-manager/issues/75) | Bot API 10.1 (render) / 10.2 (input blocks + uploads) |
| Grid of photos with no text structure | `sendMediaGroup` (album) | Classic album; also supports Live Photos since 10.0 [telegram](https://core.telegram.org/bots/api-changelog) |
| Pay-to-unlock media | `sendPaidMedia` | Unrelated to carousels |
| Tiled image layout | `<tg-collage>` / `InputRichBlockCollage` [github](https://github.com/serejaris/telegram-skills/blob/main/reference/rich-messages-spec.md) | Rich-message sibling of slideshow |

- **Where media comes from:** either HTTP(S) URLs in `<img src>`, or files you upload. For uploads, list them in `InputRichMessage.media` as `InputRichMessageMedia(id="p1", media=InputMediaPhoto(...))` and reference them as `tg://photo?id=p1`. [readthedocs](https://pytba.readthedocs.io/en/latest/types.html) [telegram](https://core.telegram.org/bots/api) The `InputMediaPhoto.media` itself can be a `file_id`, a URL or `attach://name` for multipart upload. [telegram](https://core.telegram.org/bots/api) [github](https://github.com/openclaw/openclaw/issues/128992)
- **Limits:** the official core.telegram.org/bots/api "Rich Message Limits" section allows "Up to 500 blocks, including nested blocks, list items, ordered list items, table rows, quotation blocks, and details blocks", "Up to 50 media attachments in total" and "Up to 20 columns in a table", alongside the 32,768-character cap from Telegram's blog; a 16-nesting-level figure appears only in a third-party spec copy (serejaris/telegram-skills), so verify it yourself.

### 4. Blockquotes: two systems
- **Regular messages (`sendMessage` + `parse_mode`)** use MessageEntity types `"blockquote"` (Bot API 7.0, Dec 29, 2023) and `"expandable_blockquote"` (Bot API 7.4, May 28, 2024). [telegram](https://core.telegram.org/bots/api-changelog) In HTML these are `<blockquote>…</blockquote>` and `<blockquote expandable>…</blockquote>`. In MarkdownV2 a normal quote is lines starting with `>`, and an expandable one starts with `**>` and ends with `||`. [telegram](https://core.telegram.org/bots/api-changelog) [github](https://github.com/inyogeshwar/telegram-bot-framework/blob/main/docs/09-formatting.md)
- **Rich messages** use `<blockquote>…<cite>Author [github +2](https://github.com/serejaris/telegram-skills/blob/main/reference/rich-messages-spec.md) </cite></blockquote>` → `RichBlockBlockQuotation` (`type: "blockquote"`, nested `blocks`, optional `credit`). `<blockquote expandable>` → `RichBlockExpandableBlockQuotation` (10.3, `type: "expandable_blockquote"`). `<aside>` → a centered pull quote. `<details><summary>` → a collapsible block. In Rich Markdown, `> line` makes a quote. The colored left border is drawn by the client. **You cannot set the quote color through the API.** In regular chats it follows the sender's accent color.

### 5. Custom emoji and styled buttons as of October 2026
- **Custom emoji:** in regular messages use `<tg-emoji emoji-id="…">👍</tg-emoji>` or MarkdownV2 `![👍](tg://emoji?id=…)`. In rich messages use the same tag, `<img src="tg://emoji?id=…"/>` or `RichTextCustomEmoji` (`custom_emoji_id`, `alternative_text`). Since **Bot API 9.4 (Feb 9, 2026)** these are allowed "in messages directly sent by the bot to private, group and supergroup chats if the owner of the bot has a Telegram Premium subscription." [telegram](https://core.telegram.org/bots/api-changelog) Before that, only bots with Fragment-purchased usernames could use them. [github +2](https://github.com/serejaris/telegram-skills/blob/main/reference/rich-messages-spec.md) This is the usual way to get the "inline sections with icons" look.
- **Inline-keyboard buttons (under the message), Bot API 9.4:** `InlineKeyboardButton.style` takes `"danger"` (red), `"success"` (green) or `"primary"` (blue). `icon_custom_emoji_id` adds an emoji icon and follows the same Premium/Fragment rule. [telegram](https://core.telegram.org/bots/api) [telegram](https://core.telegram.org/bots/api-changelog) Bot API 10.3 added `DisabledButton` / `disabled` and `force_reply` on `InlineKeyboardMarkup`. [grammy +3](https://grammy.dev/ref/types/inlinekeyboardbutton/abstractinlinekeyboardbutton)
- **In-message buttons (inside the rich card), Bot API 10.3:** `RichMessageButton.style` takes "danger", "success", "primary", or "link" (the button is shown as a regular link without borders). Per the official API reference, "The style 'link' is allowed only for callback buttons." Button text may contain only plain text, `RichTextCustomEmoji` and `RichTextDateTime`. [telegram](https://core.telegram.org/bots/api) An `InputRichBlockButtons` row holds 1–8 buttons and has a horizontal-alignment option. [telegram](https://core.telegram.org/bots/api) Buttons can also sit inline inside text as `RichTextButton`. [github +3](https://github.com/aiogram/aiogram/issues/1890)

### 6. Bot API releases in 2025–2026 (developer highlights)

| Version | Date | UI/messaging-relevant highlights |
|---|---|---|
| 8.2 | Jan 1, 2025 | Third-party verification (`verifyUser`/`verifyChat`), gift upgrades [telegram](https://core.telegram.org/bots/api-changelog) |
| 8.3 | Feb 12, 2025 | Video `cover` + `start_timestamp`; reactions on most service messages; gifts to channels [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.0 | Apr 11, 2025 | Business-account management (stories, gifts, Stars), Mini App `DeviceStorage`/`SecureStorage`, `giftPremiumSubscription` [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.1 | Jul 3, 2025 | Checklists (`sendChecklist`), `getMyStarBalance`, 12 poll options, Mini App `hideKeyboard` [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.2 | Aug 15, 2025 | Channel Direct Messages, Suggested Posts (`approveSuggestedPost`) [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.3 | Dec 31, 2025 | Topics in private chats, **`sendMessageDraft` streaming**, `message_effect_id` on forward/copy, gift colors [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.4 | Feb 9, 2026 | **Custom emoji for Premium-owned bots, button `style` + `icon_custom_emoji_id`**, `setMyProfilePhoto` [telegram](https://core.telegram.org/bots/api-changelog) [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.5 | Mar 1, 2026 | `date_time` entity, drafts for all bots, member tags, Mini App `BottomButton.iconCustomEmojiId` [telegram](https://core.telegram.org/bots/api-changelog) |
| 9.6 | Apr 3, 2026 | Managed bots, quizzes with multiple correct answers (`correct_option_ids` — breaking), poll revoting/descriptions [telegram](https://core.telegram.org/bots/api-changelog) |
| 10.0 | May 8, 2026 | Guest mode, bot-to-bot messaging, Live Photos (`sendLivePhoto`), media in polls, `deleteMessageReaction` / `deleteAllMessageReactions` [telegram](https://core.telegram.org/bots/api-changelog) |
| 10.1 | Jun 11, 2026 | **Rich Messages**, join-request queries, link media in polls [telegram](https://core.telegram.org/bots/api-changelog) |
| 10.2 | Jul 14, 2026 | **Rich input blocks + media uploads**, Ephemeral Messages, Communities, Mini App cross-origin hardening (auto-enabled Jul 20, 2026) [telegram](https://core.telegram.org/bots/api-changelog) |
| 10.3 | Aug 24, 2026 | **In-message buttons**, expandable rich quotes, document blocks, `ephemeral_message_parameters` (breaking), draft Stop button (`can_stop`, `MessageGenerationStopped`) [github +2](https://github.com/yabx-net/telegram/releases/tag/10.3.0) |

### 7. Do you need Premium or Business?
- **Sending rich messages:** no. Any bot can use `sendRichMessage`. The only gate is for business connections: a bot "can send rich messages on behalf of a business account only if the corresponding user can send rich messages." [telegram](https://core.telegram.org/bots/api)
- **Custom emoji / emoji button icons:** the bot *owner* needs Premium, or the bot needs a Fragment username. [gramio](https://gramio.dev/telegram/types/inlinekeyboardbutton) If neither applies, use regular Unicode emoji as icons.
- **The human Rich Text Editor:** Pavel Durov stated on X (Jul 16, 2026) that it is "Available only to Premium subscribers," and FoneArena reported it rolls out "with Telegram v12.9.0 and later." That limits *people*, not bots.

## Details: implementation in telebot

The example below builds the card from your screenshot: a heading with an underlined link, a two-photo slideshow, styled quotes, emoji-icon sections and a row of styled buttons. It uses Rich HTML because that is the most compact format.

```python
import telebot
from telebot import types

bot = telebot.TeleBot("TOKEN")  # pyTelegramBotAPI >= 4.37.0 for in-message buttons

HTML = """
<h2>My Bot — <a href="https://t.me/MyBot"><u>What's new</u></a></h2>

<tg-slideshow>
  <img src="https://example.com/slide1.jpg"/>
  <img src="tg://photo?id=p2"/>
  <figcaption>Swipe to see more</figcaption>
</tg-slideshow>

<blockquote>Version 2.0 is live!<cite>Dev team</cite></blockquote>
<blockquote expandable>Full changelog… (collapsed by default)</blockquote>

<p><tg-emoji emoji-id="5368324170671202286">⚡</tg-emoji> <b>Faster replies</b> — 2× speed</p>
<p>🛡 <b>Group tools</b> — new moderation filters</p>

<tg-button-row>
  <tg-button type="callback_data" data="updated" style="primary">Updated!!</tg-button>
  <tg-button type="url" url="https://t.me/MyGroup" style="success">Group</tg-button>
  <tg-button type="url" url="https://t.me/share/url?url=https://t.me/MyBot">Share Bot</tg-button>
  <tg-button type="callback_data" data="help" style="danger">Help!</tg-button>
</tg-button-row>
"""

rich = types.InputRichMessage(
    html=HTML,
    media=[types.InputRichMessageMedia(
        id="p2",
        media=types.InputMediaPhoto(open("slide2.jpg", "rb")))],
)
bot.send_rich_message(chat_id=CHAT_ID, rich_message=rich)

@bot.callback_query_handler(func=lambda c: c.data in ("updated", "help"))
def on_btn(call):
    bot.answer_callback_query(call.id, "OK")
```

**Implementation notes:**
- **Verify the `<tg-button>` attribute names before shipping.** The style values (`danger`, `success`, `primary`, `link`) and the 1–8 buttons-per-row limit are confirmed in the official API reference. The `type="url"` / `type="callback_data"` + `data=` spelling, however, comes from third-party libraries (richgram, tg_translator_v2, SHOREKEEPER bots), and one of those projects says "only `type=\"callback_data\"` is a confirmed spelling." [github +2](https://github.com/TeamZyro/SHOREKEEPERWAIFU/pull/10) Check the "Rich HTML style" section of core.telegram.org/bots/api, or send a test to @RichTextDemoBot, Telegram's official interactive demo. [telegram](https://core.telegram.org/bots/features)
- **Fallback without library support:** if your telebot version lacks the helpers, POST the same JSON (`{"chat_id":…, "rich_message":{"html":…}}`) to `https://api.telegram.org/bot<TOKEN>/sendRichMessage` with `requests`. For local uploads, use multipart with `attach://` names. [pypi](https://pypi.org/project/tg-rich-converter/) [github](https://github.com/rockneurotiko/ex_gram/pull/225) Several libraries had serialization bugs here (for example, Rust's frankenstein needed a fix so the nested `InputMedia*` object carries its own `"type"` field), so if you build blocks by hand, include `"type":"photo"` on nested media. [pypi](https://pypi.org/project/tg-rich-converter/) [github](https://github.com/ayrat555/frankenstein/pull/340)
- **Upgrade before you deploy.** Incoming updates that contain new rich types can break older parsers. [github](https://github.com/rockneurotiko/ex_gram/pull/224) aiogram issue #1890 (opened Aug 25, 2026, labelled "confirmed") reports: "aiogram's RichText union does not contain RichTextButton, so the complete getUpdates response fails validation. The polling offset never advances and aiogram repeatedly downloads the same batch."
- **Plan for client lag.** One integration (Hermes Agent) reported that rich messages showed "This message is currently not supported on Telegram Web," [github](https://github.com/NousResearch/hermes-agent/pull/45584) and Telegram's Aug 25 blog added an upgrade placeholder for outdated apps. [telegram](https://telegram.org/blog/welcome-messages-buttons-TG-13) Keep a `sendMessage` + HTML fallback for important flows. [github](https://github.com/librefang/librefang/pull/8003)

## Recommendations
1. **Use `sendRichMessage` + Rich HTML** for the card, and upgrade telebot to **4.37.0** (the PR #2626 release) so you get `RichMessageButton` and the 10.3 request format. If 4.37.0 is not on PyPI yet, use raw HTTP for this one call.
2. **Use `<tg-slideshow>` for the carousel.** Do not try to fake it with `sendMediaGroup` or by editing the media of a single photo.
3. **Pick a button placement.** If you need in-card buttons, use `<tg-button-row>`. If you need maximum client compatibility, use a classic `reply_markup` inline keyboard with `style`, which has been supported since 9.4 and is widely implemented.
4. **Gate custom emoji on owner Premium.** Without it, Unicode emoji give a similar "icon" look at no cost.
5. **Use expandable quotes for long text.** Use `<blockquote expandable>` inside rich messages, and `**>…||` (MarkdownV2) or `<blockquote expandable>` (HTML parse mode) in regular messages.

## Caveats
- The full official API page could not be fetched in one piece. The `RichMessageButton` field names other than `text`/`style`, the exact `<tg-button>` attributes and the `InputRichBlockPhoto` field list were confirmed only through partial snippets and third-party library code.
- The Premium-only status of the editor is confirmed directly by Telegram CEO Pavel Durov (X post, Jul 16, 2026). Telegram's blog confirms the editor and the 32,768-character limit.
- Rich-message rendering depends on the client version. Test on iOS, Android, Desktop and Web.