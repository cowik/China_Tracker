"""
Data fetching: BaoStock primary (A-share dedicated), then yfinance fallback.
"""
from __future__ import annotations
import pandas as pd
import streamlit as st
import time
import random


def _clean_ticker(ticker: str) -> str:
    """Remove suffixes and spaces."""
    ticker = str(ticker).strip()
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    return ticker.replace('.', '')


def _to_baostock_ticker(ticker: str) -> str:
    """Convert to baostock format: sh.xxxxxx or sz.xxxxxx."""
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    # Shanghai: 5 or 6开头; Shenzhen: 0, 2, 3开头 (and some ETFs start with 1)
    if ticker[0] in ('5', '6'):
        return f"sh.{ticker}"
    else:
        return f"sz.{ticker}"


def _to_yfinance_ticker(ticker: str) -> str:
    """Convert to yfinance format: xxxxxx.SS or xxxxxx.SZ."""
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _retry_download_baostock(ticker: str, start_date: str, end_date: str, retries=3):
    """Fetch daily data using BaoStock with retries."""
    import baostock as bs
    for attempt in range(retries):
        try:
            lg = bs.login()
            if lg is None or lg.error_code != '0':
                st.warning(f"BaoStock login failed: {lg.error_msg if lg else 'None'}")
                time.sleep(2)
                continue

            rs = bs.query_history_k_data_plus(
                ticker,
                "date,close",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3"  # 3 = no adjustment
            )
            if rs.error_code != '0':
                st.warning(f"BaoStock query failed: {rs.error_msg}")
                bs.logout()
                time.sleep(2)
                continue

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
            bs.logout()

            if not data_list:
                return pd.DataFrame()

            df = pd.DataFrame(data_list, columns=rs.fields)
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            # Drop any rows with missing close price
            df = df.dropna(subset=['close'])
            return df[['date', 'close']]

        except Exception as e:
            st.warning(f"BaoStock attempt {attempt+1} failed: {e}")
            time.sleep(2 * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3):
    """Fallback to yfinance."""
    import yfinance as yf
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end, progress=False, timeout=15)
            if df.empty:
                return pd.DataFrame()
            close = df['Close']
            out = close.reset_index()
            out.columns = ['date', 'close']
            out['date'] = pd.to_datetime(out['date'])
            return out
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                raise e
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    """Fetch daily OHLCV: try BaoStock first, then yfinance."""
    # 1. BaoStock
    bs_ticker = _to_baostock_ticker(ticker)
    df = _retry_download_baostock(bs_ticker, start_date, end_date)
    if not df.empty:
        st.info(f"✅ Data for {ticker} loaded via BaoStock")  # debug
        return df

    # 2. Fallback to yfinance
    yf_ticker = _to_yfinance_ticker(ticker)
    try:
        df = _retry_download_yfinance(yf_ticker, start_date, end_date)
        if not df.empty:
            st.info(f"✅ Data for {ticker} loaded via yfinance (fallback)")
            return df
    except Exception as e:
        st.warning(f"yfinance fallback failed for {ticker}: {e}")

    st.warning(f"❌ No data found for {ticker} (tried BaoStock and yfinance)")
    return pd.DataFrame()


def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    return get_stock_hist(ticker, start_date, end_date)


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    df = get_stock_hist(ticker, start_date=start_date)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].sort_index()


# Dividends placeholder – BaoStock also provides dividend data if needed
def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    return pd.DataFrame()
