# My Portfolio & ETF Tracker

A personal, single-user dashboard for tracking two China A-share model portfolios
(weight-based) and a watchlist of China-listed ETFs. Free to run, hosted on
Streamlit Community Cloud, data stored in a private Google Sheet.

---

## 1. How this works (read this first)

**Weight-based portfolios, not share counts.** Each portfolio position is a
*target weight* (%), not a literal share count. This means "dividend
reinvestment" can't be a literal "buy N more shares with cash" transaction
(there's no cash balance in a weight model). Instead, each holding's price
history is pulled *dividend-adjusted* ("hfq" in Chinese market data) - a
standard technique that already bakes in the effect of reinvesting that
holding's own dividends. Every dividend is still auto-detected and logged
for your visibility on the Manage page - it just isn't what drives the math.

**Backtest + live, stitched together.** You can upload a backtest as an
index series starting at 100 (e.g. from a spreadsheet model or another
tool). The *last date* in that upload becomes the hand-off point: from
there forward, the app tracks your actual current positions/weights day by
day, chained on with no jump at the seam. If you never upload a backtest,
live tracking just starts from your earliest position's purchase date.

**Rebalancing.** Each portfolio has a rebalancing setting (none / monthly /
quarterly / every 6 months / annually) you choose on the Manage page. With
"none", it's buy-and-hold at your original target weights (they'll drift
over time as prices move). With a rebalancing frequency chosen, all
positions reset back to their target weights on that schedule. Rebalancing
happens on calendar-month anniversaries of your live-tracking start date,
not literal trading-desk timing - a deliberate simplification.

**One important trade-off:** if you edit a portfolio's weights today, the
*entire* historical chart recalculates as if you'd always held the new
weights (from the live-tracking start date forward) rather than freezing
performance at the moment you made the edit. This is simpler to build and
reason about. If you'd like true point-in-time tracking instead (freezing
history at each edit), that's a solid future upgrade - just ask.

**Chart timeframes:** 5D, 1M, 3M, 6M, YTD, 1Y, 5Y, Max. There's no 1-Day
zoom button, since that needs intraday minute data which is a much less
reliable free data source - not worth it for a daily-check dashboard. The
comparison table still includes a 1-Day return column (computed from daily
closes, which is robust).

**Data source honesty:** all market data comes from `akshare`, which pulls
from public (unofficial) Eastmoney/Sina endpoints, free with no API key.
These occasionally change their site structure and break a function - see
[Section 4](#4-if-something-breaks) if that happens.

---

## 2. Repo structure

```
etf-tracker/
├── streamlit_app.py          # main dashboard: chart + comparison table
├── pages/
│   └── 1_Manage.py           # password-protected admin area
├── utils/
│   ├── data_fetch.py         # akshare wrappers + caching
│   ├── sheets_db.py          # Google Sheets read/write
│   ├── returns.py            # backtest chaining, rebalancing, return math
│   └── auth.py                # password check
├── data/
│   └── backtest_template.xlsx # blank template for the backtest upload
├── requirements.txt
├── .streamlit/secrets.toml.example  # shows the shape of secrets - not real ones
└── .gitignore
```

No real holdings data lives in this repo - everything is in your private
Google Sheet, referenced only by an ID + credential stored in Streamlit's
secrets manager (never committed to GitHub).

---

## 3. Setup: step by step

### Step A - Create a GitHub account & repository

1. Go to [github.com](https://github.com) and sign up (free) if you don't have an account.
2. Click the **+** icon (top right) → **New repository**.
3. Name it e.g. `etf-tracker`. Set it to **Private**. Click **Create repository**.
4. On the new repo's page, click **uploading an existing file**.
5. Drag in every file and folder from this project (keep the folder
   structure - `pages/`, `utils/`, `data/`, etc. GitHub's uploader preserves
   folder paths if you drag a whole folder from your computer).
6. Scroll down, click **Commit changes**.

You now have your code on GitHub, privately.

### Step B - Create a Google Sheet + service account (for data storage)

1. Go to [sheets.google.com](https://sheets.google.com), create a **new
   blank spreadsheet**. Name it anything, e.g. "Portfolio Tracker Data".
2. Copy the **Sheet ID** from its URL: `https://docs.google.com/spreadsheets/d/`**`THIS_LONG_ID_PART`**`/edit`. Save it somewhere - you'll paste it into secrets soon.
3. Go to [console.cloud.google.com](https://console.cloud.google.com). Sign in with the same Google account. Create a new project (top left dropdown → **New Project**), name it anything, click **Create**.
4. With that project selected, go to **APIs & Services → Library**. Search
   **Google Sheets API**, click it, click **Enable**. Repeat for **Google
   Drive API**.
5. Go to **APIs & Services → Credentials**. Click **Create Credentials →
   Service account**. Give it any name, click **Create and Continue**, then
   **Continue**, then **Done** (skip the optional role/access steps).
6. Click on the service account you just created. Go to the **Keys** tab →
   **Add Key → Create new key → JSON**. This downloads a `.json` file - keep
   it safe, you'll need values from it in Step D.
7. Copy the service account's email address (looks like
   `something@your-project.iam.gserviceaccount.com` - it's in the JSON file
   as `client_email`, and also shown on the service account's page).
8. Back in your Google Sheet, click **Share**, paste that service account
   email, give it **Editor** access, uncheck "notify people", click **Share**.

### Step C - Create a Streamlit Community Cloud account

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with
   your GitHub account (free).
2. Click **Create app** → **From an existing repo**.
3. Choose your `etf-tracker` repository, branch `main`, main file path
   `streamlit_app.py`.
4. Before clicking Deploy, click **Advanced settings**.

### Step D - Fill in secrets

Still in Advanced settings, there's a **Secrets** box. Paste this in,
filling in your own values (use `.streamlit/secrets.toml.example` in the
repo as a reference for the exact shape):

```toml
admin_password = "choose-your-own-password-here"

google_sheet_id = "paste-the-sheet-id-from-step-B2-here"

[gcp_service_account]
type = "service_account"
project_id = "paste-from-the-json-file"
private_key_id = "paste-from-the-json-file"
private_key = "paste-the-whole-private_key-value-from-the-json-file-including-BEGIN-END-lines"
client_email = "paste-from-the-json-file"
client_id = "paste-from-the-json-file"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "paste-from-the-json-file"
universe_domain = "googleapis.com"
```

Open the JSON file you downloaded in Step B6 with any text editor - every
value above (except `admin_password` and `google_sheet_id`) has a matching
key in that file. Copy each one across, keeping the quote marks.

Choose your own `admin_password` - this protects the Manage page.

Click **Save**, then **Deploy**. First deploy takes a few minutes while it
installs everything.

### Step E - First run checklist

Once deployed:
1. Open the app's URL (Streamlit gives you one, like `yourname-etf-tracker.streamlit.app`).
2. It should say "No portfolios or watchlist ETFs set up yet."
3. Go to the **Manage** page (left sidebar), enter your admin password.
4. Add one real position (e.g. ticker `600519`, weight `100`, a cost basis,
   a purchase date) to Portfolio 1 and click **Save changes**.
5. Go back to the main dashboard - you should see it appear in the chart
   dropdown and comparison table within a few seconds.
6. If you see a red warning about a data fetch failing, see
   [Section 4](#4-if-something-breaks) below - it's very likely a live
   akshare hiccup, not something wrong with your setup.

---

## 4. If something breaks

The app is built to fail gracefully - a broken ticker or a flaky data
source shows a warning message instead of crashing the whole dashboard.
If you see repeated errors mentioning `akshare`, `stock_zh_a_hist`,
`fund_etf_hist_em`, `stock_fhps_detail_em`, or similar:

- This almost always means Eastmoney/Sina (the underlying free data
  sources) changed something on their end, and the `akshare` package needs
  updating or its function signature changed slightly.
- Come back to an LLM (this one or an agent like Claude Code) with: (a) the
  exact error message/traceback, and (b) this project's files. Say
  "akshare function X is failing with this error, please fix it" - it's
  usually a small, contained fix in `utils/data_fetch.py`.

---

## 5. How to use this (plain-language guide)

**Opening the site:** just visit your Streamlit app URL any time - bookmark
it. It may take a few seconds to "wake up" if nobody's visited in a while
(free tier apps sleep when idle).

**Adding or editing a position:** go to Manage → pick Portfolio 1 or 2 →
edit the table directly (click a cell to change it, or scroll to the bottom
row and start typing to add a new one, or click the trash icon on a row to
delete it) → click **Save changes**.

**Setting rebalancing:** on the same Portfolio page, use the dropdown above
the table.

**Adding/removing watchlist ETFs:** Manage → Watchlist ETFs → same
edit-the-table pattern.

**Uploading backtest history:** Manage → Backtest history upload →
download the template, fill it in (one row per date per portfolio, an
index value starting at 100), upload it back.

**Dividends:** Manage → Dividend log → click "Scan for new dividends now"
whenever you want to check for newly-paid dividends on your held
positions. You can also directly edit/correct any row and save.

**Coming back for changes:** this project came with a spec document
that explicitly says to reuse it for future update requests - keep using
that same document with whatever LLM or coding tool you're working with,
describing what you'd like changed. For anything beyond "just push new
files to GitHub" (which auto-redeploys within a minute or two), you'll get
the same style of plain numbered steps as above.
