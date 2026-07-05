"""
Data fetching using yfinance with robust suffix handling for A-shares.
"""
from __future__ import annotations
import pandas as pd
import streamlit as st
import yfinance as yf
import time
import random


def _clean_ticker(ticker: str) -> str:
    """Remove spaces, dots, and common suffixes."""
    ticker = str(ticker).strip()
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    return ticker.replace('.', '')


def _to_yfinance_ticker(ticker: str) -> str:
    """Convert clean ticker to yfinance format with suffix."""
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    # Shanghai: 5 or 6开头; Shenzhen: 0, 2, 3开头
    if ticker[0] in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _retry_download(ticker_yf: str, start: str, end: str, retries=4, delay=2.0):
    """Download with retries and fallback to no suffix if needed."""
    attempts = [ticker_yf]
    # If the ticker already has a suffix, also try without it as fallback
    if '.' in ticker_yf:
        attempts.append(ticker_yf.split('.')[0])
    
    for attempt_ticker in attempts:
        for attempt in range(retries):
            try:
                st.write(f"🔍 Fetching {attempt_ticker}...")  # debug
                df = yf.download(attempt_ticker, start=start, end=end, progress=False, timeout=15)
                if not df.empty:
                    close = df['Close']
                    out = close.reset_index()
                    out.columns = ['date', 'close']
                    out['date'] = pd.to_datetime(out['date'])
                    return out
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(delay * (1 + random.random() * 0.5))
                else:
                    # Try next ticker variant
                    continue
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_hist(ticker: str, start_date: str = "2020-01-01", end_date: str = "20500101") -> pd.DataFrame:
    """Fetch daily OHLCV data for a stock using yfinance."""
    ticker_yf = _to_yfinance_ticker(ticker)
    try:
        df = _retry_download(ticker_yf, start_date, end_date)
        if df.empty:
            st.warning(f"No data found for {ticker} (tried {ticker_yf} and fallback)")
        return df
    except Exception as e:
        st.warning(f"Couldn't fetch price history for {ticker} ({ticker_yf}): {e}")
        return pd.DataFrame()


def get_etf_hist(ticker: str, start_date: str = "2020-01-01", end_date: str = "20500101") -> pd.DataFrame:
    """Same as stock for ETFs."""
    return get_stock_hist(ticker, start_date, end_date)


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "2020-01-01") -> pd.Series:
    """Return date-indexed close price series."""
    df = get_stock_hist(ticker, start_date=start_date)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].sort_index()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_dividends(ticker: str) -> pd.DataFrame:
    """Fetch dividend history using yfinance."""
    ticker_yf = _to_yfinance_ticker(ticker)
    try:
        tkr = yf.Ticker(ticker_yf)
        divs = tkr.dividends
        if divs.empty:
            return pd.DataFrame()
        df = divs.reset_index()
        df.columns = ['ex_date', 'amount_per_share']
        df['pay_date'] = df['ex_date']
        df['ex_date'] = pd.to_datetime(df['ex_date'])
        df['pay_date'] = pd.to_datetime(df['pay_date'])
        return df[['ex_date', 'pay_date', 'amount_per_share']]
    except Exception as e:
        st.warning(f"Couldn't fetch dividends for {ticker}: {e}")
        return pd.DataFrame()


def get_etf_dividends(ticker: str) -> pd.DataFrame:
    return get_stock_dividends(ticker)


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    return get_stock_dividends(ticker)
