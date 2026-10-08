# TGA Store | API

A production-ready Telegram marketplace bot for digital accounts, social-media services, automated payments, wallet management and reseller operations.

## Highlights

- **Native Telegram Rich Messages** (Bot API 10.3) with automatic classic-message fallback
- **Premium custom emoji** loaded from the bundled `EMOJI_IDS` packs
- English, Russian, Arabic (Saudi Arabia), Chinese and Indonesian onboarding
- User-selectable INR (`₹`) or USD (`$`) display currency
- Telegram and WhatsApp account marketplace, SMM reseller, product store and recharge flows
- Admin-controlled SMM and WhatsApp visibility switches
- Central `/admin` control center
- API Docs entry point (coming soon)
- MongoDB persistence and environment-only credentials

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

All API keys and credentials must stay in environment variables. Never commit a populated `.env` file.

## Admin

Use `/admin` for the consolidated dashboard. Existing specialist commands remain available from its sections so all current features continue to work.

## Rich-message compatibility

The bot uses `sendRichMessage` and rich edits by default. If Telegram rejects a rich request, it automatically retries through the original classic message path. Button colors are intentionally disabled; buttons use Premium custom-emoji icons where a matching bundled emoji ID exists.
