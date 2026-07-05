"""
Persistence layer: everything is stored in one Google Sheet, in separate
tabs (worksheets). This keeps real portfolio data out of the GitHub repo -
only a service-account credential (in Streamlit secrets) can access it.

Tabs (created automatically on first run if missing):
  portfolio1_positions: ticker, name, asset_type, weight, cost_basis, purchase_date
  portfolio2_positions: (same columns)
  watchlist_etfs: ticker, name, added_date
  dividends: portfolio, ticker, ex_date, pay_date, amount_per_share, detected_on
  transactions: date, portfolio, ticker, type, amount_note
  backtest_history: date, portfolio, index_value (a performance index starting at 100)
  portfolio_settings: portfolio, rebalance_frequency (none/monthly/quarterly/semiannual/annual)
"""
from __future__ import annotations
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_SCHEMAS = {
    "portfolio1_positions": ["ticker", "name", "asset_type", "weight", "cost_basis", "purchase_date"],
    "portfolio2_positions": ["ticker", "name", "asset_type", "weight", "cost_basis", "purchase_date"],
    "watchlist_etfs": ["ticker", "name", "added_date"],
    "dividends": ["portfolio", "ticker", "ex_date", "pay_date", "amount_per_share", "detected_on"],
    "transactions": ["date", "portfolio", "ticker", "type", "amount_note"],
    "backtest_history": ["date", "portfolio", "index_value"],
    "portfolio_settings": ["portfolio", "rebalance_frequency"],
}


@st.cache_resource(show_spinner=False)
def _get_client() -> gspread.Client:
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


@st.cache_resource(show_spinner=False)
def _get_spreadsheet():
    client = _get_client()
    return client.open_by_key(st.secrets["google_sheet_id"])


def _get_or_create_worksheet(tab_name: str):
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        headers = SHEET_SCHEMAS.get(tab_name, [])
        ws = ss.add_worksheet(title=tab_name, rows=200, cols=max(len(headers), 1))
        if headers:
            ws.append_row(headers)
        return ws


@st.cache_data(ttl=60, show_spinner=False)
def read_df(tab_name: str) -> pd.DataFrame:
    ws = _get_or_create_worksheet(tab_name)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=SHEET_SCHEMAS.get(tab_name, []))
    return pd.DataFrame(records)


def write_df(tab_name: str, df: pd.DataFrame) -> None:
    """Overwrites the entire tab with df's contents. Fine at personal-tracker scale
    (a few dozen rows per tab) - simpler and safer than incremental diff/patch."""
    ws = _get_or_create_worksheet(tab_name)
    ws.clear()
    headers = list(df.columns) if not df.empty else SHEET_SCHEMAS.get(tab_name, [])
    ws.append_row(headers)
    if not df.empty:
        # Convert everything to plain strings/numbers gspread can serialize
        rows = df.astype(object).where(pd.notnull(df), "").values.tolist()
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def append_rows(tab_name: str, rows: list[dict]) -> None:
    if not rows:
        return
    ws = _get_or_create_worksheet(tab_name)
    headers = SHEET_SCHEMAS.get(tab_name) or list(rows[0].keys())
    values = [[r.get(h, "") for h in headers] for r in rows]
    ws.append_rows(values, value_input_option="USER_ENTERED")


def clear_caches():
    """Call after any write so the dashboard picks up fresh data immediately.
    Clears ALL cached data (including price history), which is a little
    wasteful but simple and avoids stale-cache bugs at this app's scale."""
    st.cache_data.clear()


def get_rebalance_frequency(portfolio_label: str) -> str:
    df = read_df("portfolio_settings")
    if df.empty or portfolio_label not in set(df.get("portfolio", [])):
        return "none"
    row = df[df["portfolio"] == portfolio_label].iloc[0]
    return row.get("rebalance_frequency", "none") or "none"


def save_rebalance_frequency(portfolio_label: str, frequency: str) -> None:
    df = read_df("portfolio_settings")
    if df.empty:
        df = pd.DataFrame(columns=["portfolio", "rebalance_frequency"])
    df = df[df["portfolio"] != portfolio_label]
    df = pd.concat([df, pd.DataFrame([{"portfolio": portfolio_label, "rebalance_frequency": frequency}])], ignore_index=True)
    write_df("portfolio_settings", df)
    clear_caches()
