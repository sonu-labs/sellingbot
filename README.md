# vthquickcodes
**A fully automated telegram readymade account selling bot.**

## What this repo includes
- Telegram bot with:
  - `/start` menu with inline buttons (Balance, Account Details, Recharge, Support, Buy Account).
  - Country selection and buy flow (quantity, stock checks, balance checks).
  - Admin `/add` flow to add numbers to stock per country.
  - MongoDB store for users, stock, transactions.
  - Heroku-ready Procfile.

## Deployment

<details>
<summary><strong>Click here for the Detailed Deployment Guide</strong></summary>

### 1. Database Setup
* First, create a database by visiting [MongoDB Atlas](https://www.mongodb.com/products/platform/atlas-database).
* From there, create a database link (connection string).
* In the repository, locate `bot.py` > Line 37 `#--Mongo-setup`.

### 2. Basic Edits (in `bot.py`)
* **Lines 47-54:** Change all the variables and do not use `@`.
* **Lines 531-543:** Change every URL for the "More" Callback.
* **Line 1563:** Sales callback - set your sales admin GC/user ID there.
* **Lines 2041 & 2057:** Add your "how to use" post link.

### 3. MUSTJOIN Setup
* Locate `bot/mustjoin.py` and edit all the configurations carefully.

### 4. Recharge Flow Setup
* Locate `bot/reacharge_flow.py` and edit the lines starting from line 20 (`#config`).
* Upload your QR payment file to the main page of the repository.
* In `bot/reacharge_flow.py`, line 24, edit the value with the exact filename of the QR code you just uploaded.

### 5. Crypto Automation Setup
* First, create a wallet using [OxaPay](https://oxapay.com/wallet).
* Collect your merchant API key.
* Locate `bot/oxapay.py` and carefully change `API_KEY` with your new key.

### 6. Hosting
* Use Heroku for reliable and easy hosting!

</details>

<br>

- Set env vars: `BOT_TOKEN`, `MONGODB_URI`, `ADMIN_IDS` (comma-separated), `CURRENCY_SYMBOL` (optional), `DEFAULT_PRICE`.
- Deploy to Heroku by pushing this repo and setting configs.
- Deploy within a minute through Heroku.

## Easy deployment url :
[Deploy to Heroku](http://dashboard.heroku.com/new?template=https://github.com/{github_username}/{repository})

> **Note:** Please replace `{github_username}` and `{repository}` with your actual variables before using the link!!
