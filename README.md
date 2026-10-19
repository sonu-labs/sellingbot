# TGA Store | API

A production-ready Telegram marketplace bot for digital accounts, social-media services, automated payments, wallet management and reseller operations.

## Highlights

- **Native Telegram Rich Messages** (Bot API 10.3) for text, edits and media blocks, with automatic classic fallback
- **Premium custom emoji only** in message text, media captions and buttons, loaded from the bundled `EMOJI_IDS` packs — emojis without a Premium ID (flags, etc.) are automatically replaced with a Premium fallback, and callback toasts (which cannot carry custom emoji) are shown without emoji. The one exception is **country lists**: country buttons are plain country names with no per-country Premium icon (every other button keeps its Premium icon)
- Official bot username: **@TGAStoreAPIBot**
- **$1 = ₹100** everywhere: Server 2 / Server 3 prices, wallet USD display, display-currency conversion and USDT/crypto deposits all use the same 100 rate
- **Referral Commission** (not a deposit commission): when a referred user deposits, the *referrer* earns the admin-set `refer_commission_percent` % of that deposit; the depositor receives only their own deposit. Set it from **Admin → Refer & Commission** or `/setrefercomm <percent>`
- English, Russian, Arabic (Saudi Arabia), Chinese and Indonesian onboarding
- User-selectable INR (`₹`) or USD (`$`) display currency
- Telegram and WhatsApp account marketplace, SMM reseller, product store and recharge flows
- Admin-controlled SMM and WhatsApp visibility switches
- SMM provider keys managed from `/smmadmin` (private-chat entry; keys are not shown in panel settings)
- Manual SMM service setup for the Telegram and Instagram customer categories, with INR pricing per 1,000 and minimum/maximum quantities
- Central `/admin` control center with live analytics and bot/SMM-monitor health checks
- API Docs entry point (coming soon)
- MongoDB persistence, environment-sourced app credentials, and database-stored SMM provider keys

## Setup

1. Install Python 3.11+ and dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy `example.env` to `.env` and fill in the required values.
3. Start the bot:
   ```bash
   python bot.py
   ```

### Required environment variables

- `BOT_TOKEN` — token from BotFather
- `ADMIN_IDS` — comma-separated Telegram user IDs
- `MONGO_URI` — MongoDB connection string
- `API_ID` and `API_HASH` — Telegram application credentials for Telethon flows
- `TGLION_API_KEY`, `TGLION_ID` and `TGPVA_API_KEY` — Server 2 provider credentials (previously hard-coded in source; rotate any key that was ever committed)

Keep production credentials out of source control. Existing integrations read credentials from environment variables; SMM provider keys are stored in the bot database after being entered through `/smmadmin`. Never commit a populated `.env` file.

## Admin

Use `/admin` for the consolidated dashboard. Existing specialist commands remain available from its sections so all current features continue to work.

### SMM provider setup

1. Open `/smmadmin` in a private chat.
2. Choose **Add Nice Panel** to use `https://nicesmmpanel.com/api/v2`, or **Add Other Panel** for another HTTPS API v2 provider.
3. Enter the provider API key in the bot chat. The key message is deleted after saving; keys are never displayed in panel settings.
4. Set the panel's rate currency (USD or INR). The provider currency is used only for balance checks and reference; customer prices are the INR rate entered manually for each service.
5. Add services one at a time from **Manage Telegram / Instagram**: choose the category and provider panel, then enter the provider service ID, display name, an optional description, INR rate per 1,000, and minimum/maximum quantity. The description is shown to users in a blockquote on the purchase page and can hold instructions, warnings, or delivery notes; edit or remove it later with **Edit Description**. Telegram and Instagram are the only customer-facing SMM categories.

Provider credentials are saved in the bot database and should be protected with the same care as other production secrets.

## Rich-message compatibility

The bot uses `sendRichMessage` and rich edits by default. If Telegram rejects a rich request, it automatically retries through the original classic message path. Button colors are intentionally disabled; buttons use Premium custom-emoji icons where a matching bundled emoji ID exists.

### Buy OTPs (All Apps)

The main menu now includes **Buy OTPs (All Apps)** above Social Media Services.
It loads services and operator-specific countries from TemporaSMS (five-minute
catalog cache), promotes Telegram/Instagram/Facebook/WhatsApp/Google/TikTok,
and paginates the complete catalog. Operators are 1, 2, 3, 4, 7, 8, 9 and 10.
Prices and stock are fetched live before a two-minute purchase confirmation.

Configure `TEMPORASMS_API_KEY` in the deployment environment, never in source.
`OTP_MARKUP_PERCENT` defaults to **0** (no profit markup). Pricing assumes the
provider account returns **USD** prices and converts at the project's fixed
₹100/USD; verify the account currency and set a margin before launch.
Existing wallet balances are used; no new payment gateway is required.

Orders live in MongoDB `otp_orders`, navigation/quotes in `otp_views` (24-hour
TTL), and idempotent debit/refund markers in the user document. A persistent
background checker resumes waiting orders after restarts, checks approximately
every 30 seconds (longer with many active orders), and requests cancellation after
20 minutes. Refunds require provider cancellation confirmation. OTPs are private,
not sent to public sales channels. My OTP Orders allows manual checking/cancelling
and paginated history.

**Operational safety:** Do not retry ambiguous allocations automatically. Orders
in `allocating` or `review` need operator reconciliation against the provider
account: inspect `otp_orders` and the matching user's `otp_debits`/`otp_refunds`.
A provider timeout can mean a number was allocated even without a response.
Do not manually refund before confirming that no paid activation remains.
Single-document wallet ledger updates are atomic; this is not a multi-document
transaction system. Run one bot polling worker. At high volume, migrate ledger
markers to a transactional ledger and use distributed polling locks.

The provider's expanded API reference and a funded account were not available
for a live end-to-end transaction during implementation. Before production,
verify actual `getServices`, `getCountries`, `getPricesV3` response shapes,
`getStatus` / `setStatus(status=8)` semantics, cancellation timing and currency
with a low-cost controlled activation. Unknown price shapes/absent stock fail
closed rather than charging guessed prices.
