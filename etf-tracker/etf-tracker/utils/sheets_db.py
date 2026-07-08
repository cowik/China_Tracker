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
}

TICKER_COLUMNS = {"ticker"}

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

@st.cache_data(ttl=900, show_spinner=False)
def read_df(tab_name: str) -> pd.DataFrame:
    ws = _get_or_create_worksheet(tab_name)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=SHEET_SCHEMAS.get(tab_name, []))
    df = pd.DataFrame(records)
    for col in TICKER_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].str.replace(r'[^\d]', '', regex=True)
            df[col] = df[col].apply(lambda x: x.zfill(6) if x.isdigit() else x)
            
    # FIX: Clean numeric columns that might be saved as text with commas/dots
    for col in ["index_value", "weight", "close"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(",", ".", regex=False)
            df[col] = df[col].str.replace(r"[^\d.\-]", "", regex=True)
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def write_df(tab_name: str, df: pd.DataFrame) -> None:
    ws = _get_or_create_worksheet(tab_name)
    ws.clear()
    if df.empty:
        headers = SHEET_SCHEMAS.get(tab_name, [])
        if headers:
            ws.append_row(headers)
        return

    df = df.copy()
    for col in df.columns:
        if col == "ticker":
            df[col] = df[col].astype(str).str.strip()
        elif col in ["index_value", "weight", "close"]:
            # Convert to float, then format as string with a dot to defeat locale rules
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
        ws.update([headers] + rows, raw=True)
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
    """Wipes all Streamlit caches so fresh data is loaded from Google Sheets immediately."""
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

# --------------------------------------------------------------- dynamic portfolios --
def get_portfolios() -> dict[str, str]:
    """Returns {tab_name: label}. Migrates hardcoded ones if table is empty."""
    df = read_df("portfolios_meta")
    if df.empty:
        df = pd.DataFrame([
            {"tab_name": "portfolio1_positions", "label": "Возможности Китая"},
            {"tab_name": "portfolio2_positions", "label": "Возможности Китая. Специальная 2"}
        ])
        write_df("portfolios_meta", df)
        clear_caches()
    return dict(zip(df["tab_name"], df["label"]))

def add_portfolio(label: str) -> str:
    df = read_df("portfolios_meta")
    existing_nums = [int(t.replace("portfolio", "").replace("_positions", "")) for t in df["tab_name"] if t.startswith("portfolio") and t.endswith("_positions")]
    next_num = max(existing_nums) + 1 if existing_nums else 1
    tab_name = f"portfolio{next_num}_positions"
    
    new_row = pd.DataFrame([{"tab_name": tab_name, "label": label}])
    df = pd.concat([df, new_row], ignore_index=True)
    write_df("portfolios_meta", df)
    _get_or_create_worksheet(tab_name)
    clear_caches()
    return tab_name

def delete_portfolio(tab_name: str, label: str) -> None:
    df = read_df("portfolios_meta")
    df = df[df["tab_name"] != tab_name]
    write_df("portfolios_meta", df)
    
    try:
        ss = _get_spreadsheet()
        ws = ss.worksheet(tab_name)
        ss.del_worksheet(ws)
    except Exception:
        pass
        
    bt = read_df("backtest_history")
    if not bt.empty:
        bt = bt[bt["portfolio"] != label]
        write_df("backtest_history", bt)
        
    settings = read_df("portfolio_settings")
    if not settings.empty:
        settings = settings[settings["portfolio"] != label]
        write_df("portfolio_settings", settings)
        
    clear_caches()

# --------------------------------------------------------------- display order --
def get_display_order() -> dict[str, int]:
    """Returns {label: sort_order}. Items not in dict will default to 9999."""
    df = read_df("display_order")
    if df.empty:
        return {}
    df["sort_order"] = pd.to_numeric(df["sort_order"], errors="coerce").fillna(9999).astype(int)
    return dict(zip(df["label"], df["sort_order"]))

def save_display_order(labels: list[str]) -> None:
    df = pd.DataFrame({"label": labels, "sort_order": range(1, len(labels) + 1)})
    write_df("display_order", df)
    clear_caches()
