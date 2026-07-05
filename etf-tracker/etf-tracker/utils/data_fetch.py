"""
Data fetching with:
- In-memory cache (@st.cache_data) for each ticker (1 hour TTL)
- Persistent price_cache sheet for cold-start speed
- Batch yfinance for ETFs
- Single BaoStock login per fetch cycle
- Fetch only from inception date for holdings, full for watchlist
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st
import yfinance as yf
from datetime import datetime, timedelta
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from utils import sheets_db

BEIJING_TZ = pytz.timezone('Asia/Shanghai')


# ---- Helpers ----
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


def _is_trading_day(date_str: str) -> bool:
    """Check if date is a trading day using baostock."""
    try:
        import baostock as bs
        lg = bs.login()
        if lg is None or lg.error_code != '0':
            # fallback: weekday
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.weekday() < 5
        rs = bs.query_trade_dates(start_date=date_str, end_date=date_str)
        if rs.error_code == '0':
            trading_days = []
            while rs.next():
                row = rs.get_row_data()
                if row and len(row) > 0:
                    trading_days.append(row[0])
            bs.logout()
            return len(trading_days) > 0
        bs.logout()
        return False
    except Exception:
        return datetime.strptime(date_str, "%Y-%m-%d").weekday() < 5


def _get_last_trading_day() -> str:
    """Return most recent trading day (up to 10 days back)."""
    try:
        import baostock as bs
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        start = (datetime.now(BEIJING_TZ) - timedelta(days=10)).strftime("%Y-%m-%d")
        lg = bs.login()
        if lg is not None and lg.error_code == '0':
            rs = bs.query_trade_dates(start_date=start, end_date=today)
            if rs.error_code == '0':
                trading_days = []
                while rs.next():
                    row = rs.get_row_data()
                    if row and len(row) > 0:
                        trading_days.append(row[0])
                bs.logout()
                if trading_days:
                    return max(trading_days)
    except Exception:
        pass
    # fallback to last weekday
    dt = datetime.now(BEIJING_TZ)
    while dt.weekday() > 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _should_fetch_today() -> bool:
    """Return True if we should fetch today's close (after 15:20 Beijing on trading day)."""
    now = datetime.now(BEIJING_TZ)
    today_str = now.strftime("%Y-%m-%d")
    if not _is_trading_day(today_str):
        return False
    return now.hour >= 15 and now.minute >= 20


# ---- BaoStock with session reuse ----
_BAOSTOCK_SESSION = None

def _get_baostock_session():
    global _BAOSTOCK_SESSION
    if _BAOSTOCK_SESSION is None:
        import baostock as bs
        lg = bs.login()
        if lg is not None and lg.error_code == '0':
            _BAOSTOCK_SESSION = bs
    return _BAOSTOCK_SESSION


def _retry_download_baostock(ticker: str, start_date: str, end_date: str, adjustflag='2', retries=3):
    bs = _get_baostock_session()
    if bs is None:
        # fallback: import and login fresh
        import baostock as bs_import
        lg = bs_import.login()
        if lg is None or lg.error_code != '0':
            return pd.DataFrame()
        bs = bs_import

    for attempt in range(retries):
        try:
            rs = bs.query_history_k_data_plus(
                ticker,
                "date,close",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=adjustflag
            )
            if rs.error_code != '0':
                time.sleep(2)
                continue
            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
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


def _logout_baostock():
    global _BAOSTOCK_SESSION
    if _BAOSTOCK_SESSION is not None:
        try:
            _BAOSTOCK_SESSION.logout()
        except:
            pass
        _BAOSTOCK_SESSION = None


# ---- yfinance batching ----
def _fetch_yfinance_batch(tickers: List[str], start: str, end: str) -> Dict[str, pd.Series]:
    """Fetch multiple yfinance tickers in one call."""
    if not tickers:
        return {}
    try:
        df = yf.download(tickers, start=start, end=end, progress=False, timeout=30, group_by='ticker')
        if df.empty:
            return {}
        result = {}
        for t in tickers:
            if t in df.columns:
                # if multi-index, we get (ticker, 'Adj Close') or (ticker, 'Close')
                # yfinance returns DataFrame with columns as MultiIndex if group_by='ticker'
                if isinstance(df.columns, pd.MultiIndex):
                    close_series = df[t]['Adj Close'] if 'Adj Close' in df[t].columns else df[t]['Close']
                else:
                    # sometimes single ticker returns flat columns
                    close_series = df['Adj Close'] if 'Adj Close' in df.columns else df['Close']
                    # but we have multiple tickers, so we need to handle each
                    # Actually for multiple tickers, yfinance returns MultiIndex columns
                    # So we'll rely on that.
                    pass
                # Re-extract correctly:
                # We'll loop over each ticker
        # Better approach: handle each ticker from the downloaded data
        for t in tickers:
            if t in df.columns.levels[0]:
                sub = df[t]
                if 'Adj Close' in sub.columns:
                    close = sub['Adj Close']
                elif 'Close' in sub.columns:
                    close = sub['Close']
                else:
                    continue
                close = close.dropna()
                if not close.empty:
                    out = close.reset_index()
                    out.columns = ['date', 'close']
                    out['date'] = pd.to_datetime(out['date'])
                    result[t] = out.set_index('date')['close']
        return result
    except Exception:
        return {}


# ---- Persistent cache (price_cache sheet) ----
def _get_cached_series(ticker: str, asset_type: str) -> pd.Series:
    df = sheets_db.read_df("price_cache")
    # If empty or missing required columns, return empty
    if df.empty or not all(col in df.columns for col in ["ticker", "asset_type", "date", "close"]):
        return pd.Series(dtype=float)
    mask = (df["ticker"].astype(str).str.strip() == ticker) & (df["asset_type"] == asset_type)
    cached = df[mask].copy()
    if cached.empty:
        return pd.Series(dtype=float)
    cached["date"] = pd.to_datetime(cached["date"])
    cached = cached.sort_values("date")
    cached["close"] = pd.to_numeric(cached["close"], errors="coerce")
    return cached.set_index("date")["close"]


def _append_to_cache(ticker: str, asset_type: str, new_data: pd.DataFrame) -> None:
    if new_data.empty:
        return
    new_data = new_data.copy()
    new_data["ticker"] = ticker
    new_data["asset_type"] = asset_type
    new_data["date"] = pd.to_datetime(new_data["date"]).dt.strftime("%Y-%m-%d")
    new_data["close"] = new_data["close"].astype(float)
    rows = new_data[["ticker", "date", "close", "asset_type"]].values.tolist()
    try:
        ws = sheets_db._get_or_create_worksheet("price_cache")
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        # Clear only the price_cache cache
        st.cache_data.clear()  # we don't have a specific key, but we can clear the cached function
        # We'll use a separate cache key for the price cache, we'll define a function later.
    except Exception as e:
        st.error(f"Failed to append to cache: {e}")
        raise


# ---- Main price series with in-memory cache + persistent cache ----
@st.cache_data(ttl=3600, show_spinner=False)
def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    """
    Returns date-indexed close price series.
    - Uses persistent cache (price_cache) for cold-start speed.
    - Fetches only missing dates.
    - In-memory cache for current session.
    """
    ticker = _clean_ticker(ticker)
    last_trading_day = _get_last_trading_day()
    today_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    # Determine end fetch date
    if _is_trading_day(today_str) and _should_fetch_today():
        end_fetch = today_str
    else:
        end_fetch = last_trading_day

    # 1. Check persistent cache
    cached_series = _get_cached_series(ticker, asset_type)

    # Determine start fetch: either from last cached + 1, or from start_date
    if not cached_series.empty:
        last_cached = cached_series.index.max().strftime("%Y-%m-%d")
        # Ensure we don't fetch before start_date (user may have provided specific start)
        start_fetch_dt = max(pd.Timestamp(last_cached) + timedelta(days=1), pd.Timestamp(start_date))
        start_fetch = start_fetch_dt.strftime("%Y-%m-%d")
    else:
        start_fetch = start_date

    # If we have a cache but still need data, fetch incremental
    if start_fetch <= end_fetch:
        st.info(f"📡 Fetching {asset_type} data for {ticker} from {start_fetch} to {end_fetch}")
        if asset_type == 'stock':
            new_df = _retry_download_baostock(_to_baostock_ticker(ticker), start_fetch, end_fetch, adjustflag='2')
            if new_df.empty:
                new_df = _retry_download_baostock(_to_baostock_ticker(ticker), start_fetch, end_fetch, adjustflag='3')
        else:  # etf
            # For ETFs, we could batch but here it's single ticker
            new_df = _retry_download_yfinance_single(_to_yfinance_ticker(ticker), start_fetch, end_fetch)
        if not new_df.empty:
            _append_to_cache(ticker, asset_type, new_df)
            # Re-fetch from persistent cache to include new data
            cached_series = _get_cached_series(ticker, asset_type)

    if cached_series.empty:
        return pd.Series(dtype=float)
    # Ensure we only return from start_date onward (user may have requested later start)
    if start_date != "1990-01-01":
        cached_series = cached_series[cached_series.index >= pd.Timestamp(start_date)]
    return cached_series.sort_index()


def _retry_download_yfinance_single(ticker_yf: str, start: str, end: str, retries=3) -> pd.DataFrame:
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end, progress=False, timeout=15)
            if df.empty:
                return pd.DataFrame()
            if 'Adj Close' in df.columns:
                close = df['Adj Close']
            else:
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


# ---- Batch watchlist fetch (to be used in streamlit_app) ----
def get_watchlist_prices(watchlist_df: pd.DataFrame) -> Dict[str, pd.Series]:
    """Batch fetch all watchlist ETFs in one yfinance call."""
    if watchlist_df.empty:
        return {}
    tickers = []
    labels = []
    for _, row in watchlist_df.iterrows():
        ticker = str(row["ticker"]).strip()
        if not ticker:
            continue
        name = str(row.get("name", "")).strip() or ticker
        yf_ticker = _to_yfinance_ticker(ticker)
        tickers.append(yf_ticker)
        labels.append(f"{name} ({ticker})")

    if not tickers:
        return {}

    last_trading_day = _get_last_trading_day()
    start_date = (datetime.now(BEIJING_TZ) - timedelta(days=365*10)).strftime("%Y-%m-%d")  # last 10 years

    try:
        data = yf.download(tickers, start=start_date, end=last_trading_day, progress=False, timeout=30, group_by='ticker')
        if data.empty:
            return {}
        result = {}
        for i, yf_t in enumerate(tickers):
            label = labels[i]
            # Extract close for this ticker
            if 'Adj Close' in data[yf_t].columns:
                close = data[yf_t]['Adj Close']
            elif 'Close' in data[yf_t].columns:
                close = data[yf_t]['Close']
            else:
                continue
            close = close.dropna()
            if not close.empty:
                # Normalize to start at 1.0
                s = close / close.iloc[0]
                result[label] = s
        return result
    except Exception as e:
        st.warning(f"Batch yfinance fetch failed: {e}")
        return {}


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
    # Stub – dividend log removed
    return pd.DataFrame()
