"""
Wrappers around akshare calls, with Streamlit caching so we don't hammer
the free data source on every rerun, and defensive error handling so one
broken ticker/endpoint doesn't crash the whole dashboard.

NOTE: akshare pulls from Eastmoney/Sina's public (undocumented, unofficial)
endpoints. They occasionally change their site and break a function name
or response shape. If something here starts failing after it previously
worked, that's almost always the cause - see README "If data fetching
breaks" section for what to do.
"""
from __future__ import annotations
import akshare as ak
import pandas as pd
import streamlit as st
import time
import random


def _clean_ticker(ticker: str) -> str:
    """
    Remove common suffixes like .SH, .SZ, .SS, .SZ and whitespace.
    akshare expects plain 6-digit codes.
    """
    ticker = str(ticker).strip()
    # Remove common suffixes
    for suffix in ['.SH', '.SZ', '.SS', '.SZ']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    # Remove any remaining dots
    ticker = ticker.replace('.', '')
    return ticker


def _retry(fn, *args, retries=5, delay=2.0, **kwargs):
    last_err = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                # Add jitter to avoid hitting the server at exactly the same time
                sleep_time = delay * (1 + random.random() * 0.5)
                time.sleep(sleep_time)
    raise last_err


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_hist(ticker: str, start_date: str = "19900101", end_date: str = "20500101") -> pd.DataFrame:
    """
    A-share stock daily history, dividend-adjusted (hfq).
    Returns DataFrame with columns: date, close (at minimum), indexed by nothing
    (date is a column) - caller should set index.
    Empty DataFrame on failure (never raises), so callers can show a friendly
    'no data for X' message instead of crashing.
    """
    ticker = _clean_ticker(ticker)
    try:
        df = _retry(
            ak.stock_zh_a_hist,
            symbol=ticker, period="daily",
            start_date=start_date, end_date=end_date, adjust="hfq",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"日期": "date", "收盘": "close"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", "close"]]
    except Exception as e:
        st.warning(f"Couldn't fetch price history for stock {ticker}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_etf_hist(ticker: str, start_date: str = "19900101", end_date: str = "20500101") -> pd.DataFrame:
    """China-listed ETF daily history, dividend-adjusted (hfq)."""
    ticker = _clean_ticker(ticker)
    try:
        df = _retry(
            ak.fund_etf_hist_em,
            symbol=ticker, period="daily",
            start_date=start_date, end_date=end_date, adjust="hfq",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"日期": "date", "收盘": "close"})
        df["date"] = pd.to_datetime(df["date"])
        return df[["date", "close"]]
    except Exception as e:
        st.warning(f"Couldn't fetch price history for ETF {ticker}: {e}")
        return pd.DataFrame()


def get_price_series(ticker: str, asset_type: str, start_date: str = "19900101") -> pd.Series:
    """Convenience: fetch + return a clean date-indexed close-price Series (hfq-adjusted)."""
    fetch_fn = get_stock_hist if asset_type == "stock" else get_etf_hist
    df = fetch_fn(ticker, start_date=start_date)
    if df.empty:
        return pd.Series(dtype=float)
    return df.set_index("date")["close"].sort_index()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_dividends(ticker: str) -> pd.DataFrame:
    """
    A-share dividend history for one stock.
    Returns columns: ex_date, pay_date, amount_per_share (in yuan, per 1 share).
    Empty DataFrame on failure or if the stock has never paid a dividend.
    """
    ticker = _clean_ticker(ticker)
    try:
        df = _retry(ak.stock_fhps_detail_em, symbol=ticker)
        if df is None or df.empty:
            return pd.DataFrame()
        # Column names on this endpoint have varied slightly across akshare
        # versions; match flexibly instead of hardcoding one exact name.
        cols = {c: c for c in df.columns}
        ex_date_col = next((c for c in df.columns if "除权除息" in c), None)
        pay_date_col = next((c for c in df.columns if "红利发放" in c or "派息日" in c), None)
        amount_col = next((c for c in df.columns if "现金分红-每股分红" in c or ("分红" in c and "股" in c and "10" not in c)), None)
        if ex_date_col is None or amount_col is None:
            return pd.DataFrame()
        out = pd.DataFrame({
            "ex_date": pd.to_datetime(df[ex_date_col], errors="coerce"),
            "pay_date": pd.to_datetime(df[pay_date_col], errors="coerce") if pay_date_col else pd.NaT,
            "amount_per_share": pd.to_numeric(df[amount_col], errors="coerce"),
        })
        out = out.dropna(subset=["ex_date", "amount_per_share"])
        out = out[out["amount_per_share"] > 0]
        return out.sort_values("ex_date")
    except Exception as e:
        st.warning(f"Couldn't fetch dividend history for stock {ticker}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_etf_dividends(ticker: str) -> pd.DataFrame:
    """
    China-listed ETF dividend history. akshare's fund_etf_dividend_sina gives
    *cumulative* dividend-per-share by date; we diff it to get individual
    payout amounts. ex_date and pay_date are the same single date here since
    the source doesn't separate them - noted as a known simplification.
    """
    ticker = _clean_ticker(ticker)
    try:
        market_prefix = "sh" if ticker.startswith(("5", "6")) else "sz"
        df = _retry(ak.fund_etf_dividend_sina, symbol=f"{market_prefix}{ticker}")
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"日期": "date", "累计分红": "cumulative"})
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        df["amount_per_share"] = df["cumulative"].diff().fillna(df["cumulative"].iloc[0])
        df = df[df["amount_per_share"] > 0]
        return pd.DataFrame({
            "ex_date": df["date"],
            "pay_date": df["date"],
            "amount_per_share": df["amount_per_share"],
        })
    except Exception as e:
        st.warning(f"Couldn't fetch dividend history for ETF {ticker}: {e}")
        return pd.DataFrame()


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    return get_stock_dividends(ticker) if asset_type == "stock" else get_etf_dividends(ticker)
