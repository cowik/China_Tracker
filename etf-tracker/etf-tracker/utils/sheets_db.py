"""
Persistence layer: everything is stored in one Google Sheet, in separate tabs.
"""
from __future__ import annotations
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
    "portfolio1_positions": ["ticker", "name", "asset_type", "weight", "purchase_date"],
    "portfolio2_positions": ["ticker", "name", "asset_type", "weight", "purchase_date"],
    "watchlist_etfs": ["ticker", "name"],
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
    """Overwrites the entire tab with df's contents, safely converting dates to strings."""
    ws = _get_or_create_worksheet(tab_name)
    ws.clear()
    headers = list(df.columns) if not df.empty else SHEET_SCHEMAS.get(tab_name, [])
    ws.append_row(headers)
    if not df.empty:
        # --- SAFE CONVERSION: convert any datetime/date to ISO string ---
        df = df.copy()
        for col in df.columns:
            # Check for datetime64 columns
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                df[col] = df[col].dt.strftime('%Y-%m-%d')
            # Check for object columns that might contain date objects
            elif df[col].dtype == 'object':
                first_valid = df[col].first_valid_index()
                if first_valid is not None:
                    sample = df.loc[first_valid, col]
                    if isinstance(sample, (pd.Timestamp, datetime.datetime, datetime.date)):
                        df[col] = df[col].apply(
                            lambda x: x.strftime('%Y-%m-%d') if hasattr(x, 'strftime') else str(x)
                        )
        # Convert all to list of lists, handling NaN
        rows = df.astype(object).where(pd.notnull(df), "").values.tolist()
        try:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
        except Exception as e:
            st.error(f"Failed to save data to Google Sheets: {e}")
            raise


def append_rows(tab_name: str, rows: list[dict]) -> None:
    if not rows:
        return
    ws = _get_or_create_worksheet(tab_name)
    headers = SHEET_SCHEMAS.get(tab_name) or list(rows[0].keys())
    values = [[r.get(h, "") for h in headers] for r in rows]
    try:
        ws.append_rows(values, value_input_option="USER_ENTERED")
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
    df = read_df("portfolio_settings")
    if df.empty:
        df = pd.DataFrame(columns=["portfolio", "rebalance_frequency"])
    df = df[df["portfolio"] != portfolio_label]
    df = pd.concat([df, pd.DataFrame([{"portfolio": portfolio_label, "rebalance_frequency": frequency}])], ignore_index=True)
    write_df("portfolio_settings", df)
    clear_caches()
