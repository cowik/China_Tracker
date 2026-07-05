"""
Data fetching:
- Stocks: Cache last 3 years from BaoStock, fetch up to last trading day only.
- ETFs: Direct fetch from yfinance each time (no cache).
- Leading zeros preserved.
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st
import yfinance as yf
from datetime import datetime, timedelta
import pytz

from utils import sheets_db

BEIJING_TZ = pytz.timezone('Asia/Shanghai')


# ---- Helper functions ----
def _clean_ticker(ticker: str) -> str:
    ticker = str(ticker).strip()
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    return ticker.replace('.', '')


def _to_baostock_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"sh.{ticker}"
    else:
        return f"sz.{ticker}"


def _to_yfinance_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _get_last_trading_day() -> str:
    """Get the most recent trading day (from baostock)."""
    import baostock as bs
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    try:
        lg = bs.login()
        if lg is None or lg.error_code != '0':
            return today
        rs = bs.query_trade_dates(start_date=today, end_date=today)
        if rs.error_code != '0':
            bs.logout()
            return today
        # Check if today is a trading day
        trading_days = []
        while rs.next():
            row = rs.get_row_data()
            if row and len(row) > 0:
                trading_days.append(row[0])
        bs.logout()
        if today in trading_days:
            return today
        # If not, go back up to 7 days
        for i in range(1, 8):
            check = (datetime.now(BEIJING_TZ) - timedelta(days=i)).strftime("%Y-%m-%d")
            if check in trading_days:
                return check
        return today
    except Exception:
        return today


def _retry_download_baostock(ticker: str, start_date: str, end_date: str, adjustflag='2', retries=3):
    import baostock as bs
    for attempt in range(retries):
        try:
            lg = bs.login()
            if lg is None or lg.error_code != '0':
                time.sleep(2)
                continue
            rs = bs.query_history_k_data_plus(
                ticker,
                "date,close",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=adjustflag
            )
            if rs.error_code != '0':
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
            df = df.dropna(subset=['close'])
            return df[['date', 'close']]
        except Exception:
            time.sleep(2 * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3):
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
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                raise
    return pd.DataFrame()


# ---- Cache functions for stocks only ----
@st.cache_data(ttl=3600, show_spinner=False)
def _load_price_cache() -> pd.DataFrame:
    """Read the entire price_cache sheet."""
    return sheets_db.read_df("price_cache")


def _get_cached_series(ticker: str) -> pd.Series:
    df = _load_price_cache()
    if df.empty:
        return pd.Series(dtype=float)
    mask = df["ticker"].astype(str).str.strip() == ticker
    cached = df[mask].copy()
    if cached.empty:
        return pd.Series(dtype=float)
    cached["date"] = pd.to_datetime(cached["date"])
    cached = cached.sort_values("date")
    return cached.set_index("date")["close"]


def _update_cache(ticker: str, new_data: pd.DataFrame) -> None:
    if new_data.empty:
        return
    new_data = new_data.copy()
    new_data["ticker"] = ticker
    new_data["asset_type"] = "stock"
    new_data["date"] = pd.to_datetime(new_data["date"]).dt.strftime("%Y-%m-%d")
    new_data["close"] = new_data["close"].astype(float)
    rows = new_data[["ticker", "date", "close", "asset_type"]].to_dict(orient="records")
    sheets_db.append_rows("price_cache", rows)
    st.cache_data.clear()


def _trim_cache_to_3_years(ticker: str) -> None:
    """Remove stock data older than 3 years for this ticker."""
    df = _load_price_cache()
    if df.empty:
        return
    mask = df["ticker"].astype(str).str.strip() == ticker
    if not mask.any():
        return
    # Keep only last 3 years
    cutoff = (datetime.now() - timedelta(days=3*365)).strftime("%Y-%m-%d")
    df_ticker = df[mask].copy()
    df_ticker["date_dt"] = pd.to_datetime(df_ticker["date"])
    df_ticker = df_ticker[df_ticker["date_dt"] >= cutoff]
    df = df[~mask]
    if not df_ticker.empty:
        df_ticker = df_ticker.drop(columns=["date_dt"])
        df = pd.concat([df, df_ticker], ignore_index=True)
    sheets_db.write_df("price_cache", df)
    st.cache_data.clear()


# ---- Main price series function ----
def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    ticker = _clean_ticker(ticker)
    last_trading_day = _get_last_trading_day()
    today_beijing = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    if asset_type == 'stock':
        # ---- Stock: Use cache ----
        cached_series = _get_cached_series(ticker)
        
        # Determine fetch range: from 3 years ago to last trading day
        three_years_ago = (datetime.now() - timedelta(days=3*365)).strftime("%Y-%m-%d")
        
        if not cached_series.empty:
            last_cached = cached_series.index.max().strftime("%Y-%m-%d")
            # Start from the day after last cached, but not earlier than 3 years ago
            start_fetch = max(last_cached, three_years_ago)
            # Convert to date and add 1 day
            start_dt = datetime.strptime(start_fetch, "%Y-%m-%d") + timedelta(days=1)
            start_fetch = start_dt.strftime("%Y-%m-%d")
        else:
            start_fetch = three_years_ago
        
        # Only fetch if start <= last trading day
        if start_fetch <= last_trading_day:
            st.info(f"📡 Fetching stock data for {ticker} from {start_fetch} to {last_trading_day}")
            new_df = _retry_download_baostock(_to_baostock_ticker(ticker), start_fetch, last_trading_day)
            if not new_df.empty:
                _update_cache(ticker, new_df)
                st.cache_data.clear()
                cached_series = _get_cached_series(ticker)
        
        # Trim cache to 3 years
        _trim_cache_to_3_years(ticker)
        st.cache_data.clear()
        cached_series = _get_cached_series(ticker)
        
        if cached_series.empty:
            return pd.Series(dtype=float)
        return cached_series.sort_index()

    else:
        # ---- ETF: Direct fetch from yfinance (no cache) ----
        # Fetch from start_date to last_trading_day (or today)
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, last_trading_day)
            if df.empty:
                st.warning(f"❌ No ETF data for {ticker} ({yf_ticker})")
                return pd.Series(dtype=float)
            return df.set_index("date")["close"].sort_index()
        except Exception as e:
            st.warning(f"❌ Error fetching ETF {ticker}: {e}")
            return pd.Series(dtype=float)


# ---- Backward compatibility ----
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
    return pd.DataFrame()
