"""
Persistence layer: everything is stored in one Google Sheet, in separate tabs.

Safety model (added after the "disappearing portfolios" incident):
- read_df() remembers which tabs failed to read (transient Google API /
  network errors). A failed read still returns an empty DataFrame so pages
  degrade gracefully, but every writer can ask read_failed(tab) and must
  refuse to act on data it could not really read.
- write_df() refuses to clear-and-overwrite any tab whose last read failed.
  (The original bug: a one-off read error made portfolios_meta look empty,
  get_portfolios() then "helpfully" rewrote it with only the two built-in
  portfolios, wiping the registry of every user-added portfolio.)
- get_portfolios() only seeds the two built-in portfolios on a genuinely
  fresh sheet (no registry rows AND no portfolioN_positions tabs at all).
- pages/2_Repair.py re-registers position tabs that exist in Google Sheets
  but are missing from portfolios_meta. The repair is append-only: position
  tabs, backtest history, settings and every other tab are never touched.
"""
from __future__ import annotations
import re
import time
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
import datetime

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_SCHEMAS = {
    "portfolios_meta": ["tab_name", "label"],
    "portfolio1_positions": ["ticker", "name", "asset_type", "weight", "purchase_date"],
    "portfolio2_positions": ["ticker", "name", "asset_type", "weight", "purchase_date"],
    "watchlist_etfs": ["ticker", "name"],
    "dividends": ["portfolio", "ticker", "ex_date", "pay_date", "amount_per_share", "detected_on"],
    "transactions": ["date", "portfolio", "ticker", "type", "amount_note"],
    "backtest_history": ["date", "portfolio", "index_value"],
    "portfolio_settings": ["portfolio", "rebalance_frequency"],
    "display_order": ["label", "sort_order"],
    "price_cache": ["ticker", "asset_type", "date", "close"],
    # --- tabs added for the CSI 300 benchmark & index valuation features ---
    # NOTE: the tabs below are append-only. Never call write_df() on them.
    "index_price_cache": ["ticker", "date", "close"],
    "pe_cache": ["date", "index_key", "pe"],
    # --- tab for the top-10 constituents feature (monthly snapshots) ---
    "holdings_cache": ["index_key", "date", "code", "en_name", "weight"],
}

TICKER_COLUMNS = {"ticker"}

DEFAULT_PORTFOLIO_ROWS = [
    {"tab_name": "portfolio1_positions", "label": "Возможности Китая"},
    {"tab_name": "portfolio2_positions", "label": "Возможности Китая. Специальная 2"},
]


class SheetsWriteBlockedError(RuntimeError):
    """A destructive write was refused because the last read of that tab
    failed, so the app cannot be sure it is not erasing real data."""


# Tabs whose most recent real read attempt failed (transient Google errors).
_READ_ERRORS: set[str] = set()


def read_failed(tab_name: str) -> bool:
    """True if the last real read of this tab failed. Writers must refuse
    to overwrite such a tab: the empty DataFrame it returned is NOT the
    tab's true contents."""
    return tab_name in _READ_ERRORS


def _retry_api_call(func, *args, **kwargs):
    """Calls a gspread function and retries up to 4 times with exponential backoff
    if Google Sheets returns an API/Rate Limit error."""
    max_retries = 4
    base_delay = 2

    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if attempt < max_retries - 1:
                sleep_time = base_delay * (2 ** attempt)
                time.sleep(sleep_time)
            else:
                raise
        except Exception:
            raise

@st.cache_resource(show_spinner=False)
def _get_client() -> gspread.Client:
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def _get_spreadsheet():
    client = _get_client()
    return _retry_api_call(client.open_by_key, st.secrets["google_sheet_id"])

def _get_or_create_worksheet(tab_name: str):
    ss = _get_spreadsheet()
    try:
        return _retry_api_call(ss.worksheet, tab_name)
    except gspread.WorksheetNotFound:
        headers = SHEET_SCHEMAS.get(tab_name, [])
        ws = ss.add_worksheet(title=tab_name, rows=200, cols=max(len(headers), 1))
        if headers:
            ws.append_row(headers)
        return ws

@st.cache_data(ttl=900, show_spinner=False)
def read_df(tab_name: str) -> pd.DataFrame:
    try:
        ws = _get_or_create_worksheet(tab_name)
        records = _retry_api_call(ws.get_all_records)
    except Exception as e:
        # Remember the failure so no writer treats this empty frame as real.
        _READ_ERRORS.add(tab_name)
        st.error(f"Failed to read {tab_name} from Google Sheets after multiple retries. Please wait a minute and refresh.")
        return pd.DataFrame(columns=SHEET_SCHEMAS.get(tab_name, []))
    _READ_ERRORS.discard(tab_name)

    if not records:
        return pd.DataFrame(columns=SHEET_SCHEMAS.get(tab_name, []))
    df = pd.DataFrame(records)
    for col in TICKER_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].str.replace(r'[^\d]', '', regex=True)
            df[col] = df[col].apply(lambda x: x.zfill(6) if x.isdigit() else x)

    for col in ["index_value", "weight", "close"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(",", ".", regex=False)
            df[col] = df[col].str.replace(r"[^\d.\-]", "", regex=True)
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def write_df(tab_name: str, df: pd.DataFrame) -> None:
    # Guard: never clear-and-overwrite a tab we could not read. This is the
    # exact mechanism that once wiped portfolios_meta down to two defaults.
    if tab_name in _READ_ERRORS:
        raise SheetsWriteBlockedError(
            f"Write to '{tab_name}' was cancelled: the last read of this tab from "
            "Google Sheets failed, so saving now could erase its real contents. "
            "Reload the page and try again. Nothing was changed."
        )
    ws = _get_or_create_worksheet(tab_name)
    try:
        _retry_api_call(ws.clear)
    except Exception as e:
        st.error(f"Failed to clear {tab_name} on Google Sheets: {e}")
        raise

    if df.empty:
        headers = SHEET_SCHEMAS.get(tab_name, [])
        if headers:
            _retry_api_call(ws.append_row, headers)
        return

    df = df.copy()
    for col in df.columns:
        if col == "ticker":
            df[col] = df[col].astype(str).str.strip()
        elif col in ["index_value", "weight", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[col] = df[col].apply(lambda x: f"{x:.8f}" if pd.notnull(x) else "")
        elif pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime('%Y-%m-%d')
        elif df[col].dtype == 'object':
            first_valid = df[col].first_valid_index()
            if first_valid is not None:
                sample = df.loc[first_valid, col]
                if isinstance(sample, (pd.Timestamp, datetime.datetime, datetime.date)):
                    df[col] = df[col].apply(lambda x: x.strftime('%Y-%m-%d') if hasattr(x, 'strftime') else str(x))

    df = df.astype(object).where(pd.notnull(df), "")
    rows = df.values.tolist()
    headers = df.columns.values.tolist()

    try:
        _retry_api_call(ws.update, [headers] + rows, raw=True)
    except Exception as e:
        st.error(f"Failed to save data to Google Sheets: {e}")
        raise

def append_rows(tab_name: str, rows: list[dict]) -> None:
    if not rows:
        return
    ws = _get_or_create_worksheet(tab_name)
    headers = SHEET_SCHEMAS.get(tab_name) or list(rows[0].keys())

    clean_values = []
    for r in rows:
        row_vals = []
        for h in headers:
            val = r.get(h, "")
            if h in ["index_value", "weight", "close"]:
                try:
                    val = f"{float(val):.8f}"
                except (ValueError, TypeError):
                    val = ""
            row_vals.append(val)
        clean_values.append(row_vals)

    try:
        _retry_api_call(ws.append_rows, clean_values, value_input_option="RAW")
    except Exception as e:
        st.error(f"Failed to append rows: {e}")
        raise

def clear_caches():
    st.cache_data.clear()

def get_rebalance_frequency(portfolio_label: str) -> str:
    df = read_df("portfolio_settings")
    if df.empty or portfolio_label not in set(df.get("portfolio", [])):
        return "none"
    row = df[df["portfolio"] == portfolio_label].iloc[0]
    return row.get("rebalance_frequency", "none") or "none"

def save_rebalance_frequency(portfolio_label: str, frequency: str) -> None:
    if read_failed("portfolio_settings"):
        raise SheetsWriteBlockedError(
            "Cannot save the rebalancing setting right now: portfolio_settings "
            "could not be read from Google Sheets. Reload the page and try again."
        )
    df = read_df("portfolio_settings")
    if df.empty:
        df = pd.DataFrame(columns=["portfolio", "rebalance_frequency"])
    df = df[df["portfolio"] != portfolio_label]
    df = pd.concat([df, pd.DataFrame([{"portfolio": portfolio_label, "rebalance_frequency": frequency}])], ignore_index=True)
    write_df("portfolio_settings", df)
    clear_caches()

@st.cache_data(ttl=300, show_spinner=False)
def list_portfolio_position_tabs() -> list[str]:
    """All portfolioN_positions tabs that actually exist in the spreadsheet,
    sorted by their number. Never raises: returns [] on API failure."""
    try:
        ss = _get_spreadsheet()
        titles = [ws.title for ws in _retry_api_call(ss.worksheets)]
        tabs = [t for t in titles if re.fullmatch(r"portfolio\d+_positions", t)]
        tabs.sort(key=lambda t: int(re.search(r"\d+", t).group()))
        return tabs
    except Exception:
        return []

def _orphan_position_tabs(meta_df: pd.DataFrame) -> list[str]:
    """Position tabs that exist in the spreadsheet but are missing from the
    portfolios_meta registry."""
    registered = set()
    if not meta_df.empty and "tab_name" in meta_df.columns:
        registered = set(str(t) for t in meta_df["tab_name"])
    return [t for t in list_portfolio_position_tabs() if t not in registered]

def get_portfolios() -> dict[str, str]:
    df = read_df("portfolios_meta")

    if read_failed("portfolios_meta"):
        # A failed read must never look like "no portfolios". Show the
        # built-ins for navigation only, and touch nothing on the sheet.
        st.warning(
            "Google Sheets is not responding right now — showing only the "
            "built-in portfolios. Nothing was changed; refresh in a minute."
        )
        return {r["tab_name"]: r["label"] for r in DEFAULT_PORTFOLIO_ROWS}

    valid = not df.empty and "tab_name" in df.columns and "label" in df.columns
    orphans = _orphan_position_tabs(df if valid else pd.DataFrame())

    if orphans:
        st.warning(
            f"{len(orphans)} portfolio tab(s) exist in Google Sheets but are missing "
            f"from the registry ({', '.join(orphans)}). Open the **Repair** page in the "
            "sidebar to restore them. Position tabs and backtest data are intact."
        )
        if valid:
            return dict(zip(df["tab_name"], df["label"]))
        return {}

    if valid:
        return dict(zip(df["tab_name"], df["label"]))

    # Genuinely fresh sheet (no registry rows, no position tabs at all):
    # seed the two built-in portfolios.
    df = pd.DataFrame(DEFAULT_PORTFOLIO_ROWS)
    write_df("portfolios_meta", df)
    clear_caches()
    return dict(zip(df["tab_name"], df["label"]))

def add_portfolio(label: str) -> str:
    if read_failed("portfolios_meta"):
        raise SheetsWriteBlockedError(
            "Cannot add a portfolio right now: the portfolio registry could not "
            "be read from Google Sheets. Reload the page and try again."
        )
    df = read_df("portfolios_meta")
    existing_nums = [int(t.replace("portfolio", "").replace("_positions", "")) for t in df["tab_name"] if t.startswith("portfolio") and t.endswith("_positions")]
    # Also count the position tabs that really exist, so a wiped registry
    # can never make a new portfolio reuse an existing tab number.
    for t in list_portfolio_position_tabs():
        m = re.fullmatch(r"portfolio(\d+)_positions", t)
        if m:
            existing_nums.append(int(m.group(1)))
    next_num = max(existing_nums) + 1 if existing_nums else 1
    tab_name = f"portfolio{next_num}_positions"
    new_row = pd.DataFrame([{"tab_name": tab_name, "label": label}])
    df = pd.concat([df, new_row], ignore_index=True)
    write_df("portfolios_meta", df)
    _get_or_create_worksheet(tab_name)
    clear_caches()
    return tab_name

def delete_portfolio(tab_name: str, label: str) -> None:
    if read_failed("portfolios_meta"):
        raise SheetsWriteBlockedError(
            "Cannot delete a portfolio right now: the portfolio registry could "
            "not be read from Google Sheets. Reload the page and try again."
        )
    df = read_df("portfolios_meta")
    df = df[df["tab_name"] != tab_name]
    write_df("portfolios_meta", df)
    try:
        ss = _get_spreadsheet()
        ws = ss.worksheet(tab_name)
        ss.del_worksheet(ws)
    except Exception:
        pass
    # Clean up dependent tabs only when they were actually read: writing a
    # half-read (empty) frame would erase other portfolios' data.
    if not read_failed("backtest_history"):
        bt = read_df("backtest_history")
        if not bt.empty:
            bt = bt[bt["portfolio"] != label]
            write_df("backtest_history", bt)
    if not read_failed("portfolio_settings"):
        settings = read_df("portfolio_settings")
        if not settings.empty:
            settings = settings[settings["portfolio"] != label]
            write_df("portfolio_settings", settings)
    clear_caches()

# ---------------------------------------------------------------- repair API --

def diagnose_registry() -> dict:
    """Read-only diagnosis for the Repair page. Returns:
    - registered:  list of (tab_name, label) currently in portfolios_meta
    - orphans:     position tabs missing from the registry, sorted by number
    - previews:    orphan tab -> comma-separated tickers held in that tab
    - proposal:    orphan tab -> suggested label ('' if none could be inferred)
    - leftover:    recovered labels that no orphan was mapped to
    - bt_stats:    label -> (row_count, first_date, last_date) in backtest
    - meta_broken: True when portfolios_meta could not be parsed at all
    Nothing is written."""
    meta = read_df("portfolios_meta")
    registered = []
    if not meta.empty and "tab_name" in meta.columns and "label" in meta.columns:
        registered = [(str(t), str(l)) for t, l in zip(meta["tab_name"], meta["label"])]
    registered_tabs = {t for t, _ in registered}
    orphans = [t for t in list_portfolio_position_tabs() if t not in registered_tabs]

    bt = read_df("backtest_history")
    settings = read_df("portfolio_settings")
    order = read_df("display_order")
    watchlist = read_df("watchlist_etfs")

    etf_labels = set()
    if not watchlist.empty and "ticker" in watchlist.columns:
        for _, w in watchlist.iterrows():
            name = str(w.get("name", "")).strip() or str(w["ticker"])
            etf_labels.add(f"{name} ({w['ticker']})")

    registered_labels = {l for _, l in registered}
    cand = set()
    if not bt.empty and "portfolio" in bt.columns:
        cand |= {str(x) for x in bt["portfolio"] if str(x).strip()}
    if not settings.empty and "portfolio" in settings.columns:
        cand |= {str(x) for x in settings["portfolio"] if str(x).strip()}
    if not order.empty and "label" in order.columns:
        cand |= {str(x) for x in order["label"] if str(x).strip()}
    cand -= registered_labels
    cand -= etf_labels

    order_map = {}
    if not order.empty and "label" in order.columns:
        nums = pd.to_numeric(order.get("sort_order"), errors="coerce").fillna(99999)
        order_map = {str(l): int(n) for l, n in zip(order["label"], nums)}
    bt_first = {}
    if not bt.empty:
        bt2 = bt.copy()
        bt2["date"] = pd.to_datetime(bt2["date"], errors="coerce")
        bt_first = {str(k): str(v.date()) for k, v in bt2.groupby("portfolio")["date"].min().items()}

    def _key(label):
        return (order_map.get(label, 99999), bt_first.get(label, "9999-99-99"), label)
    cand_sorted = sorted(cand, key=_key)

    previews = {}
    for t in orphans:
        pdf = read_df(t)
        if pdf.empty or "ticker" not in pdf.columns:
            previews[t] = ""
        else:
            previews[t] = ", ".join(str(x) for x in pdf["ticker"].tolist())

    proposal = {t: (cand_sorted[i] if i < len(cand_sorted) else "") for i, t in enumerate(orphans)}
    leftover = cand_sorted[len(orphans):]

    bt_stats = {}
    if not bt.empty:
        bt2 = bt.copy()
        bt2["date"] = pd.to_datetime(bt2["date"], errors="coerce")
        for name, g in bt2.groupby("portfolio"):
            bt_stats[str(name)] = (len(g), str(g["date"].min().date()), str(g["date"].max().date()))

    return {
        "registered": registered,
        "orphans": orphans,
        "previews": previews,
        "proposal": proposal,
        "leftover": leftover,
        "bt_stats": bt_stats,
        "meta_broken": meta.empty or "tab_name" not in meta.columns or "label" not in meta.columns,
    }

def restore_registry_rows(rows: list[dict]) -> list[dict]:
    """Re-register portfolios by APPENDING rows ({'tab_name', 'label'}) to
    portfolios_meta. Existing registry rows, position tabs, backtest history
    and every other tab are never touched. Returns the rows actually written."""
    if read_failed("portfolios_meta"):
        raise SheetsWriteBlockedError(
            "Cannot restore right now: the portfolio registry could not be read "
            "from Google Sheets. Reload the page and try again."
        )
    clean = []
    for r in rows or []:
        tab = str(r.get("tab_name", "")).strip()
        label = str(r.get("label", "")).strip()
        if tab and label:
            clean.append({"tab_name": tab, "label": label})
    if clean:
        append_rows("portfolios_meta", clean)
        clear_caches()
    return clean

def remove_registry_row(tab_name: str) -> None:
    """Removes ONE row from the portfolios_meta registry. The positions tab
    itself, its backtest history and its settings are never touched — the
    Repair page can re-register it at any time."""
    if read_failed("portfolios_meta"):
        raise SheetsWriteBlockedError(
            "Cannot edit the registry right now: it could not be read from "
            "Google Sheets. Reload the page and try again."
        )
    df = read_df("portfolios_meta")
    if df.empty or tab_name not in set(df.get("tab_name", [])):
        return
    df = df[df["tab_name"] != tab_name]
    write_df("portfolios_meta", df)
    clear_caches()

def get_display_order() -> dict[str, int]:
    df = read_df("display_order")
    if df.empty:
        return {}
    df["sort_order"] = pd.to_numeric(df["sort_order"], errors="coerce").fillna(9999).astype(int)
    return dict(zip(df["label"], df["sort_order"]))

def save_display_order(labels: list[str]) -> None:
    if read_failed("display_order"):
        raise SheetsWriteBlockedError(
            "Cannot save the display order right now: it could not be read "
            "from Google Sheets. Reload the page and try again."
        )
    df = pd.DataFrame({"label": labels, "sort_order": range(1, len(labels) + 1)})
    write_df("display_order", df)
    clear_caches()
