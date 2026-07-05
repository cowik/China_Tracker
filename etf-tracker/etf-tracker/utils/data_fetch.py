"""
Data fetching using BaoStock - Free, no registration, stable A-share data.
"""
from __future__ import annotations
import pandas as pd
import streamlit as st
import baostock as bs


def _format_ticker(ticker: str) -> str:
    """Convert 6-digit code to baostock format: sh.600000 or sz.000001"""
    ticker = str(ticker).strip()
    # Remove any existing suffixes
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    ticker = ticker.replace('.', '')
    
    if not ticker:
        return ticker
    # Shanghai: starts with 5 or 6, Shenzhen: starts with 0, 2, or 3
    if ticker[0] in ('5', '6'):
        return f"sh.{ticker}"
    else:
        return f"sz.{ticker}"


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_hist(ticker: str, start_date: str = "19900101", end_date: str = "20500101") -> pd.DataFrame:
    """Fetch A-share daily OHLCV data."""
    code = _format_ticker(ticker)
    try:
        lg = bs.login()
        if lg.error_code != '0':
            st.warning(f"BaoStock login failed: {lg.error_msg}")
            return pd.DataFrame()
        
        rs = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3"  # 3 = no adjustment
        )
        
        if rs.error_code != '0':
            st.warning(f"Query failed for {ticker}: {rs.error_msg}")
            bs.logout()
            return pd.DataFrame()
        
        data_list = []
        while rs.next():
            data_list.append(rs.get_row_data())
        
        bs.logout()
        
        if not data_list:
            return pd.DataFrame()
        
        df = pd.DataFrame(data_list, columns=rs.fields)
        df['date'] = pd.to_datetime(df['date'])
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        return df[['date', 'close']]
        
    except Exception as e:
        st.warning(f"Failed to fetch data for {ticker}: {e}")
        return pd.DataFrame()


def get_etf_hist(ticker: str, start_date: str = "19900101", end_date: str = "20500101") -> pd.DataFrame:
    """ETFs use the same function as stocks."""
    return get_stock_hist(ticker, start_date, end_date)


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "19900101") -> pd.Series:
    """Return a date-indexed close price series."""
    df = get_stock_hist(ticker, start_date=start_date)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].sort_index()


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    """Dividend data placeholder - returns empty DataFrame."""
    return pd.DataFrame()
