"""
Data fetching using yfinance (Yahoo Finance) for A-shares and ETFs.
Handles ticker suffixes: .SS for Shanghai, .SZ for Shenzhen.
"""
from __future__ import annotations
import pandas as pd
import streamlit as st
import yfinance as yf
import time
import random


def _clean_ticker(ticker: str) -> str:
    """Remove spaces, dots, and suffixes like .SH, .SZ, .SS."""
    ticker = str(ticker).strip()
    # Remove common suffixes
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    # Remove any remaining dots
    ticker = ticker.replace('.', '')
    return ticker


def _to_yfinance_ticker(ticker: str) -> str:
    """
    Convert a clean A-share ticker to Yahoo Finance format.
    Shanghai: 5 or 6开头 -> .SS
    Shenzhen: 0, 3, 2开头 -> .SZ
    """
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    first_digit = ticker[0]
    if first_digit in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _retry_download(ticker_yf: str, start: str, end: str, retries=3, delay=2.0):
    """Download with retries and jitter."""
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end, progress=False)
            if df.empty:
                return pd.DataFrame()
            # Keep only 'Close' column
            close = df['Close']
            # Convert to DataFrame with 'date' and 'close' columns
            out = close.reset_index()
            out.columns = ['date', 'close']
            out['date'] = pd.to_datetime(out['date'])
            return out
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (1 + random.random() * 0.5))
            else:
                raise e
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_hist(ticker: str, start_date: str = "19900101", end_date: str = "20500101") -> pd.DataFrame:
    """
    Fetch daily historical data (close) for an A-share stock using yfinance.
    Returns DataFrame with columns: date, close.
    Empty DataFrame on failure.
    """
    ticker_yf = _to_yfinance_ticker(ticker)
    try:
        df = _retry_download(ticker_yf, start_date, end_date)
        return df
    except Exception as e:
        st.warning(f"Couldn't fetch price history for stock {ticker} ({ticker_yf}): {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_etf_hist(ticker: str, start_date: str = "19900101", end_date: str = "20500101") -> pd.DataFrame:
    """Same as stock; ETFs also work with yfinance."""
    return get_stock_hist(ticker, start_date, end_date)


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "19900101") -> pd.Series:
    """
    Returns a date-indexed Series of closing prices.
    asset_type is ignored (yfinance works for both).
    """
    df = get_stock_hist(ticker, start_date=start_date)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].sort_index()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_dividends(ticker: str) -> pd.DataFrame:
    """Fetch dividend history for a stock using yfinance."""
    ticker_yf = _to_yfinance_ticker(ticker)
    try:
        tkr = yf.Ticker(ticker_yf)
        divs = tkr.dividends
        if divs.empty:
            return pd.DataFrame()
        # Convert to DataFrame with ex_date, pay_date (same as ex_date for simplicity),
        # and amount_per_share (which is already per share)
        df = divs.reset_index()
        df.columns = ['ex_date', 'amount_per_share']
        df['pay_date'] = df['ex_date']  # Yahoo doesn't provide pay_date separately
        df['ex_date'] = pd.to_datetime(df['ex_date'])
        df['pay_date'] = pd.to_datetime(df['pay_date'])
        return df[['ex_date', 'pay_date', 'amount_per_share']]
    except Exception as e:
        st.warning(f"Couldn't fetch dividends for {ticker} ({ticker_yf}): {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_etf_dividends(ticker: str) -> pd.DataFrame:
    """Same for ETFs."""
    return get_stock_dividends(ticker)


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    """Wrapper; asset_type ignored."""
    return get_stock_dividends(ticker)
