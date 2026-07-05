"""
Data fetching using yfinance (Yahoo Finance) for all A-shares and ETFs.
Handles ticker suffixes: .SS for Shanghai, .SZ for Shenzhen.
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st
import yfinance as yf


def _clean_ticker(ticker: str) -> str:
    """Remove suffixes and spaces."""
    ticker = str(ticker).strip()
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    return ticker.replace('.', '')


def _to_yfinance_ticker(ticker: str) -> str:
    """Convert to yfinance format: xxxxxx.SS or xxxxxx.SZ."""
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3):
    """Fetch daily data using yfinance with retries and jitter."""
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


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    """
    Returns date-indexed close price series from yfinance.
    asset_type is ignored (yfinance works for both).
    """
    ticker = _clean_ticker(ticker)
    yf_ticker = _to_yfinance_ticker(ticker)
    try:
        df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
        if df.empty:
            st.warning(f"❌ No data for {ticker} ({yf_ticker})")
            return pd.Series(dtype=float)
        st.info(f"✅ {ticker} loaded via yfinance")
        return df.set_index("date")["close"].sort_index()
    except Exception as e:
        st.warning(f"❌ Error fetching {ticker}: {e}")
        return pd.Series(dtype=float)


# Backward compatibility
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_price_series(ticker, asset_type="stock", start_date=start_date)
    if s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})


def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_price_series(ticker, asset_type="etf", start_date=start_date)
    if s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    """Optional: fetch dividend history using yfinance if needed."""
    ticker = _clean_ticker(ticker)
    yf_ticker = _to_yfinance_ticker(ticker)
    try:
        tkr = yf.Ticker(yf_ticker)
        divs = tkr.dividends
        if divs.empty:
            return pd.DataFrame()
        df = divs.reset_index()
        df.columns = ['ex_date', 'amount_per_share']
        df['pay_date'] = df['ex_date']  # yfinance doesn't provide pay_date
        df['ex_date'] = pd.to_datetime(df['ex_date'])
        df['pay_date'] = pd.to_datetime(df['pay_date'])
        return df[['ex_date', 'pay_date', 'amount_per_share']]
    except Exception:
        return pd.DataFrame()
