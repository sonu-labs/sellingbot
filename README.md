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
- Full **OTP Services admin panel** (`/otpadmin` or Admin → 📲 OTP Services) for the TemporaSMS All-Apps storefront: runtime API-key management, service/operator switches, markup control, provider tools and an orders/reconciliation dashboard
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
It loads services and operator-specific countries from TemporaSMS **once** and
reuses them: catalog reads (`getServices`, `getCountries`, `getOperators`) are
cached in memory *and* in the MongoDB `otp_catalog` collection for one hour
(`CATALOG_TTL`), warmed at startup, and reused after a restart — so browsing
apps, lines, countries, pages and search results makes **zero** provider calls.
Only pricing and allocation stay live. It promotes
Telegram/Instagram/Facebook/WhatsApp/Google/TikTok and paginates the complete
catalog. Every `getServices` call (storefront, admin catalog refresh and the
popular-app picker) passes a numeric operator resolved from the live
`getOperators` response and intersected with the admin-enabled operators;
TemporaSMS returns `BAD_OPERATOR` when that parameter is missing. Non-numeric or
unavailable operator payloads fail closed instead of sending an invalid request.
Prices and stock are fetched live before a two-minute purchase confirmation and
validated live again at the moment of purchase.

**Catalog navigation** (apps and countries) works like the Server 2 / Server 3
lists: **20 items per page laid out as a two-column grid — 10 down the left, 10
down the right** (`PER_PAGE` / `GRID_COLUMNS`), numbered page buttons (`1`, `2`,
… with `• n •` marking the current page as a dead button) plus ⬅️/➡️ step
buttons, windowed to the first page, a window around the current page and the
last page for very long catalogs. A **🔍 Search** button on both catalog screens asks for the next
message and filters server-side on the casefolded name *or* code (country lists
also match their numeric code, so `22` or `+22` finds India). Queries are
capped at 80 characters, the active query is echoed as `Search: <query>` and
survives page changes, a no-match screen offers a retry and a clear-search
button, and the search prompt belongs to the user who tapped it — another
user's message never continues that session.

**Lines: automatic routing + numeric operators.** After choosing an app the
buyer picks a line. `operator` is a *numeric direct route* only, so the four
documented automatic selection values are offered first and are sent as
`type=` — never together with `operator`:

- **🤖 Smart** (`type=smart`) — eligible operators for this exact app and
  country, picked from live and historical delivery evidence.
- **⚡ Auto** (`type=auto`) — lowest final price first, moving to the next
  operator if one is unavailable.
- **💰 Cheap** (`type=cheap`) — only the single lowest-priced eligible operator.
- **🏆 Best** (`type=best`) — highest success-rate operator first, then the next
  if unavailable.

Each is explained on the line screen before the buyer chooses; the numeric
buttons below them (1, 2, 3, 4, 7, 8, 9, 10) stay direct routes. Automatic
lines read the shared country list (`getCountries` without `operator=`), fall
back to the numeric catalog operator if a deployment rejects that, and are
stored on the order as `route` / `route_type` with `operator` left empty; the
buyer-facing screens and the admin order detail name the line
(`Line: ⚡ Auto` vs `Line: Operator 3`). Admins can switch any line — automatic
type or numeric operator — on/off from `/otpadmin → ⚙️ Settings → 📡 Lines`,
which applies to new purchases immediately and invalidates outstanding quotes.

**Operator-specific service codes.** A code from `getServices` is not routable
on every line: `getPricesV3` can answer `BAD_SERVICE` (or `BAD_OPERATOR` /
`BAD_COUNTRY` / `BAD_TYPE`) for a code that works elsewhere — for example
WhatsApp `tss` on operator 9. The storefront never shows that raw token to a
buyer. Instead it probes every other enabled line — automatic types and numeric
operators — in parallel (`asyncio.gather` over live `getPricesV3` calls), keeps
only the lines that return positive stock through `price_options`, and offers
them as buttons (automatic types price identically in most deployments, so only
the first of each distinct price/stock is offered instead of four duplicates).
Probe results, including rejections, are remembered per app+country for
`ROUTE_PROBE_TTL` so a known-dead combination is not re-probed on every tap.
Tapping a line re-prices live and shows the normal **Confirm OTP Purchase**
quote (still `retail_price` + a `settings_version` stamp, so a markup or line
change invalidates it). When no other line has stock the buyer gets a plain
message — “\<App\> is not available on \<Line\> for \<Country\>. Try another
app, operator or country.” — and **no order is created and no wallet debit
happens**. Every probe uses the shared client, so each raw provider body still
reaches the admin log (never the API key), and `/otpadmin → 🛠 Provider Tools`
explains these routing codes.

**maxPrice and buy-time validation.** Because the catalog is cached, the buy
step is the only live check — and it runs *before* any money moves:
`getPricesV3` is called once more for the exact selection, and the purchase is
refused with a plain message (no order, no debit, no `getNumber`) when the line
is no longer routable, when stock is gone, or when the provider price rose above
the quote — the buyer is told the new ₹ price and asked to confirm it. A price
that stayed equal or dropped still buys at the confirmed price. Every allocation
then sends **`maxPrice` = the quoted provider cost** (`operator=` for a numeric
route *or* `type=` for automatic routing, plus `providerIds` when the live quote
returned them), so the provider can never cut more than the buyer paid for; the
live cost at buy time is stored as `live_cost` next to `cost` and both are shown
on the admin order detail screen.

Configure the TemporaSMS API key from **`/otpadmin`** (or **Admin → 📲 OTP
Services → 🔑 API Key**) in a private chat: the key message is deleted, the key
is validated with `getBalance` before saving when possible, and only a masked
form is ever displayed. The database key takes priority over the
`TEMPORASMS_API_KEY` environment variable (which remains as a fallback, and is
still read directly by the separate Server 3 catalog module) and applies
without a restart. Like the SMM provider keys, it is stored in MongoDB and is
**not encrypted at rest by the bot** — protect database access accordingly.
`OTP_MARKUP_PERCENT` defaults to **0** (no profit markup) and can likewise be
overridden at runtime from `/otpadmin`; changing price/availability settings
invalidates outstanding quotes so buyers always re-confirm the current price.
Pricing assumes the provider account returns **USD** prices and converts at
the project's fixed ₹100/USD; verify the account currency and set a margin
before launch. Existing wallet balances are used; no new payment gateway is
required.

The OTP admin panel also provides: service ON/OFF, per-line switches (the
automatic routing types `smart` / `auto` / `cheap` / `best` and the numeric
operators 1, 2, 3, 4, 7, 8, 9, 10), popular-app ordering and per-app
visibility, provider tools (connection test / key validation, balance,
catalog-cache refresh — which drops both the memory and the MongoDB catalog
copy and refetches immediately, as does an API-key change), and a paginated
orders dashboard (waiting / completed /
cancelled-refunded / unpaid / allocating / review). Switching the service or
a line off blocks only *new* purchases — existing orders can still be
checked, cancelled and refunded. Every mutating admin action is recorded in
`otp_admin_audit` without secrets; phone numbers and OTPs appear only on the
admin-only order detail screen, never in lists, logs or audit records.

Orders live in MongoDB `otp_orders`, navigation/quotes in `otp_views` (24-hour
TTL), the cached catalog in `otp_catalog` (24-hour TTL index, one hour of
freshness), and idempotent debit/refund markers in the user document. A persistent
background checker resumes waiting orders after restarts, checks approximately
every 30 seconds (longer with many active orders), and requests cancellation after
20 minutes. Refunds require provider cancellation confirmation. OTPs are private,
not sent to public sales channels. My OTP Orders allows manual checking/cancelling
and paginated history.

**Operational safety:** Do not retry ambiguous allocations automatically. Orders
in `allocating` or `review` appear under **/otpadmin → OTP Orders → Review** for
reconciliation: either attach the provider activation (verified via `getStatus`
before anything changes) or, after confirming in the provider account that no
paid activation exists, issue a manual refund — it requires a written reason and
an explicit confirmation, and is recorded in the audit trail. A provider timeout
can mean a number was allocated even without a response. Do not manually refund
before confirming that no paid activation remains.
Wallet debits/refunds use a single-document idempotent ledger (`otp_debits` /
`otp_refunds` markers written in the same atomic user-document update as the
balance change), which is crash-safe on any MongoDB deployment — including
standalone instances without multi-document transaction support — and makes
repeated clicks, retries and restarts safe. This is deliberately not a
multi-document transaction system. Run one bot polling worker. At high volume,
migrate ledger markers to a transactional ledger and use distributed polling locks.

**Production hardenings (Oct 2026):** `getCountries` for automatic routes now uses the numeric catalog operator's list first (fully cached) and only falls back to the bare variant as an explicit last resort; a negative cache (memory + MongoDB-optional, cleared by `clear_cache()`) prevents re-trying a deployment-rejected variant per screen, and `warm_catalog()` never warms a known-unsupported variant. `price_options` now zips `price`/`count` lists index-wise, handles price-keyed stock maps (`{"0.1342":0, "12.7615":3}`), per-provider `providerIds`, and scalar prices; when stock cannot be attributed to a tier it conservatively quotes the **highest** candidate with stock (never the cheapest) — over-quoting slightly is acceptable, under-capping is not. `maxPrice` is always sent, stringified with full precision (no rounding down), and `providerIds` is only pinned when its price equals the quoted cost. `WRONG_MAX_PRICE:12.7615` / `BAD_MAX_PRICE` are parsed explicitly: the order is refunded **exactly once** via the idempotent ledger, the buyer sees the real price as a fresh confirmation (“Provider price is ₹X now — confirm to buy”), and a short-lived price correction (`PRICE_CORRECTION_TTL` 10 min) for that `(service, country, route[, providerIds])` makes the next quote use the corrected cost with zero extra failed allocations; a single automatic retry is allowed only when the reported price ≤ quoted cost (reuse same order/debit, no double charge). The admin log tags every provider call with its purpose (`catalog`, `line probe`, `quote`, `buy validation`, `allocation`, `status refresh`) and collapses a line-probe sweep into one summary card (`🔎 Line probe · esb / country 96 → Operator 3: BAD_SERVICE · …`) with the full raw bodies attached as a single `.txt`; expected routing rejections are shown as `ℹ️ expected` vs `⚠️ failed`, and `_PROVIDER_FAILURES` documents `WRONG_MAX_PRICE`/`BAD_MAX_PRICE` with cost vs live vs reported on the order detail.

### Admin Analytics — Premium Report

`/sales` & `/stats` now render a premium, detailed report via `build_admin_analytics_report()` + `summarize_analytics()`:

- **Visual:** `<h2>` title with premium header, one `<blockquote>` per section with emoji header, `•` bullets, `<b>` labels, `<code>` values, `₹` with `:,`-grouped 2-decimal amounts, counts `:,`-grouped, trend arrows (📈/📉/➖) and status dots (🟢/⚪/🔴). Follows `rich_messages` conventions; `premium_button` strips emoji from button labels only — message text keeps emoji. All user-visible values are `escape()`d.
- **Sections (each degrades to `—`/`0` and the whole report never raises):**
  1. 📈 **Overview / snapshot window** — generated at UTC **and** IST, day/week boundaries, total revenue across all verticals (accounts + SMM + OTP) with today/week split and share %.
  2. 👥 **User Growth** — total, today, week, yesterday/last-week with % delta, active buyers (≥1 paid order) and conversion %, top-up users.
  3. 💰 **Wallet (INR)** — deposits total/today/week, withdrawals paid total/today/week, pending recharges/withdrawals, net flow, avg deposit.
  4. 🛒 **Account Sales** — orders + revenue total/today/week, avg order value, refund/cancel counts, **top 3 countries**, stock counters.
  5. 📱 **SMM** — orders, open, gross/net/refunded, today/week net, avg order value, top category/service.
  6. 📲 **OTP (TemporaSMS)** — from `otp_orders`: orders + revenue total/today/week, provider cost (`cost`) vs `live_cost` vs retail `price` → **gross margin ₹ and %**, refunded/cancelled counts, statuses needing attention (`waiting`, `allocating`, `review`, `refunding`, `unpaid`), top app/country/line (`route`/`route_type`/`operator` → `Line: ⚡ Auto`), catalog-cache age + enabled lines.
  7. ⚠️ **Attention / alerts** — only when non-empty: OTP `review`/`allocating` older than 10 m, stale SMM cycle, last SMM error, price mismatches from `WRONG_MAX_PRICE` corrections, low/zero provider balance, catalog older than TTL, refund backlog.
  8. 🤖 **Bot & Provider Health** — process + uptime, Telegram API reachability, SMM monitor + last cycle, OTP worker + last catalog warm/refresh, DB collection counts. Best-effort and fast: no new paid/slow provider calls (balance only if cached, otherwise `not checked`).
  9. Footer: `Generated <UTC> · <IST> · window: today/week (UTC)` + legend for symbols.

Metrics are computed in `admin_analytics.summarize_analytics()` (keyword-only, backwards compatible; existing tests still call it) with optional `otp_orders=`, `otp_catalog=`, `otp_settings=` etc., all amount parsing via `safe_amount()`/`utc_naive()`. Formatting stays in `bot.py` (or a small helper) so it is unit-testable. Empty collections are handled gracefully.

The provider's expanded API reference and a funded account were not available
for a live end-to-end transaction during implementation. Before production,
verify actual `getServices`, `getCountries`, `getPricesV3` response shapes,
`getStatus` / `setStatus(status=8)` semantics, cancellation timing and currency
with a low-cost controlled activation. Unknown price shapes/absent stock fail
closed rather than charging guessed prices.
